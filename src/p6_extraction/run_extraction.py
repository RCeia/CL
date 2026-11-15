"""Extract plot facts from every synopsis of the snapshot and turn the sampled answers into facts with confidence.

Each synopsis is sent to the LLM `samples_per_synopsis` times. A fact (film, relation, concept) gets
confidence = (samples that contain it) / (samples requested), i.e. self-consistency. Raw samples are appended
to a JSONL file as they arrive, so an interrupted run resumes where it stopped.
"""
import argparse
import hashlib
import json
import logging
import statistics
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from p0_commons.snapshot import select_movies
from p0_commons.config import Settings, load_settings, read_json, setup_logging, write_json
from p6_extraction.llm_client import LlmClient
from p0_commons.plot_schema import PlotRelation, build_messages, json_schema, load_prompt, load_relations, relations_description

logger = logging.getLogger(__name__)

FACTS_FILE = "facts.json"


def fingerprint(parts: list[str]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:12]


def parse_sample(text: str, relations: list[PlotRelation], max_values: int) -> dict[str, list[str]] | None:
    """Concept IRIs per relation for one sample, or None when the sample is not a JSON object."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    parsed: dict[str, list[str]] = {}
    for relation in relations:
        raw = payload.get(relation.name)
        concepts: list[str] = []
        for value in raw if isinstance(raw, list) else []:
            iri = relation.resolve(value) if isinstance(value, str) else None
            if iri and iri not in concepts:
                concepts.append(iri)
        parsed[relation.name] = concepts[:max_values]
    return parsed


def aggregate(texts: list[str], relations: list[PlotRelation], max_values: int, samples_requested: int) -> list[dict[str, Any]]:
    """Facts supported by at least one sample, with self-consistency confidence."""
    support: Counter[tuple[str, str]] = Counter()
    for sample in (parse_sample(text, relations, max_values) for text in texts):
        if sample is None:
            continue
        for relation_name, concepts in sample.items():
            support.update((relation_name, concept) for concept in concepts)
    by_name = {relation.name: relation for relation in relations}
    facts = []
    for (relation_name, concept), count in support.items():
        relation = by_name[relation_name]
        label = next(pref for pref, iri in relation.concepts.items() if iri == concept)
        facts.append(
            {
                "relation": relation_name,
                "relation_iri": relation.iri,
                "concept": concept,
                "value": label,
                "support": count,
                "samples": samples_requested,
                "confidence": round(count / samples_requested, 4),
            }
        )
    return sorted(facts, key=lambda fact: (fact["relation"], -fact["support"], fact["value"]))


def read_records(path: Path) -> dict[int, dict[str, Any]]:
    records: dict[int, dict[str, Any]] = {}
    if path.is_file():
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    record = json.loads(line)
                    if "error" not in record:
                        records[record["movie_id"]] = record
    return records


def movie_year(movie: dict[str, Any]) -> str:
    return (movie.get("release_date") or "")[:4] or "unknown year"


def extraction_report(
    records: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    settings: Settings,
    meta: dict[str, Any],
    wall: tuple[int, float] | None = None,
) -> dict[str, Any]:
    """`wall` = (movies extracted in this invocation, wall-clock seconds); throughput comes from it when available,
    because per-movie latency overstates the cost when several requests wait in the server's queue."""
    texts = [text for record in records for text in record["samples"]]
    parsed = [text for text in texts if _is_object(text)]
    seconds = [record["seconds"] for record in records]
    latency = statistics.mean(seconds) if seconds else 0.0
    per_movie = wall[1] / wall[0] if wall and wall[0] else latency
    levels = Counter(fact["support"] for fact in facts)
    with_facts = {fact["movie_id"] for fact in facts}
    return {
        **meta,
        "movies": len(records),
        "samples": len(texts),
        "json_object_rate": round(len(parsed) / len(texts), 4) if texts else None,
        "truncated_samples": sum(reason == "length" for record in records for reason in record.get("finish_reasons", [])),
        "facts": len(facts),
        "facts_per_movie": round(len(facts) / len(records), 2) if records else None,
        "facts_by_relation": dict(sorted(Counter(fact["relation"] for fact in facts).items())),
        "facts_by_support": {str(level): levels[level] for level in sorted(levels)},
        "movies_without_facts": sum(1 for record in records if record["movie_id"] not in with_facts),
        "latency_seconds_per_movie": round(latency, 2),
        "throughput_basis": "wall clock of this run" if wall and wall[0] else "mean latency (no movies extracted in this run)",
        "seconds_per_movie": round(per_movie, 2),
        "projected_hours": {str(size): round(size * per_movie / 3600, 2) for size in settings["pilot"]["projected_sizes"]},
    }


def _is_object(text: str) -> bool:
    try:
        return isinstance(json.loads(text), dict)
    except json.JSONDecodeError:
        return False


def run(settings: Settings, limit: int | None, concurrency: int | None) -> Path:
    max_values = settings["extraction"]["max_values_per_relation"]
    relations = load_relations(settings.path("ontology"), settings.path("plot_vocabulary"), settings["iris"]["ontology"])
    schema = json_schema(relations, max_values)
    templates = load_prompt(settings.path("extraction_prompt"))
    client = LlmClient.from_settings(settings)
    available = client.models()
    if client.model not in available:
        raise RuntimeError(f"Model {client.model} not served at {client.base_url}; available: {available}")

    run_id = fingerprint(
        [
            client.model,
            settings.path("extraction_prompt").read_text(encoding="utf-8"),
            relations_description(relations),
            json.dumps([client.temperature, client.top_p, client.samples, client.seed, max_values, client.max_tokens]),
        ]
    )
    samples_path = settings.path("extraction_runs") / f"samples_{run_id}.jsonl"
    samples_path.parent.mkdir(parents=True, exist_ok=True)
    done = read_records(samples_path)

    final_size = limit if limit is not None else settings["dataset"]["final_size"]
    movies = select_movies(settings.path("raw_tmdb"), final_size)
    pending = [movie for movie in movies if movie["id"] not in done]
    workers = concurrency or settings["extraction"]["concurrency"]
    logger.info(
        "Extraction run %s with %s: %d movies, %d already done, %d samples each, concurrency %d",
        run_id, client.model, len(movies), len(movies) - len(pending), client.samples, workers,
    )

    def extract(movie: dict[str, Any]) -> dict[str, Any]:
        messages = build_messages(templates, relations, max_values, movie["title"], movie_year(movie), movie["overview"])
        completion = client.complete(messages, schema, seed_offset=movie["id"])
        return {
            "movie_id": movie["id"],
            "title": movie["title"],
            "seconds": round(completion.seconds, 3),
            "prompt_tokens": completion.prompt_tokens,
            "completion_tokens": completion.completion_tokens,
            "finish_reasons": completion.finish_reasons,
            "samples": completion.texts,
        }

    started = time.perf_counter()
    extracted = 0
    with ThreadPoolExecutor(max_workers=workers) as pool, samples_path.open("a", encoding="utf-8") as handle:
        futures = {pool.submit(extract, movie): movie for movie in pending}
        for number, future in enumerate(as_completed(futures), start=1):
            movie = futures[future]
            try:
                record = future.result()
            except Exception as error:  # keep going; the movie is retried on the next run
                logger.warning("Movie %d failed: %s", movie["id"], error)
                continue
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            done[movie["id"]] = record
            extracted += 1
            if number % 10 == 0 or number == len(pending):
                elapsed = time.perf_counter() - started
                logger.info("Extracted %d/%d (%.1f s per movie)", number, len(pending), elapsed / number)

    wall = (extracted, time.perf_counter() - started)
    selected_ids = [movie["id"] for movie in movies]
    records = [done[movie_id] for movie_id in selected_ids if movie_id in done]
    facts = [
        {"movie_id": record["movie_id"], "title": record["title"], **fact}
        for record in records
        for fact in aggregate(record["samples"], relations, max_values, client.samples)
    ]
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta = {
        "generated_utc": stamp,
        "run_id": run_id,
        "model": client.model,
        "base_url": client.base_url,
        "samples_per_synopsis": client.samples,
        "temperature": client.temperature,
        "top_p": client.top_p,
        "max_values_per_relation": max_values,
        "snapshot_utc": read_json(settings.path("raw_tmdb") / "manifest.json")["snapshot_utc"],
        "samples_file": str(samples_path.relative_to(settings.root)).replace("\\", "/"),
        "missing_movies": len(selected_ids) - len(records),
    }
    facts_path = settings.path("extraction") / FACTS_FILE
    write_json(facts_path, {**meta, "movie_ids": [record["movie_id"] for record in records], "facts": facts})
    report_path = settings.path("reports_extraction") / "extraction_report.json"
    if not extracted and report_path.is_file():
        previous = read_json(report_path)  # a resume with nothing left to do keeps the measured throughput
        if previous.get("run_id") == run_id and previous.get("throughput_basis", "").startswith("wall"):
            wall = (previous["movies_timed"], previous["wall_seconds_timed"])
    report = extraction_report(records, facts, settings, meta, wall)
    report.update({"movies_timed": wall[0], "wall_seconds_timed": round(wall[1], 1)})
    write_json(report_path, report)
    logger.info(
        "Wrote %d facts for %d movies to %s (%d movies still missing)",
        len(facts), len(records), facts_path, len(selected_ids) - len(records),
    )
    return facts_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract plot facts with self-consistency confidence from the snapshot's synopses.")
    parser.add_argument("--limit", type=int, default=None, help="only the first N movies (default: dataset.final_size)")
    parser.add_argument("--concurrency", type=int, default=None, help="override extraction.concurrency")
    args = parser.parse_args()
    setup_logging()
    run(load_settings(), args.limit, args.concurrency)


if __name__ == "__main__":
    main()

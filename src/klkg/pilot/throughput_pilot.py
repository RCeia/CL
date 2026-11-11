import argparse
import json
import logging
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from klkg.collection.snapshot import select_movies
from klkg.config import load_settings, setup_logging, write_json
from klkg.extraction.llm_client import LlmClient
from klkg.extraction.plot_schema import (
    PlotRelation,
    build_messages,
    json_schema,
    load_prompt,
    load_relations,
    validate_output,
)

logger = logging.getLogger(__name__)


def analyse_sample(text: str, relations: list[PlotRelation], max_values: int) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"parsed": False, "valid": False, "facts": 0}
    errors = validate_output(payload, relations, max_values)
    facts = sum(len(values) for values in payload.values() if isinstance(values, list)) if isinstance(payload, dict) else 0
    return {"parsed": True, "valid": not errors, "facts": facts, "errors": errors[:5]}


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))]


def summarise(records: list[dict[str, Any]], wall_seconds: float, projected_sizes: list[int]) -> dict[str, Any]:
    ok = [record for record in records if "error" not in record]
    samples = [sample for record in ok for sample in record["analysis"]]
    latencies = [record["seconds"] for record in ok]
    completion_tokens = sum(record["completion_tokens"] for record in ok)
    per_movie = wall_seconds / len(ok) if ok else float("nan")
    return {
        "movies": len(records),
        "failed_requests": len(records) - len(ok),
        "wall_seconds": round(wall_seconds, 1),
        "seconds_per_movie": round(per_movie, 2),
        "latency_seconds": {
            "mean": round(statistics.mean(latencies), 2) if latencies else None,
            "p50": round(percentile(latencies, 0.5), 2) if latencies else None,
            "p95": round(percentile(latencies, 0.95), 2) if latencies else None,
        },
        "prompt_tokens_mean": round(statistics.mean(record["prompt_tokens"] for record in ok), 1) if ok else None,
        "completion_tokens_per_second": round(completion_tokens / wall_seconds, 1) if wall_seconds else None,
        "samples": len(samples),
        "json_parse_rate": round(sum(sample["parsed"] for sample in samples) / len(samples), 4) if samples else None,
        "schema_valid_rate": round(sum(sample["valid"] for sample in samples) / len(samples), 4) if samples else None,
        "truncated_samples": sum(reason == "length" for record in ok for reason in record["finish_reasons"]),
        "facts_per_sample_mean": round(statistics.mean(sample["facts"] for sample in samples), 2) if samples else None,
        "projected_hours": {str(size): round(size * per_movie / 3600, 2) for size in projected_sizes} if ok else {},
    }


def run(sample_size: int | None, concurrency: int | None) -> Path:
    settings = load_settings()
    pilot = settings["pilot"]
    size = sample_size or pilot["sample_size"]
    workers = concurrency or pilot["concurrency"]
    max_values = settings["extraction"]["max_values_per_relation"]
    relations = load_relations(settings.path("ontology"), settings.path("plot_vocabulary"), settings["iris"]["ontology"])
    schema = json_schema(relations, max_values)
    templates = load_prompt(settings.path("extraction_prompt"))
    client = LlmClient.from_settings(settings)
    available = client.models()
    if client.model not in available:
        raise RuntimeError(f"Model {client.model} not served at {client.base_url}; available: {available}")

    movies = select_movies(settings.path("raw_tmdb"), size)
    logger.info("Pilot on %d movies with %d samples each, concurrency %d", len(movies), client.samples, workers)

    def extract(indexed: tuple[int, dict[str, Any]]) -> dict[str, Any]:
        index, movie = indexed
        messages = build_messages(
            templates, relations, max_values, movie["title"], (movie.get("release_date") or "")[:4] or "unknown year", movie["overview"]
        )
        try:
            completion = client.complete(messages, schema, seed_offset=movie["id"])
        except Exception as error:
            logger.warning("Movie %d failed: %s", movie["id"], error)
            return {"movie_id": movie["id"], "error": str(error)}
        if (index + 1) % 10 == 0:
            logger.info("Pilot progress %d/%d", index + 1, len(movies))
        return {
            "movie_id": movie["id"],
            "seconds": completion.seconds,
            "prompt_tokens": completion.prompt_tokens,
            "completion_tokens": completion.completion_tokens,
            "finish_reasons": completion.finish_reasons,
            "samples": completion.texts,
            "analysis": [analyse_sample(text, relations, max_values) for text in completion.texts],
        }

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        records = list(pool.map(extract, enumerate(movies)))
    wall = time.perf_counter() - started

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    runs = settings.path("extraction_runs") / f"pilot_{stamp}.jsonl"
    runs.parent.mkdir(parents=True, exist_ok=True)
    with runs.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    report = {
        "generated_utc": stamp,
        "model": client.model,
        "base_url": client.base_url,
        "samples_per_synopsis": client.samples,
        "temperature": client.temperature,
        "concurrency": workers,
        "max_values_per_relation": max_values,
        "relations": {relation.name: len(relation.concepts) for relation in relations},
        "runs_file": str(runs.relative_to(settings.root)),
        **summarise(records, wall, pilot["projected_sizes"]),
    }
    target = settings.path("reports_pilot") / f"pilot_{stamp}.json"
    write_json(target, report)
    logger.info("Pilot report written to %s: %s", target, {key: report[key] for key in ("seconds_per_movie", "schema_valid_rate", "projected_hours")})
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure extraction throughput on a sample of synopses to fix the final N.")
    parser.add_argument("--sample-size", type=int, default=None, help="override pilot.sample_size")
    parser.add_argument("--concurrency", type=int, default=None, help="override pilot.concurrency")
    args = parser.parse_args()
    setup_logging()
    run(args.sample_size, args.concurrency)


if __name__ == "__main__":
    main()

"""Create the annotation sheets for the extraction evaluation.

precision_sheet.csv  ~200 extracted facts, stratified by support level (1..5 of 5 samples) so every confidence
                     level has enough items for the reliability diagram. The confidence is NOT shown to the
                     annotators; it stays in precision_key.json.
recall_sheet.csv     50 overviews x 9 relations, annotated from scratch without the LLM output, for recall and
                     Cohen's kappa between the two annotators.
"""
import argparse
import csv
import logging
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

from p0_commons.snapshot import movie_path
from p0_commons.config import Settings, load_settings, read_json, setup_logging, write_json
from p0_commons.plot_schema import load_relations
from p6_extraction.run_extraction import FACTS_FILE

logger = logging.getLogger(__name__)

PRECISION_SHEET = "precision_sheet.csv"
PRECISION_KEY = "precision_key.json"
RECALL_SHEET = "recall_sheet.csv"
ANNOTATOR_COLUMNS = ["annotator_1", "annotator_2", "final", "notes"]


def stratified_sample(facts: list[dict[str, Any]], size: int, seed: int) -> list[dict[str, Any]]:
    """Equal allocation over support levels; levels with too few facts give their share to the others."""
    rng = random.Random(seed)
    by_level: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for fact in facts:
        by_level[fact["support"]].append(fact)
    for level in by_level.values():
        rng.shuffle(level)
    quota = dict.fromkeys(by_level, 0)
    remaining = min(size, len(facts))
    while remaining:
        open_levels = [level for level in sorted(by_level) if quota[level] < len(by_level[level])]
        share = max(1, remaining // len(open_levels))
        for level in open_levels:
            take = min(share, len(by_level[level]) - quota[level], remaining)
            quota[level] += take
            remaining -= take
            if not remaining:
                break
    return [fact for level in sorted(by_level) for fact in by_level[level][: quota[level]]]


def overview_of(settings: Settings, movie_id: int) -> tuple[str, str]:
    details = read_json(movie_path(settings.path("raw_tmdb"), movie_id))
    return details.get("overview", ""), (details.get("release_date") or "")[:4]


def write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    """Semicolon-separated UTF-8 with BOM, which Excel with Portuguese settings opens in columns."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def make_precision_sheet(settings: Settings, payload: dict[str, Any], size: int, seed: int, relation_labels: dict[str, str]) -> int:
    facts = payload["facts"]
    sample = stratified_sample(facts, size, seed)
    sample.sort(key=lambda fact: (fact["title"], fact["movie_id"], fact["relation"], fact["value"]))
    rows, items = [], {}
    for number, fact in enumerate(sample, start=1):
        item = f"P{number:03d}"
        overview, year = overview_of(settings, fact["movie_id"])
        rows.append(
            {
                "item": item,
                "movie_id": fact["movie_id"],
                "title": fact["title"],
                "year": year,
                "overview": overview,
                "relation": fact["relation"],
                "statement": f"{relation_labels[fact['relation']]}: {fact['value']}",
                **dict.fromkeys(ANNOTATOR_COLUMNS, ""),
            }
        )
        items[item] = {key: fact[key] for key in ("movie_id", "relation", "concept", "value", "support", "samples", "confidence")}
    population: dict[str, int] = defaultdict(int)
    for fact in facts:
        population[str(fact["support"])] += 1
    columns = ["item", "movie_id", "title", "year", "overview", "relation", "statement", *ANNOTATOR_COLUMNS]
    annotation = settings.path("annotation")
    write_csv(annotation / PRECISION_SHEET, columns, rows)
    write_json(
        annotation / PRECISION_KEY,
        {
            "run_id": payload["run_id"],
            "model": payload["model"],
            "samples_per_synopsis": payload["samples_per_synopsis"],
            "seed": seed,
            "population_by_support": dict(sorted(population.items(), key=lambda item: int(item[0]))),
            "items": items,
        },
    )
    return len(rows)


def make_recall_sheet(settings: Settings, payload: dict[str, Any], size: int, seed: int) -> int:
    relations = load_relations(settings.path("ontology"), settings.path("plot_vocabulary"), settings["iris"]["ontology"])
    titles = {fact["movie_id"]: fact["title"] for fact in payload["facts"]}
    movie_ids = sorted(payload["movie_ids"])
    chosen = sorted(random.Random(seed + 1).sample(movie_ids, min(size, len(movie_ids))))
    rows = []
    for movie_id in chosen:
        details = read_json(movie_path(settings.path("raw_tmdb"), movie_id))
        for relation in relations:
            rows.append(
                {
                    "item": f"R{len(rows) + 1:03d}",
                    "movie_id": movie_id,
                    "title": titles.get(movie_id, details["title"]),
                    "year": (details.get("release_date") or "")[:4],
                    "overview": details.get("overview", ""),
                    "relation": relation.name,
                    "relation_label": relation.label,
                    "allowed_values": " | ".join(relation.concepts),
                    **dict.fromkeys(ANNOTATOR_COLUMNS, ""),
                }
            )
    columns = ["item", "movie_id", "title", "year", "overview", "relation", "relation_label", "allowed_values", *ANNOTATOR_COLUMNS]
    write_csv(settings.path("annotation") / RECALL_SHEET, columns, rows)
    return len(chosen)


def run(settings: Settings, force: bool) -> None:
    payload = read_json(settings.path("extraction") / FACTS_FILE)
    evaluation = settings["evaluation"]
    annotation = settings.path("annotation")
    existing = [name for name in (PRECISION_SHEET, RECALL_SHEET) if (annotation / name).is_file()]
    if existing and not force:
        raise SystemExit(f"{existing} already exist in {annotation}; they may hold annotations. Use --force to overwrite.")
    relations = load_relations(settings.path("ontology"), settings.path("plot_vocabulary"), settings["iris"]["ontology"])
    labels = {relation.name: relation.label for relation in relations}
    precision = make_precision_sheet(settings, payload, evaluation["precision_sample_size"], evaluation["seed"], labels)
    recall = make_recall_sheet(settings, payload, evaluation["recall_sample_size"], evaluation["seed"])
    logger.info("Wrote %d facts to %s and %d overviews to %s", precision, PRECISION_SHEET, recall, RECALL_SHEET)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sample extracted facts and overviews for manual annotation.")
    parser.add_argument("--force", action="store_true", help="overwrite existing sheets (and their annotations)")
    args = parser.parse_args()
    setup_logging()
    run(load_settings(), args.force)


if __name__ == "__main__":
    main()

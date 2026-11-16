"""Score every extraction run (one per model or prompt) against a full gold annotation of the snapshot.

The gold lists, for each film and relation, the vocabulary values the synopsis supports: "core" values (stated or
unavoidable, used for recall) and "acceptable" ones (defensible implications). An extracted fact is correct when its
value is in either list. Because every film is annotated, each run is scored on all its facts, not on a sample.

Values a run proposes that the annotator never looked at (not in the gold and not among the values visible while
annotating) are listed in to_review_<run>.txt; verdicts go into the gold's "reviewed_additions" so every model is
judged on the same terms.

The default gold, data/annotation/claude/gold_claude.json, was written by Claude, not by the team.
"""
import argparse
import csv
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

from p0_commons.config import Settings, load_settings, read_json, setup_logging, write_json

logger = logging.getLogger(__name__)

DEFAULT_GOLD = "data/annotation/claude/gold_claude.json"


def load_gold(path: Path) -> dict[str, Any]:
    gold = read_json(path)
    for addition in gold.get("reviewed_additions", []):
        if addition["verdict"] == "acceptable":
            cell = gold["films"][str(addition["movie_id"])][addition["relation"]]
            if addition["value"] not in cell["core"] + cell["acceptable"]:
                cell["acceptable"].append(addition["value"])
    return gold


def is_correct(gold: dict[str, Any], fact: dict[str, Any]) -> bool:
    cell = gold["films"][str(fact["movie_id"])][fact["relation"]]
    return fact["value"] in cell["core"] or fact["value"] in cell["acceptable"]


def score_run(payload: dict[str, Any], gold: dict[str, Any]) -> dict[str, Any]:
    samples = payload["samples_per_synopsis"]
    films = [str(movie_id) for movie_id in payload["movie_ids"] if str(movie_id) in gold["films"]]
    facts = [fact for fact in payload["facts"] if str(fact["movie_id"]) in gold["films"]]
    labels = [is_correct(gold, fact) for fact in facts]

    by_level: dict[int, list[bool]] = defaultdict(list)
    by_relation: dict[str, list[bool]] = defaultdict(list)
    for fact, label in zip(facts, labels):
        by_level[fact["support"]].append(label)
        if fact["support"] / samples > 0.5:
            by_relation[fact["relation"]].append(label)
    levels = [
        {
            "confidence": round(level / samples, 4),
            "facts": len(by_level[level]),
            "correct": sum(by_level[level]),
            "precision": round(sum(by_level[level]) / len(by_level[level]), 4) if by_level[level] else None,
        }
        for level in range(1, samples + 1)
    ]
    ece = sum(lv["facts"] * abs(lv["precision"] - lv["confidence"]) for lv in levels if lv["facts"]) / len(facts) if facts else None

    predicted: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
    for fact in facts:
        predicted[(str(fact["movie_id"]), fact["relation"])][fact["value"]] = fact["support"]
    core = [(film, relation, value) for film in films for relation, cell in gold["films"][film].items() for value in cell["core"]]

    def recall(min_support: int) -> float | None:
        hits = sum(1 for film, relation, value in core if predicted[(film, relation)].get(value, 0) >= min_support)
        return round(hits / len(core), 4) if core else None

    thresholds = []
    for level in range(1, samples + 1):
        kept = [label for fact, label in zip(facts, labels) if fact["support"] >= level]
        thresholds.append(
            {
                "min_confidence": round(level / samples, 4),
                "facts": len(kept),
                "precision": round(sum(kept) / len(kept), 4) if kept else None,
                "recall_core": recall(level),
            }
        )
    majority = next(row for row in thresholds if row["min_confidence"] > 0.5)
    f1 = (
        round(2 * majority["precision"] * majority["recall_core"] / (majority["precision"] + majority["recall_core"]), 4)
        if majority["precision"] and majority["recall_core"]
        else None
    )
    certain = [label for fact, label in zip(facts, labels) if fact["support"] == samples]
    return {
        "run_id": payload["run_id"],
        "model": payload["model"],
        "films": len(films),
        "facts": len(facts),
        "facts_per_film": round(len(facts) / len(films), 2) if films else None,
        "precision_all": round(sum(labels) / len(labels), 4) if labels else None,
        "ece": None if ece is None else round(ece, 4),
        "majority_vote": {**majority, "f1": f1},
        "certain_facts": {"facts": len(certain), "wrong": len(certain) - sum(certain),
                          "share_wrong": round(1 - sum(certain) / len(certain), 4) if certain else None},
        "levels": levels,
        "by_min_confidence": thresholds,
        "precision_by_relation_majority": {
            name: {"facts": len(values), "precision": round(sum(values) / len(values), 4)} for name, values in sorted(by_relation.items())
        },
    }


def unreviewed(payload: dict[str, Any], gold: dict[str, Any], seen: dict[tuple[str, str], set[str]]) -> list[dict[str, Any]]:
    """Facts whose value is outside the gold and was never shown to the annotator for that film and relation."""
    reviewed = {(str(a["movie_id"]), a["relation"], a["value"]) for a in gold.get("reviewed_additions", [])}
    rows = []
    for fact in payload["facts"]:
        key = (str(fact["movie_id"]), fact["relation"])
        if key[0] not in gold["films"] or is_correct(gold, fact):
            continue
        if fact["value"] in seen.get(key, set()) or (key[0], key[1], fact["value"]) in reviewed:
            continue
        rows.append(fact)
    return rows


def write_labels(path: Path, payload: dict[str, Any], gold: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["annotator", "movie_id", "title", "relation", "value", "support", "confidence", "label_claude"])
        for fact in payload["facts"]:
            if str(fact["movie_id"]) in gold["films"]:
                writer.writerow(["claude", fact["movie_id"], fact["title"], fact["relation"], fact["value"],
                                 fact["support"], fact["confidence"], int(is_correct(gold, fact))])


def plot(results: list[dict[str, Any]], path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axis = plt.subplots(figsize=(5.4, 4.4), dpi=150)
    axis.plot([0, 1], [0, 1], linestyle="--", color="#888780", linewidth=1, label="calibração perfeita")
    for result in results:
        points = [(lv["confidence"], lv["precision"]) for lv in result["levels"] if lv["facts"]]
        axis.plot([p[0] for p in points], [p[1] for p in points], marker="o", linewidth=1.6,
                  label=f"{result['label']} (ECE {result['ece']})")
    axis.set_xlim(0, 1.05)
    axis.set_ylim(0, 1.05)
    axis.set_xlabel("confiança (fração das 5 respostas)")
    axis.set_ylabel("precisão (anotação do Claude)")
    axis.set_title("Calibração por modelo", fontsize=10)
    axis.legend(fontsize=7, frameon=False, loc="upper left")
    figure.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path)
    plt.close(figure)


def markdown(results: list[dict[str, Any]], gold_path: str) -> str:
    lines = [
        "# Comparação de modelos de extração",
        "",
        f"Anotação de referência: `{gold_path}` (feita pelo Claude, não por humanos).",
        "",
        "| Run | Factos/filme | Precisão (todos) | Precisão (≥0.6) | Recall core (≥0.6) | F1 (≥0.6) | ECE | Factos 5/5 errados | s/filme |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        m, c = r["majority_vote"], r["certain_facts"]
        lines.append(
            f"| {r['label']} | {r['facts_per_film']} | {r['precision_all']} | {m['precision']} | {m['recall_core']} | {m['f1']} | "
            f"{r['ece']} | {c['wrong']}/{c['facts']} ({c['share_wrong']}) | {r.get('seconds_per_movie', '')} |"
        )
    return "\n".join(lines) + "\n"


def run(settings: Settings, gold_path: Path, facts_paths: list[Path]) -> dict[str, Any]:
    gold = load_gold(gold_path)
    seen: dict[tuple[str, str], set[str]] = defaultdict(set)
    for run_id in gold.get("seen_run_ids", []):
        seen_payload = read_json(settings.path("extraction") / f"facts_{run_id}.json")
        for fact in seen_payload["facts"]:
            seen[(str(fact["movie_id"]), fact["relation"])].add(fact["value"])
    out = settings.path("reports_evaluation")
    annotation = gold_path.parent
    results = []
    for path in facts_paths:
        payload = read_json(path)
        result = score_run(payload, gold)
        report = settings.path("reports_extraction") / f"extraction_report_{payload['run_id']}.json"
        if report.is_file():
            result["seconds_per_movie"] = read_json(report).get("seconds_per_movie")
        result["label"] = payload.get("label") or payload["model"]
        pending = unreviewed(payload, gold, seen)
        result["unreviewed_novel_facts"] = len(pending)
        if pending:
            with (annotation / f"to_review_{payload['run_id']}.txt").open("w", encoding="utf-8") as handle:
                for fact in sorted(pending, key=lambda f: (f["movie_id"], f["relation"], f["value"])):
                    handle.write(f"{fact['movie_id']} | {fact['relation']} | {fact['value']} | {fact['support']}\n")
        write_labels(annotation / f"fact_labels_claude_{payload['run_id']}.csv", payload, gold)
        results.append(result)
        logger.info(
            "%s: precision %.3f (all), %s (majority), recall %s, ECE %s, unreviewed %d",
            result["label"], result["precision_all"], result["majority_vote"]["precision"],
            result["majority_vote"]["recall_core"], result["ece"], len(pending),
        )
    rel = str(gold_path.relative_to(settings.root)).replace("\\", "/") if gold_path.is_relative_to(settings.root) else str(gold_path)
    write_json(out / "model_comparison.json", {"gold": rel, "annotator": gold["annotator"], "runs": results})
    (out / "model_comparison.md").write_text(markdown(results, rel), encoding="utf-8")
    plot(results, out / "model_comparison_calibration.png")
    return {"runs": results}


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare extraction runs against a full gold annotation.")
    parser.add_argument("--gold", type=Path, default=None, help=f"defaults to {DEFAULT_GOLD}")
    parser.add_argument("facts", nargs="*", type=Path, help="facts_<run>.json files (default: all in data/extraction)")
    args = parser.parse_args()
    setup_logging()
    settings = load_settings()
    gold = (args.gold or settings.root / DEFAULT_GOLD).resolve()
    facts = args.facts or sorted(settings.path("extraction").glob("facts_*.json"))
    run(settings, gold, [path.resolve() for path in facts])


if __name__ == "__main__":
    main()

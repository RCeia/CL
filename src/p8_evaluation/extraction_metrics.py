"""Score the annotated sheets: extraction precision, calibration (reliability diagram, ECE), recall and Cohen's kappa.

Precision sample is stratified by support level, so overall precision and ECE weight each level by its share of
all extracted facts (population_by_support in the key), not by its share of the sample.
"""
import argparse
import csv
import logging
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from p0_commons.config import Settings, load_settings, read_json, setup_logging, write_json
from p8_evaluation.annotation import PRECISION_KEY, PRECISION_SHEET, RECALL_SHEET
from p0_commons.plot_schema import PlotRelation, load_relations
from p6_extraction.run_extraction import FACTS_FILE

logger = logging.getLogger(__name__)

TRUE_LABELS = {"1", "y", "yes", "s", "sim", "true", "t", "correct", "c", "v"}
FALSE_LABELS = {"0", "n", "no", "nao", "não", "false", "f", "wrong", "e", "x"}
VALUE_SEPARATORS = (";", "|", ",")


def parse_label(text: str | None) -> bool | None:
    key = (text or "").strip().casefold()
    if key in TRUE_LABELS:
        return True
    if key in FALSE_LABELS:
        return False
    if key:
        raise ValueError(f"Unknown label {text!r}; use 1 (correct) or 0 (wrong)")
    return None


def read_sheet(path: Path) -> list[dict[str, str]]:
    """Accepts ';', ',' or tab separators (whatever the spreadsheet saved), judged from the header row."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        header = handle.readline()
        handle.seek(0)
        delimiter = max((";", ",", "\t"), key=header.count)
        rows = list(csv.DictReader(handle, delimiter=delimiter))
    return [row for row in rows if (row.get("item") or "").strip()]


def gold(first: Any, second: Any, final: Any) -> tuple[Any, str]:
    """Final adjudication wins; else agreement, else the only annotation; disagreement without final is unresolved."""
    if final is not None:
        return final, "adjudicated"
    if first is not None and second is not None:
        return (first, "agreed") if first == second else (None, "disagreement")
    if first is not None or second is not None:
        return (first if first is not None else second), "single"
    return None, "missing"


def wilson(correct: int, total: int, z: float = 1.96) -> tuple[float, float] | None:
    if not total:
        return None
    p = correct / total
    centre = (p + z * z / (2 * total)) / (1 + z * z / total)
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)


def cohen_kappa(pairs: list[tuple[bool, bool]]) -> dict[str, Any]:
    n = len(pairs)
    if not n:
        return {"items": 0, "kappa": None, "observed_agreement": None}
    observed = sum(a == b for a, b in pairs) / n
    first_yes = sum(a for a, _ in pairs) / n
    second_yes = sum(b for _, b in pairs) / n
    expected = first_yes * second_yes + (1 - first_yes) * (1 - second_yes)
    kappa = None if expected == 1 else (observed - expected) / (1 - expected)
    return {"items": n, "kappa": None if kappa is None else round(kappa, 4), "observed_agreement": round(observed, 4)}


def precision_metrics(rows: list[dict[str, str]], key: dict[str, Any]) -> dict[str, Any]:
    items = key["items"]
    samples = key["samples_per_synopsis"]
    population = {int(level): count for level, count in key["population_by_support"].items()}
    total_population = sum(population.values())
    statuses: dict[str, int] = defaultdict(int)
    by_level: dict[int, list[bool]] = defaultdict(list)
    by_relation: dict[str, list[bool]] = defaultdict(list)
    pairs = []
    for row in rows:
        first, second, final = (parse_label(row.get(column)) for column in ("annotator_1", "annotator_2", "final"))
        if first is not None and second is not None:
            pairs.append((first, second))
        label, status = gold(first, second, final)
        statuses[status] += 1
        if label is None:
            continue
        item = items[row["item"]]
        by_level[item["support"]].append(label)
        by_relation[item["relation"]].append(label)

    levels = []
    for level in sorted(population):
        labels = by_level.get(level, [])
        precision = sum(labels) / len(labels) if labels else None
        levels.append(
            {
                "support": level,
                "confidence": round(level / samples, 4),
                "population": population[level],
                "weight": round(population[level] / total_population, 4),
                "annotated": len(labels),
                "correct": sum(labels),
                "precision": None if precision is None else round(precision, 4),
                "wilson_95": wilson(sum(labels), len(labels)),
            }
        )
    scored = [level for level in levels if level["annotated"]]
    weight = sum(level["population"] for level in scored)
    weighted = sum(level["population"] * level["precision"] for level in scored) / weight if weight else None
    ece = (
        sum(level["population"] * abs(level["precision"] - level["confidence"]) for level in scored) / weight if weight else None
    )
    thresholds = []
    for level in levels:
        kept = [other for other in scored if other["support"] >= level["support"]]
        kept_population = sum(other["population"] for other in levels if other["support"] >= level["support"])
        kept_weight = sum(other["population"] for other in kept)
        thresholds.append(
            {
                "min_confidence": level["confidence"],
                "coverage": round(kept_population / total_population, 4),
                "precision": round(sum(o["population"] * o["precision"] for o in kept) / kept_weight, 4) if kept_weight else None,
            }
        )
    annotated = [label for labels in by_level.values() for label in labels]
    return {
        "annotated_items": len(annotated),
        "items": len(rows),
        "label_status": dict(statuses),
        "sample_precision": round(sum(annotated) / len(annotated), 4) if annotated else None,
        "precision_weighted": None if weighted is None else round(weighted, 4),
        "ece": None if ece is None else round(ece, 4),
        "levels": levels,
        "precision_at_min_confidence": thresholds,
        "precision_by_relation": {
            name: {"annotated": len(labels), "precision": round(sum(labels) / len(labels), 4)}
            for name, labels in sorted(by_relation.items())
        },
        "agreement": cohen_kappa(pairs),
    }


def split_values(text: str | None) -> list[str]:
    text = (text or "").strip()
    for separator in VALUE_SEPARATORS:
        if separator in text:
            return [part.strip() for part in text.split(separator) if part.strip()]
    return [text] if text else []


def resolve_values(text: str | None, relation: PlotRelation, unresolved: list[str]) -> set[str] | None:
    """None when the cell is empty; '-' or 'none' means the overview supports no value."""
    if not (text or "").strip():
        return None
    if text.strip().casefold() in {"-", "none", "nenhum", "nada", "0"}:
        return set()
    resolved = set()
    for value in split_values(text):
        iri = relation.resolve(value)
        if iri is None:
            unresolved.append(f"{relation.name}: {value}")
        else:
            resolved.add(iri)
    return resolved


def recall_metrics(rows: list[dict[str, str]], facts: list[dict[str, Any]], relations: list[PlotRelation], samples: int) -> dict[str, Any]:
    by_name = {relation.name: relation for relation in relations}
    extracted: dict[tuple[int, str], dict[str, int]] = defaultdict(dict)
    for fact in facts:
        extracted[(fact["movie_id"], fact["relation"])][fact["concept"]] = fact["support"]
    unresolved: list[str] = []
    pairs: list[tuple[bool, bool]] = []
    gold_cells: list[tuple[tuple[int, str], set[str]]] = []
    statuses: dict[str, int] = defaultdict(int)
    for row in rows:
        relation = by_name[row["relation"]]
        first, second, final = (resolve_values(row.get(col), relation, unresolved) for col in ("annotator_1", "annotator_2", "final"))
        if first is not None and second is not None:
            pairs.extend((concept in first, concept in second) for concept in relation.concepts.values())
        label, status = gold(first, second, final)
        statuses[status] += 1
        if label is not None:
            gold_cells.append(((int(row["movie_id"]), row["relation"]), label))

    by_threshold = []
    for level in range(1, samples + 1):
        true_positive = predicted = gold_total = 0
        for cell, gold_values in gold_cells:
            predicted_values = {concept for concept, support in extracted[cell].items() if support >= level}
            true_positive += len(predicted_values & gold_values)
            predicted += len(predicted_values)
            gold_total += len(gold_values)
        by_threshold.append(
            {
                "min_confidence": round(level / samples, 4),
                "recall": round(true_positive / gold_total, 4) if gold_total else None,
                "precision": round(true_positive / predicted, 4) if predicted else None,
                "gold_values": gold_total,
                "predicted_values": predicted,
            }
        )
    return {
        "annotated_cells": len(gold_cells),
        "cells": len(rows),
        "label_status": dict(statuses),
        "by_min_confidence": by_threshold,
        "agreement_per_value": cohen_kappa(pairs),
        "unresolved_values": sorted(set(unresolved)),
    }


def reliability_diagram(levels: list[dict[str, Any]], path: Path, title: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    scored = [level for level in levels if level["annotated"]]
    confidence = [level["confidence"] for level in scored]
    precision = [level["precision"] for level in scored]
    lower = [level["precision"] - level["wilson_95"][0] for level in scored]
    upper = [level["wilson_95"][1] - level["precision"] for level in scored]
    figure, axis = plt.subplots(figsize=(4.8, 4.2), dpi=150)
    axis.plot([0, 1], [0, 1], linestyle="--", color="#888780", linewidth=1, label="perfect calibration")
    axis.bar(confidence, precision, width=0.12, color="#378ADD", alpha=0.85, label="observed precision")
    axis.errorbar(confidence, precision, yerr=[lower, upper], fmt="none", ecolor="#0C447C", capsize=3, linewidth=1)
    for x, level in zip(confidence, scored):
        axis.annotate(f"n={level['annotated']}", (x, 0.02), ha="center", fontsize=7, color="#042C53")
    axis.set_xlim(0, 1.08)
    axis.set_ylim(0, 1.1)
    axis.set_xlabel("confidence (share of the 5 samples)")
    axis.set_ylabel("precision (annotated)")
    axis.set_title(title, fontsize=9)
    axis.legend(loc="upper left", fontsize=7, frameon=False)
    figure.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path)
    plt.close(figure)


def claude_agreement(rows: list[dict[str, str]], key: dict[str, Any], gold_path: Path) -> dict[str, Any]:
    """How often the Claude annotation agrees with the team's final label on the precision sheet."""
    from p8_evaluation.compare_models import is_correct, load_gold

    claude = load_gold(gold_path)
    pairs = []
    for row in rows:
        first, second, final = (parse_label(row.get(column)) for column in ("annotator_1", "annotator_2", "final"))
        human, _ = gold(first, second, final)
        item = key["items"][row["item"]]
        if human is None or str(item["movie_id"]) not in claude["films"]:
            continue
        pairs.append((human, is_correct(claude, item)))
    result = cohen_kappa(pairs)
    return {"items": result["items"], "accuracy_of_claude": result["observed_agreement"], "kappa": result["kappa"]}


def run(settings: Settings) -> dict[str, Any]:
    annotation = settings.path("annotation")
    payload = read_json(settings.path("extraction") / FACTS_FILE)
    key = read_json(annotation / PRECISION_KEY)
    if key["run_id"] != payload["run_id"]:
        logger.warning("The precision sheet was sampled from run %s, but facts.json is run %s", key["run_id"], payload["run_id"])
    relations = load_relations(settings.path("ontology"), settings.path("plot_vocabulary"), settings["iris"]["ontology"])
    precision = precision_metrics(read_sheet(annotation / PRECISION_SHEET), key)
    recall_path = annotation / RECALL_SHEET
    recall = recall_metrics(read_sheet(recall_path), payload["facts"], relations, payload["samples_per_synopsis"]) if recall_path.is_file() else None
    report = {"run_id": key["run_id"], "model": key["model"], "precision": precision, "recall": recall}
    claude_gold = settings.root / "data" / "annotation" / "claude" / "gold_claude.json"
    if claude_gold.is_file():
        report["claude_vs_humans"] = claude_agreement(read_sheet(annotation / PRECISION_SHEET), key, claude_gold)
    out = settings.path("reports_evaluation")
    write_json(out / "extraction_eval.json", report)
    if precision["annotated_items"]:
        reliability_diagram(
            precision["levels"],
            out / "reliability_diagram.png",
            f"{key['model']}: precision {precision['precision_weighted']}, ECE {precision['ece']}",
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Compute extraction precision, calibration, recall and agreement from the sheets.")
    parser.parse_args()
    setup_logging()
    report = run(load_settings())
    precision = report["precision"]
    logger.info(
        "Precision %s (weighted, %d/%d items annotated), ECE %s, kappa %s",
        precision["precision_weighted"], precision["annotated_items"], precision["items"], precision["ece"], precision["agreement"]["kappa"],
    )
    if report["recall"]:
        majority = next((row for row in report["recall"]["by_min_confidence"] if row["min_confidence"] > 0.5), None)
        logger.info("Recall at majority vote: %s; per-value kappa %s", majority, report["recall"]["agreement_per_value"]["kappa"])


if __name__ == "__main__":
    main()

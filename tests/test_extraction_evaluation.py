import csv

import pytest

from p0_commons.config import read_json, write_json
from p8_evaluation import annotation, extraction_metrics
from p8_evaluation.extraction_metrics import cohen_kappa, parse_label, precision_metrics

CONCEPT = "http://example.org/kl/concept/"


def make_fact(movie_id, relation, scheme, value, support):
    return {
        "movie_id": movie_id,
        "title": f"Film {movie_id}",
        "relation": relation,
        "relation_iri": f"http://example.org/kl/ontology#{relation}",
        "concept": f"{CONCEPT}{scheme}/{value.replace(' ', '-')}",
        "value": value,
        "support": support,
        "samples": 5,
        "confidence": support / 5,
    }


def test_stratified_sample_balances_levels():
    facts = [make_fact(1, "theme", "theme", f"v{i}", 5) for i in range(50)] + [
        make_fact(1, "theme", "theme", f"w{i}", 1) for i in range(3)
    ]
    sample = annotation.stratified_sample(facts, 20, seed=1)
    assert len(sample) == 20
    assert sum(fact["support"] == 1 for fact in sample) == 3  # all of the small level
    assert sum(fact["support"] == 5 for fact in sample) == 17


def test_kappa_and_labels():
    assert cohen_kappa([(True, True), (False, False), (True, True), (False, False)])["kappa"] == 1.0
    assert cohen_kappa([(True, False), (False, True)])["kappa"] == -1.0
    assert parse_label(" Sim ") is True and parse_label("0") is False and parse_label("") is None
    with pytest.raises(ValueError):
        parse_label("maybe")


def test_precision_is_weighted_by_population():
    key = {
        "samples_per_synopsis": 5,
        "population_by_support": {"1": 300, "5": 100},
        "items": {"a": {"support": 1, "relation": "theme"}, "b": {"support": 1, "relation": "theme"},
                  "c": {"support": 5, "relation": "theme"}, "d": {"support": 5, "relation": "plotEvent"}},
    }
    rows = [
        {"item": "a", "annotator_1": "0", "annotator_2": "0", "final": ""},
        {"item": "b", "annotator_1": "1", "annotator_2": "0", "final": "1"},
        {"item": "c", "annotator_1": "1", "annotator_2": "1", "final": ""},
        {"item": "d", "annotator_1": "1", "annotator_2": "", "final": ""},
    ]
    metrics = precision_metrics(rows, key)
    assert metrics["sample_precision"] == 0.75
    assert metrics["precision_weighted"] == 0.625  # 0.75 * 0.5 + 0.25 * 1.0
    assert metrics["ece"] == 0.225  # 0.75 * |0.5 - 0.2| + 0.25 * |1.0 - 1.0|
    assert metrics["precision_at_min_confidence"][-1] == {"min_confidence": 1.0, "coverage": 0.25, "precision": 1.0}
    assert metrics["agreement"]["items"] == 3
    assert metrics["label_status"] == {"agreed": 2, "adjudicated": 1, "single": 1}


def fill(path, values):
    rows = extraction_metrics.read_sheet(path)
    columns = list(rows[0])
    for row in rows:
        row.update(values(row))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)  # re-saved with commas: both separators must work
        writer.writeheader()
        writer.writerows(rows)


def test_sheets_and_report_end_to_end(project):
    facts = [
        make_fact(900001, "protagonistOccupation", "occupation", "detective", 5),
        make_fact(900001, "protagonistAgeGroup", "age-group", "elderly", 3),
        make_fact(900001, "theme", "theme", "redemption", 1),
        make_fact(900002, "protagonistGoal", "goal", "rescue", 5),
        make_fact(900002, "plotEvent", "event", "kidnapping", 4),
        make_fact(900002, "theme", "theme", "love", 1),
    ]
    write_json(
        project.path("extraction") / "facts.json",
        {"run_id": "r1", "model": "fake", "samples_per_synopsis": 5, "movie_ids": [900001, 900002], "facts": facts},
    )
    annotation.run(project, force=False)
    sheets = project.path("annotation")
    with pytest.raises(SystemExit):
        annotation.run(project, force=False)  # never overwrite annotations silently

    key = read_json(sheets / annotation.PRECISION_KEY)
    assert len(key["items"]) == 6
    assert "confidence" not in (sheets / annotation.PRECISION_SHEET).read_text(encoding="utf-8-sig").splitlines()[0]
    wrong = {item for item, value in key["items"].items() if value["value"] in {"redemption", "love"}}
    fill(sheets / annotation.PRECISION_SHEET, lambda row: {"annotator_1": "0" if row["item"] in wrong else "1"})
    gold = {
        (900001, "protagonistOccupation"): "detective",
        (900001, "protagonistAgeGroup"): "elderly",
        (900002, "protagonistGoal"): "rescue",
        (900002, "plotEvent"): "kidnapping; journey",
    }
    fill(
        sheets / annotation.RECALL_SHEET,
        lambda row: {
            "annotator_1": gold.get((int(row["movie_id"]), row["relation"]), "-"),
            "annotator_2": gold.get((int(row["movie_id"]), row["relation"]), "-"),
        },
    )
    report = extraction_metrics.run(project)
    precision = report["precision"]
    assert precision["annotated_items"] == 6
    assert precision["precision_weighted"] == round(4 / 6, 4)
    by_level = {level["support"]: level["precision"] for level in precision["levels"]}
    assert by_level == {1: 0.0, 3: 1.0, 4: 1.0, 5: 1.0}
    recall = report["recall"]
    any_sample = recall["by_min_confidence"][0]
    assert any_sample["recall"] == 0.8  # journey was not extracted
    assert recall["agreement_per_value"]["kappa"] == 1.0
    assert recall["unresolved_values"] == []
    assert (project.path("reports_evaluation") / "reliability_diagram.png").is_file()

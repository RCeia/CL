from p8_evaluation.compare_models import load_gold, score_run, unreviewed
from p0_commons.config import write_json

RELATIONS = ["antagonistType", "narrativeLocation", "plotEvent", "protagonistAgeGroup", "protagonistGoal",
             "protagonistOccupation", "settingEnvironment", "theme", "timePeriod"]


def empty_film():
    return {name: {"core": [], "acceptable": []} for name in RELATIONS}


def fact(movie_id, relation, value, support):
    return {"movie_id": movie_id, "title": "t", "relation": relation, "value": value, "support": support,
            "samples": 5, "confidence": support / 5}


def test_scores_precision_recall_and_calibration(tmp_path):
    film = empty_film()
    film["protagonistOccupation"] = {"core": ["detective"], "acceptable": ["police officer"]}
    film["timePeriod"] = {"core": ["present day"], "acceptable": []}
    gold_path = tmp_path / "gold.json"
    write_json(gold_path, {"annotator": "test", "films": {"1": film},
                           "reviewed_additions": [{"movie_id": 1, "relation": "theme", "value": "grief", "verdict": "acceptable"}]})
    gold = load_gold(gold_path)
    payload = {"run_id": "r", "model": "m", "samples_per_synopsis": 5, "movie_ids": [1], "facts": [
        fact(1, "protagonistOccupation", "detective", 5),       # core, right
        fact(1, "protagonistOccupation", "police officer", 3),  # acceptable, right
        fact(1, "narrativeLocation", "Australia", 5),           # wrong at 5/5
        fact(1, "theme", "grief", 1),                           # right through a reviewed addition
        fact(1, "theme", "love", 1),                            # wrong
    ]}
    result = score_run(payload, gold)
    assert result["precision_all"] == 0.6
    assert result["certain_facts"] == {"facts": 2, "wrong": 1, "share_wrong": 0.5}
    majority = result["majority_vote"]
    assert majority["facts"] == 3 and majority["precision"] == round(2 / 3, 4)
    assert majority["recall_core"] == 0.5  # detective found, present day missed
    by_level = {level["confidence"]: level["precision"] for level in result["levels"]}
    assert by_level[0.2] == 0.5 and by_level[0.6] == 1.0 and by_level[1.0] == 0.5
    seen = {("1", "theme"): {"love"}}
    assert [f["value"] for f in unreviewed(payload, gold, seen)] == ["Australia"]

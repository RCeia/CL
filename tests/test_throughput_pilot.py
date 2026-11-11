import json

from klkg.config import read_json
from klkg.extraction.llm_client import Completion, LlmClient
from klkg.pilot import throughput_pilot


def test_pilot_writes_report_and_runs(project, monkeypatch):
    monkeypatch.setenv("KLKG_CONFIG", str(project.root / "config" / "settings.yaml"))
    answer = json.dumps({
        "antagonistType": [], "narrativeLocation": [], "plotEvent": ["investigation"], "protagonistAgeGroup": ["adult"],
        "protagonistGoal": [], "protagonistOccupation": ["detective"], "settingEnvironment": ["urban"], "theme": [], "timePeriod": [],
    })
    monkeypatch.setattr(LlmClient, "models", lambda self: [self.model])
    monkeypatch.setattr(
        LlmClient,
        "complete",
        lambda self, messages, schema, seed_offset=0: Completion([answer] * 4 + ["not json"], 300, 200, 0.5, ["stop"] * 5),
    )
    report = read_json(throughput_pilot.run(sample_size=5, concurrency=2))
    assert report["movies"] == 2
    assert report["samples"] == 10
    assert report["json_parse_rate"] == 0.8
    assert report["schema_valid_rate"] == 0.8
    assert report["facts_per_sample_mean"] == 3.2
    assert set(report["projected_hours"]) == {"2000", "2500", "3000"}
    assert (project.root / report["runs_file"]).is_file()

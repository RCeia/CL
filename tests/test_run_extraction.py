import json

from p0_commons.config import read_json
from p6_extraction import run_extraction
from p6_extraction.llm_client import Completion, LlmClient
from p0_commons.plot_schema import load_relations

KEYS = [
    "antagonistType", "narrativeLocation", "plotEvent", "protagonistAgeGroup", "protagonistGoal",
    "protagonistOccupation", "settingEnvironment", "theme", "timePeriod",
]
OCCUPATION = "http://example.org/kl/concept/occupation/"


def answer(**values):
    return json.dumps({key: values.get(key, []) for key in KEYS})


def relations_of(settings):
    return load_relations(settings.path("ontology"), settings.path("plot_vocabulary"), settings["iris"]["ontology"])


def test_confidence_is_share_of_requested_samples(project):
    relations = relations_of(project)
    texts = [
        answer(protagonistOccupation=["detective"], plotEvent=["investigation"]),
        answer(protagonistOccupation=["detective", "detective"], plotEvent=["investigation"]),
        answer(protagonistOccupation=["private investigator"]),  # altLabel of detective
        answer(protagonistOccupation=["journalist", "not a concept"]),
        "not json",
    ]
    facts = {(fact["relation"], fact["value"]): fact for fact in run_extraction.aggregate(texts, relations, 3, 5)}
    assert facts[("protagonistOccupation", "detective")]["support"] == 3
    assert facts[("protagonistOccupation", "detective")]["confidence"] == 0.6
    assert facts[("protagonistOccupation", "detective")]["concept"] == OCCUPATION + "detective"
    assert facts[("protagonistOccupation", "journalist")]["confidence"] == 0.2
    assert facts[("plotEvent", "investigation")]["support"] == 2
    assert len(facts) == 3


def test_shuffle_changes_order_per_seed_not_content(project):
    relations = relations_of(project)
    first, second = run_extraction.shuffled(relations, 1), run_extraction.shuffled(relations, 2)
    for original, a, b in zip(relations, first, second):
        assert set(a.concepts.items()) == set(original.concepts.items()) == set(b.concepts.items())
    assert any(list(a.concepts) != list(b.concepts) for a, b in zip(first, second))
    assert [list(r.concepts) for r in run_extraction.shuffled(relations, 1)] == [list(r.concepts) for r in first]


def test_shuffled_run_sends_one_order_per_sample(project, monkeypatch):
    sent = []

    def complete_each(self, variants, schema, seed_offset=0):
        sent.append(variants)
        return Completion([answer(protagonistOccupation=["detective"])] * len(variants), 1, 1, 0.1, ["stop"] * len(variants))

    monkeypatch.setattr(LlmClient, "models", lambda self: [self.model])
    monkeypatch.setattr(LlmClient, "complete_each", complete_each)
    payload = read_json(run_extraction.run(project, limit=1, concurrency=1, shuffle=True))
    assert len(sent) == 1 and len(sent[0]) == 5
    assert len({variant[1]["content"] for variant in sent[0]}) > 1  # the user messages differ in value order
    assert payload["shuffle_values"] is True and payload["label"].endswith("shuffle")
    assert (project.path("extraction") / f"facts_{payload['run_id']}.json").is_file()


def test_run_writes_facts_and_resumes(project, monkeypatch):
    calls = []

    def complete(self, messages, schema, seed_offset=0):
        calls.append(seed_offset)
        texts = [answer(protagonistOccupation=["detective"])] * 4 + [answer(protagonistGoal=["justice"])]
        return Completion(texts, 500, 100, 0.1, ["stop"] * 5)

    monkeypatch.setattr(LlmClient, "models", lambda self: [self.model])
    monkeypatch.setattr(LlmClient, "complete", complete)
    facts_path = run_extraction.run(project, limit=None, concurrency=2)
    payload = read_json(facts_path)
    assert sorted(payload["movie_ids"]) == [900001, 900002]
    assert len(payload["facts"]) == 4
    detective = next(f for f in payload["facts"] if f["movie_id"] == 900001 and f["value"] == "detective")
    assert detective["confidence"] == 0.8
    report = read_json(project.path("reports_extraction") / "extraction_report.json")
    assert report["facts_by_support"] == {"1": 2, "4": 2}
    assert report["json_object_rate"] == 1.0

    run_extraction.run(project, limit=None, concurrency=2)
    assert len(calls) == 2  # second run reused the stored samples

import pytest

from klkg.extraction.plot_schema import build_messages, json_schema, load_prompt, load_relations, validate_output

EXPECTED = {
    "antagonistType",
    "narrativeLocation",
    "plotEvent",
    "protagonistAgeGroup",
    "protagonistGoal",
    "protagonistOccupation",
    "settingEnvironment",
    "theme",
    "timePeriod",
}


@pytest.fixture(scope="module")
def relations():
    from klkg.config import load_settings

    settings = load_settings()
    return settings, load_relations(settings.path("ontology"), settings.path("plot_vocabulary"), settings["iris"]["ontology"])


def test_nine_relations_with_closed_value_sets(relations):
    _, loaded = relations
    assert {relation.name for relation in loaded} == EXPECTED
    assert all(relation.concepts for relation in loaded)


def test_schema_is_closed(relations):
    _, loaded = relations
    schema = json_schema(loaded, 3)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == EXPECTED
    assert "journalist" in schema["properties"]["protagonistOccupation"]["items"]["enum"]


def test_labels_resolve_to_concepts(relations):
    _, loaded = relations
    occupation = next(relation for relation in loaded if relation.name == "protagonistOccupation")
    assert occupation.resolve("Cop") == "http://example.org/kl/concept/occupation/police-officer"
    assert occupation.resolve("journalist") == "http://example.org/kl/concept/occupation/journalist"
    assert occupation.resolve("astronomer") is None


def test_prompt_and_validation(relations):
    settings, loaded = relations
    messages = build_messages(load_prompt(settings.path("extraction_prompt")), loaded, 3, "A Film", "2001", "A synopsis.")
    assert [message["role"] for message in messages] == ["system", "user"]
    assert "A synopsis." in messages[1]["content"] and "protagonistOccupation" in messages[1]["content"]
    good = {name: [] for name in EXPECTED} | {"theme": ["family"]}
    assert validate_output(good, loaded, 3) == []
    bad = {name: [] for name in EXPECTED if name != "theme"} | {"plotEvent": ["picnic"], "extra": []}
    errors = validate_output(bad, loaded, 3)
    assert any("picnic" in error for error in errors)
    assert any("theme" in error for error in errors)
    assert any("extra" in error for error in errors)

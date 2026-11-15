import rdflib

from p9_questions.template_questions import TEMPLATES, generate
from p4_base_kg.build_base_kg import build


def test_questions_match_their_sparql(prepared):
    output = prepared.path("rdf") / "base.ttl"
    build(prepared, output)
    graph = rdflib.Graph().parse(output)
    questions = generate(graph, per_hop=10, max_answers=25, seed=3)
    assert questions
    assert len({q["id"] for q in questions}) == len(questions)
    assert {q["hops"] for q in questions} <= {1, 2, 3}
    for question in questions:
        answers = sorted(str(row[0]) for row in graph.query(question["sparql"]))
        assert answers == [answer["iri"] for answer in question["answers"]]
        assert 1 <= len(answers) <= 25
        assert question["topic_entities"][0]["label"] in question["question"]
    directed = next(q for q in questions if q["template"] == "film_director" and "Fixture Film One" in q["question"])
    assert [answer["iri"] for answer in directed["answers"]] == ["http://example.org/kl/resource/person/200"]


def test_templates_are_well_formed():
    assert len({template.name for template in TEMPLATES}) == len(TEMPLATES)
    assert {template.hops for template in TEMPLATES} == {1, 2, 3}
    assert all("{t}" in template.pattern and "?answer" in template.pattern for template in TEMPLATES)

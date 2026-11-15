from decimal import Decimal

import pytest
import rdflib
from rdflib import Literal, URIRef
from rdflib.namespace import RDF, SH, XSD

from p0_commons.config import write_json
from p4_base_kg.build_base_kg import build as build_base
from p7_enriched_kg.build_enriched_kg import build as build_enriched
from p0_commons.validate_shacl import run_validation

KL = rdflib.Namespace("http://example.org/kl/ontology#")
RES = rdflib.Namespace("http://example.org/kl/resource/")
CONCEPT = "http://example.org/kl/concept/"


def fact(movie_id, relation, scheme, slug, value, support):
    return {
        "movie_id": movie_id,
        "title": "t",
        "relation": relation,
        "relation_iri": f"http://example.org/kl/ontology#{relation}",
        "concept": f"{CONCEPT}{scheme}/{slug}",
        "value": value,
        "support": support,
        "samples": 5,
        "confidence": support / 5,
    }


@pytest.fixture(scope="module")
def enriched(prepared):
    rdf = prepared.path("rdf")
    build_base(prepared, rdf / "base.ttl")
    facts = [
        fact(900001, "protagonistOccupation", "occupation", "detective", "detective", 5),
        fact(900001, "protagonistAgeGroup", "age-group", "elderly", "elderly", 2),
        fact(900002, "protagonistGoal", "goal", "rescue", "rescue", 4),
    ]
    write_json(prepared.path("extraction") / "facts.json", {"model": "fake-model", "run_id": "r1", "facts": facts})
    stats = build_enriched(prepared, rdf / "enriched.ttl")
    graph = rdflib.Graph().parse(rdf / "enriched.ttl")
    return prepared, stats, graph


def shapes_check(settings, *paths):
    return run_validation(list(paths), settings.path("enriched_shapes"), [settings.path("ontology"), settings.path("plot_vocabulary")])


def test_facts_are_direct_and_reified(enriched):
    _, stats, graph = enriched
    film = RES["film/900001"]
    detective = URIRef(CONCEPT + "occupation/detective")
    assert (film, KL.protagonistOccupation, detective) in graph
    node = RES["fact/900001/protagonistOccupation/detective"]
    assert (node, RDF.type, KL.PlotFact) in graph
    assert (node, RDF.subject, film) in graph
    assert (node, RDF.object, detective) in graph
    assert (node, KL.confidence, Literal(Decimal("1.0"), datatype=XSD.decimal)) in graph
    assert stats["facts"] == 3
    assert stats["films_with_facts"] == 2


def test_enriched_shapes_pass(enriched):
    settings, _, _ = enriched
    rdf = settings.path("rdf")
    valid, _, summary = shapes_check(settings, rdf / "base.ttl", rdf / "enriched.ttl")
    assert valid, summary


@pytest.mark.parametrize(
    ("corrupt", "message"),
    [
        ("wrong_scheme", "not a concept of the relation's kl:valueScheme"),
        ("missing_direct_triple", "not asserted"),
        ("bad_confidence", "confidence is not support / sample count"),
    ],
)
def test_enriched_shapes_catch_errors(enriched, tmp_path, corrupt, message):
    settings, _, graph = enriched
    broken = rdflib.Graph()
    for triple in graph:
        broken.add(triple)
    node = RES["fact/900002/protagonistGoal/rescue"]
    film, goal = RES["film/900002"], URIRef(CONCEPT + "goal/rescue")
    if corrupt == "wrong_scheme":
        theme = URIRef(CONCEPT + "theme/love")
        broken.set((node, RDF.object, theme))
        broken.add((film, KL.protagonistGoal, theme))
    elif corrupt == "missing_direct_triple":
        broken.remove((film, KL.protagonistGoal, goal))
    else:
        broken.set((node, KL.confidence, Literal(Decimal("0.2"), datatype=XSD.decimal)))
    path = tmp_path / "broken.ttl"
    broken.serialize(path, format="turtle")
    valid, report, summary = shapes_check(settings, settings.path("rdf") / "base.ttl", path)
    assert not valid, corrupt
    messages = [str(text) for text in report.objects(None, SH.resultMessage)]
    assert any(message in text for text in messages), messages


def test_min_confidence_filters_facts(enriched, tmp_path):
    settings, _, _ = enriched
    data = {**settings.data, "enrichment": {"min_confidence": 0.5}}
    stricter = type(settings)(root=settings.root, data=data)
    stats = build_enriched(stricter, tmp_path / "enriched.ttl")
    assert stats["facts"] == 2
    assert stats["facts_dropped_below_min_confidence"] == 1

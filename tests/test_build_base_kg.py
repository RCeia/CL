import pytest
import rdflib
from rdflib import Literal, URIRef
from rdflib.namespace import OWL, RDF, XSD

from klkg.kg.build_base_kg import build, check_prefixes
from klkg.kg.validate_shacl import run_validation

KL = rdflib.Namespace("http://example.org/kl/ontology#")
RES = rdflib.Namespace("http://example.org/kl/resource/")
SCHEMA = rdflib.Namespace("http://schema.org/")
WD = rdflib.Namespace("http://www.wikidata.org/entity/")


@pytest.fixture(scope="module")
def base(prepared):
    output = prepared.path("rdf") / "base.ttl"
    build(prepared, output)
    graph = rdflib.Graph()
    graph.parse(output)
    return prepared, output, graph


def test_film_metadata(base):
    _, _, graph = base
    film = RES["film/900001"]
    assert (film, RDF.type, KL.Film) in graph
    assert (film, SCHEMA.name, Literal("Fixture Film One")) in graph
    assert (film, SCHEMA.datePublished, Literal("2001-05-04", datatype=XSD.date)) in graph
    assert (film, KL.voteAverage, Literal("7.4", datatype=XSD.decimal)) in graph
    assert (film, OWL.sameAs, WD.Q999000001) in graph
    assert (film, SCHEMA.isPartOf, RES["collection/77001"]) in graph
    assert (film, SCHEMA.inLanguage, RES["language/en"]) in graph
    assert len(set(graph.objects(film, SCHEMA.actor))) == 10


def test_crew_and_credits(base):
    _, _, graph = base
    film = RES["film/900001"]
    assert set(graph.objects(film, SCHEMA.director)) == {RES["person/200"]}
    assert set(graph.objects(film, KL.writer)) == {RES["person/200"], RES["person/201"]}
    credit = RES["credit/c103x3"]
    assert (film, KL.hasCastCredit, credit) in graph
    assert (credit, KL.creditedPerson, RES["person/103"]) in graph
    assert (credit, KL.castOrder, Literal("3", datatype=XSD.integer)) in graph
    assert graph.value(credit, KL.characterName) is None


def test_missing_values_produce_no_triples(base):
    _, _, graph = base
    film = RES["film/900002"]
    for predicate in (SCHEMA.datePublished, KL.runtimeMinutes, KL.budget, KL.voteAverage, SCHEMA.inLanguage, KL.imdbId):
        assert graph.value(film, predicate) is None, predicate
    assert (film, OWL.sameAs, WD.Q999000002) in graph
    assert graph.value(RES["person/101"], OWL.sameAs) is None


def test_literal_nan_is_kept(base):
    _, _, graph = base
    assert (RES["keyword/9002"], SCHEMA.name, Literal("nan")) in graph


def test_rml_is_generated(base):
    settings, _, _ = base
    rml = rdflib.Graph().parse(settings.path("rml_mapping"))
    assert len(set(rml.subjects(URIRef("http://w3id.org/rml/logicalSource")))) > 10


def test_shacl_passes(base):
    settings, output, _ = base
    valid, _, summary = run_validation([output], settings.path("base_shapes"), [settings.path("ontology"), settings.path("plot_vocabulary")])
    assert valid, summary
    assert summary["by_severity"].get("Violation", 0) == 0


def test_shacl_catches_a_broken_film(base, tmp_path):
    settings, output, graph = base
    graph.add((RES["film/900001"], SCHEMA.name, Literal("Duplicate title", datatype=XSD.string)))
    broken = tmp_path / "broken.ttl"
    graph.serialize(broken, format="turtle")
    valid, _, summary = run_validation([broken], settings.path("base_shapes"), [settings.path("ontology")])
    assert not valid
    assert summary["by_severity"]["Violation"] >= 1


def test_prefix_mismatch_is_rejected(project):
    data = {**project.data, "iris": {**project["iris"], "resource": "http://example.org/other/"}}
    with pytest.raises(ValueError):
        check_prefixes(project.path("yarrrml_mapping"), type(project)(root=project.root, data=data))

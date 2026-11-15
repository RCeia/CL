"""Turn the extracted facts into the enriched named graph.

Every fact is written twice:
  - as a direct triple, e.g. res:film/27205 kl:protagonistOccupation occupation:spy, so SPARQL hops stay short;
  - as a kl:PlotFact node (standard RDF reification with rdf:subject / rdf:predicate / rdf:object) that
    carries the confidence, the support and the model, so systems can filter or rank by confidence.
"""
import argparse
import logging
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import rdflib
from rdflib import Literal, Namespace, URIRef
from rdflib.namespace import RDF, SKOS, XSD

from p0_commons.config import Settings, load_settings, read_json, setup_logging, write_json
from p6_extraction.run_extraction import FACTS_FILE

logger = logging.getLogger(__name__)


def fact_iri(resource: Namespace, fact: dict[str, Any]) -> URIRef:
    concept = fact["concept"].rstrip("/").rsplit("/", 1)[-1]
    return resource[f"fact/{fact['movie_id']}/{fact['relation']}/{concept}"]


def build_graph(facts: list[dict[str, Any]], settings: Settings, model: str, min_confidence: float) -> rdflib.Graph:
    kl = Namespace(settings["iris"]["ontology"])
    res = Namespace(settings["iris"]["resource"])
    graph = rdflib.Graph()
    graph.bind("kl", kl)
    graph.bind("res", res)
    graph.bind("klc", settings["iris"]["concept"])
    graph.bind("skos", SKOS)
    for fact in facts:
        if fact["confidence"] < min_confidence:
            continue
        film = res[f"film/{fact['movie_id']}"]
        predicate = URIRef(fact["relation_iri"])
        concept = URIRef(fact["concept"])
        node = fact_iri(res, fact)
        graph.add((film, predicate, concept))
        graph.add((node, RDF.type, kl.PlotFact))
        graph.add((node, RDF.subject, film))
        graph.add((node, RDF.predicate, predicate))
        graph.add((node, RDF.object, concept))
        graph.add((node, kl.confidence, Literal(Decimal(str(fact["confidence"])), datatype=XSD.decimal)))
        graph.add((node, kl.support, Literal(fact["support"], datatype=XSD.integer)))
        graph.add((node, kl.sampleCount, Literal(fact["samples"], datatype=XSD.integer)))
        graph.add((node, kl.extractionModel, Literal(model, datatype=XSD.string)))
    return graph


def statistics(graph: rdflib.Graph, facts: list[dict[str, Any]], min_confidence: float) -> dict[str, Any]:
    kept = [fact for fact in facts if fact["confidence"] >= min_confidence]
    return {
        "triples": len(graph),
        "facts": len(kept),
        "facts_dropped_below_min_confidence": len(facts) - len(kept),
        "films_with_facts": len({fact["movie_id"] for fact in kept}),
        "facts_by_relation": dict(sorted(Counter(fact["relation"] for fact in kept).items())),
        "facts_by_confidence": {str(level): count for level, count in sorted(Counter(fact["confidence"] for fact in kept).items())},
    }


def build(settings: Settings, output: Path) -> dict[str, Any]:
    payload = read_json(settings.path("extraction") / FACTS_FILE)
    min_confidence = float(settings["enrichment"]["min_confidence"])
    graph = build_graph(payload["facts"], settings, payload["model"], min_confidence)
    output.parent.mkdir(parents=True, exist_ok=True)
    graph.serialize(output, format="turtle")
    stats = {"model": payload["model"], "run_id": payload["run_id"], "min_confidence": min_confidence, **statistics(graph, payload["facts"], min_confidence)}
    write_json(output.with_name(output.stem + "_stats.json"), stats)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Write the extracted plot facts as the enriched RDF graph.")
    parser.add_argument("--output", type=Path, default=None, help="defaults to <paths.rdf>/enriched.ttl")
    args = parser.parse_args()
    setup_logging()
    settings = load_settings()
    output = args.output or settings.path("rdf") / "enriched.ttl"
    stats = build(settings, output)
    logger.info("Wrote %s: %d facts for %d films, %d triples", output, stats["facts"], stats["films_with_facts"], stats["triples"])


if __name__ == "__main__":
    main()

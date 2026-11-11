import argparse
import configparser
import contextlib
import logging
from collections import Counter
from pathlib import Path

import morph_kgc
import rdflib
import yaml
from morph_kgc.mapping.yarrrml import load_yarrrml
from rdflib.namespace import OWL, RDF, XSD

from klkg.config import Settings, load_settings, setup_logging, write_json

logger = logging.getLogger(__name__)

RML_NAMESPACE = "http://w3id.org/rml/"
RML_SOURCE = rdflib.URIRef(RML_NAMESPACE + "source")
RML_REFERENCE_FORMULATION = rdflib.URIRef(RML_NAMESPACE + "referenceFormulation")
MAPPING_NAMESPACE = "http://example.org/kl/mapping#"
MAPPING_INTERIM_DIR = "data/interim"
NAMESPACES = {"schema": "http://schema.org/", "owl": str(OWL), "xsd": str(XSD), "wd": "http://www.wikidata.org/entity/"}


def check_prefixes(yarrrml_path: Path, settings: Settings) -> None:
    with yarrrml_path.open(encoding="utf-8") as handle:
        prefixes = yaml.safe_load(handle)["prefixes"]
    expected = {"kl": settings["iris"]["ontology"], "res": settings["iris"]["resource"], "wd": settings["iris"]["wikidata_entity"]}
    mismatched = {name: (prefixes.get(name), iri) for name, iri in expected.items() if prefixes.get(name) != iri}
    if mismatched:
        raise ValueError(f"YARRRML prefixes differ from settings.yaml iris: {mismatched}")


def generate_rml(yarrrml_path: Path, rml_path: Path, interim_dir: str) -> rdflib.Graph:
    translated = load_yarrrml(str(yarrrml_path))
    graph = rdflib.Graph()
    graph.bind("rml", RML_NAMESPACE)
    graph.bind("map", MAPPING_NAMESPACE)
    for subject, predicate, value in translated:
        if isinstance(subject, rdflib.URIRef) and ":" not in subject:
            subject = rdflib.URIRef(MAPPING_NAMESPACE + subject)
        if predicate == RML_SOURCE and str(value).startswith(MAPPING_INTERIM_DIR):
            value = rdflib.Literal(interim_dir + str(value)[len(MAPPING_INTERIM_DIR):])
        if predicate == RML_REFERENCE_FORMULATION and isinstance(value, rdflib.Literal):
            value = rdflib.URIRef(value)
        graph.add((subject, predicate, value))
    rml_path.parent.mkdir(parents=True, exist_ok=True)
    graph.serialize(rml_path, format="turtle")
    return graph


def materialize(root: Path, ini_path: Path) -> rdflib.Graph:
    parser = configparser.ConfigParser()
    parser.read(ini_path, encoding="utf-8")
    for section in parser.sections():
        if parser.has_option(section, "mappings"):
            mapping = Path(parser.get(section, "mappings"))
            if not (root / mapping).is_file():
                raise FileNotFoundError(root / mapping)
    with contextlib.chdir(root):
        return morph_kgc.materialize(str(ini_path))


def statistics(graph: rdflib.Graph) -> dict[str, object]:
    classes = Counter(str(cls) for cls in graph.objects(None, RDF.type))
    predicates = Counter(str(predicate) for predicate in graph.predicates())
    return {
        "triples": len(graph),
        "classes": dict(sorted(classes.items())),
        "predicates": dict(sorted(predicates.items())),
    }


def build(settings: Settings, output: Path) -> dict[str, object]:
    yarrrml_path = settings.path("yarrrml_mapping")
    check_prefixes(yarrrml_path, settings)
    generate_rml(yarrrml_path, settings.path("rml_mapping"), settings["paths"]["interim"].rstrip("/"))
    graph = materialize(settings.root, settings.path("morph_kgc_config"))
    graph.bind("kl", settings["iris"]["ontology"])
    graph.bind("res", settings["iris"]["resource"])
    for prefix, namespace in NAMESPACES.items():
        graph.bind(prefix, namespace)
    output.parent.mkdir(parents=True, exist_ok=True)
    graph.serialize(output, format="turtle")
    stats = statistics(graph)
    write_json(output.with_name(output.stem + "_stats.json"), stats)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate RML from YARRRML and materialise the base KG with morph-kgc.")
    parser.add_argument("--output", type=Path, default=None, help="defaults to <paths.rdf>/base.ttl")
    parser.add_argument("--rml-only", action="store_true", help="only regenerate the RML mapping from YARRRML")
    args = parser.parse_args()
    setup_logging()
    settings = load_settings()
    if args.rml_only:
        check_prefixes(settings.path("yarrrml_mapping"), settings)
        generate_rml(settings.path("yarrrml_mapping"), settings.path("rml_mapping"), settings["paths"]["interim"].rstrip("/"))
        logger.info("Wrote %s", settings.path("rml_mapping"))
        return
    output = args.output or settings.path("rdf") / "base.ttl"
    stats = build(settings, output)
    logger.info("Wrote %s with %d triples", output, stats["triples"])


if __name__ == "__main__":
    main()

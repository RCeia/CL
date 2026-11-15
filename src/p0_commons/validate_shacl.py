import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

import rdflib
from pyshacl import validate
from rdflib.namespace import SH

from p0_commons.config import load_settings, setup_logging, write_json

logger = logging.getLogger(__name__)


def load_graph(paths: list[Path]) -> rdflib.Graph:
    graph = rdflib.Graph()
    for path in paths:
        graph.parse(path)
    return graph


def run_validation(data: list[Path], shapes: Path, ontology: list[Path]) -> tuple[bool, rdflib.Graph, dict[str, object]]:
    # The ontology and the SKOS vocabulary are merged into the data graph so that sh:class checks on concepts and
    # the SHACL-SPARQL constraints can see them (pyshacl only mixes ont_graph in when inference is on).
    conforms, report, _ = validate(
        data_graph=load_graph(data + ontology),
        shacl_graph=load_graph([shapes]),
        inference="none",
        allow_warnings=True,
        advanced=True,
    )
    results = list(report.subjects(rdflib.RDF.type, SH.ValidationResult))
    severities = Counter(str(report.value(result, SH.resultSeverity)).rsplit("#", 1)[-1] for result in results)
    by_shape = Counter(
        (str(report.value(result, SH.resultSeverity)).rsplit("#", 1)[-1], str(report.value(result, SH.resultPath)))
        for result in results
    )
    summary = {
        "conforms": conforms,
        "results": len(results),
        "by_severity": dict(severities),
        "by_severity_and_path": [{"severity": key[0], "path": key[1], "count": count} for key, count in by_shape.most_common()],
    }
    return severities.get("Violation", 0) == 0, report, summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a KG serialisation against SHACL shapes.")
    parser.add_argument("--data", type=Path, nargs="+", default=None, help="defaults to <paths.rdf>/base.ttl")
    parser.add_argument("--shapes", type=Path, default=None, help="defaults to paths.base_shapes")
    parser.add_argument("--name", default=None, help="name used for the report files (default: base or enriched)")
    parser.add_argument(
        "--enriched", action="store_true", help="validate base.ttl + enriched.ttl against paths.enriched_shapes"
    )
    args = parser.parse_args()
    setup_logging()
    settings = load_settings()
    rdf = settings.path("rdf")
    if args.enriched:
        data = args.data or [rdf / "base.ttl", rdf / "enriched.ttl"]
        shapes = args.shapes or settings.path("enriched_shapes")
        name = args.name or "enriched"
    else:
        data = args.data or [rdf / "base.ttl"]
        shapes = args.shapes or settings.path("base_shapes")
        name = args.name or "base"
    valid, report, summary = run_validation(data, shapes, [settings.path("ontology"), settings.path("plot_vocabulary")])
    out = settings.path("reports_shacl")
    out.mkdir(parents=True, exist_ok=True)
    report.serialize(out / f"{name}_report.ttl", format="turtle")
    write_json(out / f"{name}_summary.json", summary)
    logger.info("SHACL %s: %s", "passed" if valid else "failed", summary["by_severity"])
    sys.exit(0 if valid else 1)


if __name__ == "__main__":
    main()

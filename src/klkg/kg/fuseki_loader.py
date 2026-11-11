import argparse
import logging
import os
from pathlib import Path

import requests

from klkg.config import Settings, load_settings, setup_logging

logger = logging.getLogger(__name__)

CONTENT_TYPES = {".ttl": "text/turtle", ".nt": "application/n-triples", ".rdf": "application/rdf+xml", ".trig": "application/trig"}


class FusekiClient:
    def __init__(self, url: str, dataset: str, gsp: str, query: str, timeout: float, auth: tuple[str, str] | None):
        self.base = f"{url.rstrip('/')}/{dataset}"
        self.server = url.rstrip("/")
        self.gsp_url = f"{self.base}/{gsp}"
        self.query_url = f"{self.base}/{query}"
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = auth

    @classmethod
    def from_settings(cls, settings: Settings) -> "FusekiClient":
        fuseki = settings["fuseki"]
        user, password = os.environ.get("FUSEKI_USER"), os.environ.get("FUSEKI_PASSWORD")
        return cls(
            url=settings.value("fuseki", "url", env="FUSEKI_URL"),
            dataset=fuseki["dataset"],
            gsp=fuseki["gsp_endpoint"],
            query=fuseki["query_endpoint"],
            timeout=fuseki["timeout_seconds"],
            auth=(user, password) if user and password else None,
        )

    def ping(self) -> None:
        response = self.session.get(f"{self.server}/$/ping", timeout=30)
        response.raise_for_status()

    def upload(self, path: Path, graph: str, replace: bool) -> None:
        content_type = CONTENT_TYPES.get(path.suffix)
        if content_type is None:
            raise ValueError(f"Unsupported RDF file extension: {path}")
        method = self.session.put if replace else self.session.post
        with path.open("rb") as body:
            response = method(
                self.gsp_url,
                params={"graph": graph},
                data=body,
                headers={"Content-Type": f"{content_type}; charset=utf-8"},
                timeout=self.timeout,
            )
        response.raise_for_status()

    def count(self, graph: str) -> int:
        query = f"SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH <{graph}> {{ ?s ?p ?o }} }}"
        response = self.session.post(
            self.query_url,
            data={"query": query},
            headers={"Accept": "application/sparql-results+json"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return int(response.json()["results"]["bindings"][0]["n"]["value"])


def targets(settings: Settings) -> dict[str, tuple[str, list[Path]]]:
    graphs = settings["graphs"]
    rdf = settings.path("rdf")
    return {
        "ontology": (graphs["ontology"], [settings.path("ontology"), settings.path("plot_vocabulary")]),
        "base": (graphs["base"], [rdf / "base.ttl"]),
        "enriched": (graphs["enriched"], [rdf / "enriched.ttl"]),
    }


def load(settings: Settings, names: list[str]) -> dict[str, int]:
    client = FusekiClient.from_settings(settings)
    client.ping()
    available = targets(settings)
    counts = {}
    for name in names:
        graph, files = available[name]
        missing = [path for path in files if not path.is_file()]
        if missing:
            raise FileNotFoundError(f"Cannot load {name}: missing {missing}")
        for position, path in enumerate(files):
            logger.info("Uploading %s into <%s>", path.name, graph)
            client.upload(path, graph, replace=position == 0)
        counts[name] = client.count(graph)
        logger.info("<%s> now holds %d triples", graph, counts[name])
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Load KG files into named graphs of the Fuseki dataset.")
    parser.add_argument("targets", nargs="*", choices=["ontology", "base", "enriched"], help="defaults to ontology and base")
    args = parser.parse_args()
    setup_logging()
    load(load_settings(), args.targets or ["ontology", "base"])


if __name__ == "__main__":
    main()

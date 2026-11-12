import argparse
import logging
import re
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from klkg.collection.snapshot import iter_candidate_details, load_manifest, selected_people
from klkg.config import Settings, load_settings, read_json, setup_logging, write_json

logger = logging.getLogger(__name__)

QID_PATTERN = re.compile(r"^Q[1-9]\d*$")
ENTITY_PREFIX = "http://www.wikidata.org/entity/"

FILM_QUERY = "SELECT ?key ?item WHERE { VALUES ?key { %s } ?item wdt:P4947 ?key . }"
PERSON_QUERY = "SELECT ?key ?item WHERE { VALUES ?key { %s } ?item wdt:P4985 ?key . }"
COUNTRY_QUERY = "SELECT ?key ?item WHERE { VALUES ?key { %s } ?item wdt:P297 ?key . }"
LANGUAGE_QUERY = "SELECT ?key ?item WHERE { VALUES ?key { %s } ?item wdt:P218 ?key . }"
NARRATIVE_LOCATION_QUERY = """
SELECT ?item ?location ?locationLabel ?country WHERE {
  VALUES ?item { %s }
  ?item wdt:P840 ?location .
  OPTIONAL { ?location wdt:P17 ?country . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""


class WikidataClient:
    def __init__(self, endpoint: str, contact: str, min_interval: float, max_retries: int, timeout: float):
        self.endpoint = endpoint
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.timeout = timeout
        self.last_request = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/sparql-results+json",
                "User-Agent": f"klkg/0.1 ({contact}) python-requests/{requests.__version__}",
            }
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "WikidataClient":
        wikidata = settings["wikidata"]
        return cls(
            endpoint=wikidata["sparql_endpoint"],
            contact=settings.value("wikidata", "contact", env="WIKIDATA_CONTACT"),
            min_interval=wikidata["min_interval_seconds"],
            max_retries=wikidata["max_retries"],
            timeout=wikidata["timeout_seconds"],
        )

    def select(self, query: str) -> list[dict[str, str]]:
        for attempt in range(self.max_retries + 1):
            wait = self.min_interval - (time.monotonic() - self.last_request)
            if wait > 0:
                time.sleep(wait)
            self.last_request = time.monotonic()
            try:
                response = self.session.post(self.endpoint, data={"query": query}, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as error:
                if attempt == self.max_retries:
                    raise
                self.backoff(attempt, None, str(error))
                continue
            if response.status_code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                self.backoff(attempt, response.headers.get("Retry-After"), f"HTTP {response.status_code}")
                continue
            response.raise_for_status()
            return [
                {name: cell["value"] for name, cell in binding.items()}
                for binding in response.json()["results"]["bindings"]
            ]
        raise RuntimeError("Unreachable retry state")

    @staticmethod
    def backoff(attempt: int, retry_after: str | None, reason: str) -> None:
        delay = float(retry_after) if retry_after and retry_after.isdigit() else min(5 * 2 ** attempt, 120)
        logger.warning("Wikidata retry in %.0fs after %s", delay, reason)
        time.sleep(delay)


def qid_of(uri: str) -> str:
    return uri.removeprefix(ENTITY_PREFIX)


def qid_number(qid: str) -> int:
    return int(qid[1:])


def batched(items: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def string_values(keys: list[str]) -> str:
    return " ".join(f'"{key}"' for key in keys)


def entity_values(qids: list[str]) -> str:
    return " ".join(f"wd:{qid}" for qid in qids)


def resolve_keys(
    client: WikidataClient,
    keys: Iterable[str],
    template: str,
    batch_size: int,
    render: Callable[[list[str]], str] = string_values,
) -> dict[str, list[str]]:
    ordered = sorted(set(keys))
    found: dict[str, set[str]] = defaultdict(set)
    for number, batch in enumerate(batched(ordered, batch_size), start=1):
        for row in client.select(template % render(batch)):
            found[row["key"]].add(qid_of(row["item"]))
        logger.info("Resolved batch %d (%d keys so far matched)", number, len(found))
    return {key: sorted(qids, key=qid_number) for key, qids in found.items()}


def pick_unique(matches: dict[str, list[str]]) -> tuple[dict[str, str], dict[str, list[str]]]:
    chosen = {key: qids[0] for key, qids in matches.items()}
    ambiguous = {key: qids for key, qids in matches.items() if len(qids) > 1}
    return chosen, ambiguous


def align_films(
    tmdb_qids: dict[str, str | None], wikidata_matches: dict[str, list[str]]
) -> tuple[dict[str, dict[str, str]], list[dict[str, Any]]]:
    aligned: dict[str, dict[str, str]] = {}
    conflicts: list[dict[str, Any]] = []
    for tmdb_id, external in tmdb_qids.items():
        external = external if external and QID_PATTERN.match(external) else None
        matches = wikidata_matches.get(tmdb_id, [])
        if len(matches) == 1:
            aligned[tmdb_id] = {"qid": matches[0], "source": "wikidata_p4947"}
        elif matches:
            qid = external if external in matches else matches[0]
            aligned[tmdb_id] = {"qid": qid, "source": "wikidata_p4947_ambiguous"}
        elif external:
            aligned[tmdb_id] = {"qid": external, "source": "tmdb_external_ids"}
        if external and matches and external not in matches:
            conflicts.append({"tmdb_id": tmdb_id, "tmdb_external_ids": external, "wikidata_p4947": matches})
    return aligned, conflicts


def collect_keys(raw_tmdb: Path, dataset: dict[str, Any]) -> dict[str, Any]:
    films: dict[str, str | None] = {}
    persons: set[str] = set()
    countries: set[str] = set()
    languages: set[str] = set()
    for details in iter_candidate_details(raw_tmdb):
        films[str(details["id"])] = (details.get("external_ids") or {}).get("wikidata_id")
        persons.update(str(member["id"]) for member in selected_people(details, dataset))
        countries.update(country["iso_3166_1"] for country in details.get("production_countries", []))
        languages.update(language["iso_639_1"] for language in details.get("spoken_languages", []))
        if details.get("original_language"):
            languages.add(details["original_language"])
    return {"films": films, "persons": persons, "countries": countries, "languages": languages}


def narrative_locations(client: WikidataClient, film_qids: dict[str, str], batch_size: int) -> dict[str, list[dict[str, str]]]:
    by_qid: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    for batch in batched(sorted(set(film_qids.values()), key=qid_number), batch_size):
        for row in client.select(NARRATIVE_LOCATION_QUERY % entity_values(batch)):
            location = qid_of(row["location"])
            # "unknown value" statements come back as .well-known/genid IRIs, not entities
            if not QID_PATTERN.match(location):
                continue
            entry = by_qid[qid_of(row["item"])].setdefault(
                location, {"qid": location, "label": row.get("locationLabel", location), "countries": []}
            )
            country = qid_of(row["country"]) if "country" in row else None
            if country and QID_PATTERN.match(country) and country not in entry["countries"]:
                entry["countries"].append(country)
    result = {}
    for tmdb_id, qid in film_qids.items():
        if qid in by_qid:
            result[tmdb_id] = sorted(by_qid[qid].values(), key=lambda entry: qid_number(entry["qid"]))
    return result


def cached(path: Path, refresh: bool, snapshot: str, compute: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    if path.is_file() and not refresh:
        payload = read_json(path)
        if payload.get("snapshot_utc") == snapshot:
            logger.info("Using cached %s", path.name)
            return payload
        logger.info("%s belongs to another snapshot; querying again", path.name)
    payload = {"snapshot_utc": snapshot, **compute()}
    write_json(path, payload)
    return payload


def run(refresh: bool) -> None:
    settings = load_settings()
    raw_tmdb = settings.path("raw_tmdb")
    out = settings.path("raw_wikidata")
    batch_size = settings["wikidata"]["batch_size"]
    client = WikidataClient.from_settings(settings)
    snapshot = load_manifest(raw_tmdb)["snapshot_utc"]
    keys = collect_keys(raw_tmdb, settings["dataset"])

    def films() -> dict[str, Any]:
        matches = resolve_keys(client, keys["films"].keys(), FILM_QUERY, batch_size)
        aligned, conflicts = align_films(keys["films"], matches)
        return {"aligned": aligned, "conflicts": conflicts}

    def mapping_for(values: set[str], template: str) -> Callable[[], dict[str, Any]]:
        def compute() -> dict[str, Any]:
            chosen, ambiguous = pick_unique(resolve_keys(client, values, template, batch_size))
            return {"aligned": chosen, "ambiguous": ambiguous}

        return compute

    film_result = cached(out / "films.json", refresh, snapshot, films)
    person_result = cached(out / "persons.json", refresh, snapshot, mapping_for(keys["persons"], PERSON_QUERY))
    country_result = cached(out / "countries.json", refresh, snapshot, mapping_for(keys["countries"], COUNTRY_QUERY))
    language_result = cached(out / "languages.json", refresh, snapshot, mapping_for(keys["languages"], LANGUAGE_QUERY))
    film_qids = {tmdb_id: entry["qid"] for tmdb_id, entry in film_result["aligned"].items()}
    locations = cached(
        out / "narrative_locations.json", refresh, snapshot, lambda: {"by_film": narrative_locations(client, film_qids, batch_size)}
    )["by_film"]

    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "snapshot_utc": snapshot,
        "endpoint": settings["wikidata"]["sparql_endpoint"],
        "films": {"total": len(keys["films"]), "aligned": len(film_result["aligned"]), "conflicts": len(film_result["conflicts"])},
        "persons": {"total": len(keys["persons"]), "aligned": len(person_result["aligned"]), "ambiguous": len(person_result["ambiguous"])},
        "countries": {"total": len(keys["countries"]), "aligned": len(country_result["aligned"])},
        "languages": {"total": len(keys["languages"]), "aligned": len(language_result["aligned"])},
        "films_with_narrative_location": len(locations),
    }
    write_json(out / "alignment_report.json", report)
    logger.info("Alignment report: %s", report)


def main() -> None:
    parser = argparse.ArgumentParser(description="Align the TMDB snapshot with Wikidata in batches.")
    parser.add_argument("--refresh", action="store_true", help="query Wikidata again even if cached results exist")
    args = parser.parse_args()
    setup_logging()
    run(args.refresh)


if __name__ == "__main__":
    main()

import argparse
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any

from klkg.collection.snapshot import (
    CONFIGURATION_DIR,
    load_manifest,
    select_cast,
    select_directors,
    select_movies,
    select_writers,
    selected_people,
)
from klkg.config import load_settings, read_json, setup_logging, write_json

logger = logging.getLogger(__name__)

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
GENDERS = {1: "female", 2: "male", 3: "non-binary"}
NO_LANGUAGE = "xx"


def clean_text(value: Any) -> str | None:
    text = (value or "").strip() if isinstance(value, str) else None
    return text or None


def positive(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and value > 0 else None


def clean_date(value: Any) -> str | None:
    return value if isinstance(value, str) and DATE_PATTERN.match(value) else None


def load_optional(path: Path) -> dict[str, Any]:
    if path.is_file():
        return read_json(path)
    logger.warning("%s not found; continuing without it", path)
    return {}


def credit_record(movie_id: int, member: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"movie_id": movie_id, "person_id": member["id"], "credit_id": member["credit_id"], **extra}


def movie_record(details: dict[str, Any], dataset: dict[str, Any], film_qids: dict[str, dict[str, str]]) -> dict[str, Any]:
    movie_id = details["id"]
    external = details.get("external_ids") or {}
    aligned = film_qids.get(str(movie_id))
    original_language = clean_text(details.get("original_language"))
    collection = details.get("belongs_to_collection")
    has_votes = (details.get("vote_count") or 0) > 0
    return {
        "tmdb_id": movie_id,
        "title": clean_text(details.get("title")),
        "original_title": clean_text(details.get("original_title")),
        "overview": clean_text(details.get("overview")),
        "release_date": clean_date(details.get("release_date")),
        "runtime": positive(details.get("runtime")),
        "budget": positive(details.get("budget")),
        "revenue": positive(details.get("revenue")),
        "vote_average": details.get("vote_average") if has_votes else None,
        "vote_count": details.get("vote_count"),
        "original_language": None if original_language == NO_LANGUAGE else original_language,
        "imdb_id": clean_text(details.get("imdb_id") or external.get("imdb_id")),
        "wikidata_id": aligned["qid"] if aligned else clean_text(external.get("wikidata_id")),
        "genres": [{"movie_id": movie_id, "id": genre["id"], "name": genre["name"]} for genre in details.get("genres", [])],
        "keywords": [
            {"movie_id": movie_id, "id": keyword["id"], "name": keyword["name"]}
            for keyword in (details.get("keywords") or {}).get("keywords", [])
        ],
        "collections": [{"movie_id": movie_id, "id": collection["id"], "name": collection["name"]}] if collection else [],
        "production_companies": [
            {"movie_id": movie_id, "id": company["id"], "name": company["name"]}
            for company in details.get("production_companies", [])
        ],
        "production_countries": [
            {"movie_id": movie_id, "iso_3166_1": country["iso_3166_1"]} for country in details.get("production_countries", [])
        ],
        "spoken_languages": [
            {"movie_id": movie_id, "iso_639_1": language["iso_639_1"]}
            for language in details.get("spoken_languages", [])
            if language["iso_639_1"] != NO_LANGUAGE
        ],
        "cast": [
            credit_record(movie_id, member, character=clean_text(member.get("character")), order=member.get("order"))
            for member in select_cast(details, dataset["max_cast_members"])
        ],
        "directors": [
            credit_record(movie_id, member, job=member["job"], department=member["department"])
            for member in select_directors(details, dataset)
        ],
        "writers": [
            credit_record(movie_id, member, job=member["job"], department=member["department"])
            for member in select_writers(details, dataset)
        ],
    }


def person_records(movies: list[dict[str, Any]], raw_movies: list[dict[str, Any]], dataset: dict[str, Any], person_qids: dict[str, str]) -> list[dict[str, Any]]:
    people: dict[int, dict[str, Any]] = {}
    for details in raw_movies:
        for member in selected_people(details, dataset):
            if member["id"] not in people:
                people[member["id"]] = {
                    "tmdb_id": member["id"],
                    "name": clean_text(member.get("name")),
                    "gender": GENDERS.get(member.get("gender")),
                    "wikidata_id": person_qids.get(str(member["id"])),
                }
    referenced = {credit["person_id"] for movie in movies for key in ("cast", "directors", "writers") for credit in movie[key]}
    return [people[person_id] for person_id in sorted(referenced)]


def code_records(codes: set[str], names: dict[str, str], qids: dict[str, str]) -> list[dict[str, Any]]:
    return [{"iso": code, "name": names.get(code, code), "wikidata_id": qids.get(code)} for code in sorted(codes)]


def keyword_frequencies(movies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter((keyword["id"], keyword["name"]) for movie in movies for keyword in movie["keywords"])
    return [{"id": key[0], "name": key[1], "films": count} for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0][1]))]


def prepare(raw_tmdb: Path, raw_wikidata: Path, interim: Path, dataset: dict[str, Any], require_overview: bool) -> dict[str, Any]:
    raw_movies = select_movies(raw_tmdb, dataset["final_size"], require_overview)
    films = load_optional(raw_wikidata / "films.json").get("aligned", {})
    persons = load_optional(raw_wikidata / "persons.json").get("aligned", {})
    countries = load_optional(raw_wikidata / "countries.json").get("aligned", {})
    languages = load_optional(raw_wikidata / "languages.json").get("aligned", {})
    movies = [movie_record(details, dataset, films) for details in raw_movies]

    country_names = {entry["iso_3166_1"]: entry["english_name"] for entry in read_json(raw_tmdb / CONFIGURATION_DIR / "countries.json")}
    language_names = {entry["iso_639_1"]: entry["english_name"] for entry in read_json(raw_tmdb / CONFIGURATION_DIR / "languages.json")}
    used_countries = {country["iso_3166_1"] for movie in movies for country in movie["production_countries"]}
    used_languages = {language["iso_639_1"] for movie in movies for language in movie["spoken_languages"]}
    used_languages.update(movie["original_language"] for movie in movies if movie["original_language"])

    write_json(interim / "movies.json", movies)
    write_json(interim / "persons.json", person_records(movies, raw_movies, dataset, persons))
    write_json(interim / "countries.json", code_records(used_countries, country_names, countries))
    write_json(interim / "languages.json", code_records(used_languages, language_names, languages))
    write_json(interim / "keyword_frequencies.json", keyword_frequencies(movies))
    summary = {
        "snapshot_utc": load_manifest(raw_tmdb)["snapshot_utc"],
        "requested_size": dataset["final_size"],
        "movies": len(movies),
        "movies_with_wikidata": sum(1 for movie in movies if movie["wikidata_id"]),
        "movie_ids": [movie["tmdb_id"] for movie in movies],
    }
    write_json(interim / "selection.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Turn the raw snapshot into mapping-ready nested JSON.")
    parser.add_argument("--final-size", type=int, default=None, help="override dataset.final_size")
    args = parser.parse_args()
    setup_logging()
    settings = load_settings()
    dataset = dict(settings["dataset"])
    if args.final_size is not None:
        dataset["final_size"] = args.final_size
    summary = prepare(
        settings.path("raw_tmdb"),
        settings.path("raw_wikidata"),
        settings.path("interim"),
        dataset,
        settings["collection"]["require_overview"],
    )
    logger.info("Prepared %d movies (%d with Wikidata IDs)", summary["movies"], summary["movies_with_wikidata"])


if __name__ == "__main__":
    main()

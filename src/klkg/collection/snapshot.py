from collections.abc import Iterator
from pathlib import Path
from typing import Any

from klkg.config import read_json

MANIFEST = "manifest.json"
MOVIES_DIR = "movies"
DISCOVER_DIR = "discover"
CONFIGURATION_DIR = "configuration"


def manifest_path(raw_dir: Path) -> Path:
    return raw_dir / MANIFEST


def movie_path(raw_dir: Path, movie_id: int) -> Path:
    return raw_dir / MOVIES_DIR / f"{movie_id}.json"


def load_manifest(raw_dir: Path) -> dict[str, Any]:
    return read_json(manifest_path(raw_dir))


def has_overview(details: dict[str, Any]) -> bool:
    return bool((details.get("overview") or "").strip())


def iter_candidate_details(raw_dir: Path) -> Iterator[dict[str, Any]]:
    for candidate in load_manifest(raw_dir)["candidates"]:
        path = movie_path(raw_dir, candidate["id"])
        if path.is_file():
            yield read_json(path)


def select_movies(raw_dir: Path, final_size: int | None, require_overview: bool = True) -> list[dict[str, Any]]:
    selected = []
    for details in iter_candidate_details(raw_dir):
        if require_overview and not has_overview(details):
            continue
        selected.append(details)
        if final_size is not None and len(selected) >= final_size:
            break
    return selected


def select_cast(details: dict[str, Any], max_cast_members: int) -> list[dict[str, Any]]:
    cast = details.get("credits", {}).get("cast", [])
    return sorted(cast, key=lambda member: (member.get("order", 10**6), member["credit_id"]))[:max_cast_members]


def select_crew(details: dict[str, Any], jobs: list[str] | None = None, departments: list[str] | None = None) -> list[dict[str, Any]]:
    crew = details.get("credits", {}).get("crew", [])
    selected = [
        member
        for member in crew
        if (jobs and member.get("job") in jobs) or (departments and member.get("department") in departments)
    ]
    return sorted(selected, key=lambda member: (member["id"], member["credit_id"]))


def select_directors(details: dict[str, Any], dataset_settings: dict[str, Any]) -> list[dict[str, Any]]:
    return select_crew(details, jobs=dataset_settings["director_jobs"])


def select_writers(details: dict[str, Any], dataset_settings: dict[str, Any]) -> list[dict[str, Any]]:
    return select_crew(details, departments=dataset_settings["writer_departments"])


def selected_people(details: dict[str, Any], dataset_settings: dict[str, Any]) -> list[dict[str, Any]]:
    return (
        select_cast(details, dataset_settings["max_cast_members"])
        + select_directors(details, dataset_settings)
        + select_writers(details, dataset_settings)
    )

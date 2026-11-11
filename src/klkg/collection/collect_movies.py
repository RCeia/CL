import argparse
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from klkg.collection.snapshot import (
    CONFIGURATION_DIR,
    DISCOVER_DIR,
    load_manifest,
    manifest_path,
    movie_path,
)
from klkg.collection.tmdb_client import TmdbClient
from klkg.config import load_settings, require_env, setup_logging, write_json

logger = logging.getLogger(__name__)

MAX_DISCOVER_PAGES = 500


def discover_params(collection: dict[str, Any]) -> dict[str, Any]:
    params = {
        "sort_by": collection["sort_by"],
        "include_adult": str(collection["include_adult"]).lower(),
        "include_video": str(collection["include_video"]).lower(),
        "primary_release_date.gte": f"{collection['release_year_min']}-01-01",
        "primary_release_date.lte": f"{collection['release_year_max']}-12-31",
    }
    if collection.get("original_language"):
        params["with_original_language"] = collection["original_language"]
    if collection.get("min_vote_count") is not None:
        params["vote_count.gte"] = collection["min_vote_count"]
    return params


def collect_candidates(client: TmdbClient, collection: dict[str, Any], raw_dir: Path, pool_size: int) -> list[dict[str, Any]]:
    params = discover_params(collection)
    candidates: dict[int, dict[str, Any]] = {}
    page, total_pages = 1, 1
    while len(candidates) < pool_size and page <= min(total_pages, MAX_DISCOVER_PAGES):
        payload = client.get("discover/movie", {**params, "page": page})
        if payload is None:
            break
        write_json(raw_dir / DISCOVER_DIR / f"page_{page:04d}.json", payload)
        total_pages = payload.get("total_pages", page)
        for result in payload.get("results", []):
            if result["id"] in candidates:
                continue
            if collection["require_overview"] and not (result.get("overview") or "").strip():
                continue
            candidates[result["id"]] = {
                "id": result["id"],
                "title": result.get("title"),
                "release_date": result.get("release_date"),
                "vote_count": result.get("vote_count", 0),
            }
        logger.info("Discover page %d/%d: %d candidates", page, total_pages, len(candidates))
        page += 1
    ordered = sorted(candidates.values(), key=lambda item: (-item["vote_count"], item["id"]))
    return ordered[:pool_size]


def fetch_details(client: TmdbClient, raw_dir: Path, movie_ids: list[int], append: list[str], workers: int) -> dict[str, int]:
    pending = [movie_id for movie_id in movie_ids if not movie_path(raw_dir, movie_id).is_file()]
    counts = {"already_present": len(movie_ids) - len(pending), "fetched": 0, "not_found": 0}
    params = {"append_to_response": ",".join(append)}

    def fetch(movie_id: int) -> bool:
        details = client.get(f"movie/{movie_id}", params)
        if details is None:
            return False
        write_json(movie_path(raw_dir, movie_id), details)
        return True

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, movie_id): movie_id for movie_id in pending}
        for done, future in enumerate(as_completed(futures), start=1):
            if future.result():
                counts["fetched"] += 1
            else:
                counts["not_found"] += 1
                logger.warning("Movie %d not found", futures[future])
            if done % 100 == 0 or done == len(pending):
                logger.info("Details %d/%d", done, len(pending))
    return counts


def fetch_configuration(client: TmdbClient, raw_dir: Path) -> None:
    for name in ("countries", "languages"):
        target = raw_dir / CONFIGURATION_DIR / f"{name}.json"
        if not target.is_file():
            write_json(target, client.get(f"configuration/{name}"))


def run(refresh: bool, pool_size: int | None) -> None:
    settings = load_settings()
    raw_dir = settings.path("raw_tmdb")
    tmdb = settings["tmdb"]
    collection = settings["collection"]
    size = pool_size or collection["candidate_pool_size"]
    client = TmdbClient.from_settings(tmdb, require_env("TMDB_API_KEY"))

    if manifest_path(raw_dir).is_file() and not refresh:
        manifest = load_manifest(raw_dir)
        if manifest["candidate_pool_size"] != size:
            raise SystemExit(
                f"The existing snapshot has a pool of {manifest['candidate_pool_size']} candidates, not {size}; "
                "run with --refresh to rebuild it (downloaded details are reused)"
            )
        logger.info("Resuming snapshot from %s with %d candidates", manifest["snapshot_utc"], len(manifest["candidates"]))
    else:
        candidates = collect_candidates(client, collection, raw_dir, size)
        manifest = {
            "snapshot_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tmdb_base_url": tmdb["base_url"],
            "language": tmdb["language"],
            "append_to_response": tmdb["append_to_response"],
            "discover_params": discover_params(collection),
            "require_overview": collection["require_overview"],
            "candidate_pool_size": size,
            "candidates": candidates,
        }
        write_json(manifest_path(raw_dir), manifest)

    fetch_configuration(client, raw_dir)
    counts = fetch_details(
        client,
        raw_dir,
        [candidate["id"] for candidate in manifest["candidates"]],
        tmdb["append_to_response"],
        tmdb["workers"],
    )
    manifest["details_counts"] = counts
    manifest["details_completed_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    write_json(manifest_path(raw_dir), manifest)
    logger.info("Snapshot ready: %s", counts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect a reproducible TMDB snapshot of candidate movies.")
    parser.add_argument("--refresh", action="store_true", help="rebuild the candidate list instead of resuming")
    parser.add_argument("--pool-size", type=int, default=None, help="override collection.candidate_pool_size")
    args = parser.parse_args()
    setup_logging()
    run(args.refresh, args.pool_size)


if __name__ == "__main__":
    main()

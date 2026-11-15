import logging
import threading
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class RateLimiter:
    def __init__(self, requests_per_second: float):
        self.interval = 1.0 / requests_per_second
        self.lock = threading.Lock()
        self.next_slot = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next_slot)
            self.next_slot = slot + self.interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)


def is_v4_token(credential: str) -> bool:
    return credential.count(".") == 2 and credential.startswith("eyJ")


class TmdbClient:
    def __init__(
        self,
        credential: str,
        base_url: str,
        language: str,
        requests_per_second: float,
        max_retries: int,
        timeout_seconds: float,
    ):
        self.base_url = base_url.rstrip("/")
        self.language = language
        self.max_retries = max_retries
        self.timeout = timeout_seconds
        self.limiter = RateLimiter(requests_per_second)
        self.local = threading.local()
        self.headers = {"Accept": "application/json"}
        self.auth_params: dict[str, str] = {}
        if is_v4_token(credential):
            self.headers["Authorization"] = f"Bearer {credential}"
        else:
            self.auth_params["api_key"] = credential

    @classmethod
    def from_settings(cls, settings: dict[str, Any], credential: str) -> "TmdbClient":
        return cls(
            credential=credential,
            base_url=settings["base_url"],
            language=settings["language"],
            requests_per_second=settings["requests_per_second"],
            max_retries=settings["max_retries"],
            timeout_seconds=settings["timeout_seconds"],
        )

    def session(self) -> requests.Session:
        if not hasattr(self.local, "session"):
            session = requests.Session()
            session.headers.update(self.headers)
            self.local.session = session
        return self.local.session

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        query = {"language": self.language, **self.auth_params, **(params or {})}
        url = f"{self.base_url}/{path.lstrip('/')}"
        for attempt in range(self.max_retries + 1):
            self.limiter.wait()
            try:
                response = self.session().get(url, params=query, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as error:
                if attempt == self.max_retries:
                    raise
                self.backoff(attempt, None, f"{path}: {error}")
                continue
            if response.status_code == 404:
                return None
            if response.status_code in RETRYABLE_STATUS and attempt < self.max_retries:
                self.backoff(attempt, response.headers.get("Retry-After"), f"{path}: HTTP {response.status_code}")
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError(f"Unreachable retry state for {path}")

    @staticmethod
    def backoff(attempt: int, retry_after: str | None, reason: str) -> None:
        delay = float(retry_after) if retry_after and retry_after.isdigit() else min(2 ** attempt, 60)
        logger.warning("Retrying in %.1fs after %s", delay, reason)
        time.sleep(delay)

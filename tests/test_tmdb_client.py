import pytest

from klkg.collection import tmdb_client
from klkg.collection.tmdb_client import TmdbClient, is_v4_token

V4 = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJ4In0.c2lnbmF0dXJl"
V3 = "0123456789abcdef0123456789abcdef"


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self.payload = payload
        self.headers = headers or {}

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append((url, params))
        return self.responses.pop(0)


def make(credential, responses):
    client = TmdbClient(credential, "https://api.example/3", "en-US", 1000, 3, 5)
    client.local.session = FakeSession(responses)
    return client


def test_token_detection():
    assert is_v4_token(V4)
    assert not is_v4_token(V3)
    assert make(V4, []).headers["Authorization"] == f"Bearer {V4}"
    assert make(V3, []).auth_params == {"api_key": V3}


def test_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(tmdb_client.time, "sleep", lambda seconds: None)
    client = make(V3, [FakeResponse(429, headers={"Retry-After": "1"}), FakeResponse(502), FakeResponse(200, {"id": 1})])
    assert client.get("movie/1") == {"id": 1}
    url, params = client.local.session.calls[-1]
    assert url == "https://api.example/3/movie/1"
    assert params["language"] == "en-US" and params["api_key"] == V3


def test_not_found_and_client_errors(monkeypatch):
    monkeypatch.setattr(tmdb_client.time, "sleep", lambda seconds: None)
    assert make(V3, [FakeResponse(404)]).get("movie/2") is None
    with pytest.raises(RuntimeError):
        make(V3, [FakeResponse(401)]).get("movie/3")

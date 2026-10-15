import asyncio

import pytest

from src import esports_api
from src.esports_api import EsportsApiClient, EsportsAuthError


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise esports_api.requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def make_client(api_key="key"):
    return EsportsApiClient(api_key=api_key, log=lambda _msg: None)


def test_missing_key_raises_auth_error_without_request(monkeypatch):
    calls = []
    monkeypatch.setattr(esports_api.requests, "get", lambda *a, **k: calls.append(1))
    with pytest.raises(EsportsAuthError):
        make_client(api_key="")._get_json("getSchedule")
    assert calls == []


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_key_raises_auth_error(monkeypatch, status):
    monkeypatch.setattr(esports_api.requests, "get", lambda *a, **k: FakeResponse(status))
    with pytest.raises(EsportsAuthError, match=str(status)):
        make_client()._get_json("getSchedule")


def test_server_error_is_not_auth_error(monkeypatch):
    monkeypatch.setattr(esports_api.requests, "get", lambda *a, **k: FakeResponse(503))
    with pytest.raises(esports_api.requests.HTTPError):
        make_client()._get_json("getSchedule")


def test_league_lookup_propagates_auth_error(monkeypatch):
    monkeypatch.setattr(esports_api.requests, "get", lambda *a, **k: FakeResponse(403))
    with pytest.raises(EsportsAuthError):
        asyncio.run(make_client().resolve_league_ids(["worlds"]))


def test_league_lookup_falls_back_on_outage(monkeypatch):
    monkeypatch.setattr(esports_api.requests, "get", lambda *a, **k: FakeResponse(503))
    resolved = asyncio.run(make_client().resolve_league_ids(["worlds"]))
    assert resolved == {"worlds": esports_api.KNOWN_LEAGUE_IDS["worlds"]}

import asyncio

import pytest

from src import patch_notes_api
from src.patch_notes_api import PatchNotesClient
from src.patch_notes_logic import PatchNotesParseError
from tests.test_patch_notes_logic import ARTICLE_HTML, make_item, make_listing_html


class FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise patch_notes_api.requests.HTTPError(f"HTTP {self.status_code}")


def make_client(locale="en-US"):
    return PatchNotesClient(locale=locale, log=lambda _msg: None)


def test_listing_url_uses_lowercase_locale():
    assert make_client("EN-GB").listing_url == "https://www.leagueoflegends.com/en-gb/news/tags/patch-notes/"


def test_fetch_articles_parses_listing(monkeypatch):
    calls = []
    html = make_listing_html([make_item("Patch 26.21 Notes", "patch-26-21-notes", "2026-10-20T18:00:00Z")])

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(200, html)

    monkeypatch.setattr(patch_notes_api.requests, "get", fake_get)
    articles = asyncio.run(make_client().fetch_articles())

    assert [a["title"] for a in articles] == ["Patch 26.21 Notes"]
    assert calls[0][0].endswith("/en-us/news/tags/patch-notes/")
    assert "User-Agent" in calls[0][1]["headers"]
    assert calls[0][1]["timeout"]


def test_fetch_articles_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(patch_notes_api.requests, "get", lambda *a, **k: FakeResponse(503))
    with pytest.raises(patch_notes_api.requests.HTTPError):
        asyncio.run(make_client().fetch_articles())


def test_fetch_articles_raises_on_redesigned_page(monkeypatch):
    monkeypatch.setattr(patch_notes_api.requests, "get", lambda *a, **k: FakeResponse(200, "<html></html>"))
    with pytest.raises(PatchNotesParseError):
        asyncio.run(make_client().fetch_articles())


def test_fetch_article_sections_parses_article(monkeypatch):
    monkeypatch.setattr(patch_notes_api.requests, "get", lambda *a, **k: FakeResponse(200, ARTICLE_HTML))
    sections = asyncio.run(make_client().fetch_article_sections("https://x/"))
    assert sections[0] == ("Champions", ["Azir", "Nunu & Willump"])


def test_fetch_article_sections_returns_empty_on_failure(monkeypatch):
    def boom(*_a, **_k):
        raise patch_notes_api.requests.ConnectionError("offline")

    monkeypatch.setattr(patch_notes_api.requests, "get", boom)
    assert asyncio.run(make_client().fetch_article_sections("https://x/")) == []

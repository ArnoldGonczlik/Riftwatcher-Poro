import asyncio
import json

import pytest
import requests
import discord
from types import SimpleNamespace

from src import champion_icons as icons


class Emoji:
    def __init__(self, name, emoji_id=123):
        self.name = name
        self.id = emoji_id

    def __str__(self):
        return f"<:{self.name}:{self.id}>"


class Client:
    def __init__(self, emojis=()):
        self.emojis = list(emojis)
        self.fetches = 0
        self.uploads = []

    async def fetch_application_emojis(self):
        self.fetches += 1
        return list(self.emojis)

    async def create_application_emoji(self, *, name, image):
        self.uploads.append((name, image))
        emoji = Emoji(name)
        self.emojis.append(emoji)
        return emoji


@pytest.mark.parametrize("raw,expected", [(None, False), ("0", False), ("1", True), ("ON", True), ("garbage", False)])
def test_setting_defaults_off(raw, expected):
    assert icons.champion_icons_enabled(raw) is expected


def test_reuses_application_emoji_after_restart_without_download(monkeypatch):
    monkeypatch.setattr(icons, "_load_catalog", lambda: pytest.fail("Unexpected CDN request"))
    client = Client([Emoji("rw_champ_90")])
    for _restart in range(2):
        service = icons.ChampionIcons(client, lambda _m: None)
        assert asyncio.run(service.get(90)) == "<:rw_champ_90:123>"
    assert client.uploads == []


def test_concurrent_requests_upload_once_and_cache_catalog(monkeypatch):
    calls = []
    monkeypatch.setattr(icons, "_load_catalog", lambda: calls.append("catalog") or {90: "malz", 103: "ahri"})
    monkeypatch.setattr(icons, "_download", lambda url, limit: calls.append(url) or b"png")
    client = Client()
    service = icons.ChampionIcons(client, lambda _m: None)

    async def run():
        results = await asyncio.gather(service.get(90), service.get(90))
        assert results == ["<:rw_champ_90:123>"] * 2
        assert await service.get(103) == "<:rw_champ_103:123>"

    asyncio.run(run())
    assert client.fetches == 1
    assert client.uploads == [("rw_champ_90", b"png"), ("rw_champ_103", b"png")]
    assert calls == ["catalog", "malz", "ahri"]


def test_failures_fall_back_and_back_off_then_recover(monkeypatch):
    clock = [1000]
    monkeypatch.setattr(icons.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(icons, "_load_catalog", lambda: {90: "malz"})
    calls = []

    def failing_download(url, limit):
        calls.append(url)
        raise requests.Timeout("offline")

    monkeypatch.setattr(icons, "_download", failing_download)
    client = Client()
    service = icons.ChampionIcons(client, lambda _m: None)
    assert asyncio.run(service.get(90)) is None
    assert asyncio.run(service.get(90)) is None
    assert calls == ["malz"]
    clock[0] += icons.RETRY_SECONDS + 1
    # Simulate an upload that completed remotely despite a prior timeout.
    client.emojis = [Emoji("rw_champ_90")]
    assert asyncio.run(service.get(90)) == "<:rw_champ_90:123>"
    assert calls == ["malz"]


def test_deleted_emoji_is_refetched_and_recreated(monkeypatch):
    clock = [1000]
    monkeypatch.setattr(icons.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(icons, "_load_catalog", lambda: {90: "malz"})
    monkeypatch.setattr(icons, "_download", lambda url, limit: b"png")
    client = Client([Emoji("rw_champ_90")])
    service = icons.ChampionIcons(client, lambda _m: None)
    assert asyncio.run(service.get(90))
    client.emojis.clear()
    clock[0] += icons.EMOJI_CACHE_SECONDS + 1
    assert asyncio.run(service.get(90))
    assert len(client.uploads) == 1


@pytest.mark.parametrize("champion_id", [None, "bad", -1, 0])
def test_missing_ids_fall_back_without_requests(champion_id):
    client = Client()
    assert asyncio.run(icons.ChampionIcons(client, lambda _m: None).get(champion_id)) is None
    assert client.fetches == 0


def test_catalog_uses_numeric_ids_and_official_image_filename(monkeypatch):
    urls = []

    def download(url, limit):
        urls.append(url)
        if url.endswith("versions.json"):
            return b'["16.21.1"]'
        return json.dumps({"data": {
            "Fiddlesticks": {"key": "9", "image": {"full": "Fiddlesticks.png"}},
            "Bad": {"key": "10", "image": {"full": "../elsewhere.png"}},
        }}).encode()

    monkeypatch.setattr(icons, "_download", download)
    assert icons._load_catalog() == {9: f"{icons.CDN}/cdn/16.21.1/img/champion/Fiddlesticks.png"}
    assert urls == [f"{icons.CDN}/api/versions.json", f"{icons.CDN}/cdn/16.21.1/data/en_US/champion.json"]


def test_download_enforces_size_limit(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def raise_for_status(self):
            pass

        def iter_content(self, _size):
            yield b"1234"
            yield b"5678"

    monkeypatch.setattr(icons.requests, "get", lambda *args, **kwargs: Response())
    with pytest.raises(ValueError, match="size limit"):
        icons._download("https://example.test", 5)


def test_discord_permission_failure_uses_text_and_backs_off():
    client = Client()

    async def forbidden():
        client.fetches += 1
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "No access")

    client.fetch_application_emojis = forbidden
    service = icons.ChampionIcons(client, lambda _m: None)
    assert asyncio.run(service.get(90)) is None
    assert asyncio.run(service.get(103)) is None
    assert client.fetches == 1


def test_timeout_falls_back_without_consuming_recap(monkeypatch):
    async def timeout(coro, **_kwargs):
        coro.close()
        raise TimeoutError

    monkeypatch.setattr(icons.asyncio, "wait_for", timeout)
    service = icons.ChampionIcons(Client(), lambda _m: None)
    assert asyncio.run(service.get(90)) is None
    assert service._retry_at > 0

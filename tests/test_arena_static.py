from types import SimpleNamespace

import requests

from src import arena_static


def test_load_arena_display_names_parses_augments_and_items(monkeypatch):
    arena_static.reset_arena_display_name_cache()

    def fake_fetch_json(url):
        if url == arena_static.COMMUNITY_DRAGON_ARENA_URL:
            return [
                {"id": 93, "nameTRA": "Warmup Routine"},
                {"id": 323, "nameTRA": "Cerberus"},
            ]
        if url == arena_static.DATA_DRAGON_VERSIONS_URL:
            return ["16.12.1"]
        if url == arena_static.DATA_DRAGON_ITEM_URL_TEMPLATE.format(version="16.12.1"):
            return {"data": {"3157": {"name": "Zhonya's Hourglass"}, "3089": {"name": "Rabadon's Deathcap"}}}
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(arena_static, "_fetch_json", fake_fetch_json)

    names = arena_static.load_arena_display_names()

    assert names["augment_names"] == {
        "93": "Warmup Routine", 
        "323": "Cerberus",
    }
    assert names["item_names"] == {"3157": "Zhonya's Hourglass", "3089": "Rabadon's Deathcap"}

    arena_static.reset_arena_display_name_cache()


def test_load_arena_display_names_falls_back_to_empty_maps(monkeypatch):
    arena_static.reset_arena_display_name_cache()

    def fake_fetch_json(_url):
        raise requests.RequestException("temporary failure")

    monkeypatch.setattr(arena_static, "_fetch_json", fake_fetch_json)

    names = arena_static.load_arena_display_names()

    assert names == {
        "augment_names": {}, 
        "item_names": {}
    }

    arena_static.reset_arena_display_name_cache()


def _fake_static_data(fail_urls, calls):
    def fake_fetch_json(url):
        calls.append(url)
        if url in fail_urls:
            raise requests.RequestException("temporary failure")
        if url == arena_static.COMMUNITY_DRAGON_ARENA_URL:
            return [{"id": 93, "nameTRA": "Warmup Routine"}]
        if url == arena_static.DATA_DRAGON_VERSIONS_URL:
            return ["16.12.1"]
        if url == arena_static.DATA_DRAGON_ITEM_URL_TEMPLATE.format(version="16.12.1"):
            return {"data": {"3157": {"name": "Zhonya's Hourglass"}}}
        raise AssertionError(f"unexpected url: {url}")

    return fake_fetch_json


def test_failed_load_is_not_cached_and_retries_after_cooldown(monkeypatch):
    arena_static.reset_arena_display_name_cache()
    now = {"value": 1000.0}
    calls = []
    fail_urls = {arena_static.COMMUNITY_DRAGON_ARENA_URL, arena_static.DATA_DRAGON_VERSIONS_URL}
    monkeypatch.setattr(arena_static, "time", SimpleNamespace(monotonic=lambda: now["value"]))
    monkeypatch.setattr(arena_static, "_fetch_json", _fake_static_data(fail_urls, calls))

    assert arena_static.load_arena_display_names() == {"augment_names": {}, "item_names": {}}
    first_call_count = len(calls)

    # Within the cooldown: no new fetch attempts.
    now["value"] += arena_static.RETRY_AFTER_FAILURE_SECONDS - 1
    arena_static.load_arena_display_names()
    assert len(calls) == first_call_count

    # After the cooldown with the sources back up: names load and stay cached.
    fail_urls.clear()
    now["value"] += 1
    names = arena_static.load_arena_display_names()
    assert names["augment_names"] == {"93": "Warmup Routine"}
    assert names["item_names"] == {"3157": "Zhonya's Hourglass"}

    calls_after_success = len(calls)
    arena_static.load_arena_display_names()
    assert len(calls) == calls_after_success

    arena_static.reset_arena_display_name_cache()


def test_one_source_failing_does_not_drop_the_other(monkeypatch):
    arena_static.reset_arena_display_name_cache()
    now = {"value": 1000.0}
    calls = []
    fail_urls = {arena_static.DATA_DRAGON_VERSIONS_URL}
    monkeypatch.setattr(arena_static, "time", SimpleNamespace(monotonic=lambda: now["value"]))
    monkeypatch.setattr(arena_static, "_fetch_json", _fake_static_data(fail_urls, calls))

    names = arena_static.load_arena_display_names()
    assert names["augment_names"] == {"93": "Warmup Routine"}
    assert names["item_names"] == {}

    # Only the failed source is retried after the cooldown.
    fail_urls.clear()
    calls.clear()
    now["value"] += arena_static.RETRY_AFTER_FAILURE_SECONDS
    names = arena_static.load_arena_display_names()
    assert arena_static.COMMUNITY_DRAGON_ARENA_URL not in calls
    assert names["item_names"] == {"3157": "Zhonya's Hourglass"}

    arena_static.reset_arena_display_name_cache()


def test_unexpected_payload_shape_is_treated_as_failure(monkeypatch):
    arena_static.reset_arena_display_name_cache()

    def fake_fetch_json(url):
        if url == arena_static.DATA_DRAGON_VERSIONS_URL:
            return ["16.12.1"]
        return ["not", "a", "dict"]  # item.json shape is wrong -> AttributeError inside loader

    monkeypatch.setattr(arena_static, "_fetch_json", fake_fetch_json)

    names = arena_static.load_arena_display_names()
    assert names["item_names"] == {}

    arena_static.reset_arena_display_name_cache()

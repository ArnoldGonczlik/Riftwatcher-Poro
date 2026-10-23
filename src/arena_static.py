import threading
import time

import requests


COMMUNITY_DRAGON_ARENA_URL = "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/v1/cherry-augments.json"
DATA_DRAGON_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
DATA_DRAGON_ITEM_URL_TEMPLATE = "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/item.json"
STATIC_DATA_TIMEOUT_SECONDS = 5
# After a failed (or empty) load, wait this long before trying that source again.
RETRY_AFTER_FAILURE_SECONDS = 600

_CACHE_LOCK = threading.Lock()
# Only successful, non-empty loads are cached; None means "not loaded yet".
_CACHE = {"augment_names": None, "item_names": None}
_LAST_FAILURE_AT = {"augment_names": None, "item_names": None}


def _fetch_json(url):
    response = requests.get(url, timeout=STATIC_DATA_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


HARDCODED_AUGMENTS = {}


def _load_augment_names():
    data = _fetch_json(COMMUNITY_DRAGON_ARENA_URL)
    names = {}
    if isinstance(data, list):
        for row in data:
            augment_id = row.get("id")
            name = row.get("nameTRA") or row.get("name")
            if augment_id is None or not name:
                continue
            names[str(augment_id)] = str(name)
    return names


def _load_item_names():
    versions = _fetch_json(DATA_DRAGON_VERSIONS_URL)
    if not versions:
        return {}
    data = _fetch_json(DATA_DRAGON_ITEM_URL_TEMPLATE.format(version=versions[0]))
    names = {}
    for item_id, row in (data.get("data", {}) or {}).items():
        name = row.get("name")
        if not name:
            continue
        names[str(item_id)] = str(name)
    return names


_SOURCES = ("augment_names", "item_names")


def _run_loader(key):
    if key == "augment_names":
        return _load_augment_names()
    return _load_item_names()


def _due_for_load(key, now):
    if _CACHE[key] is not None:
        return False
    last_failure = _LAST_FAILURE_AT[key]
    return last_failure is None or (now - last_failure) >= RETRY_AFTER_FAILURE_SECONDS


def load_arena_display_names():
    """Return Arena augment/item display names, loading any source that isn't cached yet.

    Best effort: failures yield empty maps (callers fall back to raw IDs) and are retried
    after RETRY_AFTER_FAILURE_SECONDS instead of being cached for the process lifetime.
    """
    with _CACHE_LOCK:
        now = time.monotonic()
        keys_to_load = [key for key in _SOURCES if _due_for_load(key, now)]

    for key in keys_to_load:
        try:
            names = _run_loader(key)
        except Exception:  # Network errors, bad JSON, unexpected payload shapes.
            names = {}
        with _CACHE_LOCK:
            if names:
                _CACHE[key] = names
                _LAST_FAILURE_AT[key] = None
            else:
                _LAST_FAILURE_AT[key] = time.monotonic()

    with _CACHE_LOCK:
        augment_names = {**HARDCODED_AUGMENTS, **(_CACHE["augment_names"] or {})}
        item_names = dict(_CACHE["item_names"] or {})
    return {"augment_names": augment_names, "item_names": item_names}


def reset_arena_display_name_cache():
    with _CACHE_LOCK:
        for key in _CACHE:
            _CACHE[key] = None
            _LAST_FAILURE_AT[key] = None

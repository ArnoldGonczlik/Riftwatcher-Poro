import threading

import requests


COMMUNITY_DRAGON_ARENA_URL = "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/v1/cherry-augments.json"
DATA_DRAGON_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
DATA_DRAGON_ITEM_URL_TEMPLATE = "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/item.json"
STATIC_DATA_TIMEOUT_SECONDS = 5

_CACHE_LOCK = threading.Lock()
_CACHE = None


def _fetch_json(url):
    response = requests.get(url, timeout=STATIC_DATA_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


HARDCODED_AUGMENTS = {}


def _load_augment_names():
    try:
        data = _fetch_json(COMMUNITY_DRAGON_ARENA_URL)
    except requests.RequestException:
        data = []
    names = HARDCODED_AUGMENTS.copy()
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


def load_arena_display_names():
    global _CACHE
    with _CACHE_LOCK:
        if _CACHE is not None:
            return _CACHE

    augment_names = {}
    item_names = {}
    try:
        augment_names = _load_augment_names()
    except requests.RequestException:
        augment_names = {}
    try:
        item_names = _load_item_names()
    except requests.RequestException:
        item_names = {}

    loaded = {"augment_names": augment_names, "item_names": item_names}
    with _CACHE_LOCK:
        if _CACHE is None:
            _CACHE = loaded
        return _CACHE


def reset_arena_display_name_cache():
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = None

import asyncio
import json

import pytest

from src import discord_patch_worker as worker
from src.patch_notes_logic import PatchNotesParseError

NOW = 1_800_000_000
DAY = 24 * 3600


def article(n, age_seconds):
    return {
        "id": f"patchNote.{n}",
        "url": f"https://lol/patch-{n}/",
        "title": f"Patch 26.{n} Notes",
        "description": f"Patch {n} blurb",
        "published_at": NOW - age_seconds,
    }


class FakePatchClient:
    def __init__(self, articles=None, sections=None, error=None):
        self.articles = articles or []
        self.sections = sections if sections is not None else [("Champions", ["Azir"])]
        self.error = error
        self.section_requests = []

    async def fetch_articles(self):
        if self.error:
            raise self.error
        return list(self.articles)

    async def fetch_article_sections(self, url):
        self.section_requests.append(url)
        return self.sections


class FakeChannel:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send(self, content):
        if self.fail:
            raise RuntimeError("discord down")
        self.sent.append(content)


def make_state(initial=None):
    state = dict(initial or {})
    return state, state.get, state.__setitem__


def run_cycle(client, channel, state_get, state_set, **kwargs):
    return asyncio.run(
        worker.process_patch_notes_cycle(
            patch_client=client,
            channel=channel,
            db_get_state=state_get,
            db_set_state=state_set,
            log=lambda _msg: None,
            now_unix=NOW,
            **kwargs,
        )
    )


def seen_keys(state):
    return json.loads(state[worker.PATCH_NOTES_SEEN_STATE_KEY])["keys"]


def test_first_cycle_baselines_without_posting():
    state, get, put = make_state()
    client = FakePatchClient([article(21, DAY), article(20, 15 * DAY)])
    channel = FakeChannel()

    posted = run_cycle(client, channel, get, put)

    assert posted == 0
    assert channel.sent == []
    assert client.section_requests == []
    assert "patchNote.21" in seen_keys(state) and "patchNote.20" in seen_keys(state)
    assert state[worker.PATCH_NOTES_LAST_CHECKED_STATE_KEY] == str(NOW)


def test_new_article_is_posted_once():
    state, get, put = make_state({worker.PATCH_NOTES_SEEN_STATE_KEY: json.dumps({"keys": ["patchNote.21"]})})
    client = FakePatchClient([article(22, 3600), article(21, 14 * DAY)])
    channel = FakeChannel()

    assert run_cycle(client, channel, get, put) == 1
    assert len(channel.sent) == 1
    assert "**Patch 26.22 Notes**" in channel.sent[0]
    assert "**Champions (1):** Azir" in channel.sent[0]
    assert client.section_requests == ["https://lol/patch-22/"]
    assert "patchNote.22" in seen_keys(state)

    assert run_cycle(client, channel, get, put) == 0
    assert len(channel.sent) == 1


def test_old_unseen_article_is_not_posted():
    state, get, put = make_state({worker.PATCH_NOTES_SEEN_STATE_KEY: json.dumps({"keys": []})})
    client = FakePatchClient([article(21, 10 * DAY)])
    channel = FakeChannel()

    assert run_cycle(client, channel, get, put) == 0
    assert channel.sent == []


def test_article_fetch_failure_still_posts_fallback():
    state, get, put = make_state({worker.PATCH_NOTES_SEEN_STATE_KEY: json.dumps({"keys": []})})
    client = FakePatchClient([article(22, 60)], sections=[])
    channel = FakeChannel()

    assert run_cycle(client, channel, get, put) == 1
    assert "Champions" not in channel.sent[0]
    assert channel.sent[0].endswith("https://lol/patch-22/")


def test_listing_failure_raises_and_keeps_state():
    state, get, put = make_state({worker.PATCH_NOTES_SEEN_STATE_KEY: json.dumps({"keys": ["x"]})})
    client = FakePatchClient(error=PatchNotesParseError("redesign"))

    with pytest.raises(PatchNotesParseError):
        run_cycle(client, FakeChannel(), get, put)
    assert worker.PATCH_NOTES_LAST_CHECKED_STATE_KEY not in state
    assert seen_keys(state) == ["x"]


def test_send_failure_raises_and_does_not_mark_seen():
    state, get, put = make_state({worker.PATCH_NOTES_SEEN_STATE_KEY: json.dumps({"keys": []})})
    client = FakePatchClient([article(22, 60)])

    with pytest.raises(RuntimeError):
        run_cycle(client, FakeChannel(fail=True), get, put)
    assert seen_keys(state) == []


def test_get_last_checked_unix_handles_bad_values():
    assert asyncio.run(worker.get_last_checked_unix(lambda _key: None)) == 0
    assert asyncio.run(worker.get_last_checked_unix(lambda _key: "garbage")) == 0
    assert asyncio.run(worker.get_last_checked_unix(lambda _key: "123")) == 123

import asyncio
import json
from datetime import datetime, timezone

import pytest

from src.discord_esports_worker import (
    ESPORTS_SCHEDULE_STATE_KEY,
    ESPORTS_SNAPSHOT_STATE_KEY,
    process_esports_cycle,
)

UTC = timezone.utc


def raw_event(match_id, state="unstarted", wins=(0, 0), outcomes=(None, None), start="2026-10-15T18:00:00Z"):
    return {
        "startTime": start,
        "state": state,
        "type": "match",
        "blockName": "Play-Ins",
        "league": {"name": "Worlds", "slug": "worlds"},
        "match": {
            "id": match_id,
            "teams": [
                {"name": "Karmine Corp", "code": "KC", "result": {"outcome": outcomes[0], "gameWins": wins[0]}},
                {"name": "Cloud9 Kia", "code": "C9", "result": {"outcome": outcomes[1], "gameWins": wins[1]}},
            ],
            "strategy": {"type": "bestOf", "count": 5},
        },
    }


class FakeChannel:
    def __init__(self):
        self.messages = []

    async def send(self, content):
        self.messages.append(content)


class FakeEsportsClient:
    def __init__(self, events_by_league):
        self.events_by_league = events_by_league

    async def resolve_league_ids(self, slugs):
        return {slug: f"id-{slug}" for slug in slugs if f"id-{slug}" in self.events_by_league}

    async def fetch_schedule_events(self, league_id):
        value = self.events_by_league[league_id]
        if isinstance(value, Exception):
            raise value
        return value


class FakeState(dict):
    def get_state(self, key):
        return self.get(key)

    def set_state(self, key, value):
        self[key] = value


def run_cycle(client, state, channel, *, now, leagues=("worlds",), spoilers=True):
    logs = []
    result = asyncio.run(
        process_esports_cycle(
            esports_client=client,
            league_slugs=leagues,
            channel=channel,
            report_timezone=UTC,
            report_day_start_hour=6,
            spoiler_tags=spoilers,
            db_get_state=state.get_state,
            db_set_state=state.set_state,
            log=logs.append,
            now_utc=now,
        )
    )
    return result, logs


def test_first_cycle_baselines_silently_but_posts_daily_schedule():
    client = FakeEsportsClient({"id-worlds": [raw_event("m1", state="inProgress", wins=(0, 1))]})
    state = FakeState()
    channel = FakeChannel()

    result, logs = run_cycle(client, state, channel, now=datetime(2026, 10, 15, 19, 0, tzinfo=UTC))

    assert result == {"announcements": 0, "schedule_posted": True}
    assert len(channel.messages) == 1
    assert "Esports today" in channel.messages[0]
    assert state[ESPORTS_SCHEDULE_STATE_KEY] == "2026-10-15"
    snapshot = json.loads(state[ESPORTS_SNAPSHOT_STATE_KEY])
    assert snapshot["baselined_leagues"] == ["worlds"]
    assert snapshot["matches"]["m1"]["wins"] == [0, 1]
    assert any("Baselined" in line for line in logs)


def test_game_win_then_match_win_announced_once_each():
    state = FakeState()
    channel = FakeChannel()
    now = datetime(2026, 10, 15, 19, 0, tzinfo=UTC)
    run_cycle(FakeEsportsClient({"id-worlds": [raw_event("m1", state="inProgress", wins=(0, 1))]}), state, channel, now=now)
    channel.messages.clear()

    run_cycle(FakeEsportsClient({"id-worlds": [raw_event("m1", state="inProgress", wins=(1, 1))]}), state, channel, now=now)
    assert len(channel.messages) == 1
    assert "Game 2: ||**Karmine Corp** wins" in channel.messages[0]

    completed = raw_event("m1", state="completed", wins=(1, 3), outcomes=("loss", "win"))
    run_cycle(FakeEsportsClient({"id-worlds": [completed]}), state, channel, now=now)
    assert len(channel.messages) == 2
    assert "||**Cloud9 Kia** wins the series 3-1||" in channel.messages[1]

    run_cycle(FakeEsportsClient({"id-worlds": [completed]}), state, channel, now=now)
    assert len(channel.messages) == 2


def test_live_announcement_when_match_starts():
    state = FakeState()
    channel = FakeChannel()
    now = datetime(2026, 10, 15, 17, 0, tzinfo=UTC)
    run_cycle(FakeEsportsClient({"id-worlds": [raw_event("m1")]}), state, channel, now=now)
    channel.messages.clear()

    run_cycle(FakeEsportsClient({"id-worlds": [raw_event("m1", state="inProgress")]}), state, channel, now=now)
    assert len(channel.messages) == 1
    assert "LIVE" in channel.messages[0]


def test_daily_schedule_posted_once_per_report_day():
    client = FakeEsportsClient({"id-worlds": [raw_event("m1", start="2026-10-16T18:00:00Z")]})
    state = FakeState()
    channel = FakeChannel()

    run_cycle(client, state, channel, now=datetime(2026, 10, 16, 6, 1, tzinfo=UTC))
    run_cycle(client, state, channel, now=datetime(2026, 10, 16, 9, 0, tzinfo=UTC))
    assert sum("Esports today" in m for m in channel.messages) == 1

    run_cycle(client, state, channel, now=datetime(2026, 10, 17, 6, 1, tzinfo=UTC))
    # No matches on the 17th: nothing posted, but the day is still marked handled.
    assert sum("Esports today" in m for m in channel.messages) == 1
    assert state[ESPORTS_SCHEDULE_STATE_KEY] == "2026-10-17"


def test_new_league_is_baselined_without_backlog_announcements():
    state = FakeState()
    channel = FakeChannel()
    now = datetime(2026, 10, 15, 19, 0, tzinfo=UTC)
    run_cycle(FakeEsportsClient({"id-worlds": []}), state, channel, now=now)

    recent_msi = raw_event("msi1", state="completed", wins=(3, 0), start="2026-10-15T12:00:00Z")
    client = FakeEsportsClient({"id-worlds": [], "id-msi": [recent_msi]})
    run_cycle(client, state, channel, now=now, leagues=("worlds", "msi"))
    assert not any("wins the series" in m for m in channel.messages)


def test_partial_failure_keeps_snapshot_and_skips_schedule():
    state = FakeState()
    channel = FakeChannel()
    now = datetime(2026, 10, 15, 19, 0, tzinfo=UTC)
    client = FakeEsportsClient({"id-worlds": [raw_event("m1", state="inProgress")], "id-msi": []})
    state[ESPORTS_SCHEDULE_STATE_KEY] = "2026-10-15"
    run_cycle(client, state, channel, now=now, leagues=("worlds", "msi"))

    failing = FakeEsportsClient({"id-worlds": RuntimeError("503"), "id-msi": []})
    state.pop(ESPORTS_SCHEDULE_STATE_KEY)
    result, logs = run_cycle(failing, state, channel, now=now, leagues=("worlds", "msi"))

    assert result["schedule_posted"] is False
    assert ESPORTS_SCHEDULE_STATE_KEY not in state
    assert "m1" in json.loads(state[ESPORTS_SNAPSHOT_STATE_KEY])["matches"]
    assert any("Schedule fetch failed for worlds" in line for line in logs)


def test_all_leagues_failing_raises_for_worker_error_stats():
    client = FakeEsportsClient({"id-worlds": RuntimeError("403 Forbidden")})
    with pytest.raises(RuntimeError):
        run_cycle(client, FakeState(), FakeChannel(), now=datetime(2026, 10, 15, 19, 0, tzinfo=UTC))

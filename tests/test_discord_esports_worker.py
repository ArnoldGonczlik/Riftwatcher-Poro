import asyncio
import json
from datetime import datetime, timezone

import discord
import pytest

from src.discord_esports_worker import (
    ESPORTS_AUTH_ALERT_STATE_KEY,
    ESPORTS_SCHEDULE_STATE_KEY,
    ESPORTS_SNAPSHOT_STATE_KEY,
    ESPORTS_WEEKLY_SCHEDULE_STATE_KEY,
    process_esports_cycle,
)
from src.esports_api import EsportsAuthError

UTC = timezone.utc


def raw_event(match_id, state="unstarted", wins=(0, 0), outcomes=(None, None), start="2026-10-15T18:00:00Z", swap=False, teams=None):
    teams = teams or [
        {"name": "Karmine Corp", "code": "KC", "result": {"outcome": outcomes[0], "gameWins": wins[0]}},
        {"name": "Cloud9 Kia", "code": "C9", "result": {"outcome": outcomes[1], "gameWins": wins[1]}},
    ]
    return {
        "startTime": start,
        "state": state,
        "type": "match",
        "blockName": "Play-Ins",
        "league": {"name": "Worlds", "slug": "worlds"},
        "match": {
            "id": match_id,
            "teams": teams[::-1] if swap else teams,
            "strategy": {"type": "bestOf", "count": 5},
        },
    }


class FakeMessage:
    def __init__(self, message_id):
        self.id = message_id


class FakePartialMessage:
    def __init__(self, channel, message_id):
        self.channel = channel
        self.id = message_id

    async def edit(self, *, content):
        if self.id in self.channel.deleted_ids:
            raise discord.NotFound(FakeResponse(404), "Unknown Message")
        self.channel.edits.append((self.id, content))
        self.channel.by_id[self.id] = content

    async def delete(self):
        self.channel.deleted_ids.add(self.id)


class FakeResponse:
    def __init__(self, status):
        self.status = status
        self.reason = "Not Found"


class FakeChannel:
    def __init__(self):
        self.messages = []
        self.by_id = {}
        self.edits = []
        self.deleted_ids = set()
        self._next_id = 1000

    async def send(self, content):
        self.messages.append(content)
        self._next_id += 1
        self.by_id[self._next_id] = content
        return FakeMessage(self._next_id)

    def get_partial_message(self, message_id):
        return FakePartialMessage(self, message_id)


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

    assert result == {"announcements": 0, "schedule_posted": True, "weekly_schedule_posted": False}
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


def test_flapping_api_and_swapped_sides_post_each_update_once():
    """Replays the Play-Ins pattern: stale cache responses, side swaps, score lagging completion."""
    state = FakeState()
    channel = FakeChannel()
    now = datetime(2026, 10, 15, 17, 0, tzinfo=UTC)
    run_cycle(FakeEsportsClient({"id-worlds": [raw_event("m1")]}), state, channel, now=now)
    channel.messages.clear()

    sequence = [
        raw_event("m1", state="inProgress"),  # LIVE
        raw_event("m1"),  # stale: back to unstarted
        raw_event("m1", state="inProgress"),
        raw_event("m1", state="inProgress", wins=(1, 0), swap=True),  # Game 1 KC, sides swapped
        raw_event("m1", state="inProgress", wins=(0, 0)),  # stale score
        raw_event("m1", state="inProgress", wins=(1, 0)),
        raw_event("m1", state="inProgress", wins=(1, 1), swap=True),  # Game 2 C9
        raw_event("m1", state="inProgress", wins=(2, 1)),  # Game 3 KC
        raw_event("m1", state="completed", wins=(2, 1), outcomes=("win", "loss"), swap=True),  # score lagging
        raw_event("m1", state="completed", wins=(3, 1), outcomes=("win", "loss")),  # final
        raw_event("m1", state="inProgress", wins=(2, 1), swap=True),  # stale
        raw_event("m1", state="completed", wins=(3, 1), outcomes=("win", "loss"), swap=True),
    ]
    for minute, raw in enumerate(sequence):
        cycle_now = now.replace(minute=minute)
        run_cycle(FakeEsportsClient({"id-worlds": [raw]}), state, channel, now=cycle_now)

    assert len(channel.messages) == 5, channel.messages
    assert "LIVE" in channel.messages[0]
    assert "Game 1: ||**Karmine Corp** wins \u00B7 KC 1-0 C9||" in channel.messages[1]
    assert "Game 2: ||**Cloud9 Kia** wins \u00B7 KC 1-1 C9||" in channel.messages[2]
    assert "Game 3: ||**Karmine Corp** wins \u00B7 KC 2-1 C9||" in channel.messages[3]
    assert "||**Karmine Corp** wins the series 3-1||" in channel.messages[4]
    assert all("**Karmine Corp** vs **Cloud9 Kia**" in m for m in channel.messages)


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


def test_weekly_schedule_posted_monday_before_daily_and_only_once():
    events = [
        raw_event("mon", start="2026-10-19T18:00:00Z"),
        raw_event("fri", start="2026-10-23T17:00:00Z"),
        raw_event("sun-late", start="2026-10-26T05:00:00Z"),  # before Monday's 06:00 day start: still this week
        raw_event("next-mon", start="2026-10-26T18:00:00Z"),  # next week
        raw_event("last-sun", start="2026-10-18T18:00:00Z"),  # previous week
    ]
    client = FakeEsportsClient({"id-worlds": events})
    state = FakeState()
    channel = FakeChannel()

    result, logs = run_cycle(client, state, channel, now=datetime(2026, 10, 19, 6, 1, tzinfo=UTC))

    assert result["weekly_schedule_posted"] is True
    assert result["schedule_posted"] is True
    assert len(channel.messages) == 2
    weekly, daily = channel.messages
    assert "Esports this week" in weekly and "Mon 19 Oct \u2013 Sun 25 Oct" in weekly
    assert "**Mon 19 Oct** \u00B7 Worlds Play-Ins" in weekly
    assert "**Fri 23 Oct**" in weekly
    assert "**Sun 25 Oct**" in weekly  # the 05:00 UTC Monday match belongs to Sunday's report day
    assert weekly.count("\u2022 ") == 3
    assert "Esports today" in daily
    weekly_state = json.loads(state[ESPORTS_WEEKLY_SCHEDULE_STATE_KEY])
    assert weekly_state["week"] == "2026-10-19"
    assert weekly_state["contents"] == [weekly]
    assert len(weekly_state["message_ids"]) == 1
    assert any("Posted weekly schedule" in line for line in logs)

    channel.messages.clear()
    result, _ = run_cycle(client, state, channel, now=datetime(2026, 10, 19, 12, 0, tzinfo=UTC))
    assert result["weekly_schedule_posted"] is False
    assert channel.messages == []


def test_weekly_schedule_not_posted_on_other_days_or_before_monday_day_start():
    client = FakeEsportsClient({"id-worlds": [raw_event("fri", start="2026-10-23T17:00:00Z")]})
    state = FakeState()
    channel = FakeChannel()
    # Monday 05:00 UTC is still Sunday's report day (day start 06:00).
    run_cycle(client, state, channel, now=datetime(2026, 10, 19, 5, 0, tzinfo=UTC))
    run_cycle(client, state, channel, now=datetime(2026, 10, 21, 9, 0, tzinfo=UTC))
    assert not any("Esports this week" in m for m in channel.messages)
    assert ESPORTS_WEEKLY_SCHEDULE_STATE_KEY not in state


def test_weekly_schedule_empty_week_posts_nothing_but_marks_week():
    client = FakeEsportsClient({"id-worlds": []})
    state = FakeState()
    channel = FakeChannel()
    result, _ = run_cycle(client, state, channel, now=datetime(2026, 10, 19, 6, 1, tzinfo=UTC))
    assert result["weekly_schedule_posted"] is False
    assert channel.messages == []
    assert json.loads(state[ESPORTS_WEEKLY_SCHEDULE_STATE_KEY])["week"] == "2026-10-19"


TBD_TEAMS = [{"name": "TBD", "code": "TBD"}, {"name": "TBD", "code": "TBD"}]
RESOLVED_TEAMS = [
    {"name": "Gen.G Esports", "code": "GEN", "result": {"gameWins": 0}},
    {"name": "T1", "code": "T1", "result": {"gameWins": 0}},
]


def _weekly_setup():
    state = FakeState()
    channel = FakeChannel()
    fri = raw_event("fri", start="2026-10-23T17:00:00Z")
    sat_tbd = raw_event("sat", start="2026-10-24T17:00:00Z", teams=TBD_TEAMS)
    run_cycle(FakeEsportsClient({"id-worlds": [fri, sat_tbd]}), state, channel, now=datetime(2026, 10, 19, 6, 1, tzinfo=UTC))
    weekly_id = json.loads(state[ESPORTS_WEEKLY_SCHEDULE_STATE_KEY])["message_ids"][0]
    assert "**TBD** vs **TBD**" in channel.by_id[weekly_id]
    channel.messages.clear()
    return state, channel, fri, weekly_id


def test_weekly_post_is_edited_when_tbd_resolves():
    state, channel, fri, weekly_id = _weekly_setup()
    sat_resolved = raw_event("sat", start="2026-10-24T17:00:00Z", teams=RESOLVED_TEAMS)

    _, logs = run_cycle(
        FakeEsportsClient({"id-worlds": [fri, sat_resolved]}), state, channel, now=datetime(2026, 10, 23, 22, 0, tzinfo=UTC)
    )

    assert not any("Esports this week" in m for m in channel.messages)  # edited, not reposted
    assert len(channel.edits) == 1 and channel.edits[0][0] == weekly_id
    assert "**Gen.G Esports** vs **T1**" in channel.by_id[weekly_id]
    assert "TBD" not in channel.by_id[weekly_id]
    assert json.loads(state[ESPORTS_WEEKLY_SCHEDULE_STATE_KEY])["contents"] == [channel.by_id[weekly_id]]
    assert any("Updated weekly schedule" in line for line in logs)

    # Unchanged matchups, or only live/score changes, don't trigger further edits.
    live_fri = raw_event("fri", state="inProgress", wins=(1, 0), start="2026-10-23T17:00:00Z")
    run_cycle(FakeEsportsClient({"id-worlds": [live_fri, sat_resolved]}), state, channel, now=datetime(2026, 10, 23, 22, 5, tzinfo=UTC))
    assert len(channel.edits) == 1


def test_weekly_post_ignores_stale_tbd_after_resolution():
    state, channel, fri, weekly_id = _weekly_setup()
    sat_resolved = raw_event("sat", start="2026-10-24T17:00:00Z", teams=RESOLVED_TEAMS)
    sat_tbd = raw_event("sat", start="2026-10-24T17:00:00Z", teams=TBD_TEAMS)
    now = datetime(2026, 10, 23, 22, 0, tzinfo=UTC)
    run_cycle(FakeEsportsClient({"id-worlds": [fri, sat_resolved]}), state, channel, now=now)
    run_cycle(FakeEsportsClient({"id-worlds": [fri, sat_tbd]}), state, channel, now=now)

    assert len(channel.edits) == 1
    assert "**Gen.G Esports** vs **T1**" in channel.by_id[weekly_id]
    snapshot = json.loads(state[ESPORTS_SNAPSHOT_STATE_KEY])
    assert snapshot["matches"]["sat"]["codes"] == ["GEN", "T1"]


def test_weekly_post_deleted_stops_tracking():
    state, channel, fri, weekly_id = _weekly_setup()
    channel.deleted_ids.add(weekly_id)
    sat_resolved = raw_event("sat", start="2026-10-24T17:00:00Z", teams=RESOLVED_TEAMS)
    _, logs = run_cycle(
        FakeEsportsClient({"id-worlds": [fri, sat_resolved]}), state, channel, now=datetime(2026, 10, 23, 22, 0, tzinfo=UTC)
    )
    assert any("was deleted" in line for line in logs)
    weekly_state = json.loads(state[ESPORTS_WEEKLY_SCHEDULE_STATE_KEY])
    assert weekly_state["week"] == "2026-10-19" and weekly_state["message_ids"] == []
    assert not any("Esports this week" in m for m in channel.messages)  # no repost mid-week


def test_weekly_post_edit_failure_is_retried_next_cycle():
    state, channel, fri, weekly_id = _weekly_setup()
    sat_resolved = raw_event("sat", start="2026-10-24T17:00:00Z", teams=RESOLVED_TEAMS)
    now = datetime(2026, 10, 23, 22, 0, tzinfo=UTC)

    original_edit = FakePartialMessage.edit

    async def failing_edit(self, *, content):
        raise RuntimeError("503")

    FakePartialMessage.edit = failing_edit
    try:
        _, logs = run_cycle(FakeEsportsClient({"id-worlds": [fri, sat_resolved]}), state, channel, now=now)
    finally:
        FakePartialMessage.edit = original_edit
    assert any("Failed to update weekly schedule" in line for line in logs)

    run_cycle(FakeEsportsClient({"id-worlds": [fri, sat_resolved]}), state, channel, now=now)
    assert "**Gen.G Esports** vs **T1**" in channel.by_id[weekly_id]


def test_next_monday_starts_a_new_weekly_post():
    state, channel, fri, weekly_id = _weekly_setup()
    next_week = raw_event("next", start="2026-10-28T20:00:00Z")
    run_cycle(FakeEsportsClient({"id-worlds": [fri, next_week]}), state, channel, now=datetime(2026, 10, 26, 6, 1, tzinfo=UTC))
    assert any("Esports this week" in m and "Mon 26 Oct" in m for m in channel.messages)
    assert channel.edits == []
    weekly_state = json.loads(state[ESPORTS_WEEKLY_SCHEDULE_STATE_KEY])
    assert weekly_state["week"] == "2026-10-26" and weekly_state["message_ids"] != [weekly_id]


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
    client = FakeEsportsClient({"id-worlds": RuntimeError("503 Service Unavailable")})
    state = FakeState()
    channel = FakeChannel()
    with pytest.raises(RuntimeError):
        run_cycle(client, state, channel, now=datetime(2026, 10, 15, 19, 0, tzinfo=UTC))
    # Ordinary outages must not trigger the key alert.
    assert channel.messages == []
    assert ESPORTS_AUTH_ALERT_STATE_KEY not in state


def test_auth_failure_posts_alert_once_then_recovery():
    now = datetime(2026, 10, 15, 19, 0, tzinfo=UTC)
    state = FakeState()
    channel = FakeChannel()
    failing = FakeEsportsClient({"id-worlds": EsportsAuthError("LoL Esports API returned HTTP 403 for getSchedule.")})

    for _ in range(2):
        with pytest.raises(EsportsAuthError):
            run_cycle(failing, state, channel, now=now)

    assert len(channel.messages) == 1
    assert "ESPORTS_API_KEY" in channel.messages[0]
    assert "HTTP 403" in channel.messages[0]
    assert state[ESPORTS_AUTH_ALERT_STATE_KEY] == "1"

    channel.messages.clear()
    run_cycle(FakeEsportsClient({"id-worlds": []}), state, channel, now=now)
    assert any("working again" in m for m in channel.messages)
    assert state[ESPORTS_AUTH_ALERT_STATE_KEY] == "0"

    channel.messages.clear()
    run_cycle(FakeEsportsClient({"id-worlds": []}), state, channel, now=now)
    assert not any("working again" in m for m in channel.messages)


def test_auth_failure_during_league_lookup_alerts():
    class LookupFails(FakeEsportsClient):
        async def resolve_league_ids(self, slugs):
            raise EsportsAuthError("ESPORTS_API_KEY is not set.")

    state = FakeState()
    channel = FakeChannel()
    with pytest.raises(EsportsAuthError):
        run_cycle(LookupFails({}), state, channel, now=datetime(2026, 10, 15, 19, 0, tzinfo=UTC))
    assert len(channel.messages) == 1
    assert "not set" in channel.messages[0]

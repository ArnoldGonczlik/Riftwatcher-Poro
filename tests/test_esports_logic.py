from datetime import datetime, timezone

from src.esports_logic import (
    diff_events,
    events_in_window,
    format_announcement,
    format_daily_schedule,
    match_winner_index,
    parse_schedule_event,
    snapshot_entry,
)


def raw_event(match_id="m1", state="inProgress", wins=(0, 0), outcomes=(None, None), start="2026-10-15T18:00:00Z", best_of=5, block="Play-Ins", event_type="match"):
    return {
        "startTime": start,
        "state": state,
        "type": event_type,
        "blockName": block,
        "league": {"name": "Worlds", "slug": "worlds"},
        "match": {
            "id": match_id,
            "teams": [
                {"name": "Karmine Corp", "code": "KC", "result": {"outcome": outcomes[0], "gameWins": wins[0]}},
                {"name": "Cloud9 Kia", "code": "C9", "result": {"outcome": outcomes[1], "gameWins": wins[1]}},
            ],
            "strategy": {"type": "bestOf", "count": best_of},
        },
    }


def event(**kwargs):
    return parse_schedule_event(raw_event(**kwargs))


NOW = int(datetime(2026, 10, 15, 20, 0, tzinfo=timezone.utc).timestamp())


# --- parsing ----------------------------------------------------------------


def test_parse_schedule_event_normalizes_fields():
    parsed = event(wins=(1, 2))
    assert parsed["match_id"] == "m1"
    assert parsed["start_unix"] == int(datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc).timestamp())
    assert parsed["league"] == "Worlds"
    assert parsed["block"] == "Play-Ins"
    assert parsed["best_of"] == 5
    assert [t["code"] for t in parsed["teams"]] == ["KC", "C9"]
    assert [t["wins"] for t in parsed["teams"]] == [1, 2]


def test_parse_schedule_event_skips_non_match_and_malformed():
    assert parse_schedule_event(raw_event(event_type="show")) is None
    broken = raw_event()
    broken["match"]["teams"] = broken["match"]["teams"][:1]
    assert parse_schedule_event(broken) is None
    assert parse_schedule_event(None) is None


def test_parse_schedule_event_handles_tbd_teams_without_results():
    raw = raw_event(state="unstarted")
    raw["match"]["teams"] = [{"name": "TBD", "code": "TBD"}, {"name": "TBD", "code": "TBD"}]
    parsed = parse_schedule_event(raw)
    assert [t["wins"] for t in parsed["teams"]] == [0, 0]


def test_match_winner_prefers_outcome_then_game_wins():
    assert match_winner_index(event(state="completed", wins=(2, 3), outcomes=("loss", "win"))) == 1
    assert match_winner_index(event(state="completed", wins=(3, 1))) == 0
    assert match_winner_index(event(state="completed", wins=(1, 1))) is None


# --- diffing ----------------------------------------------------------------


def test_diff_announces_live_when_match_starts():
    previous = {"m1": snapshot_entry(event(state="unstarted"))}
    result = diff_events(previous, [event(state="inProgress")], now_unix=NOW)
    assert [a["kind"] for a in result] == ["live"]


def test_diff_announces_game_win_with_winner_and_game_number():
    previous = {"m1": snapshot_entry(event(wins=(0, 1)))}
    result = diff_events(previous, [event(wins=(1, 1))], now_unix=NOW)
    assert len(result) == 1
    assert result[0]["kind"] == "game"
    assert result[0]["winner_index"] == 0
    assert result[0]["game_number"] == 2


def test_diff_live_and_game_when_start_and_first_game_missed():
    previous = {"m1": snapshot_entry(event(state="unstarted"))}
    result = diff_events(previous, [event(wins=(0, 1))], now_unix=NOW)
    assert [a["kind"] for a in result] == ["live", "game"]
    assert result[1]["winner_index"] == 1


def test_diff_score_update_when_both_teams_gained():
    previous = {"m1": snapshot_entry(event(wins=(0, 0)))}
    result = diff_events(previous, [event(wins=(1, 1))], now_unix=NOW)
    assert result[0]["kind"] == "game"
    assert result[0]["winner_index"] is None


def test_diff_announces_match_win_once():
    previous = {"m1": snapshot_entry(event(wins=(2, 1)))}
    completed = event(state="completed", wins=(3, 1), outcomes=("win", "loss"))
    result = diff_events(previous, [completed], now_unix=NOW)
    assert [a["kind"] for a in result] == ["match"]
    assert result[0]["winner_index"] == 0

    previous = {"m1": snapshot_entry(completed)}
    assert diff_events(previous, [completed], now_unix=NOW) == []


def test_diff_no_changes_no_announcements():
    current = event(wins=(1, 0))
    assert diff_events({"m1": snapshot_entry(current)}, [current], now_unix=NOW) == []
    unstarted = event(state="unstarted")
    assert diff_events({"m1": snapshot_entry(unstarted)}, [unstarted], now_unix=NOW) == []


def test_diff_new_inprogress_match_announces_live_without_game_spam():
    result = diff_events({}, [event(wins=(1, 1))], now_unix=NOW)
    assert [a["kind"] for a in result] == ["live"]


def test_diff_new_completed_match_only_if_recent():
    recent = event(state="completed", wins=(3, 0), start="2026-10-15T12:00:00Z")
    old = event(match_id="old", state="completed", wins=(3, 0), start="2025-11-09T07:00:00Z")
    result = diff_events({}, [recent, old], now_unix=NOW)
    assert [a["event"]["match_id"] for a in result] == ["m1"]


# --- formatting -------------------------------------------------------------


def test_format_game_announcement_with_spoilers():
    text = format_announcement(
        {"kind": "game", "event": event(wins=(1, 1)), "winner_index": 0, "game_number": 2},
        spoilers=True,
    )
    assert "Worlds Play-Ins" in text
    assert "**Karmine Corp** vs **Cloud9 Kia**" in text
    assert "Game 2: ||**Karmine Corp** wins \u00B7 KC 1-1 C9||" in text


def test_format_match_announcement_bo5_and_bo1_without_spoilers():
    bo5 = event(state="completed", wins=(1, 3), outcomes=("loss", "win"))
    text = format_announcement({"kind": "match", "event": bo5, "winner_index": 1}, spoilers=False)
    assert "**Cloud9 Kia** wins the series 3-1" in text
    assert "||" not in text

    bo1 = event(state="completed", wins=(1, 0), best_of=1)
    text = format_announcement({"kind": "match", "event": bo1, "winner_index": 0}, spoilers=True)
    assert "||**Karmine Corp** wins||" in text


def test_format_live_announcement():
    text = format_announcement({"kind": "live", "event": event()})
    assert text.startswith("\U0001F534 **LIVE**")
    assert "Bo5" in text


def test_daily_schedule_groups_and_marks_state():
    events = [
        event(match_id="a", state="completed", wins=(3, 1), start="2026-10-15T12:00:00Z"),
        event(match_id="b", state="inProgress", wins=(1, 1), start="2026-10-15T18:00:00Z"),
        event(match_id="c", state="unstarted", start="2026-10-15T23:00:00Z", block="Swiss"),
    ]
    text = format_daily_schedule(events, "2026-10-15", spoilers=True)
    assert "Esports today" in text and "2026-10-15" in text
    assert "**Worlds Play-Ins**" in text and "**Worlds Swiss**" in text
    assert "||KC 3-1 C9||" in text
    assert "LIVE" in text
    assert f"<t:{events[2]['start_unix']}:t>" in text


def test_daily_schedule_none_when_empty():
    assert format_daily_schedule([], "2026-10-15") is None


def test_events_in_window_filters_and_sorts():
    late = event(match_id="late", start="2026-10-16T05:00:00Z")
    early = event(match_id="early", start="2026-10-15T07:00:00Z")
    outside = event(match_id="outside", start="2026-10-16T07:00:00Z")
    start = int(datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc).timestamp())
    result = events_in_window([late, outside, early], start, start + 24 * 3600)
    assert [e["match_id"] for e in result] == ["early", "late"]

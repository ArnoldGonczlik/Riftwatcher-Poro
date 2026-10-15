"""Pure helpers for esports news: event parsing, snapshot diffing, and message formatting."""

from datetime import datetime

# Matches first seen already completed are only announced if they started this recently.
NEW_COMPLETED_MAX_AGE_SECONDS = 24 * 3600

ANNOUNCEMENT_ORDER = {"live": 0, "game": 1, "match": 2}


def _parse_start_unix(raw_start):
    if not raw_start:
        return None
    try:
        return int(datetime.fromisoformat(str(raw_start).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def parse_schedule_event(raw_event):
    """Normalize one getSchedule event; returns None for non-match or malformed events."""
    if not isinstance(raw_event, dict) or raw_event.get("type") != "match":
        return None
    match = raw_event.get("match") or {}
    match_id = match.get("id")
    raw_teams = match.get("teams") or []
    if not match_id or len(raw_teams) != 2:
        return None

    teams = []
    for raw_team in raw_teams:
        result = raw_team.get("result") or {}
        name = raw_team.get("name") or raw_team.get("code") or "TBD"
        teams.append(
            {
                "name": name,
                "code": raw_team.get("code") or name,
                "wins": int(result.get("gameWins") or 0),
                "outcome": result.get("outcome"),
            }
        )

    return {
        "match_id": str(match_id),
        "start_unix": _parse_start_unix(raw_event.get("startTime")),
        "state": raw_event.get("state") or "unstarted",
        "league": (raw_event.get("league") or {}).get("name") or "",
        "block": raw_event.get("blockName") or "",
        "best_of": int((match.get("strategy") or {}).get("count") or 1),
        "teams": teams,
    }


def snapshot_entry(event):
    return {
        "state": event["state"],
        "wins": [event["teams"][0]["wins"], event["teams"][1]["wins"]],
        "start_unix": event["start_unix"],
    }


def match_winner_index(event):
    """Index (0/1) of the series winner, or None if undetermined."""
    teams = event["teams"]
    for index, team in enumerate(teams):
        if team.get("outcome") == "win":
            return index
    if teams[0]["wins"] != teams[1]["wins"]:
        return 0 if teams[0]["wins"] > teams[1]["wins"] else 1
    return None


def diff_events(previous_matches, events, *, now_unix):
    """Compare current events against the stored snapshot and return announcements.

    Each announcement is a dict: {"kind": "live"|"game"|"match", "event": event, ...}.
    """
    announcements = []
    for event in events:
        previous = previous_matches.get(event["match_id"])
        state = event["state"]
        previous_state = previous["state"] if previous else None
        current_wins = [event["teams"][0]["wins"], event["teams"][1]["wins"]]

        if state == "inProgress":
            if previous_state in (None, "unstarted"):
                announcements.append({"kind": "live", "event": event})
            if previous is None:
                continue  # No baseline score to diff against.
            previous_wins = list(previous.get("wins") or [0, 0])
            gained = [current_wins[i] - int(previous_wins[i] or 0) for i in (0, 1)]
            if gained[0] <= 0 and gained[1] <= 0:
                continue
            if gained[0] > 0 and gained[1] > 0:
                winner_index = None  # Missed polls; both teams took games.
            else:
                winner_index = 0 if gained[0] > 0 else 1
            announcements.append(
                {
                    "kind": "game",
                    "event": event,
                    "winner_index": winner_index,
                    "game_number": sum(current_wins),
                }
            )
            continue

        if state == "completed":
            if previous_state == "completed":
                continue
            if previous is None:
                start_unix = event["start_unix"] or 0
                if now_unix - start_unix > NEW_COMPLETED_MAX_AGE_SECONDS:
                    continue
            announcements.append({"kind": "match", "event": event, "winner_index": match_winner_index(event)})

    announcements.sort(
        key=lambda item: (item["event"]["start_unix"] or 0, item["event"]["match_id"], ANNOUNCEMENT_ORDER[item["kind"]])
    )
    return announcements


def events_in_window(events, start_unix, end_unix):
    selected = [e for e in events if e["start_unix"] is not None and start_unix <= e["start_unix"] < end_unix]
    return sorted(selected, key=lambda e: (e["start_unix"], e["match_id"]))


def _spoiler(text, enabled):
    return f"||{text}||" if enabled else text


def _header(event):
    return " ".join(part for part in (event["league"], event["block"]) if part) or "Esports"


def _score_line(event):
    a, b = event["teams"]
    return f"{a['code']} {a['wins']}-{b['wins']} {b['code']}"


def _matchup(event):
    a, b = event["teams"]
    return f"**{a['name']}** vs **{b['name']}**"


def format_live_announcement(event):
    return (
        f"\U0001F534 **LIVE** \u00B7 {_header(event)}\n"
        f"{_matchup(event)} \u00B7 Bo{event['best_of']}"
    )


def format_game_announcement(event, winner_index, game_number, *, spoilers=True):
    if winner_index is None:
        result = f"Score update: {_score_line(event)}"
        label = "Update"
    else:
        winner = event["teams"][winner_index]["name"]
        result = f"**{winner}** wins \u00B7 {_score_line(event)}"
        label = f"Game {game_number}"
    return (
        f"\U0001F3AE {_header(event)} \u00B7 {_matchup(event)}\n"
        f"{label}: {_spoiler(result, spoilers)}"
    )


def format_match_announcement(event, winner_index, *, spoilers=True):
    if winner_index is None:
        result = f"Match finished \u00B7 {_score_line(event)}"
    else:
        winner = event["teams"][winner_index]
        loser = event["teams"][1 - winner_index]
        if event["best_of"] <= 1:
            result = f"**{winner['name']}** wins"
        else:
            result = f"**{winner['name']}** wins the series {winner['wins']}-{loser['wins']}"
    return (
        f"\U0001F3C6 {_header(event)} \u00B7 {_matchup(event)}\n"
        f"{_spoiler(result, spoilers)}"
    )


def format_announcement(announcement, *, spoilers=True):
    kind = announcement["kind"]
    event = announcement["event"]
    if kind == "live":
        return format_live_announcement(event)
    if kind == "game":
        return format_game_announcement(
            event, announcement["winner_index"], announcement["game_number"], spoilers=spoilers
        )
    return format_match_announcement(event, announcement["winner_index"], spoilers=spoilers)


def format_daily_schedule(events, cycle_date, *, spoilers=True):
    """Render today's matches grouped by league/stage; returns None when there are none."""
    if not events:
        return None
    lines = [f"\U0001F4C5 **Esports today** \u00B7 {cycle_date}"]
    current_header = None
    for event in events:
        header = _header(event)
        if header != current_header:
            lines.append(f"\n**{header}**")
            current_header = header
        line = f"\u2022 <t:{event['start_unix']}:t> \u00B7 {_matchup(event)} \u00B7 Bo{event['best_of']}"
        if event["state"] == "inProgress":
            line += " \u00B7 \U0001F534 LIVE"
        elif event["state"] == "completed":
            line += f" \u00B7 \u2705 {_spoiler(_score_line(event), spoilers)}"
        lines.append(line)
    lines.append("\n_Times shown in your local timezone._")
    return "\n".join(lines)

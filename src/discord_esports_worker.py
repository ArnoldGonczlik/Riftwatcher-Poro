import asyncio
import json
import time
from datetime import date, timedelta

import discord

from src.esports_api import EsportsAuthError
from src.esports_logic import (
    align_event,
    events_in_window,
    format_announcement,
    format_daily_schedule,
    format_weekly_schedule,
    parse_schedule_event,
    reconcile_events,
    restore_resolved_teams,
    snapshot_entry,
)
from src.report_logic import (
    get_report_cycle_key,
    get_report_cycle_start_unix_seconds,
    get_report_day_start_unix_seconds,
)

ESPORTS_SNAPSHOT_STATE_KEY = "esports_match_snapshot"
ESPORTS_SCHEDULE_STATE_KEY = "esports_schedule_posted_cycle"
# JSON: {"week": "<monday report date>", "message_ids": [...], "contents": [...]}
ESPORTS_WEEKLY_SCHEDULE_STATE_KEY = "esports_weekly_schedule_posted_week"
ESPORTS_AUTH_ALERT_STATE_KEY = "esports_auth_alert_sent"
# Snapshot entries no longer returned by the API are dropped after this age.
SNAPSHOT_RETENTION_SECONDS = 7 * 24 * 3600

ESPORTS_AUTH_ALERT_TEXT = (
    "\u26A0\uFE0F LoL Esports API rejected the request ({reason}). "
    "`ESPORTS_API_KEY` is missing or Riot rotated it. Grab the current `x-api-key` header from any "
    "`esports-api.lolesports.com` request on lolesports.com (browser DevTools \u2192 Network) "
    "and update the `ESPORTS_API_KEY` variable. Esports updates are paused until then."
)
ESPORTS_AUTH_RECOVERED_TEXT = "\u2705 LoL Esports API key is working again; esports updates resumed."


def _load_snapshot(raw_value):
    empty = {"baselined_leagues": [], "matches": {}}
    if not raw_value:
        return empty
    try:
        data = json.loads(raw_value)
    except (TypeError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    return {
        "baselined_leagues": list(data.get("baselined_leagues") or []),
        "matches": dict(data.get("matches") or {}),
    }


async def _set_auth_alert_flag(db_set_state, value):
    await asyncio.to_thread(db_set_state, ESPORTS_AUTH_ALERT_STATE_KEY, value)


async def _report_auth_failure(*, channel, reason, db_get_state, db_set_state, log):
    if await asyncio.to_thread(db_get_state, ESPORTS_AUTH_ALERT_STATE_KEY) == "1":
        return
    try:
        await channel.send(ESPORTS_AUTH_ALERT_TEXT.format(reason=reason))
    except Exception as exc:
        log(f"[esports] Failed to post ESPORTS_API_KEY alert: {exc}")
        return
    await _set_auth_alert_flag(db_set_state, "1")
    log(f"[esports] Posted ESPORTS_API_KEY alert: {reason}")


async def _report_auth_recovered(*, channel, db_get_state, db_set_state, log):
    if await asyncio.to_thread(db_get_state, ESPORTS_AUTH_ALERT_STATE_KEY) != "1":
        return
    try:
        await channel.send(ESPORTS_AUTH_RECOVERED_TEXT)
    except Exception as exc:
        log(f"[esports] Failed to post ESPORTS_API_KEY recovery notice: {exc}")
        return
    await _set_auth_alert_flag(db_set_state, "0")
    log("[esports] ESPORTS_API_KEY recovered.")


def _load_weekly_state(raw_value):
    empty = {"week": None, "message_ids": [], "contents": []}
    if not raw_value:
        return empty
    try:
        data = json.loads(raw_value)
    except (TypeError, ValueError):
        return {**empty, "week": str(raw_value)}  # Legacy plain week key; nothing to edit.
    if not isinstance(data, dict):
        return {**empty, "week": str(raw_value)}
    message_ids = [int(value) for value in data.get("message_ids") or []]
    contents = [str(value) for value in data.get("contents") or []]
    if len(contents) != len(message_ids):
        contents = [""] * len(message_ids)  # Unknown text: force a re-render on next sync.
    return {"week": data.get("week"), "message_ids": message_ids, "contents": contents}


def _render_weekly(all_events, week_start, *, report_timezone, report_day_start_hour, spoiler_tags):
    days = []
    for offset in range(7):
        day = week_start + timedelta(days=offset)
        start = get_report_day_start_unix_seconds(report_timezone, day, report_day_start_hour)
        end = get_report_day_start_unix_seconds(report_timezone, day + timedelta(days=1), report_day_start_hour)
        days.append((day, events_in_window(all_events, start, end)))
    # No LIVE/score markers: the tracked post only changes when matchups or times change.
    messages = format_weekly_schedule(days, week_start, spoilers=spoiler_tags, show_status=False)
    return messages, sum(len(events) for _, events in days)


async def _edit_weekly_messages(*, channel, state, messages):
    """Bring the tracked weekly messages in line with `messages`, updating `state` as it goes."""
    ids = state["message_ids"]
    contents = state["contents"]
    for index, text in enumerate(messages):
        if index < len(ids):
            if contents[index] != text:
                await channel.get_partial_message(ids[index]).edit(content=text)
                contents[index] = text
        else:
            # Grew past the original message count; continue in a new message.
            sent = await channel.send(text)
            ids.append(int(sent.id))
            contents.append(text)
    while len(ids) > len(messages):
        try:
            await channel.get_partial_message(ids[-1]).delete()
        except discord.NotFound:
            pass
        ids.pop()
        contents.pop()


async def _sync_weekly_schedule(
    *, channel, all_events, cycle_key, report_timezone, report_day_start_hour, spoiler_tags, db_get_state, db_set_state, log
):
    """Post the week's schedule on Monday's report day, then keep that post updated all week.

    Returns True when a new weekly post was made this cycle.
    """
    cycle_date = date.fromisoformat(cycle_key)
    week_start = cycle_date - timedelta(days=cycle_date.weekday())
    week_key = week_start.isoformat()
    state = _load_weekly_state(await asyncio.to_thread(db_get_state, ESPORTS_WEEKLY_SCHEDULE_STATE_KEY))
    render_kwargs = {
        "report_timezone": report_timezone,
        "report_day_start_hour": report_day_start_hour,
        "spoiler_tags": spoiler_tags,
    }

    if state["week"] != week_key:
        if cycle_date != week_start:
            return False  # Only post on Monday; a missed Monday is skipped rather than posted mid-week.
        messages, match_count = _render_weekly(all_events, week_start, **render_kwargs)
        state = {"week": week_key, "message_ids": [], "contents": []}
        for text in messages:
            sent = await channel.send(text)
            state["message_ids"].append(int(sent.id))
            state["contents"].append(text)
        await asyncio.to_thread(db_set_state, ESPORTS_WEEKLY_SCHEDULE_STATE_KEY, json.dumps(state))
        if messages:
            log(f"[esports] Posted weekly schedule for week of {week_key} ({match_count} matches).")
        return bool(messages)

    if not state["message_ids"]:
        return False  # Empty week, legacy state, or the post was deleted: nothing to update.
    messages, _ = _render_weekly(all_events, week_start, **render_kwargs)
    if not messages or messages == state["contents"]:
        return False  # Never blank out the post if the week suddenly renders empty.
    before = json.dumps(state)
    try:
        await _edit_weekly_messages(channel=channel, state=state, messages=messages)
        log(f"[esports] Updated weekly schedule for week of {week_key}.")
    except discord.NotFound:
        log(f"[esports] Weekly schedule message for {week_key} was deleted; no longer updating it.")
        state["message_ids"], state["contents"] = [], []
    except Exception as exc:
        # Retry next cycle; state only records edits that actually went through.
        log(f"[esports] Failed to update weekly schedule for {week_key}: {exc}")
    if json.dumps(state) != before:
        await asyncio.to_thread(db_set_state, ESPORTS_WEEKLY_SCHEDULE_STATE_KEY, json.dumps(state))
    return False


async def process_esports_cycle(
    *,
    esports_client,
    league_slugs,
    channel,
    report_timezone,
    report_day_start_hour,
    spoiler_tags,
    db_get_state,
    db_set_state,
    log,
    now_utc=None,
):
    try:
        result, fetched_leagues = await _run_esports_cycle(
            esports_client=esports_client,
            league_slugs=league_slugs,
            channel=channel,
            report_timezone=report_timezone,
            report_day_start_hour=report_day_start_hour,
            spoiler_tags=spoiler_tags,
            db_get_state=db_get_state,
            db_set_state=db_set_state,
            log=log,
            now_utc=now_utc,
        )
    except EsportsAuthError as exc:
        log(f"[esports] Auth failure: {exc}")
        await _report_auth_failure(
            channel=channel, reason=str(exc), db_get_state=db_get_state, db_set_state=db_set_state, log=log
        )
        raise
    if fetched_leagues:
        await _report_auth_recovered(channel=channel, db_get_state=db_get_state, db_set_state=db_set_state, log=log)
    return result


async def _run_esports_cycle(
    *,
    esports_client,
    league_slugs,
    channel,
    report_timezone,
    report_day_start_hour,
    spoiler_tags,
    db_get_state,
    db_set_state,
    log,
    now_utc=None,
):
    now_unix = int(now_utc.timestamp()) if now_utc is not None else int(time.time())

    league_ids = await esports_client.resolve_league_ids(league_slugs)
    if not league_ids:
        log("[esports] No valid leagues configured; skipping cycle.")
        return {"announcements": 0, "schedule_posted": False, "weekly_schedule_posted": False}, 0

    snapshot = _load_snapshot(await asyncio.to_thread(db_get_state, ESPORTS_SNAPSHOT_STATE_KEY))
    matches = snapshot["matches"]
    baselined = set(snapshot["baselined_leagues"])

    announcements = []
    all_events = []
    seen_match_ids = set()
    failures = 0
    for slug, league_id in league_ids.items():
        try:
            raw_events = await esports_client.fetch_schedule_events(league_id)
        except EsportsAuthError:
            raise
        except Exception as exc:
            failures += 1
            log(f"[esports] Schedule fetch failed for {slug}: {exc}")
            continue

        events = [event for event in map(parse_schedule_event, raw_events) if event]
        # The API swaps team order between games; keep the order first stored per match.
        events = [align_event(event, matches.get(event["match_id"])) for event in events]
        if slug in baselined:
            league_announcements, entries = reconcile_events(matches, events, now_unix=now_unix)
            announcements.extend(league_announcements)
        else:
            # First observation of this league: record state silently, no backlog spam.
            log(f"[esports] Baselined {len(events)} events for {slug} without announcements.")
            baselined.add(slug)
            entries = {event["match_id"]: snapshot_entry(event) for event in events}

        matches.update(entries)
        seen_match_ids.update(entries)
        # Schedules show the last known matchup if the API serves a stale TBD view.
        all_events.extend(restore_resolved_teams(event, matches.get(event["match_id"])) for event in events)

    if failures and failures == len(league_ids):
        raise RuntimeError("All esports schedule fetches failed.")

    schedule_posted = False
    weekly_schedule_posted = False
    cycle_key = get_report_cycle_key(report_timezone, report_day_start_hour, now_utc=now_utc)
    # Only post schedules from a complete fetch: weekly on Monday's report day (then kept
    # updated all week), daily every report day.
    if failures == 0:
        weekly_schedule_posted = await _sync_weekly_schedule(
            channel=channel,
            all_events=all_events,
            cycle_key=cycle_key,
            report_timezone=report_timezone,
            report_day_start_hour=report_day_start_hour,
            spoiler_tags=spoiler_tags,
            db_get_state=db_get_state,
            db_set_state=db_set_state,
            log=log,
        )
        posted_cycle = await asyncio.to_thread(db_get_state, ESPORTS_SCHEDULE_STATE_KEY)
        if posted_cycle != cycle_key:
            window_start = get_report_cycle_start_unix_seconds(report_timezone, report_day_start_hour, now_utc=now_utc)
            todays_events = events_in_window(all_events, window_start, window_start + 24 * 3600)
            schedule_text = format_daily_schedule(todays_events, cycle_key, spoilers=spoiler_tags)
            if schedule_text:
                await channel.send(schedule_text)
                schedule_posted = True
                log(f"[esports] Posted daily schedule for {cycle_key} ({len(todays_events)} matches).")
            await asyncio.to_thread(db_set_state, ESPORTS_SCHEDULE_STATE_KEY, cycle_key)

    sent = 0
    for announcement in announcements:
        try:
            await channel.send(format_announcement(announcement, spoilers=spoiler_tags))
            sent += 1
            log(f"[esports] Posted {announcement['kind']} update for match {announcement['event']['match_id']}.")
        except Exception as exc:
            log(f"[esports] Failed to post {announcement['kind']} update: {exc}")

    cutoff = now_unix - SNAPSHOT_RETENTION_SECONDS
    for match_id in list(matches.keys()):
        if match_id in seen_match_ids:
            continue
        start_unix = matches[match_id].get("start_unix")
        if start_unix is not None and start_unix < cutoff:
            del matches[match_id]

    await asyncio.to_thread(
        db_set_state,
        ESPORTS_SNAPSHOT_STATE_KEY,
        json.dumps({"baselined_leagues": sorted(baselined), "matches": matches}, separators=(",", ":")),
    )
    result = {"announcements": sent, "schedule_posted": schedule_posted, "weekly_schedule_posted": weekly_schedule_posted}
    return result, len(league_ids) - failures

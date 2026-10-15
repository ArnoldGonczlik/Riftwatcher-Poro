import asyncio
import json
import time

from src.esports_logic import (
    diff_events,
    events_in_window,
    format_announcement,
    format_daily_schedule,
    parse_schedule_event,
    snapshot_entry,
)
from src.report_logic import get_report_cycle_key, get_report_cycle_start_unix_seconds

ESPORTS_SNAPSHOT_STATE_KEY = "esports_match_snapshot"
ESPORTS_SCHEDULE_STATE_KEY = "esports_schedule_posted_cycle"
# Snapshot entries no longer returned by the API are dropped after this age.
SNAPSHOT_RETENTION_SECONDS = 7 * 24 * 3600


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
    now_unix = int(now_utc.timestamp()) if now_utc is not None else int(time.time())

    league_ids = await esports_client.resolve_league_ids(league_slugs)
    if not league_ids:
        log("[esports] No valid leagues configured; skipping cycle.")
        return {"announcements": 0, "schedule_posted": False}

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
        except Exception as exc:
            failures += 1
            log(f"[esports] Schedule fetch failed for {slug}: {exc}")
            continue

        events = [event for event in map(parse_schedule_event, raw_events) if event]
        if slug in baselined:
            announcements.extend(diff_events(matches, events, now_unix=now_unix))
        else:
            # First observation of this league: record state silently, no backlog spam.
            log(f"[esports] Baselined {len(events)} events for {slug} without announcements.")
            baselined.add(slug)

        for event in events:
            matches[event["match_id"]] = snapshot_entry(event)
            seen_match_ids.add(event["match_id"])
        all_events.extend(events)

    if failures and failures == len(league_ids):
        raise RuntimeError("All esports schedule fetches failed.")

    schedule_posted = False
    cycle_key = get_report_cycle_key(report_timezone, report_day_start_hour, now_utc=now_utc)
    # Only post the daily schedule from a complete fetch, once per report day.
    if failures == 0:
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
    return {"announcements": sent, "schedule_posted": schedule_posted}

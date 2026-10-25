"""Conservative LP attribution from League-V4 snapshots, not Match-V5 LP data."""

import json
import math

from src.rank_logic import APEX_TIERS, DIVISION_ORDER, TIER_ORDER, format_rank, normalize_queue_type


QUEUE_TYPES = {420: "RANKED_SOLO_5X5", 440: "RANKED_FLEX_SR"}


def recap_rank_state_key(riot_id):
    # Independent of the rank notifier: it may refresh its baseline before a recap.
    return f"recap_rank_snapshot::{riot_id.casefold()}"


def rank_snapshot(entries, observed_at):
    if not isinstance(entries, list):
        raise ValueError("Ranked entries must be a list")
    queues = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Ranked entry must be an object")
        queue = normalize_queue_type(entry.get("queueType"))
        tier = str(entry.get("tier", "")).upper()
        division = str(entry.get("rank", "")).upper()
        values = [entry.get(key) for key in ("leaguePoints", "wins", "losses")]
        if queue not in QUEUE_TYPES.values() or tier not in TIER_ORDER:
            continue
        if tier not in APEX_TIERS and division not in DIVISION_ORDER:
            continue
        if any(type(value) is not int or value < 0 for value in values):
            continue
        queues[queue] = dict(tier=tier, rank=division, leaguePoints=values[0],
                             wins=values[1], losses=values[2])
    return {"observed_at": observed_at, "queues": queues}


def load_rank_snapshot(raw):
    try:
        data = json.loads(raw)
        # Revalidate persisted state rather than trusting old/corrupt JSON.
        observed_at = float(data["observed_at"])
        if not math.isfinite(observed_at) or observed_at < 0:
            return None
        entries = [dict(entry, queueType=queue) for queue, entry in data["queues"].items()]
        return rank_snapshot(entries, observed_at)
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def _rank_points(entry):
    tier = entry["tier"]
    # Apex tiers share one LP ladder; GM/Challenger are not extra divisions.
    if tier in APEX_TIERS:
        return TIER_ORDER["MASTER"] * 400 + entry["leaguePoints"]
    return TIER_ORDER[tier] * 400 + DIVISION_ORDER[entry["rank"]] * 100 + entry["leaguePoints"]


def format_recap_rank(previous, current, *, queue_id, end_ts, won, queue_match_count,
                      history_complete, confirmed):
    queue = QUEUE_TYPES.get(queue_id)
    if queue is None:
        return None
    entry = (current or {}).get("queues", {}).get(queue)
    if entry is None:
        return "Rank N/A LP N/A"
    rank = format_rank(entry)
    old = (previous or {}).get("queues", {}).get(queue)
    delta = None
    if (old and confirmed and history_complete and queue_match_count == 1
            and previous["observed_at"] < end_ts):
        wins = entry["wins"] - old["wins"]
        losses = entry["losses"] - old["losses"]
        # Counter resets, missed games and mismatched results are not attributable.
        if (wins, losses) == ((1, 0) if won else (0, 1)):
            change = _rank_points(entry) - _rank_points(old)
            # Zero may be a partially updated rank response. Do not call it 0 LP.
            if (won and change > 0) or (not won and change < 0):
                delta = change
    suffix = "N/A" if delta is None else f"{delta:+d}LP"
    return f"{rank} | {suffix} \u2192 {entry['leaguePoints']}LP"

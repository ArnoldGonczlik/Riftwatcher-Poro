import json

import pytest

from src.recap_rank import format_recap_rank, load_rank_snapshot, rank_snapshot


def snapshot(lp=50, wins=10, losses=5, tier="GOLD", division="II", queue="RANKED_SOLO_5x5", at=100):
    return rank_snapshot([dict(queueType=queue, tier=tier, rank=division,
                               leaguePoints=lp, wins=wins, losses=losses)], at)


def render(old, new, **overrides):
    args = dict(queue_id=420, end_ts=200, won=True, queue_match_count=1,
                history_complete=True, confirmed=True)
    args.update(overrides)
    return format_recap_rank(old, new, **args)


@pytest.mark.parametrize("old,new,won,expected", [
    (snapshot(), snapshot(lp=74, wins=11), True, "+24LP"),
    (snapshot(), snapshot(lp=32, losses=6), False, "-18LP"),
    (snapshot(lp=90), snapshot(lp=14, wins=11, division="I"), True, "+24LP"),
    (snapshot(lp=90, division="I"), snapshot(lp=14, wins=11, tier="PLATINUM", division="IV"), True, "+24LP"),
    (snapshot(lp=5), snapshot(lp=75, losses=6, division="III"), False, "-30LP"),
    (snapshot(lp=90, tier="DIAMOND", division="I"), snapshot(lp=14, wins=11, tier="MASTER", division="I"), True, "+24LP"),
    (snapshot(lp=500, tier="MASTER", division="I"), snapshot(lp=524, wins=11, tier="GRANDMASTER", division="I"), True, "+24LP"),
    (snapshot(lp=800, tier="CHALLENGER", division="I"), snapshot(lp=780, losses=6, tier="GRANDMASTER", division="I"), False, "-20LP"),
])
def test_observed_changes_across_rank_boundaries(old, new, won, expected):
    line = render(old, new, won=won)
    assert expected in line
    assert "\n" not in line


@pytest.mark.parametrize("old,new,overrides", [
    (None, snapshot(), {}),
    (snapshot(), None, {}),
    (snapshot(), snapshot(lp=98, wins=12), {}),
    (snapshot(), snapshot(lp=74, wins=11), {"queue_match_count": 2}),
    (snapshot(), snapshot(lp=74, wins=11), {"history_complete": False}),
    (snapshot(), snapshot(lp=74, wins=11), {"confirmed": False}),
    (snapshot(), snapshot(lp=74, wins=11), {"end_ts": 100}),
    (snapshot(), snapshot(lp=74, wins=0, losses=0), {}),
    (snapshot(), snapshot(lp=74), {}),
    (snapshot(), snapshot(lp=50, wins=11), {}),
    (snapshot(), snapshot(lp=32, wins=11), {}),
    (snapshot(), snapshot(lp=74, losses=6), {}),
    (snapshot(), snapshot(lp=74, wins=11, queue="RANKED_FLEX_SR"), {}),
])
def test_ambiguous_changes_are_not_attributed(old, new, overrides):
    assert "N/A" in render(old, new, **overrides)


def test_flex_and_unranked_queues():
    old = snapshot(queue="RANKED_FLEX_SR")
    new = snapshot(lp=74, wins=11, queue="RANKED_FLEX_SR")
    assert "+24LP" in render(old, new, queue_id=440)
    assert render(old, new, queue_id=450) is None
    assert render(old, new, queue_id=1700) is None


@pytest.mark.parametrize("raw", [None, "garbage", "[]", "null", "{}",
    '{"observed_at": "nan", "queues": {}}',
    '{"observed_at": 100, "queues": {"RANKED_SOLO_5X5": null}}'])
def test_invalid_saved_state(raw):
    assert load_rank_snapshot(raw) is None


def test_snapshot_round_trip_and_missing_fields():
    original = snapshot()
    assert load_rank_snapshot(json.dumps(original)) == original
    assert rank_snapshot([{"queueType": "RANKED_SOLO_5x5", "tier": "GOLD", "rank": "II"}], 100)["queues"] == {}


@pytest.mark.parametrize("entries", [None, {}, [None]])
def test_invalid_api_payload(entries):
    with pytest.raises(ValueError):
        rank_snapshot(entries, 100)

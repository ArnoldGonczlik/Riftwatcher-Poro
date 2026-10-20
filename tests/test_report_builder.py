from src.services.report_builder import (
    BADGE_MIN_GAMES_WEEKLY,
    BADGE_PRIOR_GAMES_WEEKLY,
    GRINDER_BADGE,
    get_leader_badges_by_player,
)

DAMAGE_BADGE = "\U0001F4A5"
KILLS_BADGE = "\U0001F5E1\uFE0F"


def _entry(name, games, minutes, **totals):
    wins = games // 2
    losses = games - wins
    performance = {"minutes_total": float(minutes), **totals}
    return (name, {}, wins, losses, 0.0, performance, 0.0)


def test_high_rate_player_beats_high_volume_player():
    ranked_results = [
        _entry("Grinder", 10, 300, player_damage=200_000),  # ~667 dmg/min
        _entry("Ace", 2, 50, player_damage=80_000),  # 1600 dmg/min
    ]

    badges = get_leader_badges_by_player(ranked_results)

    assert DAMAGE_BADGE in badges["Ace"]
    assert DAMAGE_BADGE not in badges["Grinder"]
    assert GRINDER_BADGE in badges["Grinder"]
    assert GRINDER_BADGE not in badges["Ace"]


def test_players_below_min_games_get_no_badges():
    ranked_results = [
        _entry("OneGame", 1, 25, player_damage=60_000, kills=20),
        _entry("Regular", 3, 90, player_damage=60_000, kills=10),
    ]

    badges = get_leader_badges_by_player(ranked_results)

    assert badges["OneGame"] == []
    assert DAMAGE_BADGE in badges["Regular"]
    assert KILLS_BADGE in badges["Regular"]


def test_weekly_min_games_is_stricter():
    ranked_results = [
        _entry("TwoGames", 2, 50, player_damage=100_000),
        _entry("ThreeGames", 3, 90, player_damage=60_000),
    ]

    daily = get_leader_badges_by_player(ranked_results)
    weekly = get_leader_badges_by_player(
        ranked_results,
        min_games=BADGE_MIN_GAMES_WEEKLY,
        prior_games=BADGE_PRIOR_GAMES_WEEKLY,
    )

    assert DAMAGE_BADGE in daily["TwoGames"]
    assert weekly["TwoGames"] == []
    assert DAMAGE_BADGE in weekly["ThreeGames"]


def test_shrinkage_favors_slightly_lower_rate_over_bigger_sample():
    # Ace has the best raw rate (1000/min) over 2 games; Steady is close (900/min)
    # over 10 games. A low-damage third player pulls the group average down, so the
    # small sample is shrunk harder and Steady takes the badge.
    ranked_results = [
        _entry("Ace", 2, 60, player_damage=60_000),
        _entry("Steady", 10, 300, player_damage=270_000),
        _entry("Low", 10, 300, player_damage=90_000),
    ]

    badges = get_leader_badges_by_player(ranked_results)
    assert DAMAGE_BADGE in badges["Steady"]
    assert DAMAGE_BADGE not in badges["Ace"]

    no_shrink = get_leader_badges_by_player(ranked_results, prior_games=0)
    assert DAMAGE_BADGE in no_shrink["Ace"]


def test_no_eligible_players_returns_empty_badges():
    ranked_results = [
        _entry("A", 1, 30, player_damage=10_000),
        _entry("B", 1, 30, player_damage=20_000),
    ]

    assert get_leader_badges_by_player(ranked_results) == {"A": [], "B": []}


def test_players_without_minutes_skip_rate_badges_but_can_grind():
    ranked_results = [
        _entry("NoMinutes", 4, 0, player_damage=50_000),
        _entry("Timed", 2, 60, player_damage=30_000),
    ]

    badges = get_leader_badges_by_player(ranked_results)

    assert badges["NoMinutes"] == [GRINDER_BADGE]
    assert DAMAGE_BADGE in badges["Timed"]

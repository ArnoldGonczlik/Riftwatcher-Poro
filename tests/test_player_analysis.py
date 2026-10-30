"""!analyze: guarded rule-based findings, the compact card, and the command handler."""

import asyncio
import contextvars
from datetime import datetime, timedelta, timezone

import discord
import pytest

from src.discord_command_handlers import handle_incoming_message
from src.player_analysis import (
    ANALYSIS_ACCENT,
    analysis_start,
    analyze_player,
    build_analysis_view,
    format_analysis_sections,
    format_analysis_text,
    parse_analyze_args,
    resolve_tracked_player,
)
from tests.discord_fakes import view_text

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
UTC = timezone.utc


@pytest.fixture(autouse=True)
def freeze_analysis_command_clock(monkeypatch):
    from src.commands import analysis_handlers

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(analysis_handlers, "datetime", FixedDatetime)


@pytest.mark.parametrize("raw, expected", [
    ("Player 2", ("Player 2", "solo", 1)),
    ("Player 2 3", ("Player 2", "solo", 3)),
    ("Player 2 flex 3", ("Player 2", "flex", 3)),
    ("Player 2 3 flex", ("Player 2", "flex", 3)),
    ("Player 2#EUW 3", ("Player 2#EUW", "solo", 3)),
])
def test_numeric_player_names_are_preserved(raw, expected):
    assert parse_analyze_args(raw, tracked=[("Player 2#EUW", "p2")]) == expected


def test_numeric_name_command_uses_selected_months_in_database_query():
    channel, calls = run_command("!analyze Player 2 3 flex", tracked=[("Player 2#EUW", "p2")])
    assert calls == [("rows", "p2", analysis_start(NOW, 3)), ("totals", 440)]
    assert "Player 2#EUW" in channel.sent[0] and "Last 3 months" in channel.sent[0]


def test_ambiguous_numeric_names_require_tag_instead_of_selecting_another_player():
    tracked = [("Player 2#EUW", "p2"), ("Player 2#NA1", "p2na"), ("Player#EUW", "p")]
    channel, calls = run_command("!analyze Player 2", tracked=tracked)
    assert calls == [] and "Several tracked players" in channel.sent[0]


def test_lane_cs_at_ten_excludes_games_ending_before_ten_minutes():
    from src.player_analysis import _metric_findings

    rows = [dict(game(29 - i / 2, True, role="MIDDLE"),
                 game_end_at=NOW - timedelta(hours=(40 - i) * 12),
                 deaths=0, lane_cs_10=90 if i < 20 else 20,
                 duration_seconds=1800 if i < 20 else 480) for i in range(40)]
    assert _metric_findings(rows) == ([], [])


def test_analysis_computation_errors_get_safe_reply(monkeypatch):
    from src.commands import analysis_handlers

    def fail_analysis(**kwargs):
        raise RuntimeError("private database details")

    monkeypatch.setattr(analysis_handlers, "analyze_player", fail_analysis)
    logs = []
    channel, _ = run_command("!analyze NoxVain", logs=logs)
    assert channel.sent[0].startswith("Analysis failed:")
    assert "private database details" not in channel.sent[0]
    assert "private database details" in logs[-1]


def test_long_analysis_plain_text_fallback_is_split_without_losing_text(monkeypatch):
    from src.commands import analysis_handlers

    text = "A full finding with its evidence.\n" * 100
    monkeypatch.setattr(analysis_handlers, "format_analysis_text", lambda _analysis: text)
    channel, _ = run_command("!analyze NoxVain", channel=FakeChannel(reject_views=True))
    assert len(channel.sent) > 1
    assert "".join(channel.sent) == text


@pytest.mark.parametrize("raw, expected", [
    ("Not a snake 3", ("Not a snake", "solo", 3)),
    ("Name#EUW 3 FLEX", ("Name#EUW", "flex", 3)),
    ("Name#EUW flex 12", ("Name#EUW", "flex", 12)),
])
def test_months_and_queue_suffixes(raw, expected):
    assert parse_analyze_args(raw) == expected


@pytest.mark.parametrize("suffix", ["0", "13", "-1", "+3", "²", "1.5", "3 4", "solo flex"])
def test_invalid_analysis_arguments_fail_before_database(suffix):
    channel, calls = run_command(f"!analyze NoxVain {suffix}")
    assert calls == []
    assert "month" in channel.sent[0]


def test_calendar_window_clamps_month_end_and_leap_year():
    from src.player_analysis import analysis_start

    for year, day in ((2024, 29), (2025, 28)):
        now = datetime(year, 3, 31, 12, tzinfo=UTC)
        assert analysis_start(now) == datetime(year, 2, day, 12, tzinfo=UTC)
    assert analysis_start(datetime(2026, 1, 31, tzinfo=UTC), 3) == datetime(2025, 10, 31, tzinfo=UTC)


def test_default_month_filters_old_future_and_boundary_games():
    from src.player_analysis import analysis_start

    rows = [game(1, True), game(29, False), game(60, True), game(-1, True)]
    rows.append(dict(game(1, True), game_end_at=analysis_start(NOW)))
    kwargs = dict(riot_id="Nox#EUW", games=rows, queue="solo", role_baselines={},
                  names_by_puuid={}, now=NOW, tz=UTC, day_start_hour=6)
    monthly = analyze_player(**kwargs)
    quarterly = analyze_player(**kwargs, months=3)
    assert (monthly.games, monthly.wins, monthly.months) == (2, 1, 1)
    assert quarterly.games == 4


def test_last_twenty_summary_uses_selected_queue_and_latest_games():
    rows = [dict(game(29 - i, i >= 10, role="MIDDLE"), cs=180, kill_participation=0.65) for i in range(30)]
    rows += [dict(game(1, False, queue=440), cs=0)]
    report = analyze(rows)
    sections = format_analysis_sections(report)
    assert sections[1].startswith("**Last 20 games")
    assert "20W-0L" in sections[1]
    assert "CS/min **6.0**" in sections[1] and "KP **65%**" in sections[1]
    assert sections[2].startswith("**Strengths**")


def measured_games(role="MIDDLE", count=40):
    return [dict(game(29 - i / 2, i % 2 == 0, role=role),
                 game_end_at=NOW - timedelta(hours=(40 - i) * 12), cs=240 if i < 20 else 150,
                 vision_score=30 if i < 20 else 9, deaths=2 if i < 20 else 5,
                 kill_participation=0.7 if i < 20 else 0.4,
                 dragon_takedowns=3 if i < 20 else 1, baron_takedowns=0, herald_takedowns=0)
            for i in range(count)]


def test_multiple_evidence_backed_work_on_findings_without_fillers():
    report = analyze(measured_games())
    assert len(report.tips) == 3
    assert all("earlier 20 vs recent 20 games" in text for text in report.tips)
    assert report.strengths == []
    assert any("CS/min" in text for text in report.tips)
    assert any("vision/min" in text for text in report.tips)


def test_improving_metrics_create_strengths():
    rows = measured_games()
    for row in rows:
        row["game_end_at"] = NOW - (row["game_end_at"] - (NOW - timedelta(days=29)))
    report = analyze(rows)
    assert len(report.strengths) == 3
    assert report.tips == []


def test_missing_or_mixed_context_metrics_do_not_invent_weaknesses():
    rows = measured_games()
    for row in rows[20:]:
        row["champion_name"] = "Ahri"
    assert analyze(rows).tips == []
    for row in rows:
        row["champion_name"] = "Lux"
        for key in ("cs", "vision_score", "kill_participation"):
            row[key] = None
        row["deaths"] = 3
    report = analyze(rows)
    assert report.tips == [] and report.strengths == []
    summary = format_analysis_sections(report)[1]
    assert "CS/min **N/A**" in summary and "KP **N/A**" in summary
    assert "CS 0/20" in summary


def test_jungle_objectives_supported_but_support_never_gets_farming_advice():
    from src.player_analysis import _metric_findings

    _, tips = _metric_findings(measured_games(role="JUNGLE"))
    assert any("epic objective takedowns/game" in f.text for f in tips)
    _, tips = _metric_findings(measured_games(role="UTILITY"))
    assert not any("CS/min" in f.text or "CS at 10" in f.text for f in tips)


def test_fewer_than_ten_measured_games_per_side_gives_no_metric_findings():
    from src.player_analysis import _metric_findings

    assert _metric_findings(measured_games(count=19)) == ([], [])


def test_noisy_or_tiny_metric_changes_do_not_become_advice():
    from src.player_analysis import _metric_findings

    rows = measured_games()
    for i, row in enumerate(rows):
        row.update(deaths=3, vision_score=None, kill_participation=None,
                   cs=(0 if i % 2 else 600) + (30 if i >= 20 else 0))
    assert _metric_findings(rows) == ([], [])
    for i, row in enumerate(rows):
        row["cs"] = 180 + (3 if i >= 20 else 0)
    assert _metric_findings(rows) == ([], [])


def test_role_changes_do_not_create_metric_trends():
    from src.player_analysis import _metric_findings

    rows = measured_games()
    for row in rows[20:]:
        row["team_position"] = "UTILITY"
    assert _metric_findings(rows) == ([], [])


def test_new_monthly_trend_works_without_ninety_days_of_history():
    rows = [game(29 - i / 2, i >= 20) for i in range(40)]
    assert any("latest 20 games" in line for line in analyze(rows).strengths)


def test_manual_channel_blocks_automatic_channel_before_queries():
    blocked, calls = run_command("!analyze NoxVain 3", manual_channel_id=888)
    assert calls == [] and blocked.sent == ["Use `!analyze` in <#888>."]
    allowed, calls = run_command("!analyze NoxVain 3", channel=FakeChannel(888), manual_channel_id=888)
    assert len(calls) == 2 and "Last 3 months" in allowed.sent[0]


def game(days_ago, win, *, champion="Lux", role="UTILITY", queue=420, kills=2, deaths=3, assists=8,
         minutes=30, gold_diff=0, cs_diff=0, teammates=(), hour=12):
    end = (NOW - timedelta(days=days_ago)).replace(hour=hour)
    return {
        "queue_id": queue,
        "game_end_at": end,
        "duration_seconds": minutes * 60,
        "champion_name": champion,
        "team_position": role,
        "win": win,
        "kills": kills,
        "deaths": deaths,
        "assists": assists,
        "opponent_gold_diff": gold_diff,
        "opponent_cs_diff": cs_diff,
        "tracked_teammates": list(teammates),
    }


def games_with(wins, losses, **kwargs):
    """`wins + losses` games spread one per day (oldest first), alternating results evenly."""
    total = wins + losses
    results = [i * wins // total != (i + 1) * wins // total for i in range(total)]
    start = kwargs.pop("start_days_ago", 300)
    return [game(start - i, result, **kwargs) for i, result in enumerate(results)]


def analyze(games, *, queue="solo", baselines=None, names=None):
    return analyze_player(
        riot_id="NoxVain#EUW",
        games=games,
        queue=queue,
        role_baselines=baselines or {},
        names_by_puuid=names or {},
        now=NOW,
        tz=UTC,
        day_start_hour=6,
        months=12,
    )


def test_parse_analyze_args_takes_optional_queue_word():
    assert parse_analyze_args("Not a snake flex") == ("Not a snake", "flex", 1)
    assert parse_analyze_args("NoxVain#EUW SOLO") == ("NoxVain#EUW", "solo", 1)
    assert parse_analyze_args("NoxVain") == ("NoxVain", "solo", 1)


TRACKED = [("NoxVain#EUW", "nox"), ("Rayke#EUW", "rayke"), ("Rayke#NA1", "rayke-na")]


@pytest.mark.parametrize("query, expected", [
    ("noxvain#euw", ("NoxVain#EUW", "nox")),
    ("NOXVAIN", ("NoxVain#EUW", "nox")),
    ("Rayke#NA1", ("Rayke#NA1", "rayke-na")),
])
def test_resolve_tracked_player_by_riot_id_or_unique_name(query, expected):
    assert resolve_tracked_player(query, TRACKED) == (expected, None)


def test_resolve_tracked_player_errors():
    assert resolve_tracked_player("Rayke", TRACKED)[1] == "Several tracked players are called `Rayke`; add the `#Tag`."
    assert resolve_tracked_player("Some`one", TRACKED)[1] == "`Some'one` isn't a tracked player."
    assert resolve_tracked_player("  ", TRACKED)[0] is None


def test_too_few_games_gives_no_findings_and_points_to_other_queue():
    games = games_with(5, 5) + games_with(12, 13, queue=440)

    analysis = analyze(games)
    sections = format_analysis_sections(analysis)

    assert not analysis.enough_games and analysis.strengths == [] and analysis.tips == []
    assert len(sections) == 3
    assert "Not enough Solo/Duo games to analyze yet: 10 in the last 12 months, 20 needed." in sections[2]
    assert "`!analyze NoxVain#EUW 12 flex`" in sections[2]


def test_best_and_weakest_champions_need_ten_games_and_a_clear_gap():
    games = (
        games_with(14, 6, champion="Zyra", start_days_ago=300)
        + games_with(3, 9, champion="Lux", start_days_ago=270)
        + games_with(10, 10, champion="Seraphine", start_days_ago=250)
        + games_with(5, 0, champion="Rell", start_days_ago=220)  # too few games to judge
    )

    analysis = analyze(games)

    assert "**Zyra** 14/20 (70%): your best pick" in analysis.strengths
    assert "**Lux** 3/12 (25%): your weakest pick" in analysis.tips
    assert not any("Seraphine" in line or "Rell" in line for line in analysis.strengths + analysis.tips)


def test_off_roles_tip_names_records_and_lane_gold():
    games = (
        games_with(30, 20, role="UTILITY", champion="A", start_days_ago=300)
        + games_with(4, 8, role="JUNGLE", champion="B", gold_diff=-3000, start_days_ago=240)
        + games_with(5, 7, role="TOP", champion="C", gold_diff=-2000, start_days_ago=220)
    )

    analysis = analyze(games)

    assert "Stay on support: 60% in 50 games; Jungle 4/12, Top 5/12, ~2.5k gold behind your role opponent at game end" in analysis.tips


def test_trend_down_mentions_rising_kda_and_trend_up_is_a_strength():
    older = games_with(30, 20, champion="A", start_days_ago=300, deaths=4)
    recent_down = games_with(8, 17, champion="B", start_days_ago=80, deaths=2)
    recent_up = games_with(20, 5, champion="B", start_days_ago=80)

    down = analyze(older + recent_down)
    up = analyze(older + recent_up)

    assert "Last 90 days 32% (25), 60% before, even though KDA rose 2.50 → 5.00" in down.tips
    assert "Trending up: 80% in the last 90 days (25), 60% before" in up.strengths


def test_tilt_tip_and_no_tilt_strength():
    tilted = []
    steady = []
    pairs = [(True, True), (True, False), (False, True), (False, False)]
    for day in range(40):
        # Loss then a quick follow-up game that is mostly lost; win then mostly won.
        tilted += [game(300 - day, False, hour=12), game(300 - day, day % 4 == 0, hour=13)]
        tilted += [game(300 - day, True, hour=18), game(300 - day, True, hour=19)]
        # The second game's result doesn't depend on the first one's.
        first, second = pairs[day % 4]
        steady += [game(300 - day, first, hour=12), game(300 - day, second, hour=13)]

    assert any(line.startswith("Consider a break after losses: next-game wins 25%") for line in analyze(tilted).tips)
    assert any(line.startswith("Steady after losses:") for line in analyze(steady).strengths)


def test_long_sessions_tip():
    games = []
    for day in range(25):
        games += [game(300 - day, True, hour=10), game(300 - day, True, hour=12), game(300 - day, day % 2 == 0, hour=14)]
        games += [game(300 - day, False, hour=16), game(300 - day, day % 5 == 0, hour=18)]

    tips = analyze(games).tips

    assert "Consider shorter sessions: 10% from game 4 on (50 games), 84% before (75)" in tips


def test_only_the_best_duo_partner_is_named():
    games = (
        games_with(20, 5, teammates=["snake"], champion="A", start_days_ago=300)
        + games_with(5, 20, teammates=["gizhmo"], champion="A", start_days_ago=260)
        + games_with(10, 10, champion="A", start_days_ago=220)
    )
    names = {"snake": "Not a snake#EUW", "gizhmo": "gizhmo#EUW"}

    analysis = analyze(games, names=names)
    text = format_analysis_text(analysis)

    assert "Duo with **Not a snake**: 20/25 (80%)" in analysis.strengths
    assert "gizhmo" not in text


def test_role_kda_and_deaths_compare_against_group_baseline():
    baselines = {"UTILITY": {"games": 400, "kills": 800, "deaths": 1200, "assists": 2800, "minutes": 12000}}
    strong = games_with(15, 15, kills=3, deaths=2, assists=12)
    feeding = games_with(15, 15, kills=1, deaths=6, assists=8)
    small_group = {"UTILITY": {**baselines["UTILITY"], "games": 30}}

    assert "Support KDA 7.50, against 3.00 for the group's supports" in analyze(strong, baselines=baselines).strengths
    assert "You die more than the group's supports: 2.0 vs 1.0 deaths per 10 min" in analyze(feeding, baselines=baselines).tips
    assert analyze(strong, baselines=small_group).strengths == []


def test_laning_slip_only_for_laners():
    adc = games_with(25, 25, role="BOTTOM", cs_diff=8, start_days_ago=300) + games_with(12, 13, role="BOTTOM", cs_diff=-6, start_days_ago=80)
    support = [dict(g, team_position="UTILITY") for g in adc]

    assert "End-game CS gap slipped: -6 CS vs your opponent in the last 90 days (+8 before)" in analyze(adc).tips
    assert not any("End-game CS" in line for line in analyze(support).tips)


def test_at_most_three_findings_strongest_first():
    games = []
    for index, (wins, losses) in enumerate([(10, 0), (9, 1), (8, 2), (8, 2)]):
        games += games_with(wins, losses, champion=f"Good{index}", start_days_ago=300 - 20 * index)
    games += games_with(10, 30, champion="Filler", start_days_ago=200)

    analysis = analyze(games)

    assert len(analysis.strengths) <= 3 and len(analysis.tips) <= 3
    assert analysis.strengths[0].startswith("**Good0** 10/10 (100%) and **Good1** 9/10 (90%)")


def test_sections_header_footer_and_escaping():
    games = games_with(12, 10) + games_with(30, 25, queue=440)

    analysis = analyze_player(
        riot_id="xX_snake_Xx#EUW", games=games, queue="solo", role_baselines={}, names_by_puuid={},
        now=NOW, tz=UTC, day_start_hour=6, months=12,
    )
    header, recent, strengths, tips, footer = format_analysis_sections(analysis)

    assert header == (
        "**xX\\_snake\\_Xx#EUW** · Solo/Duo analysis\n"
        "-# Last 12 months · Solo/Duo 12W-10L (55%) · Flex 30W-25L (55%)"
    )
    assert strengths == "**Strengths**\nNothing stands out yet."
    assert tips == "**Work on**\nNo clear weak spots in these games."
    assert footer.startswith("-# Solo/Duo games since 31 Dec 2025 · patterns need 10+ games per comparison; observations, not causes · ")
    assert footer.endswith("`!analyze xX_snake_Xx#EUW 12 flex` for Flex")


def test_header_without_other_queue_games():
    analysis = analyze(games_with(12, 10), queue="flex")

    assert format_analysis_sections(analysis)[0].endswith("-# Last 12 months · no Flex games · Solo/Duo 12W-10L (55%)")


def test_build_analysis_view_is_one_accented_container():
    analysis = analyze(games_with(14, 6, champion="Zyra") + games_with(3, 9, champion="Lux", start_days_ago=200))

    view = build_analysis_view(analysis)

    assert len(view.children) == 1
    container = view.children[0]
    assert getattr(container.accent_colour, "value", container.accent_colour) == ANALYSIS_ACCENT
    assert view_text(view) == format_analysis_text(analysis)
    assert sum(1 for item in container.walk_children() if isinstance(item, discord.ui.Separator)) == 4


class FakeChannel:
    def __init__(self, channel_id=999, reject_views=False):
        self.id = channel_id
        self.reject_views = reject_views
        self.sent = []

    async def send(self, content=None, *, view=None, tts=False):
        if content is not None and len(content) > 2000:
            raise ValueError("Discord content limit exceeded")
        if view is not None and self.reject_views:
            raise discord.HTTPException(type("Response", (), {"status": 400, "reason": "Bad"})(), "bad card")
        self.sent.append(view_text(view) if view is not None else content)


class FakeAuthor:
    bot = False
    id = 1
    guild_permissions = None
    roles = []


class FakeMessage:
    def __init__(self, content, channel):
        self.content = content
        self.channel = channel
        self.author = FakeAuthor()


def default_rows():
    return games_with(14, 6, champion="Zyra", start_days_ago=26) + games_with(3, 9, champion="Lux", start_days_ago=15)


def run_command(content, *, channel=None, db_enabled=True, rows=None, calls=None, fail=False, logs=None, manual_channel_id=None, tracked=None):
    channel = channel or FakeChannel()
    calls = calls if calls is not None else []

    def load_rows(puuid, since, *, until):
        assert until == NOW
        calls.append(("rows", puuid, since))
        if fail:
            raise RuntimeError("connection to server at 10.0.0.1 failed")
        return rows if rows is not None else default_rows()

    def load_totals(queue_id, since, *, until):
        assert until == NOW
        calls.append(("totals", queue_id))
        return {}

    asyncio.run(handle_incoming_message(
        message=FakeMessage(content, channel),
        channel_id=777,
        friends=[],
        riot_client=None,
        poro_service=None,
        report_timezone_name="UTC",
        report_day_start_hour=6,
        db_enabled=db_enabled,
        start_monotonic=0.0,
        daily_request_lock=asyncio.Lock(),
        request_id_context=contextvars.ContextVar("request_id", default=None),
        create_request_id=lambda _prefix: "cmd-1",
        get_or_create_report_message=None,
        remember_report_message=None,
        normalize_riot_id=lambda riot_id: riot_id,
        db_upsert_player=None,
        log=(logs.append if logs is not None else (lambda _msg: None)),
        events_channel_id=999,
        manual_commands_channel_id=manual_channel_id,
        db_load_tracked_puuids=lambda: tracked if tracked is not None else [("NoxVain#EUW", "nox"), ("Pending#EUW", None)],
        db_load_player_analysis_rows=load_rows,
        db_load_role_group_totals=load_totals,
    ))
    return channel, calls


def test_analyze_command_sends_card_for_tracked_player():
    logs = []
    channel, calls = run_command("!analyze noxvain flex", logs=logs)

    assert calls == [("rows", "nox", analysis_start(NOW)), ("totals", 440)]
    assert len(channel.sent) == 1 and channel.sent[0].startswith("**NoxVain#EUW** · Flex analysis")
    assert logs[-1] == "[analyze] Sent flex analysis for NoxVain#EUW: months=1 games=0 strengths=0 tips=0."


def test_analyse_alias_and_default_queue():
    channel, calls = run_command("!analyse NoxVain#EUW")

    assert ("totals", 420) in calls
    assert "Solo/Duo analysis" in channel.sent[0] and "**Zyra** 14/20 (70%): your best pick" in channel.sent[0]
    assert "**Lux** 3/12 (25%): your weakest pick" in channel.sent[0]


def test_analyze_falls_back_to_plain_text_when_card_is_rejected():
    logs = []
    channel, _calls = run_command("!analyze NoxVain", channel=FakeChannel(reject_views=True), logs=logs)

    assert len(channel.sent) == 1 and "Last 1 month" in channel.sent[0] and "**Strengths**" in channel.sent[0]
    assert "[analyze] Discord rejected the analysis card (HTTP 400); sending it as plain text." in logs


@pytest.mark.parametrize("content, expected", [
    ("!analyze", "Usage: `!analyze Name#Tag [months] [solo|flex]` (1 month of Solo/Duo by default)."),
    ("!analyze Pending", "`Pending` isn't a tracked player."),
    ("!analyze Nobody#EUW solo", "`Nobody#EUW` isn't a tracked player."),
])
def test_analyze_usage_and_unknown_players(content, expected):
    channel, calls = run_command(content)

    assert channel.sent == [expected] and calls == []


def test_analyze_without_database_and_on_db_errors():
    no_db, _ = run_command("!analyze NoxVain", db_enabled=False)
    failed, _ = run_command("!analyze NoxVain", fail=True)

    assert no_db.sent == ["Player analysis needs the database, which isn't configured."]
    assert failed.sent[0].startswith("Analysis failed: ")
    assert "10.0.0.1" not in failed.sent[0] and "(ref `cmd-1`)" in failed.sent[0]


def test_analyze_in_wrong_channel_points_to_events_channel():
    channel, calls = run_command("!analyze NoxVain", channel=FakeChannel(channel_id=5))

    assert channel.sent == ["Use `!analyze` in <#999>."] and calls == []


def test_analyze_routing_and_help():
    from src.commands.routing import command_channel_id, format_help_text, is_supported_command

    for content in ("!analyze", "!analyze noxvain flex", "!analyse", "!analyse noxvain"):
        assert is_supported_command(content)
        assert command_channel_id(content, daily_channel_id=1, weekly_channel_id=2, events_channel_id=3) == 3
    help_text = format_help_text(report_day_start_hour=6, daily_channel_id=1, weekly_channel_id=2, events_channel_id=3)
    assert "`!analyze Name#Tag [months] [solo|flex]`" in help_text


def test_db_analysis_queries_map_rows(monkeypatch):
    from src.db import match_stats as db_match_stats

    queries = []

    def fake_execute(query, params=None, fetch=False, fetchone=False):
        queries.append((query, params))
        if query.startswith("SELECT queue_id"):
            return [tuple(range(len(db_match_stats.ANALYSIS_ROW_COLUMNS)))]
        return [("UTILITY", 10, 20, 30, 80, 300.0)]

    monkeypatch.setattr(db_match_stats, "db_execute", fake_execute)

    rows = db_match_stats.db_load_player_analysis_rows("nox", NOW)
    totals = db_match_stats.db_load_role_group_totals(440, NOW)

    assert rows == [dict(zip(db_match_stats.ANALYSIS_ROW_COLUMNS, range(len(db_match_stats.ANALYSIS_ROW_COLUMNS))))]
    assert queries[0][1] == ("nox", [420, 440], NOW) and "NOT remake" in queries[0][0]
    assert totals == {"UTILITY": {"games": 10, "kills": 20, "deaths": 30, "assists": 80, "minutes": 300.0}}
    assert queries[1][1] == (440, NOW) and "GROUP BY team_position" in queries[1][0]


def test_database_player_and_baseline_queries_share_the_end_of_window(monkeypatch):
    from src.db import match_stats as db_match_stats

    queries = []

    def fake_execute(query, params=None, **kwargs):
        queries.append((query, params))
        return []

    monkeypatch.setattr(db_match_stats, "db_execute", fake_execute)
    start = analysis_start(NOW, 3)
    db_match_stats.db_load_player_analysis_rows("p", start, until=NOW)
    db_match_stats.db_load_role_group_totals(420, start, until=NOW)
    assert queries[0][1] == ("p", [420, 440], start, NOW)
    assert queries[1][1] == (420, start, NOW)
    assert all("AND game_end_at <= %s" in query for query, _params in queries)

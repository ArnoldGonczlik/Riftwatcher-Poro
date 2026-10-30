"""`!analyze`: a selected window of one player's ranked games turned into strengths and tips.

Pure logic over `player_match_stats` rows (see src/match_stats.py); no Discord, Riot or
DB access. Findings need minimum samples and practical gaps; metric trends also
apply a noise guard. These heuristics describe stored games, not causes of losses.
Candidates are ranked by gap size weighted by sample size, and the card shows at most
three strengths and three things to work on, each with its record.

Only the best duo partner is ever named; a weak duo is never called out in public.
"""

import calendar
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

import discord

QUEUES = {
    "solo": {"queue_id": 420, "label": "Solo/Duo"},
    "flex": {"queue_id": 440, "label": "Flex"},
}
DEFAULT_QUEUE = "solo"
DEFAULT_MONTHS = 1
MAX_ANALYSIS_MONTHS = 12
TREND_DAYS = 90

MIN_QUEUE_GAMES = 20
MIN_GROUP_GAMES = 10  # champions, off-roles
MIN_PARTNER_GAMES = 15
MIN_SPLIT_GAMES = 20  # each side of trend / tilt / session comparisons
MIN_BASELINE_GAMES = 50  # group games in a role before comparing against it

CHAMPION_GAP_PP = 8
ROLE_GAP_PP = 10
PARTNER_GAP_PP = 6
TREND_GAP_PP = 6
TILT_GAP_PP = 10
NO_TILT_MAX_GAP_PP = 4
SESSION_GAP_PP = 8
GAMES_BEFORE_LONG_SESSION = 3
NEXT_GAME_WINDOW = timedelta(minutes=70)
LANE_CS_DROP = 5
KDA_RATIO = 1.2
DEATHS_RATIO = 1.15
MAX_FINDINGS = 3

ANALYSIS_ACCENT = 0x378ADD

ROLE_NAMES = {"TOP": "Top", "JUNGLE": "Jungle", "MIDDLE": "Mid", "BOTTOM": "ADC", "UTILITY": "Support"}
ROLE_IN_SENTENCE = {"TOP": "top", "JUNGLE": "jungle", "MIDDLE": "mid", "BOTTOM": "ADC", "UTILITY": "support"}
ROLE_PLURALS = {"TOP": "top laners", "JUNGLE": "junglers", "MIDDLE": "mid laners", "BOTTOM": "ADCs", "UTILITY": "supports"}


@dataclass
class Finding:
    text: str
    score: float


@dataclass
class PlayerAnalysis:
    riot_id: str
    queue: str
    games: int
    wins: int
    other_queue_games: int
    other_queue_wins: int
    since: object = None
    months: int = DEFAULT_MONTHS
    recent: list = field(default_factory=list)
    strengths: list = field(default_factory=list)
    tips: list = field(default_factory=list)

    @property
    def enough_games(self):
        return self.games >= MIN_QUEUE_GAMES


def analysis_start(now, months=DEFAULT_MONTHS):
    """Rolling calendar months ending now, clamping short months (31 Mar -> 28 Feb)."""
    if not 1 <= months <= MAX_ANALYSIS_MONTHS:
        raise ValueError("Choose a whole number of months from 1 to 12.")
    year, month = divmod(now.year * 12 + now.month - 1 - months, 12)
    month += 1
    return now.replace(year=year, month=month, day=min(now.day, calendar.monthrange(year, month)[1]))


def _parse_analyze_suffixes(words):
    queue, months = DEFAULT_QUEUE, DEFAULT_MONTHS
    seen = set()
    while words:
        token = words[-1].casefold()
        numeric = token.lstrip("+-").replace(".", "", 1).isdigit()
        if token in QUEUES:
            kind = "queue"
        elif numeric:
            kind = "months"
        else:
            break
        if kind in seen:
            raise ValueError("Use one month count and one queue: `!analyze Name#Tag 3 flex`.")
        seen.add(kind)
        words.pop()
        if kind == "queue":
            queue = token
        else:
            if not token.isdecimal() or not 1 <= int(token) <= MAX_ANALYSIS_MONTHS:
                raise ValueError("Choose a whole number of months from 1 to 12.")
            months = int(token)
    return " ".join(words), queue, months


def parse_analyze_args(raw: str, *, tracked=()):
    """Optional months/queue suffix; a tracked name takes precedence over options.

    Longest matching name first preserves names such as "Player 2", including
    when followed by another number. Full Riot IDs disambiguate shared names.
    """
    words = raw.split()
    for suffix_length in range(min(2, len(words) - 1) + 1):
        prefix = " ".join(words[:-suffix_length] if suffix_length else words)
        wanted = prefix.casefold()
        matches_name = any(
            wanted == name.casefold()
            or ("#" not in wanted and wanted == name.split("#", 1)[0].casefold())
            for name, _puuid in tracked
        )
        if not matches_name:
            continue
        suffix = words[-suffix_length:] if suffix_length else []
        remainder, queue, months = _parse_analyze_suffixes(suffix)
        if not remainder:
            return prefix, queue, months
    return _parse_analyze_suffixes(words)


def resolve_tracked_player(query, tracked):
    """Find a tracked (riot_id, puuid) by full Riot ID or, if unique, by name alone.

    Returns (match, error_text); exactly one of them is None.
    """
    query = query.strip().replace("`", "'")
    wanted = query.casefold()
    if not wanted:
        return None, "Give a tracked player's Riot ID, like `Name#Tag`."
    exact = [entry for entry in tracked if entry[0].casefold() == wanted]
    if exact:
        return exact[0], None
    if "#" not in wanted:
        by_name = [entry for entry in tracked if entry[0].split("#", 1)[0].casefold() == wanted]
        if len(by_name) == 1:
            return by_name[0], None
        if len(by_name) > 1:
            return None, f"Several tracked players are called `{query}`; add the `#Tag`."
    return None, f"`{query}` isn't a tracked player."


def _pct(wins, games):
    return round(100 * wins / games) if games else 0


def _record(games):
    wins = sum(1 for g in games if g["win"])
    return wins, len(games)


def _weight(*sizes):
    return math.sqrt(min(sizes))


def _kda(games):
    kills = sum(g["kills"] for g in games)
    deaths = sum(g["deaths"] for g in games)
    assists = sum(g["assists"] for g in games)
    return (kills + assists) / max(1, deaths)


def _per_ten_minutes(games, key):
    minutes = sum(g["duration_seconds"] for g in games) / 60
    return 10 * sum(g[key] or 0 for g in games) / minutes if minutes else None


def _avg(games, key):
    values = [g[key] for g in games if g.get(key) is not None]
    return sum(values) / len(values) if values else None


def _bold(text):
    return f"**{discord.utils.escape_markdown(str(text))}**"


def _champion_findings(games, overall_pct):
    strengths, tips = [], []
    by_champion = defaultdict(list)
    for g in games:
        by_champion[g["champion_name"] or "?"].append(g)
    good, bad = [], []
    for champion, champ_games in by_champion.items():
        wins, n = _record(champ_games)
        if n < MIN_GROUP_GAMES:
            continue
        pct = _pct(wins, n)
        if pct >= 55 and pct >= overall_pct + CHAMPION_GAP_PP:
            good.append(((pct - overall_pct) * _weight(n), champion, wins, n, pct))
        elif pct <= 45 and pct <= overall_pct - CHAMPION_GAP_PP:
            bad.append(((overall_pct - pct) * _weight(n), champion, wins, n, pct))
    for found, target, label in ((good, strengths, "best"), (bad, tips, "weakest")):
        top = sorted(found, reverse=True)[:2]
        if not top:
            continue
        parts = [f"{_bold(champion)} {wins}/{n} ({pct}%)" for _score, champion, wins, n, pct in top]
        noun = "picks" if len(top) > 1 else "pick"
        target.append(Finding(f"{' and '.join(parts)}: your {label} {noun}", sum(t[0] for t in top) / len(top)))
    return strengths, tips


def _main_role(games):
    by_role = defaultdict(list)
    for g in games:
        if g["team_position"]:
            by_role[g["team_position"]].append(g)
    if not by_role:
        return None, by_role
    main = max(by_role, key=lambda role: len(by_role[role]))
    return main, by_role


def _role_findings(games, role_baselines, now):
    strengths, tips = [], []
    main, by_role = _main_role(games)
    if main is None or len(by_role[main]) < MIN_SPLIT_GAMES:
        return strengths, tips
    main_games = by_role[main]
    main_wins, main_n = _record(main_games)
    main_pct = _pct(main_wins, main_n)

    weak_roles = []
    for role, role_games in by_role.items():
        wins, n = _record(role_games)
        if role != main and n >= MIN_GROUP_GAMES and _pct(wins, n) <= main_pct - ROLE_GAP_PP:
            weak_roles.append((n, role, wins, role_games))
    if weak_roles:
        # Worst win rate first; more games break ties.
        weak_roles.sort(key=lambda entry: (_pct(entry[2], entry[0]), -entry[0]))
        parts = [f"{ROLE_NAMES.get(role, role)} {wins}/{n}" for n, role, wins, _games in weak_roles[:2]]
        text = f"Stay on {ROLE_IN_SENTENCE.get(main, main)}: {main_pct}% in {main_n} games; {', '.join(parts)}"
        gold = _avg([g for _n, _role, _wins, role_games in weak_roles[:2] for g in role_games], "opponent_gold_diff")
        if gold is not None and gold <= -1000:
            text += f", ~{abs(gold) / 1000:.1f}k gold behind your role opponent at game end"
        gap = main_pct - min(_pct(w, n) for n, _r, w, _g in weak_roles)
        tips.append(Finding(text, gap * _weight(sum(n for n, *_ in weak_roles))))

    base = (role_baselines or {}).get(main)
    if base and base.get("games", 0) >= MIN_BASELINE_GAMES and base.get("minutes"):
        group_kda = (base["kills"] + base["assists"]) / max(1, base["deaths"])
        my_kda = _kda(main_games)
        role_name = ROLE_NAMES.get(main, main)
        plural = ROLE_PLURALS.get(main, "players in that role")
        if group_kda > 0 and my_kda >= group_kda * KDA_RATIO:
            strengths.append(Finding(
                f"{role_name} KDA {my_kda:.2f}, against {group_kda:.2f} for the group's {plural}",
                100 * (my_kda / group_kda - 1) * _weight(main_n) / 2,
            ))
        group_deaths = 10 * base["deaths"] / base["minutes"]
        my_deaths = _per_ten_minutes(main_games, "deaths")
        if my_deaths is not None and group_deaths and my_deaths >= group_deaths * DEATHS_RATIO:
            tips.append(Finding(
                f"You die more than the group's {plural}: {my_deaths:.1f} vs {group_deaths:.1f} deaths per 10 min",
                100 * (my_deaths / group_deaths - 1) * _weight(main_n) / 2,
            ))

    if main != "UTILITY":
        cutoff = now - timedelta(days=TREND_DAYS)
        recent = [g for g in main_games if g["game_end_at"] > cutoff and g["opponent_cs_diff"] is not None]
        older = [g for g in main_games if g["game_end_at"] <= cutoff and g["opponent_cs_diff"] is not None]
        if len(recent) >= MIN_SPLIT_GAMES and len(older) >= MIN_SPLIT_GAMES:
            recent_cs, older_cs = _avg(recent, "opponent_cs_diff"), _avg(older, "opponent_cs_diff")
            if recent_cs <= older_cs - LANE_CS_DROP:
                tips.append(Finding(
                    f"End-game CS gap slipped: {recent_cs:+.0f} CS vs your opponent in the last {TREND_DAYS} days "
                    f"({older_cs:+.0f} before)",
                    (older_cs - recent_cs) * _weight(len(recent), len(older)) / 2,
                ))
    return strengths, tips


def _partner_findings(games, overall_pct, names_by_puuid):
    by_partner = defaultdict(list)
    for g in games:
        for partner in g["tracked_teammates"] or ():
            by_partner[partner].append(g)
    best = None
    for partner, partner_games in by_partner.items():
        wins, n = _record(partner_games)
        pct = _pct(wins, n)
        if n >= MIN_PARTNER_GAMES and pct >= 55 and pct >= overall_pct + PARTNER_GAP_PP:
            score = (pct - overall_pct) * _weight(n)
            if best is None or score > best[0]:
                best = (score, partner, wins, n, pct)
    if best is None:
        return []
    score, partner, wins, n, pct = best
    name = names_by_puuid.get(partner, "a friend").split("#", 1)[0]
    return [Finding(f"Duo with {_bold(name)}: {wins}/{n} ({pct}%)", score)]


def _trend_findings(games, now):
    cutoff = now - timedelta(days=TREND_DAYS)
    recent = [g for g in games if g["game_end_at"] > cutoff]
    older = [g for g in games if g["game_end_at"] <= cutoff]
    period = f"last {TREND_DAYS} days"
    if min(len(recent), len(older)) < MIN_SPLIT_GAMES:
        ordered = sorted(games, key=lambda g: g["game_end_at"], reverse=True)
        recent, older = ordered[:20], ordered[20:]
        period = "latest 20 games"
    if len(recent) < MIN_SPLIT_GAMES or len(older) < MIN_SPLIT_GAMES:
        return [], []
    recent_pct = _pct(*_record(recent))
    older_pct = _pct(*_record(older))
    # A few flipped results in small monthly samples are not a useful trend.
    gap_needed = TREND_GAP_PP
    if period == "latest 20 games":
        p, q = recent_pct / 100, older_pct / 100
        gap_needed = max(gap_needed, 200 * math.sqrt(p * (1 - p) / len(recent) + q * (1 - q) / len(older)))
    weight = _weight(len(recent), len(older))
    if recent_pct >= older_pct + gap_needed:
        return [Finding(f"Trending up: {recent_pct}% in the {period} ({len(recent)}), {older_pct}% before",
                        (recent_pct - older_pct) * weight)], []
    if recent_pct <= older_pct - gap_needed:
        text = f"{period.capitalize()} {recent_pct}% ({len(recent)}), {older_pct}% before"
        recent_kda, older_kda = _kda(recent), _kda(older)
        if recent_kda >= older_kda + 0.2:
            text += f", even though KDA rose {older_kda:.2f} → {recent_kda:.2f}"
        return [], [Finding(text, (older_pct - recent_pct) * weight)]
    return [], []


def _session_findings(games, tz, day_start_hour):
    """Tilt (the game right after a loss) and long sessions (game 4+ of a day)."""
    strengths, tips = [], []
    ordered = sorted(games, key=lambda g: g["game_end_at"])
    after_loss, after_win = [], []
    for previous, current in zip(ordered, ordered[1:]):
        if current["game_end_at"] - previous["game_end_at"] <= NEXT_GAME_WINDOW:
            (after_win if previous["win"] else after_loss).append(current)
    if len(after_loss) >= MIN_SPLIT_GAMES and len(after_win) >= MIN_SPLIT_GAMES:
        loss_pct, win_pct = _pct(*_record(after_loss)), _pct(*_record(after_win))
        weight = _weight(len(after_loss), len(after_win))
        if loss_pct <= win_pct - TILT_GAP_PP:
            tips.append(Finding(
                f"Consider a break after losses: next-game wins {loss_pct}% ({len(after_loss)} games), "
                f"vs {win_pct}% after wins ({len(after_win)})",
                (win_pct - loss_pct) * weight,
            ))
        elif abs(win_pct - loss_pct) <= NO_TILT_MAX_GAP_PP:
            strengths.append(Finding(f"Steady after losses: {loss_pct}% ({len(after_loss)} games), {win_pct}% after wins ({len(after_win)})", 2 * weight))

    by_day = defaultdict(list)
    for g in ordered:
        local = g["game_end_at"].astimezone(tz) - timedelta(hours=day_start_hour)
        by_day[local.date()].append(g)
    early = [g for day in by_day.values() for g in day[:GAMES_BEFORE_LONG_SESSION]]
    late = [g for day in by_day.values() for g in day[GAMES_BEFORE_LONG_SESSION:]]
    if len(early) >= MIN_SPLIT_GAMES and len(late) >= MIN_SPLIT_GAMES:
        early_pct, late_pct = _pct(*_record(early)), _pct(*_record(late))
        if late_pct <= early_pct - SESSION_GAP_PP:
            tips.append(Finding(
                f"Consider shorter sessions: {late_pct}% from game "
                f"{GAMES_BEFORE_LONG_SESSION + 1} on ({len(late)} games), {early_pct}% before ({len(early)})",
                (early_pct - late_pct) * _weight(len(early), len(late)),
            ))
    return strengths, tips


def _top(findings):
    return [f.text for f in sorted(findings, key=lambda f: f.score, reverse=True)[:MAX_FINDINGS]]


def _rate(game, key, per_minutes=1):
    minutes = (game.get("duration_seconds") or 0) / 60
    value = game.get(key)
    return per_minutes * value / minutes if value is not None and minutes > 0 else None


def _objectives(game):
    values = [game.get(key) for key in ("dragon_takedowns", "baron_takedowns", "herald_takedowns")]
    return sum(values) if all(value is not None for value in values) else None


def _lane_cs_at_ten(game):
    # Early surrenders can carry a partial first-ten-minutes counter.
    return game.get("lane_cs_10") if (game.get("duration_seconds") or 0) >= 600 else None


def _metric_findings(games):
    """Compare recent and earlier games on the SAME champion and role.

    Both samples need 10 measured games; require a practical gap and a gap larger
    than three standard errors. Missing measurements never turn into zeroes.
    These are descriptive signals, not diagnoses or patch-adjusted benchmarks.
    """
    strengths, tips = [], []
    groups = defaultdict(list)
    for game in games:
        if game.get("champion_name") and game.get("team_position") in ROLE_NAMES:
            groups[game["champion_name"], game["team_position"]].append(game)
    # label, measurement, minimum gap, higher is better, roles, action
    metrics = (
        ("CS/min", lambda g: _rate(g, "cs"), 0.7, True, {"TOP", "MIDDLE", "BOTTOM", "JUNGLE"}, "Review missed farm and rotations"),
        ("lane CS at 10 min", _lane_cs_at_ten, 8, True, {"TOP", "MIDDLE", "BOTTOM"}, "Review early last-hitting and recalls"),
        ("deaths/10 min", lambda g: _rate(g, "deaths", per_minutes=10), 0.5, False, set(ROLE_NAMES), "Review deaths before objectives"),
        ("kill participation %", lambda g: None if g.get("kill_participation") is None else 100 * g["kill_participation"], 10, True, set(ROLE_NAMES), "Review missed team fights and rotations"),
        ("vision/min", lambda g: _rate(g, "vision_score"), 0.2, True, set(ROLE_NAMES), "Review vision setup before objectives"),
        ("wards cleared/10 min", lambda g: _rate(g, "wards_killed", per_minutes=10), 0.5, True, {"UTILITY", "JUNGLE"}, "Review sweeper use and control wards"),
        ("epic objective takedowns/game", _objectives, 0.5, True, {"JUNGLE"}, "Review objective timing and team setup"),
    )
    for (champion, role), group in sorted(groups.items()):
        ordered = sorted(group, key=lambda g: g["game_end_at"], reverse=True)
        split = min(20, len(ordered) // 2)
        if split < 10:
            continue
        for label, measure, minimum_gap, higher_good, roles, action in metrics:
            if role not in roles:
                continue
            recent = [v for g in ordered[:split] if (v := measure(g)) is not None and math.isfinite(v)]
            earlier = [v for g in ordered[split:split + 50] if (v := measure(g)) is not None and math.isfinite(v)]
            if min(len(recent), len(earlier)) < 10:
                continue
            new, old = statistics.mean(recent), statistics.mean(earlier)
            gap = new - old
            noise = math.sqrt(statistics.variance(recent) / len(recent) + statistics.variance(earlier) / len(earlier))
            if abs(gap) < max(minimum_gap, abs(old) * 0.15, 3 * noise):
                continue
            improved = (gap > 0) == higher_good
            context = f"{_bold(champion)} {ROLE_NAMES[role]}"
            text = f"{context}: {label} {old:.1f} → {new:.1f} (earlier {len(earlier)} vs recent {len(recent)} games)"
            if not improved:
                text += f". {action}"
            finding = Finding(text, 20 * abs(gap) / minimum_gap * _weight(len(recent), len(earlier)))
            (strengths if improved else tips).append(finding)
    return strengths, tips


def _recent_summary(games):
    if not games:
        return "**Last 20 games · selected queue and period**\nNo games stored in this window."
    wins, count = _record(games)
    farm = [g for g in games if g.get("cs") is not None and (g.get("duration_seconds") or 0) > 0]
    minutes = sum(g["duration_seconds"] for g in farm) / 60
    cs = f"{sum(g['cs'] for g in farm) / minutes:.1f}" if minutes else "N/A"
    kp = [g["kill_participation"] for g in games if g.get("kill_participation") is not None]
    participation = f"{100 * statistics.mean(kp):.0f}%" if kp else "N/A"
    coverage = ""
    if len(farm) != count or len(kp) != count:
        coverage = f"\n-# Measured: CS {len(farm)}/{count} · KP {len(kp)}/{count}"
    return (
        f"**Last {count} games · selected queue and period**\n"
        f"{wins}W-{count - wins}L ({_pct(wins, count)}%) · CS/min **{cs}** · "
        f"KDA **{_kda(games):.2f}** · KP **{participation}**{coverage}"
    )


def analyze_player(*, riot_id, games, queue, role_baselines, names_by_puuid, now, tz, day_start_hour, months=DEFAULT_MONTHS):
    """Build the analysis for one queue from the player's ranked rows (both queues, no remakes)."""
    since = analysis_start(now, months)
    games = [g for g in games if since < g["game_end_at"] <= now]
    queue_id = QUEUES[queue]["queue_id"]
    queue_games = [g for g in games if g["queue_id"] == queue_id]
    other_games = [g for g in games if g["queue_id"] != queue_id]
    wins, n = _record(queue_games)
    other_wins, other_n = _record(other_games)
    analysis = PlayerAnalysis(
        riot_id=riot_id,
        queue=queue,
        months=months,
        recent=sorted(queue_games, key=lambda g: g["game_end_at"], reverse=True)[:20],
        games=n,
        wins=wins,
        other_queue_games=other_n,
        other_queue_wins=other_wins,
        since=min((g["game_end_at"] for g in queue_games), default=None),
    )
    if not analysis.enough_games:
        return analysis
    overall_pct = _pct(wins, n)
    strengths, tips = [], []
    for found_strengths, found_tips in (
        _metric_findings(queue_games),
        _champion_findings(queue_games, overall_pct),
        _role_findings(queue_games, role_baselines, now),
        _trend_findings(queue_games, now),
        _session_findings(queue_games, tz, day_start_hour),
    ):
        strengths.extend(found_strengths)
        tips.extend(found_tips)
    strengths.extend(_partner_findings(queue_games, overall_pct, names_by_puuid))
    analysis.strengths = _top(strengths)
    analysis.tips = _top(tips)
    return analysis


def _queue_record(label, wins, games):
    if not games:
        return f"no {label} games"
    return f"{label} {wins}W-{games - wins}L ({_pct(wins, games)}%)"


def format_analysis_sections(analysis):
    """Text blocks for the card (header, strengths, tips, footer); also joined for plain text."""
    label = QUEUES[analysis.queue]["label"]
    other = "flex" if analysis.queue == "solo" else "solo"
    other_label = QUEUES[other]["label"]
    name = discord.utils.escape_markdown(analysis.riot_id)
    records = [_queue_record(label, analysis.wins, analysis.games)]
    if analysis.other_queue_games:
        records.append(_queue_record(other_label, analysis.other_queue_wins, analysis.other_queue_games))
    period = f"last {analysis.months} month{'s' if analysis.months != 1 else ''}"
    summary = _recent_summary(analysis.recent)
    header = f"**{name}** · {label} analysis\n-# {period.capitalize()} · {' · '.join(records)}"
    # Inline code needs literal underscores; use the tag to avoid ambiguous names.
    command_name = analysis.riot_id.replace("`", "'")
    if not analysis.enough_games:
        body = (
            f"Not enough {label} games to analyze yet: {analysis.games} in the {period}, "
            f"{MIN_QUEUE_GAMES} needed."
        )
        if analysis.other_queue_games >= MIN_QUEUE_GAMES:
            body += f" Try `!analyze {command_name} {analysis.months} {other}`."
        return [header, summary, body]
    strengths = "\n".join(f"✅ {line}" for line in analysis.strengths) or "Nothing stands out yet."
    tips = "\n".join(f"⚠️ {line}" for line in analysis.tips) or "No clear weak spots in these games."
    since = analysis.since.strftime("%d %b %Y").lstrip("0") if analysis.since else "?"
    footer = (
        f"-# {label} games since {since} · patterns need 10+ games per comparison; observations, not causes · "
        f"`!analyze {command_name} {analysis.months} {other}` for {other_label}"
    )
    return [header, summary, f"**Strengths**\n{strengths}", f"**Work on**\n{tips}", footer]


def format_analysis_text(analysis):
    return "\n\n".join(format_analysis_sections(analysis))


def build_analysis_view(analysis):
    """Components V2 card: one container, sections split by separators."""
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=ANALYSIS_ACCENT)
    sections = format_analysis_sections(analysis)
    for index, section in enumerate(sections):
        if index:
            container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(discord.ui.TextDisplay(section))
    view.add_item(container)
    return view

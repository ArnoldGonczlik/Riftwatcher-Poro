import asyncio

import pytest

from src.commands.routing import command_channel_id, enforce_command_channel, is_supported_command


@pytest.mark.parametrize("command", ["!analyze", "!analyse name 3", "!records", "!record name"])
def test_manual_reports_have_their_own_channel(command):
    assert is_supported_command(command)
    assert command_channel_id(command, daily_channel_id=1, weekly_channel_id=2,
                              events_channel_id=3, manual_commands_channel_id=4) == 4


def test_help_available_in_manual_channel_but_admin_commands_stay_in_events():
    from src.commands.routing import format_help_text

    kwargs = dict(daily_channel_id=1, weekly_channel_id=2, events_channel_id=3, manual_commands_channel_id=4)
    assert command_channel_id("!help", **kwargs) == (3, 4)
    assert command_channel_id("!add name#tag", **kwargs) == 3
    help_text = format_help_text(report_day_start_hour=6, match_recap_channel_id=5, **kwargs)
    assert "[months]" in help_text and "1 month by default" in help_text
    assert "(run in <#4>)" in help_text
    assert len(format_help_text(report_day_start_hour=6, **{
        key: 123456789012345678 for key in (*kwargs, "match_recap_channel_id")
    })) <= 2000


class FakeChannel:
    def __init__(self, channel_id):
        self.id = channel_id
        self.sent_messages = []

    async def send(self, content):
        self.sent_messages.append(content)


class FakeMessage:
    def __init__(self, content, channel_id):
        self.content = content
        self.channel = FakeChannel(channel_id)


class FakeContext:
    def __init__(self, *, content, channel_id, daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=404):
        self.content = content
        self.content_lower = content.casefold()
        self.message = FakeMessage(content, channel_id)
        self.daily_channel_id = daily_channel_id
        self.weekly_channel_id = weekly_channel_id
        self.events_channel_id = events_channel_id
        self.match_recap_channel_id = match_recap_channel_id


@pytest.mark.parametrize(
    "content_lower, expected",
    [
        ("!daily", True),
        ("!weekly", True),
        ("!streak", True),
        ("!streak alpha#na1", True),
        ("!profile", True),
        ("!profile alpha#na1", True),
        ("!tts", True),
        ("!tts on", True),
        ("!champdisplay", True),
        ("!champdisplay both", True),
        ("!add", True),
        ("!add alpha#na1", True),
        ("!remove", True),
        ("!remove alpha#na1", True),
        ("!debugplayer alpha#na1", True),
        ("!backfill", True),
        ("!backfill 2026-01-01 2026-01-31", True),
        ("!help", True),
        ("!unknown", False),
        ("hello", False),
    ],
)
def test_is_supported_command_matrix(content_lower, expected):
    assert is_supported_command(content_lower) is expected


def test_command_channel_id_matrix_with_recap_channel():
    assert command_channel_id("!daily", daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=404) == 101
    assert command_channel_id("!weekly", daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=404) == 202
    assert command_channel_id("!streak", daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=404) == 404
    assert command_channel_id("!streak alpha#na1", daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=404) == 404
    assert command_channel_id("!profile alpha#na1", daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=404) == 303
    assert command_channel_id("!remove alpha#na1", daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=404) == 303
    assert command_channel_id("!backfill 2026-01-01 2026-01-31", daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=404) == 303
    assert command_channel_id("!tts", daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=404) == (303, 404)
    assert command_channel_id("!tts off", daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=404) == (303, 404)
    assert command_channel_id("!help", daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=404) == 303


def test_command_channel_id_fallbacks_without_recap_channel():
    assert command_channel_id("!streak", daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=None) == 303
    assert command_channel_id("!tts", daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=None) == (303,)


def test_champdisplay_runs_in_events_or_recap_channel():
    for content in ("!champdisplay", "!champdisplay icon"):
        assert command_channel_id(content, daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=404) == (303, 404)
        assert command_channel_id(content, daily_channel_id=101, weekly_channel_id=202, events_channel_id=303, match_recap_channel_id=None) == (303,)


def test_enforce_command_channel_allows_right_channel():
    ctx = FakeContext(content="!Daily", channel_id=101)
    blocked = asyncio.run(enforce_command_channel(ctx))
    assert blocked is False
    assert ctx.message.channel.sent_messages == []


def test_enforce_command_channel_prompts_daily_channel():
    ctx = FakeContext(content="!Daily", channel_id=303)
    blocked = asyncio.run(enforce_command_channel(ctx))
    assert blocked is True
    assert ctx.message.channel.sent_messages == ["Use `!Daily` in <#101>."]


def test_enforce_command_channel_prompts_recap_for_bare_streak():
    ctx = FakeContext(content="!streak", channel_id=303)
    blocked = asyncio.run(enforce_command_channel(ctx))
    assert blocked is True
    assert ctx.message.channel.sent_messages == ["Use `!streak` in <#404>."]


def test_enforce_command_channel_prompts_events_or_recap_for_tts():
    ctx = FakeContext(content="!tts status", channel_id=101)
    blocked = asyncio.run(enforce_command_channel(ctx))
    assert blocked is True
    assert ctx.message.channel.sent_messages == ["Use `!tts` in <#303> or <#404>."]


def test_enforce_command_channel_ignores_non_commands():
    ctx = FakeContext(content="hello world", channel_id=101)
    blocked = asyncio.run(enforce_command_channel(ctx))
    assert blocked is False
    assert ctx.message.channel.sent_messages == []

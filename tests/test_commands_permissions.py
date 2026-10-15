from types import SimpleNamespace

from src.commands.permissions import format_admin_denied_text, has_admin_access, is_admin_command


def _member(*, administrator=False, manage_guild=False, role_ids=()):
    return SimpleNamespace(
        guild_permissions=SimpleNamespace(administrator=administrator, manage_guild=manage_guild),
        roles=[SimpleNamespace(id=role_id) for role_id in role_ids],
    )


def test_is_admin_command_covers_mutating_forms_only():
    assert is_admin_command("!add alpha#na1")
    assert is_admin_command("!remove alpha#na1")
    assert is_admin_command("!backfill 2026-01-01 2026-01-31")
    assert is_admin_command("!tts on")
    assert is_admin_command("!tts off")


def test_is_admin_command_leaves_usage_and_read_only_forms_open():
    assert not is_admin_command("!add")
    assert not is_admin_command("!remove")
    assert not is_admin_command("!backfill")
    assert not is_admin_command("!tts")
    assert not is_admin_command("!tts status")
    assert not is_admin_command("!profile alpha#na1")
    assert not is_admin_command("!health")
    assert not is_admin_command("!daily")


def test_has_admin_access_allows_administrator_and_manage_guild():
    assert has_admin_access(_member(administrator=True))
    assert has_admin_access(_member(manage_guild=True))


def test_has_admin_access_denies_regular_member_without_role():
    assert not has_admin_access(_member())
    assert not has_admin_access(_member(role_ids=(1, 2)), admin_role_id=55)


def test_has_admin_access_allows_configured_role():
    assert has_admin_access(_member(role_ids=(1, 55)), admin_role_id=55)


def test_has_admin_access_ignores_roles_when_no_role_configured():
    assert not has_admin_access(_member(role_ids=(55,)), admin_role_id=None)
    assert not has_admin_access(_member(role_ids=(55,)), admin_role_id=0)


def test_has_admin_access_denies_dm_users():
    assert not has_admin_access(SimpleNamespace())


def test_format_admin_denied_text_mentions_role_only_when_configured():
    assert format_admin_denied_text("!add") == "`!add` requires the **Manage Server** permission."
    assert format_admin_denied_text("!add", 55) == "`!add` requires the **Manage Server** permission or the <@&55> role."

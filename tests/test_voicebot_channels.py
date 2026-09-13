from __future__ import annotations

import discord
import pytest

from gatebound_support.voicebot.channels import (
    CHANNEL_NAME_PREFIX,
    MAX_CHANNEL_NAME_LENGTH,
    build_support_channel_name,
    build_voice_channel_overwrites,
    format_call_duration,
    has_staff_permissions,
    is_support_channel_name,
    sanitize_display_name,
    select_staff_roles,
)


class _FakeRole:
    def __init__(self, name: str, permissions: discord.Permissions) -> None:
        self.name = name
        self.permissions = permissions

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"_FakeRole({self.name!r})"


# ---- name sanitizing ----


@pytest.mark.parametrize(
    ("display_name", "expected"),
    [
        ("Aidenn", "Aidenn"),
        ("  Aidenn  ", "Aidenn"),
        ("Aidenn\nthe\tBrave", "Aidenn the Brave"),
        ("@everyone#hack`", "everyonehack"),
        ("@#`", "player"),
        ("", "player"),
        ("   ", "player"),
    ],
)
def test_sanitize_display_name(display_name: str, expected: str) -> None:
    assert sanitize_display_name(display_name) == expected


def test_build_support_channel_name_has_prefix() -> None:
    assert build_support_channel_name("Aidenn") == "support-Aidenn"


def test_build_support_channel_name_truncated_to_100_chars() -> None:
    name = build_support_channel_name("x" * 200)
    assert len(name) == MAX_CHANNEL_NAME_LENGTH
    assert name.startswith(CHANNEL_NAME_PREFIX)


def test_build_support_channel_name_empty_display_name_uses_fallback() -> None:
    assert build_support_channel_name("") == "support-player"


def test_is_support_channel_name() -> None:
    assert is_support_channel_name("support-Aidenn") is True
    assert is_support_channel_name("general") is False
    assert is_support_channel_name("Support-Aidenn") is False  # case sensitive, matches Discord


# ---- staff role selection ----


def test_has_staff_permissions_administrator() -> None:
    assert has_staff_permissions(discord.Permissions(administrator=True)) is True


def test_has_staff_permissions_manage_guild() -> None:
    assert has_staff_permissions(discord.Permissions(manage_guild=True)) is True


def test_has_staff_permissions_plain_role() -> None:
    assert has_staff_permissions(discord.Permissions(send_messages=True)) is False


def test_select_staff_roles_filters_correctly() -> None:
    admin = _FakeRole("Admin", discord.Permissions(administrator=True))
    manager = _FakeRole("Manager", discord.Permissions(manage_guild=True))
    member_role = _FakeRole("Member", discord.Permissions(send_messages=True, connect=True))
    everyone = _FakeRole("@everyone", discord.Permissions.none())

    result = select_staff_roles([admin, manager, member_role, everyone])

    assert result == [admin, manager]


def test_select_staff_roles_empty_when_none_qualify() -> None:
    member_role = _FakeRole("Member", discord.Permissions(send_messages=True))
    assert select_staff_roles([member_role]) == []


# ---- overwrite construction ----


def test_build_voice_channel_overwrites_denies_everyone() -> None:
    everyone = "everyone-role"
    overwrites = build_voice_channel_overwrites(
        everyone_role=everyone, member="member", bot_member="bot", staff_roles=[]
    )
    allow, deny = overwrites[everyone].pair()
    assert deny.view_channel is True
    assert deny.connect is True
    assert allow.value == 0


def test_build_voice_channel_overwrites_allows_member_and_bot() -> None:
    member = "member"
    bot_member = "bot"
    overwrites = build_voice_channel_overwrites(
        everyone_role="everyone", member=member, bot_member=bot_member, staff_roles=[]
    )
    for target in (member, bot_member):
        allow, deny = overwrites[target].pair()
        assert allow.view_channel is True
        assert allow.connect is True
        assert allow.speak is True
        assert deny.value == 0


def test_build_voice_channel_overwrites_staff_can_view_but_not_auto_connect() -> None:
    staff_role = _FakeRole("Admin", discord.Permissions(administrator=True))
    overwrites = build_voice_channel_overwrites(
        everyone_role="everyone", member="member", bot_member="bot", staff_roles=[staff_role]
    )
    allow, _deny = overwrites[staff_role].pair()
    assert allow.view_channel is True
    # staff are not granted connect/speak -- they can see the channel exists, nothing more
    assert allow.connect is None or allow.connect is False
    assert allow.speak is None or allow.speak is False


def test_build_voice_channel_overwrites_result_has_exactly_expected_targets() -> None:
    staff_role = _FakeRole("Admin", discord.Permissions(administrator=True))
    overwrites = build_voice_channel_overwrites(
        everyone_role="everyone", member="member", bot_member="bot", staff_roles=[staff_role]
    )
    assert set(overwrites.keys()) == {"everyone", "member", "bot", staff_role}


# ---- call duration formatting ----


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (5, "5s"),
        (59, "59s"),
        (60, "1m 00s"),
        (65, "1m 05s"),
        (3661, "61m 01s"),
    ],
)
def test_format_call_duration(seconds: float, expected: str) -> None:
    assert format_call_duration(seconds) == expected


def test_format_call_duration_negative_clamped_to_zero() -> None:
    assert format_call_duration(-5) == "0s"

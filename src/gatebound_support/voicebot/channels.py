"""Pure helpers for the private per-call voice channel: name sanitizing, permission-overwrite
construction, staff-role selection, and call-duration formatting. Kept dependency-light
(only ``discord``'s plain value objects — ``Permissions``/``PermissionOverwrite``, neither
of which makes a network call) so these are unit-testable without a live connection.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

import discord

CHANNEL_NAME_PREFIX = "support-"
MAX_CHANNEL_NAME_LENGTH = 100
FALLBACK_NAME = "player"

_DISALLOWED_CHARS = re.compile(r"[`@#:]")
_WHITESPACE = re.compile(r"\s+")


def sanitize_display_name(display_name: str) -> str:
    """Strips characters that would make the channel name confusing or that Discord itself
    rejects/mangles (mentions, code fences, extra whitespace), collapses whitespace, and
    falls back to a generic name for a display name that sanitizes to nothing (all-emoji,
    all-symbols, empty)."""
    cleaned = _DISALLOWED_CHARS.sub("", display_name or "")
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    return cleaned or FALLBACK_NAME


def build_support_channel_name(display_name: str) -> str:
    """``support-<display name>``, sanitized and truncated to Discord's 100-char channel
    name limit (the prefix is never truncated away — it's what startup cleanup and
    ``is_support_channel_name`` key off of)."""
    name = f"{CHANNEL_NAME_PREFIX}{sanitize_display_name(display_name)}"
    return name[:MAX_CHANNEL_NAME_LENGTH]


def is_support_channel_name(name: str) -> bool:
    """Used by the startup cleanup sweep to recognize leftover temporary call channels from
    a previous (crashed/restarted) run."""
    return name.startswith(CHANNEL_NAME_PREFIX)


def has_staff_permissions(permissions: discord.Permissions) -> bool:
    return permissions.administrator or permissions.manage_guild


def select_staff_roles(roles: Iterable[Any]) -> list[Any]:
    """Any role with Administrator or Manage Guild — SPEC: staff can see the call channel
    exists, without being required to join it."""
    return [role for role in roles if has_staff_permissions(role.permissions)]


def build_voice_channel_overwrites(
    *,
    everyone_role: Any,
    member: Any,
    bot_member: Any,
    staff_roles: Iterable[Any],
) -> dict[Any, discord.PermissionOverwrite]:
    """@everyone is denied View Channel + Connect (so the call can't be overheard or
    joined by other members); the invoking player and the bot itself get View + Connect +
    Speak; staff roles (Administrator/Manage Guild) get View only, so they can see the
    channel exists without being pulled into it."""
    overwrites: dict[Any, discord.PermissionOverwrite] = {
        everyone_role: discord.PermissionOverwrite(view_channel=False, connect=False),
        member: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True),
        bot_member: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True),
    }
    for role in staff_roles:
        overwrites.setdefault(role, discord.PermissionOverwrite(view_channel=True))
    return overwrites


def format_call_duration(seconds: float) -> str:
    """``12s`` / ``3m 05s`` — used in the "Call ended (<duration>)" thread note."""
    total_seconds = max(0, round(seconds))
    minutes, secs = divmod(total_seconds, 60)
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"

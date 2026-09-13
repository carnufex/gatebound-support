"""The "one conversation per guild at a time" state machine (busy-line behaviour). Kept as
plain, synchronous, dependency-free logic so it's trivially unit-testable without a running
event loop or a real Discord connection.
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class ActiveCall:
    channel_id: int
    user_id: int


class CallRegistry:
    """At most one active voice call per guild. ``try_start`` is the only way to register
    one, and it atomically refuses a second call for a guild that already has one — the
    caller replies with the ephemeral "the line is busy" message on a ``False`` return."""

    def __init__(self) -> None:
        self._active: dict[int, ActiveCall] = {}

    def try_start(self, guild_id: int, *, channel_id: int, user_id: int) -> bool:
        if guild_id in self._active:
            return False
        self._active[guild_id] = ActiveCall(channel_id=channel_id, user_id=user_id)
        return True

    def is_busy(self, guild_id: int) -> bool:
        return guild_id in self._active

    def end(self, guild_id: int) -> None:
        self._active.pop(guild_id, None)

    def active_call(self, guild_id: int) -> ActiveCall | None:
        return self._active.get(guild_id)

    def active_user_id(self, guild_id: int) -> int | None:
        call = self._active.get(guild_id)
        return call.user_id if call else None

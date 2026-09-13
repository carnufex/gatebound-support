"""The "one conversation per guild at a time" state machine (busy-line behaviour), plus the
"waiting for the player to join their private channel" call-phase state machine. Kept as
plain, synchronous, dependency-free logic so both are trivially unit-testable without a
running event loop or a real Discord connection.
"""

from __future__ import annotations

import dataclasses
import enum

#: How long a private call channel waits for the invoking player to join it before the
#: bot gives up, deletes the channel, and posts "Call not started" (coordinator's voice
#: isolation change: the player is no longer required to already be in a voice channel, so
#: there has to be a bounded wait for them to join the one the bot just created for them).
JOIN_TIMEOUT_SECONDS = 120.0


@dataclasses.dataclass(frozen=True)
class ActiveCall:
    channel_id: int
    user_id: int


class CallRegistry:
    """At most one active voice call per guild. ``try_start`` is the only way to register
    one, and it atomically refuses a second call for a guild that already has one — the
    caller replies with the ephemeral "the line is busy" message on a ``False`` return.
    A guild is busy from the moment ``/support`` reserves it (channel creation, ticket
    creation, waiting for the player to join) through the end of the call, not just while
    the ElevenLabs session itself is active."""

    def __init__(self) -> None:
        self._active: dict[int, ActiveCall] = {}

    def try_start(self, guild_id: int, *, channel_id: int, user_id: int) -> bool:
        if guild_id in self._active:
            return False
        self._active[guild_id] = ActiveCall(channel_id=channel_id, user_id=user_id)
        return True

    def set_channel_id(self, guild_id: int, channel_id: int) -> None:
        """The temporary voice channel doesn't exist yet at ``try_start`` time (it's created
        just after reserving the guild), so its id is filled in once known."""
        call = self._active.get(guild_id)
        if call is not None:
            self._active[guild_id] = ActiveCall(channel_id=channel_id, user_id=call.user_id)

    def is_busy(self, guild_id: int) -> bool:
        return guild_id in self._active

    def end(self, guild_id: int) -> None:
        self._active.pop(guild_id, None)

    def active_call(self, guild_id: int) -> ActiveCall | None:
        return self._active.get(guild_id)

    def active_user_id(self, guild_id: int) -> int | None:
        call = self._active.get(guild_id)
        return call.user_id if call else None


class CallPhase(enum.Enum):
    #: The bot created a private channel and is waiting for the invoking player to join it.
    WAITING_FOR_JOIN = "waiting_for_join"
    #: The player joined; the bot connected and the ElevenLabs session is running.
    ACTIVE = "active"


@dataclasses.dataclass
class PendingCall:
    """The join-phase state machine for one ``/support`` call. Time is injected (``now``
    passed in rather than read from the clock) so ``is_expired`` is deterministic to test.
    """

    guild_id: int
    user_id: int
    channel_id: int
    created_at: float
    phase: CallPhase = CallPhase.WAITING_FOR_JOIN

    def is_waiting(self) -> bool:
        return self.phase is CallPhase.WAITING_FOR_JOIN

    def mark_active(self) -> None:
        self.phase = CallPhase.ACTIVE

    def is_expired(self, *, now: float, timeout_seconds: float = JOIN_TIMEOUT_SECONDS) -> bool:
        """True once ``timeout_seconds`` have passed while still waiting for the join —
        always False once ``mark_active`` has been called, and always False before the
        deadline regardless of phase."""
        return self.is_waiting() and (now - self.created_at) >= timeout_seconds

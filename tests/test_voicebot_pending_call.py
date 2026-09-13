from __future__ import annotations

from gatebound_support.voicebot.state import JOIN_TIMEOUT_SECONDS, CallPhase, PendingCall


def _pending(created_at: float = 1000.0) -> PendingCall:
    return PendingCall(guild_id=1, user_id=2, channel_id=3, created_at=created_at)


def test_starts_waiting_for_join() -> None:
    call = _pending()
    assert call.phase is CallPhase.WAITING_FOR_JOIN
    assert call.is_waiting() is True


def test_not_expired_before_timeout() -> None:
    call = _pending(created_at=1000.0)
    assert call.is_expired(now=1000.0 + JOIN_TIMEOUT_SECONDS - 1) is False


def test_expired_once_timeout_elapsed() -> None:
    call = _pending(created_at=1000.0)
    assert call.is_expired(now=1000.0 + JOIN_TIMEOUT_SECONDS) is True


def test_expired_well_past_timeout() -> None:
    call = _pending(created_at=1000.0)
    assert call.is_expired(now=1000.0 + JOIN_TIMEOUT_SECONDS + 500) is True


def test_mark_active_transitions_phase() -> None:
    call = _pending()
    call.mark_active()
    assert call.phase is CallPhase.ACTIVE
    assert call.is_waiting() is False


def test_active_call_never_reports_expired() -> None:
    call = _pending(created_at=1000.0)
    call.mark_active()
    # even long after the join deadline, an active call is not "expired" -- the join window
    # only applies while still waiting for the player to join
    assert call.is_expired(now=1000.0 + JOIN_TIMEOUT_SECONDS + 10_000) is False


def test_custom_timeout_seconds_respected() -> None:
    call = _pending(created_at=0.0)
    assert call.is_expired(now=30.0, timeout_seconds=60.0) is False
    assert call.is_expired(now=60.0, timeout_seconds=60.0) is True

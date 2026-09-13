from __future__ import annotations

from gatebound_support.voicebot.state import CallRegistry


def test_try_start_succeeds_when_idle() -> None:
    registry = CallRegistry()
    assert registry.try_start(1, channel_id=100, user_id=200) is True
    assert registry.is_busy(1) is True
    assert registry.active_user_id(1) == 200


def test_second_call_in_same_guild_is_busy() -> None:
    registry = CallRegistry()
    assert registry.try_start(1, channel_id=100, user_id=200) is True
    assert registry.try_start(1, channel_id=101, user_id=300) is False
    # original call is unaffected by the rejected attempt
    assert registry.active_user_id(1) == 200


def test_different_guilds_are_independent() -> None:
    registry = CallRegistry()
    assert registry.try_start(1, channel_id=100, user_id=200) is True
    assert registry.try_start(2, channel_id=100, user_id=200) is True
    assert registry.is_busy(1) is True
    assert registry.is_busy(2) is True


def test_end_frees_the_guild_for_a_new_call() -> None:
    registry = CallRegistry()
    registry.try_start(1, channel_id=100, user_id=200)
    registry.end(1)
    assert registry.is_busy(1) is False
    assert registry.try_start(1, channel_id=101, user_id=300) is True


def test_end_on_idle_guild_is_a_noop() -> None:
    registry = CallRegistry()
    registry.end(999)  # must not raise
    assert registry.is_busy(999) is False


def test_active_call_returns_channel_and_user() -> None:
    registry = CallRegistry()
    registry.try_start(1, channel_id=100, user_id=200)
    call = registry.active_call(1)
    assert call is not None
    assert call.channel_id == 100
    assert call.user_id == 200


def test_active_user_id_none_when_idle() -> None:
    registry = CallRegistry()
    assert registry.active_user_id(42) is None
    assert registry.active_call(42) is None

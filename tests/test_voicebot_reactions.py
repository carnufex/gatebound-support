from __future__ import annotations

from gatebound_support.voicebot.reactions import (
    OPEN_TICKET_EMOJI,
    PRIMARY_CLOSE_EMOJI,
    START_CALL_EMOJI,
    InstructionsReactionAction,
    instructions_message_action,
    is_close_reaction,
)


def test_open_ticket_emoji_maps_to_open_ticket_action() -> None:
    assert instructions_message_action(OPEN_TICKET_EMOJI) is InstructionsReactionAction.OPEN_TICKET


def test_start_call_emoji_maps_to_start_call_action() -> None:
    assert instructions_message_action(START_CALL_EMOJI) is InstructionsReactionAction.START_CALL


def test_unrelated_emoji_maps_to_no_action() -> None:
    assert instructions_message_action("\U0001f44d") is None  # 👍
    assert instructions_message_action(PRIMARY_CLOSE_EMOJI) is None


def test_empty_string_maps_to_no_action() -> None:
    assert instructions_message_action("") is None


def test_primary_close_emoji_is_a_close_reaction() -> None:
    assert is_close_reaction(PRIMARY_CLOSE_EMOJI) is True


def test_lock_emoji_is_also_a_close_reaction() -> None:
    assert is_close_reaction("\U0001f512") is True  # 🔒


def test_instructions_emojis_are_not_close_reactions() -> None:
    assert is_close_reaction(OPEN_TICKET_EMOJI) is False
    assert is_close_reaction(START_CALL_EMOJI) is False


def test_unrelated_emoji_is_not_a_close_reaction() -> None:
    assert is_close_reaction("\U0001f44d") is False  # 👍

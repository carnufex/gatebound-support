"""Pure emoji -> ChatOps-action mapping for the reaction-based shortcuts on the pinned
instructions message (open a ticket, start a call) and on ticket threads (close). Kept
dependency-free so the mapping is unit-testable without a live Discord connection —
``str(payload.emoji)`` for a unicode reaction is just the emoji character itself (confirmed
against ``discord.PartialEmoji.__str__`` in ``.venv/Lib/site-packages/discord/
partial_emoji.py``: it returns ``self.name``, and ``self.id`` is None for unicode emoji), so
these functions take and compare plain strings.
"""

from __future__ import annotations

import enum

OPEN_TICKET_EMOJI = "\U0001f3ab"  # 🎫
START_CALL_EMOJI = "\U0001f4de"  # 📞
#: The reaction the bot itself adds to a ticket's opening post.
PRIMARY_CLOSE_EMOJI = "✅"  # ✅
#: Either of these on a bot-authored message inside a ticket thread closes it.
CLOSE_EMOJIS = frozenset({PRIMARY_CLOSE_EMOJI, "\U0001f512"})  # ✅ 🔒


class InstructionsReactionAction(enum.Enum):
    OPEN_TICKET = "open_ticket"
    START_CALL = "start_call"


def instructions_message_action(emoji: str) -> InstructionsReactionAction | None:
    """Maps a reaction on the pinned instructions message to the action it triggers, or
    ``None`` for any other emoji (ignored)."""
    if emoji == OPEN_TICKET_EMOJI:
        return InstructionsReactionAction.OPEN_TICKET
    if emoji == START_CALL_EMOJI:
        return InstructionsReactionAction.START_CALL
    return None


def is_close_reaction(emoji: str) -> bool:
    return emoji in CLOSE_EMOJIS

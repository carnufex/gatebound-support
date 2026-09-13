"""Shared ticket-thread opening post formatting (SPEC §5.3).

Used by both the OAuth ticket flow (`routes/tickets.py`, a player who came from an
ElevenLabs-issued `/t/<token>` link) and the Discord bot's direct ticket creation
(`voicebot/bot.py`, `/ticket` and the persistent "Open a ticket" button, plus `/support`
voice calls) so every private thread's opening message looks the same regardless of how the
ticket was opened.
"""

from __future__ import annotations


def opening_post(
    *,
    ticket_id: str,
    category: str,
    priority: str,
    summary: str,
    account_name: str | None,
    conversation_id: str | None,
    discord_user_id: str,
    footer: str = "Transcript follows when the conversation ends.",
) -> str:
    lines = [
        f"**Ticket:** {ticket_id}",
        f"**Category:** {category}",
        f"**Priority:** {priority}",
        f"**Summary:** {summary}",
        f"**Account:** {account_name or 'unknown'}",
        f"**Conversation:** {conversation_id or 'none'}",
        f"<@{discord_user_id}>",
        footer,
    ]
    return "\n".join(lines)

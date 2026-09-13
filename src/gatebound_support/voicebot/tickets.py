"""Pure helpers for the bot's text-ticket flow (``/ticket``, the persistent "Open a ticket"
button, and closing a ticket via ``/close``, its persistent button, or a closing-emoji
reaction). Split out from bot.py so this logic is unit-testable without discord.py.
"""

from __future__ import annotations

from typing import Any

ALLOWED_CATEGORIES = ("account", "bug", "payment", "report_player", "other")

#: Discord message component payload for the persistent "Close ticket" button, attached (via
#: raw REST — see ``discord.DiscordClient.create_ticket_thread``/``post_message``) to every
#: new ticket's opening post and to the voice call's "Call ended" follow-up. Style 4 =
#: Danger (red), matching ``discord.ButtonStyle.danger`` on the ``CloseTicketView`` that
#: handles the click — persistent-view dispatch matches purely on (component_type,
#: custom_id), so it works regardless of which process posted the message.
CLOSE_TICKET_CUSTOM_ID = "gb_close_ticket"


def close_ticket_button_components() -> list[dict[str, Any]]:
    return [
        {
            "type": 1,  # action row
            "components": [
                {"type": 2, "style": 4, "label": "Close ticket", "custom_id": CLOSE_TICKET_CUSTOM_ID},
            ],
        }
    ]


def can_close_ticket(*, member_id: str, ticket_owner_id: str | None, has_manage_threads: bool) -> bool:
    """Who may close a ticket thread (via ``/close``, the button, or a closing reaction):
    the ticket's own Discord user, or anyone with Manage Threads on the parent channel."""
    if ticket_owner_id is not None and member_id == ticket_owner_id:
        return True
    return has_manage_threads

# Order matters: checked top to bottom, first match wins. "report_player" before "bug" so a
# summary like "this player is exploiting a bug" lands as report_player, since a report is
# the actionable category there.
_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("account", ("account", "login", "log in", "password", "locked out", "banned", "ban")),
    ("payment", ("payment", "pay", "purchase", "premium", "coins", "refund", "charge", "billing")),
    (
        "report_player",
        ("report", "cheat", "cheating", "hack", "hacking", "abuse", "harass", "scam", "exploit"),
    ),
    ("bug", ("bug", "glitch", "crash", "error", "broken", "stuck")),
)


def map_category(text: str) -> str:
    """Maps the modal's free-text "what is it about?" answer to one of SPEC's five ticket
    categories. Modals can't show a select control, so the input is free text; an exact
    category name is accepted as-is, otherwise the first matching keyword wins, otherwise
    "other" (never a hard failure — the modal must always be submittable)."""
    normalized = (text or "").strip().lower()
    if normalized in ALLOWED_CATEGORIES:
        return normalized
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(keyword in normalized for keyword in keywords):
            return category
    return "other"

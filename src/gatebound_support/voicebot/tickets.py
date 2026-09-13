"""Pure helpers for the bot's text-ticket flow (``/ticket`` and the persistent "Open a
ticket" button). Split out from bot.py so the category mapping is unit-testable without
discord.py.
"""

from __future__ import annotations

ALLOWED_CATEGORIES = ("account", "bug", "payment", "report_player", "other")

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

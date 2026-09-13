from __future__ import annotations

import pytest

from gatebound_support.voicebot.tickets import map_category


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("account", "account"),
        ("Account", "account"),
        ("  BUG  ", "bug"),
        ("payment", "payment"),
        ("report_player", "report_player"),
        ("other", "other"),
        ("I can't log in to my account", "account"),
        ("my password isn't working", "account"),
        ("the game crashed and I lost my items", "bug"),
        ("there's a glitch near the spawn", "bug"),
        ("I was charged twice for premium", "payment"),
        ("I want a refund for my purchase", "payment"),
        ("this player is cheating", "report_player"),
        ("someone is harassing me in chat", "report_player"),
        ("I want to suggest a new feature", "other"),
        ("", "other"),
    ],
)
def test_map_category(text: str, expected: str) -> None:
    assert map_category(text) == expected


def test_report_player_wins_over_bug_for_exploit_reports() -> None:
    assert map_category("this player is exploiting a bug to duplicate items") == "report_player"

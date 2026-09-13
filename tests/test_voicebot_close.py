from __future__ import annotations

from gatebound_support.voicebot.tickets import (
    CLOSE_TICKET_CUSTOM_ID,
    can_close_ticket,
    close_ticket_button_components,
)

# ---- permission check ----


def test_owner_can_close() -> None:
    assert can_close_ticket(member_id="42", ticket_owner_id="42", has_manage_threads=False) is True


def test_random_member_without_manage_threads_refused() -> None:
    assert can_close_ticket(member_id="99", ticket_owner_id="42", has_manage_threads=False) is False


def test_staff_with_manage_threads_allowed_even_if_not_owner() -> None:
    assert can_close_ticket(member_id="99", ticket_owner_id="42", has_manage_threads=True) is True


def test_owner_with_manage_threads_allowed() -> None:
    assert can_close_ticket(member_id="42", ticket_owner_id="42", has_manage_threads=True) is True


def test_unknown_owner_falls_back_to_manage_threads() -> None:
    assert can_close_ticket(member_id="42", ticket_owner_id=None, has_manage_threads=False) is False
    assert can_close_ticket(member_id="42", ticket_owner_id=None, has_manage_threads=True) is True


# ---- close button component shape ----


def test_close_ticket_button_components_shape() -> None:
    components = close_ticket_button_components()
    assert len(components) == 1
    row = components[0]
    assert row["type"] == 1
    assert len(row["components"]) == 1
    button = row["components"][0]
    assert button["type"] == 2
    assert button["style"] == 4  # danger
    assert button["custom_id"] == CLOSE_TICKET_CUSTOM_ID
    assert button["label"]

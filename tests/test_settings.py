from __future__ import annotations

from gatebound_support.settings import enabled


def test_enabled_true_for_real_value() -> None:
    assert enabled("some-secret") is True


def test_enabled_false_for_empty_string() -> None:
    assert enabled("") is False


def test_enabled_false_for_unset_literal() -> None:
    assert enabled("unset") is False
    assert enabled("Unset") is False
    assert enabled("UNSET") is False


def test_enabled_false_for_none() -> None:
    assert enabled(None) is False

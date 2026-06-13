"""Tests for weekday bitmask encoding."""

from __future__ import annotations

from chihiros_led_control.weekday_encoding import (
    WeekdaySelect,
    encode_selected_weekdays,
)


def test_encode_everyday() -> None:
    """Everyday encodes all weekday bits."""
    assert encode_selected_weekdays([WeekdaySelect.everyday]) == 127


def test_encode_selected_weekdays() -> None:
    """Selected weekdays encode the expected bitmask."""
    assert (
        encode_selected_weekdays(
            [WeekdaySelect.monday, WeekdaySelect.wednesday, WeekdaySelect.sunday]
        )
        == 81
    )

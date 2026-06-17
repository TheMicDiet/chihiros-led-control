"""Tests for auto schedule validation helpers."""

from __future__ import annotations

from chihiros_led_control.schedule_validation import (
    SCHEDULE_WEEKDAYS,
    find_duplicate_schedule_weekdays,
    normalize_schedule_weekdays,
)
from chihiros_led_control.weekday_encoding import WeekdaySelect


def test_normalize_schedule_weekdays_expands_empty_selection() -> None:
    """Missing weekday selection targets every concrete weekday."""
    assert normalize_schedule_weekdays(None) == frozenset(SCHEDULE_WEEKDAYS)


def test_normalize_schedule_weekdays_expands_everyday() -> None:
    """Everyday targets every concrete weekday."""
    assert normalize_schedule_weekdays([WeekdaySelect.everyday]) == frozenset(SCHEDULE_WEEKDAYS)


def test_find_duplicate_schedule_weekdays_detects_everyday_overlap() -> None:
    """Schedules cannot contain multiple periods for the same concrete weekday."""
    duplicate = find_duplicate_schedule_weekdays(
        [
            [WeekdaySelect.everyday],
            [WeekdaySelect.monday],
        ]
    )

    assert duplicate is not None
    assert duplicate.first_index == 0
    assert duplicate.second_index == 1
    assert duplicate.weekdays == (WeekdaySelect.monday,)


def test_find_duplicate_schedule_weekdays_allows_distinct_weekdays() -> None:
    """Different weekdays can each have one period."""
    assert (
        find_duplicate_schedule_weekdays(
            [
                [WeekdaySelect.monday],
                [WeekdaySelect.tuesday],
            ]
        )
        is None
    )

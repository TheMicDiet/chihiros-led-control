"""Helpers for validating Chihiros auto schedules."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .weekday_encoding import WeekdaySelect

SCHEDULE_WEEKDAYS = (
    WeekdaySelect.monday,
    WeekdaySelect.tuesday,
    WeekdaySelect.wednesday,
    WeekdaySelect.thursday,
    WeekdaySelect.friday,
    WeekdaySelect.saturday,
    WeekdaySelect.sunday,
)


@dataclass(frozen=True)
class DuplicateScheduleWeekdays:
    """Pair of schedule periods that target the same weekdays."""

    first_index: int
    second_index: int
    weekdays: tuple[WeekdaySelect, ...]


def normalize_schedule_weekdays(selection: Iterable[WeekdaySelect] | None) -> frozenset[WeekdaySelect]:
    """Expand an optional schedule weekday selection to concrete weekdays."""
    selected = set(selection or ())
    if not selected or WeekdaySelect.everyday in selected:
        return frozenset(SCHEDULE_WEEKDAYS)
    return frozenset(selected)


def find_duplicate_schedule_weekdays(
    period_weekdays: Sequence[Iterable[WeekdaySelect] | None],
) -> DuplicateScheduleWeekdays | None:
    """Return the first pair of periods that target any of the same weekdays."""
    normalized_periods = [normalize_schedule_weekdays(weekdays) for weekdays in period_weekdays]
    for index, weekdays in enumerate(normalized_periods):
        for other_index, other_weekdays in enumerate(normalized_periods[index + 1 :], start=index + 1):
            duplicates = tuple(
                weekday for weekday in SCHEDULE_WEEKDAYS if weekday in weekdays and weekday in other_weekdays
            )
            if duplicates:
                return DuplicateScheduleWeekdays(index, other_index, duplicates)
    return None

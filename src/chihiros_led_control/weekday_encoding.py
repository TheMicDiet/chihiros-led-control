"""Module helping for weeday encoding."""

from enum import Enum


class WeekdaySelect(str, Enum):
    """Weekday list."""

    monday = "monday"
    tuesday = "tuesday"
    wednesday = "wednesday"
    thursday = "thursday"
    friday = "friday"
    saturday = "saturday"
    sunday = "sunday"
    everyday = "everyday"


_WEEKDAY_BITS: dict[WeekdaySelect, int] = {
    WeekdaySelect.monday: 64,
    WeekdaySelect.tuesday: 32,
    WeekdaySelect.wednesday: 16,
    WeekdaySelect.thursday: 8,
    WeekdaySelect.friday: 4,
    WeekdaySelect.saturday: 2,
    WeekdaySelect.sunday: 1,
}


def encode_selected_weekdays(selection: list[WeekdaySelect], *, sunday_first: bool = False) -> int:
    """Encode weekday names in ISO or Sunday-first wire order."""
    if WeekdaySelect.everyday in selection:
        return 127
    mask = sum(bit for day, bit in _WEEKDAY_BITS.items() if day in selection)
    return (mask >> 1) | ((mask & 1) << 6) if sunday_first else mask

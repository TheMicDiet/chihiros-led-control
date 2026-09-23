"""Magnetic-stirrer family commands and helpers."""

from __future__ import annotations

from collections.abc import Sequence

from .dosing import DosingWorkPoint
from .frame import create_command_encoding


def _validate_work_point_time(point: DosingWorkPoint) -> None:
    if not 0 <= point.start_hour <= 23 or not 0 <= point.start_minute <= 59:
        raise ValueError("Work point start time must be a valid wall-clock time")


STIRRER_MAX_SECONDS = 999
STIRRER_SPEED_DEFAULT = 40
STIRRER_ML_PER_MINUTE = 0.6


def validate_stirrer_work_points(points: Sequence[DosingWorkPoint]) -> None:
    """Reject duplicate or overlapping stirrer timer work points.

    The vendor app compares dose-derived intervals in ordinary wall-clock
    coordinates. Endpoints are inclusive; it does not compare the last point
    with the first point across midnight.
    """
    intervals: list[tuple[int, int]] = []
    for point in points:
        _validate_work_point_time(point)
        start = point.start_hour * 60 + point.start_minute
        duration = round(stirrer_minutes_for_dosage(point.volume_ml))
        end = start + duration
        intervals.append((start, end))

    for index, (start, end) in enumerate(intervals):
        for other_start, other_end in intervals[index + 1 :]:
            if start <= other_end and other_start <= end:
                raise ValueError("Stir work points overlap")


def stirrer_dosage_for_minutes(minutes: float) -> float:
    """Convert stir run minutes to the dosage volume carried on the wire.

    The stirrer reuses the pump's timer-point encoding; the app equates
    ``dosage_ml / 0.6`` with run minutes (the pump's 0.6 mL/min dose rate).
    """
    return minutes * STIRRER_ML_PER_MINUTE


def stirrer_minutes_for_dosage(dosage_ml: float) -> float:
    """Convert a stirrer timer-point dosage back to run minutes."""
    return dosage_ml / STIRRER_ML_PER_MINUTE


def _validate_stirrer_channel(channel: int) -> None:
    if channel < 0 or channel > 7:
        raise ValueError("Channel must be between 0 and 7")


def create_stirrer_pre_second_command(
    msg_id: tuple[int, int],
    channel: int,
    seconds: int,
    speed: int,
) -> bytearray:
    """Create the stirrer's ``stirrerPreSecond`` frame ``(0xA5, 42)``.

    Payload ``[channel, sec_hi, sec_lo, speed]``: the only wire carrier for
    the stir speed. ``seconds`` is the pre-stir time (0 to 999 s, the app's
    ``stirrer_time_max`` bound). The 0-100 speed range is an implementation
    assumption — the docs only pin the default of 40.
    """
    _validate_stirrer_channel(channel)
    if not 0 <= seconds <= STIRRER_MAX_SECONDS:
        raise ValueError(f"Pre-stir seconds must be between 0 and {STIRRER_MAX_SECONDS}")
    if not 0 <= speed <= 100:
        raise ValueError("Stir speed must be between 0 and 100")
    parameters = [channel, seconds >> 8, seconds & 0xFF, speed]
    return create_command_encoding(165, 42, msg_id, parameters, avoid_reserved_byte=False)


__all__ = [
    "STIRRER_MAX_SECONDS",
    "STIRRER_SPEED_DEFAULT",
    "STIRRER_ML_PER_MINUTE",
    "stirrer_dosage_for_minutes",
    "stirrer_minutes_for_dosage",
    "validate_stirrer_work_points",
    "create_stirrer_pre_second_command",
]

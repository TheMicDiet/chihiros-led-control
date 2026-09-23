"""Passive notification value objects and the ParsedNotification union."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RuntimeNotification:
    """Parsed LED runtime/status notification."""

    firmware_version: int
    runtime_minutes: int
    raw: bytes = field(default=b"", compare=False)


@dataclass(frozen=True)
class FanStatusNotification:
    """Parsed fan-equipped LED status notification."""

    firmware_version: int
    fan_rpm: int
    temperature_celsius: int
    raw: bytes = field(default=b"", compare=False)


@dataclass(frozen=True)
class SchedulePoint:
    """Parsed auto schedule point."""

    hour: int
    minute: int
    levels: Mapping[str, int]


@dataclass(frozen=True)
class ScheduleSnapshotNotification:
    """Parsed auto schedule/status snapshot notification."""

    firmware_version: int
    points: tuple[SchedulePoint, ...]
    raw: bytes = field(default=b"", compare=False)


@dataclass(frozen=True)
class DosingTotalsNotification:
    """Per-channel lifetime dosed volumes reported by a dosing pump."""

    total_dosed_ul: tuple[int, ...]
    raw: bytes = field(default=b"", compare=False)


@dataclass(frozen=True)
class DosingDailyNotification:
    """Per-channel ``dosed today`` volumes reported by a dosing pump."""

    dose_use_in_day_ul: tuple[int, ...]
    raw: bytes = field(default=b"", compare=False)


@dataclass(frozen=True)
class HeaterTemperatureNotification:
    """Setting and measured temperatures pushed by a Chihiros heater."""

    setting_temperature_celsius: float
    current_temperature_celsius: float
    raw: bytes = field(default=b"", compare=False)


@dataclass(frozen=True)
class HeaterStatusNotification:
    """Runtime/alarm status pushed by a Chihiros heater."""

    firmware_version: int
    work_time_hours: int
    alarms: int
    raw: bytes = field(default=b"", compare=False)


ParsedNotification = (
    RuntimeNotification
    | FanStatusNotification
    | ScheduleSnapshotNotification
    | DosingTotalsNotification
    | DosingDailyNotification
    | HeaterTemperatureNotification
    | HeaterStatusNotification
)

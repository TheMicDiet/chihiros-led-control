"""Development fake for LED-family controllers."""

# These fake methods mirror runtime protocols; narrow surfaces intentionally omit docs.
# ruff: noqa: D102, D107

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import datetime

from ..vendor.chihiros_led_control.models import LedFeature, LedSpec
from ..vendor.chihiros_led_control.protocol.led import (
    FanStatusNotification,
    RuntimeNotification,
    SchedulePoint,
    ScheduleSnapshotNotification,
)
from .base import FakeBaseDevice


class FakeLedDevice(FakeBaseDevice):
    """In-memory LED client with only LED-family operations."""

    def __init__(self, device_info) -> None:
        super().__init__(device_info)
        spec = self.model.spec
        if not isinstance(spec, LedSpec):
            raise TypeError("LED fake requires an LED specification")
        self._brightness = {color: 0 for color in spec.channels}
        self._auto_mode = False
        self._auto_curve_points: list[tuple[int, int, int]] = []
        self._fan_speed = 0
        self._fan_auto = False
        self._fan_start_temp = 38
        self._fan_stop_temp = 33
        self._temp_protect = False
        self._bluetooth_led = False
        self.last_runtime_notification: RuntimeNotification | None = None
        self.last_fan_status_notification: FanStatusNotification | None = None
        self.last_schedule_snapshot_notification: ScheduleSnapshotNotification | None = None

    @property
    def colors(self) -> dict[str, int]:
        return dict(self.model.spec.channels)  # type: ignore[union-attr]

    async def query_status(self) -> None:
        await asyncio.sleep(0)
        self.last_runtime_notification = RuntimeNotification(
            firmware_version=23,
            runtime_minutes=511,
            raw=bytes.fromhex("5b 17 0a 00 01 0a 01 ff ff ff ff 0c 36 2d"),
        )
        self.last_schedule_snapshot_notification = ScheduleSnapshotNotification(
            firmware_version=23,
            points=(self._schedule_point(8, 0, 15), self._schedule_point(12, 0, 70), self._schedule_point(20, 30, 0)),
        )
        self._notify_callbacks(self.last_runtime_notification)
        self._notify_callbacks(self.last_schedule_snapshot_notification)

    async def set_brightness(self, brightness: int | Sequence[int] | Mapping[str | int, int]) -> None:
        await asyncio.sleep(0)
        if isinstance(brightness, int):
            self._set_uniform_brightness(brightness)
        elif isinstance(brightness, Mapping):
            self._set_mapped_brightness(brightness)
        else:
            self._set_sequence_brightness(brightness)

    def _set_uniform_brightness(self, brightness: int) -> None:
        for color in self._brightness:
            self._brightness[color] = brightness

    def _set_mapped_brightness(self, brightness: Mapping[str | int, int]) -> None:
        for color, level in brightness.items():
            if isinstance(color, str) and color in self._brightness:
                self._brightness[color] = level

    def _set_sequence_brightness(self, brightness: Sequence[int]) -> None:
        for color, level in zip(self._brightness, brightness, strict=False):
            self._brightness[color] = level

    async def turn_on(self) -> None:
        await self.set_brightness(100)

    async def turn_off(self) -> None:
        await self.set_brightness(0)

    async def enable_auto_mode(self, timestamp: datetime | None = None) -> None:
        del timestamp
        self._auto_mode = True
        await self.query_status()

    async def set_manual_mode(self) -> None:
        self._auto_mode = False

    async def set_auto_point(self, channel: int, minutes: int, level: int) -> None:
        await asyncio.sleep(0)
        self._auto_curve_points.append((channel, minutes, level))

    async def set_auto_curve(self, points: Sequence[tuple[int, int, int]]) -> None:
        await asyncio.sleep(0)
        self._auto_curve_points.extend(points)

    async def add_setting(self, sunrise, sunset, max_brightness=100, ramp_up_in_minutes=0, weekdays=None) -> None:
        del sunrise, sunset, max_brightness, ramp_up_in_minutes, weekdays
        await self.query_status()

    async def remove_setting(self, sunrise, sunset, ramp_up_in_minutes=0, weekdays=None) -> None:
        del sunrise, sunset, ramp_up_in_minutes, weekdays
        await self.query_status()

    async def reset_settings(self) -> None:
        self._auto_curve_points.clear()
        await self.query_status()

    async def set_fan_speed(self, speed_percent: int) -> None:
        await asyncio.sleep(0)
        self._require_feature(LedFeature.FAN)
        if speed_percent < 0 or speed_percent > 100:
            raise ValueError("Fan speed must be between 0 and 100 percent")
        if 0 < speed_percent < self.model.min_fan_speed:
            speed_percent = self.model.min_fan_speed
        self._fan_speed = speed_percent
        self._fan_auto = False
        self.last_fan_status_notification = FanStatusNotification(27, speed_percent * 20, 25)
        self._notify_callbacks(self.last_fan_status_notification)

    async def set_fan_auto(self) -> None:
        await asyncio.sleep(0)
        self._require_feature(LedFeature.FAN)
        self._fan_auto = True

    async def set_fan_start_stop_temp(self, start_temp: int, stop_temp: int) -> None:
        await asyncio.sleep(0)
        self._require_feature(LedFeature.FAN)
        self._fan_start_temp = start_temp
        self._fan_stop_temp = stop_temp

    async def set_temp_protect(self, enabled: bool) -> None:
        await asyncio.sleep(0)
        self._require_feature(LedFeature.TEMPERATURE_PROTECTION)
        self._temp_protect = enabled

    async def set_bluetooth_led(self, enabled: bool) -> None:
        await asyncio.sleep(0)
        self._require_feature(LedFeature.INDICATOR_LED)
        self._bluetooth_led = enabled

    def _require_feature(self, feature: LedFeature) -> None:
        spec = self.model.spec
        if not isinstance(spec, LedSpec) or feature not in spec.features:
            raise ValueError(f"Model does not support {feature.value}: {self.model.name}")

    @property
    def fan_auto(self) -> bool:
        return self._fan_auto

    @property
    def fan_start_temp(self) -> int:
        return self._fan_start_temp

    @property
    def fan_stop_temp(self) -> int:
        return self._fan_stop_temp

    @property
    def temp_protect(self) -> bool:
        return self._temp_protect

    @property
    def bluetooth_led(self) -> bool:
        return self._bluetooth_led

    def _schedule_point(self, hour: int, minute: int, level: int) -> SchedulePoint:
        return SchedulePoint(hour=hour, minute=minute, levels={color: level for color in self.colors})


__all__ = ["FakeLedDevice"]

"""Development-only fake Chihiros devices for local Home Assistant testing."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from .dosing import normalize_pump_count
from .vendor.chihiros_led_control.models import (
    DOSING_PUMP,
    RGB_CHANNELS,
    WHITE_CHANNELS,
    WRGB_CHANNELS,
    X300_CHANNELS,
    DeviceModel,
)
from .vendor.chihiros_led_control.protocol import (
    DosingDailyNotification,
    DosingTotalsNotification,
    FanStatusNotification,
    ParsedNotification,
    RuntimeNotification,
    SchedulePoint,
    ScheduleSnapshotNotification,
)

FAKE_DEVICES_ENV = "CHIHIROS_FAKE_DEVICES"
FAKE_ADDRESS_PREFIX = "FA:CE:C0"


@dataclass(frozen=True)
class FakeChihirosDeviceInfo:
    """Static fake device metadata."""

    address: str
    name: str
    model: DeviceModel


FAKE_DEVICES = (
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:01",
        name="DYNW60-fake",
        model=DeviceModel("Fake WRGB II", ("DYNW60",), RGB_CHANNELS),
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:02",
        name="DYWPRO60-fake",
        model=DeviceModel("Fake WRGB II Pro", ("DYWPRO60",), WRGB_CHANNELS),
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:03",
        name="DYNA2-fake",
        model=DeviceModel("Fake A II", ("DYNA2",), WHITE_CHANNELS),
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:04",
        name="DYDOSE-fake",
        model=DOSING_PUMP,
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:05",
        name="DYVVD3-fake",
        model=DeviceModel(
            "Fake WRGB VIVID III",
            ("DYVVD3",),
            WRGB_CHANNELS,
            has_fan=True,
            min_fan_speed=25,
        ),
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:06",
        name="DYA-fake",
        model=DeviceModel("Fake A Series", ("DYA",), WHITE_CHANNELS),
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:07",
        name="DYC-fake",
        model=DeviceModel("Fake New C", ("DYC",), WHITE_CHANNELS),
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:08",
        name="DYARGB-fake",
        model=DeviceModel("Fake RGB+APLUS", ("DYARGB",), RGB_CHANNELS),
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:09",
        name="DYREE-fake",
        model=DeviceModel("Fake RGB VIVID", ("DYREE",), RGB_CHANNELS),
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:0A",
        name="DYRGBV-fake",
        model=DeviceModel("Fake RGB VIVID II", ("DYRGBV",), RGB_CHANNELS),
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:0B",
        name="DYSEA-fake",
        model=DeviceModel("Fake SEA_LED", ("DYSEA",), WRGB_CHANNELS),
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:0C",
        name="DYONE-fake",
        model=DeviceModel("Fake Commander X", ("DYONE",), WHITE_CHANNELS),
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:0D",
        name="DYTWO-fake",
        model=DeviceModel("Fake X300", ("DYTWO",), X300_CHANNELS),
    ),
    FakeChihirosDeviceInfo(
        address=f"{FAKE_ADDRESS_PREFIX}:00:00:0E",
        name="DYNLED-fake",
        model=DeviceModel(
            "Fake Commander 4",
            ("DYNLED",),
            WRGB_CHANNELS,
            sea_led_family=True,
        ),
    ),
)
FAKE_DEVICES_BY_ADDRESS = {device.address: device for device in FAKE_DEVICES}

NotificationCallback = Callable[[ParsedNotification], None]


def fake_devices_enabled() -> bool:
    """Return whether fake devices are enabled for local development."""
    return os.environ.get(FAKE_DEVICES_ENV, "").lower() in {"1", "true", "yes", "on"}


def is_fake_address(address: str) -> bool:
    """Return whether an address belongs to a configured fake device."""
    return address in FAKE_DEVICES_BY_ADDRESS


def create_fake_device(address: str, pump_count: int = 4) -> FakeChihirosDevice:
    """Create a fake Chihiros device from a fake address."""
    return FakeChihirosDevice(FAKE_DEVICES_BY_ADDRESS[address], pump_count)


def iter_enabled_fake_devices(current_addresses: Iterable[str]) -> tuple[FakeChihirosDeviceInfo, ...]:
    """Return fake devices that can be shown in discovery."""
    if not fake_devices_enabled():
        return ()
    configured_addresses = set(current_addresses)
    return tuple(device for device in FAKE_DEVICES if device.address not in configured_addresses)


class FakeChihirosDevice:
    """Small in-memory Chihiros device replacement for HA UI testing."""

    def __init__(self, device_info: FakeChihirosDeviceInfo, pump_count: int = 4) -> None:
        """Initialize the fake device."""
        self._device_info = device_info
        self.pump_count = normalize_pump_count(pump_count)
        self.model = device_info.model
        self._callbacks: set[NotificationCallback] = set()
        self._brightness = {color: 0 for color in self.model.color_channels}
        self._dosed_ml = [0.0] * self.pump_count
        self._auto_mode = False
        self._auto_curve_points: list[tuple[int, int, int]] = []
        self._fan_speed = 0
        self._fan_auto = False
        self._fan_start_temp = 38
        self._fan_stop_temp = 33
        self.last_runtime_notification: RuntimeNotification | None = None
        self.last_fan_status_notification: FanStatusNotification | None = None
        self.last_schedule_snapshot_notification: ScheduleSnapshotNotification | None = None
        self.last_dosing_totals_notification: DosingTotalsNotification | None = None
        self.last_dosing_daily_notification: DosingDailyNotification | None = None

    @property
    def address(self) -> str:
        """Return the fake BLE address."""
        return self._device_info.address

    @property
    def name(self) -> str:
        """Return the fake device name."""
        return self._device_info.name

    @property
    def model_name(self) -> str:
        """Return the fake model name."""
        return self.model.name

    @property
    def colors(self) -> dict[str, int]:
        """Return supported fake color channels."""
        return dict(self.model.color_channels)

    def add_notification_callback(self, callback: NotificationCallback) -> Callable[[], None]:
        """Register a callback for fake parsed notifications."""
        self._callbacks.add(callback)

        def remove_callback() -> None:
            self._callbacks.discard(callback)

        return remove_callback

    async def query_status(self) -> None:
        """Publish fake runtime and schedule notifications."""
        await asyncio.sleep(0)
        self.last_runtime_notification = RuntimeNotification(
            firmware_version=23,
            runtime_minutes=511,
            raw=bytes.fromhex("5b 17 0a 00 01 0a 01 ff ff ff ff 0c 36 2d"),
        )
        self.last_schedule_snapshot_notification = ScheduleSnapshotNotification(
            firmware_version=23,
            points=(
                self._schedule_point(8, 0, 15),
                self._schedule_point(12, 0, 70),
                self._schedule_point(20, 30, 0),
            ),
        )
        self._notify_callbacks(self.last_runtime_notification)
        self._notify_callbacks(self.last_schedule_snapshot_notification)
        if self.model.name == DOSING_PUMP.name:
            self.last_dosing_totals_notification = self._dosing_totals_notification()
            self.last_dosing_daily_notification = self._dosing_daily_notification()
            self._notify_callbacks(self.last_dosing_totals_notification)
            self._notify_callbacks(self.last_dosing_daily_notification)

    async def set_brightness(self, brightness: int | Sequence[int] | Mapping[str | int, int]) -> None:
        """Set fake brightness state."""
        await asyncio.sleep(0)
        if isinstance(brightness, int):
            for color in self._brightness:
                self._brightness[color] = brightness
            return
        if isinstance(brightness, Mapping):
            for color, level in brightness.items():
                if isinstance(color, str) and color in self._brightness:
                    self._brightness[color] = level
            return
        for color, level in zip(self._brightness, brightness, strict=False):
            self._brightness[color] = level

    async def turn_on(self) -> None:
        """Turn on all fake channels."""
        await self.set_brightness(100)

    async def turn_off(self) -> None:
        """Turn off all fake channels."""
        await self.set_brightness(0)

    async def enable_auto_mode(self, timestamp: datetime | None = None) -> None:
        """Enable fake auto mode."""
        del timestamp
        self._auto_mode = True
        await self.query_status()

    async def set_manual_mode(self) -> None:
        """Enable fake manual mode."""
        self._auto_mode = False

    async def set_fan_speed(self, speed_percent: int) -> None:
        """Set fake fan speed and publish a fake fan status notification."""
        await asyncio.sleep(0)
        if not self.model.has_fan:
            raise ValueError(f"Model does not support fan control: {self.model.name}")
        if speed_percent < 0 or speed_percent > 100:
            raise ValueError("Fan speed must be between 0 and 100 percent")
        if 0 < speed_percent < self.model.min_fan_speed:
            speed_percent = self.model.min_fan_speed
        self._fan_speed = speed_percent
        self._fan_auto = False
        self.last_fan_status_notification = FanStatusNotification(
            firmware_version=27,
            fan_rpm=speed_percent * 20,
            temperature_celsius=25,
        )
        self._notify_callbacks(self.last_fan_status_notification)

    async def set_fan_auto(self) -> None:
        """Switch the fake fan to temperature-controlled auto mode."""
        await asyncio.sleep(0)
        if not self.model.has_fan:
            raise ValueError(f"Model does not support fan control: {self.model.name}")
        self._fan_auto = True

    async def set_fan_start_stop_temp(self, start_temp: int, stop_temp: int) -> None:
        """Store the fake fan auto-mode start/stop temperatures."""
        await asyncio.sleep(0)
        if not self.model.has_fan:
            raise ValueError(f"Model does not support fan control: {self.model.name}")
        self._fan_start_temp = start_temp
        self._fan_stop_temp = stop_temp

    @property
    def fan_auto(self) -> bool:
        """Return whether the fake fan is in auto mode."""
        return self._fan_auto

    @property
    def fan_start_temp(self) -> int:
        """Return the fake fan start temperature."""
        return self._fan_start_temp

    @property
    def fan_stop_temp(self) -> int:
        """Return the fake fan stop temperature."""
        return self._fan_stop_temp

    async def add_setting(
        self,
        sunrise: datetime,
        sunset: datetime,
        max_brightness: int | Sequence[int] | Mapping[str | int, int] = 100,
        ramp_up_in_minutes: int = 0,
        weekdays: list[object] | None = None,
    ) -> None:
        """Accept fake schedule writes."""
        del sunrise, sunset, max_brightness, ramp_up_in_minutes, weekdays
        await self.query_status()

    async def remove_setting(
        self,
        sunrise: datetime,
        sunset: datetime,
        ramp_up_in_minutes: int = 0,
        weekdays: list[object] | None = None,
    ) -> None:
        """Accept fake schedule deletes."""
        del sunrise, sunset, ramp_up_in_minutes, weekdays
        await self.query_status()

    async def reset_settings(self) -> None:
        """Accept fake schedule resets."""
        self._auto_curve_points.clear()
        await self.query_status()

    async def set_auto_point(self, channel: int, minutes: int, level: int) -> None:
        """Record a fake auto-curve point."""
        await asyncio.sleep(0)
        self._auto_curve_points.append((channel, minutes, level))

    async def set_auto_curve(self, points: Sequence[tuple[int, int, int]]) -> None:
        """Record a fake auto-curve batch."""
        await asyncio.sleep(0)
        self._auto_curve_points.extend(points)

    async def dose_ml(self, pump_idx: int, volume_ml: float) -> None:
        """Record a fake manual dose for local dosing pump testing."""
        await asyncio.sleep(0)
        self._dosed_ml[pump_idx] = round(self._dosed_ml[pump_idx] + volume_ml, 1)
        self.last_dosing_totals_notification = self._dosing_totals_notification()
        self.last_dosing_daily_notification = self._dosing_daily_notification()
        self._notify_callbacks(self.last_dosing_totals_notification)
        self._notify_callbacks(self.last_dosing_daily_notification)

    def _dosing_totals_notification(self) -> DosingTotalsNotification:
        """Return the fake pump's lifetime totals as a device notification."""
        return DosingTotalsNotification(
            tuple(round(volume * 1000) for volume in self._dosed_ml),
            raw=b"",
        )

    def _dosing_daily_notification(self) -> DosingDailyNotification:
        """Return the fake pump's dosed-today totals as a device notification."""
        return DosingDailyNotification(
            tuple(round(volume * 1000) for volume in self._dosed_ml),
            raw=b"",
        )

    async def disconnect(self) -> None:
        """Disconnect the fake device."""
        await asyncio.sleep(0)

    def _schedule_point(self, hour: int, minute: int, level: int) -> SchedulePoint:
        """Create a schedule point for all fake channels."""
        return SchedulePoint(
            hour=hour,
            minute=minute,
            levels={color: level for color in self.model.color_channels},
        )

    def _notify_callbacks(self, notification: ParsedNotification) -> None:
        """Notify fake subscribers."""
        for callback in tuple(self._callbacks):
            callback(notification)

"""Development-only fake Chihiros devices for local Home Assistant testing."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from .vendor.chihiros_led_control.models import RGB_CHANNELS, WHITE_CHANNELS, WRGB_CHANNELS, DeviceModel
from .vendor.chihiros_led_control.protocol import (
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
)
FAKE_DEVICES_BY_ADDRESS = {device.address: device for device in FAKE_DEVICES}

NotificationCallback = Callable[[ParsedNotification], None]


def fake_devices_enabled() -> bool:
    """Return whether fake devices are enabled for local development."""
    return os.environ.get(FAKE_DEVICES_ENV, "").lower() in {"1", "true", "yes", "on"}


def is_fake_address(address: str) -> bool:
    """Return whether an address belongs to a configured fake device."""
    return address in FAKE_DEVICES_BY_ADDRESS


def create_fake_device(address: str) -> FakeChihirosDevice:
    """Create a fake Chihiros device from a fake address."""
    return FakeChihirosDevice(FAKE_DEVICES_BY_ADDRESS[address])


class FakeChihirosDevice:
    """Small in-memory Chihiros device replacement for HA UI testing."""

    def __init__(self, device_info: FakeChihirosDeviceInfo) -> None:
        """Initialize the fake device."""
        self._device_info = device_info
        self.model = device_info.model
        self._callbacks: set[NotificationCallback] = set()
        self._brightness = {color: 0 for color in self.model.color_channels}
        self._auto_mode = False
        self.last_runtime_notification: RuntimeNotification | None = None
        self.last_schedule_snapshot_notification: ScheduleSnapshotNotification | None = None

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
        self.last_runtime_notification = RuntimeNotification(firmware_version=23, runtime_minutes=511)
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

    async def enable_auto_mode(self) -> None:
        """Enable fake auto mode."""
        self._auto_mode = True
        await self.query_status()

    async def set_manual_mode(self) -> None:
        """Enable fake manual mode."""
        self._auto_mode = False
        await self.turn_on()

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
        await self.query_status()

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

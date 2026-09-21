"""Runtime device resolution for the Home Assistant integration."""
# ruff: noqa: D102

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from bleak.backends.device import BLEDevice
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .dosing import entry_pump_count
from .fake import create_fake_device, fake_devices_enabled, is_fake_address
from .vendor.chihiros_led_control import create_device, needs_device_type
from .vendor.chihiros_led_control.exceptions import UnsupportedDeviceError
from .vendor.chihiros_led_control.models import DeviceKind, DeviceModel, LedFeature, LedSpec
from .vendor.chihiros_led_control.protocol.led import (
    FanStatusNotification,
    RuntimeNotification,
    ScheduleSnapshotNotification,
)
from .vendor.chihiros_led_control.protocol.notifications import ParsedNotification
from .vendor.chihiros_led_control.weekday_encoding import WeekdaySelect

NotificationCallback = Callable[[ParsedNotification], None]


def is_device_kind(device: object, kind: DeviceKind) -> bool:
    """Return whether a runtime device belongs to a family."""
    candidate = getattr(device, "device_kind", None)
    return candidate is kind or candidate == kind or candidate == kind.value


def has_led_feature(device: object, feature: LedFeature) -> bool:
    """Return whether an LED profile advertises an optional feature."""
    model = getattr(device, "model", None)
    spec = getattr(model, "spec", None)
    return isinstance(spec, LedSpec) and feature in spec.features


class BaseChihirosClient(Protocol):
    """Common identity, notification, status, and lifecycle operations."""

    model: DeviceModel
    last_runtime_notification: RuntimeNotification | None
    last_fan_status_notification: FanStatusNotification | None
    last_schedule_snapshot_notification: ScheduleSnapshotNotification | None

    @property
    def device_kind(self) -> DeviceKind:
        """Return the device-family discriminator."""

    @property
    def address(self) -> str:
        """Return the device address."""

    @property
    def name(self) -> str:
        """Return the advertised/display name."""

    @property
    def model_name(self) -> str:
        """Return the model name."""

    def add_notification_callback(self, callback: NotificationCallback) -> Callable[[], None]:
        """Register a parsed notification callback."""

    async def query_status(self) -> None:
        """Request the latest status snapshot."""

    async def disconnect(self) -> None:
        """Disconnect the client."""


class LedChihirosClient(BaseChihirosClient, Protocol):
    """Home Assistant-facing LED client surface."""

    @property
    def colors(self) -> Mapping[str, int]:
        """Return supported color channels."""

    @property
    def fan_auto(self) -> bool:
        """Return whether the fan is in automatic mode."""

    @property
    def fan_start_temp(self) -> int:
        """Return the fan start temperature."""

    @property
    def fan_stop_temp(self) -> int:
        """Return the fan stop temperature."""

    @property
    def temp_protect(self) -> bool:
        """Return whether temperature protection is enabled."""

    @property
    def bluetooth_led(self) -> bool:
        """Return whether the indicator LED is enabled."""

    async def set_brightness(self, brightness: int | Sequence[int] | Mapping[str | int, int]) -> None: ...

    async def turn_on(self) -> None: ...

    async def turn_off(self) -> None: ...

    async def enable_auto_mode(self, timestamp: datetime | None = None) -> None: ...

    async def set_manual_mode(self) -> None: ...

    async def set_auto_point(self, channel: int, minutes: int, level: int) -> None: ...

    async def set_auto_curve(self, points: Sequence[tuple[int, int, int]]) -> None: ...

    async def set_fan_speed(self, speed_percent: int) -> None: ...

    async def set_fan_auto(self) -> None: ...

    async def set_fan_start_stop_temp(self, start_temp: int, stop_temp: int) -> None: ...

    async def set_temp_protect(self, enabled: bool) -> None: ...

    async def set_bluetooth_led(self, enabled: bool) -> None: ...

    async def add_setting(
        self,
        sunrise: datetime,
        sunset: datetime,
        max_brightness: int | Sequence[int] | Mapping[str | int, int] = 100,
        ramp_up_in_minutes: int = 0,
        weekdays: list[WeekdaySelect] | None = None,
    ) -> None: ...

    async def remove_setting(
        self,
        sunrise: datetime,
        sunset: datetime,
        ramp_up_in_minutes: int = 0,
        weekdays: list[WeekdaySelect] | None = None,
    ) -> None: ...

    async def reset_settings(self) -> None: ...


class DosingChihirosClient(BaseChihirosClient, Protocol):
    """Home Assistant-facing dosing pump client surface."""

    async def dose_ml(self, pump_idx: int, volume_ml: float) -> bytes: ...

    async def reset_channel(self, channel: int) -> bytes: ...

    async def calibrate_channel(
        self,
        channel: int,
        *,
        seconds: int | None = None,
        volume_ml: float | None = None,
    ) -> bytes: ...

    async def set_channel_active(self, channel: int, *, active: bool = True, compensate: bool = False) -> None: ...

    async def apply_dosing_settings(
        self,
        channel: int,
        dose_per_day_ml: float | None,
        frequency: int = 127,
        *,
        is_first_setting: bool = True,
    ) -> None: ...

    async def set_schedule(self, channel: int, mode: object, points: Sequence[object]) -> None: ...

    async def set_dose_delay(self, enabled: bool) -> None: ...

    async def program_channel(self, channel: int, **kwargs: Any) -> None: ...


class StirrerChihirosClient(BaseChihirosClient, Protocol):
    """Home Assistant-facing magnetic stirrer client surface."""

    async def stir(self, channel: int, on: bool, *, seconds: int | None = None) -> None: ...

    async def set_pre_second(
        self,
        channel: int,
        seconds: int,
        speed: int = 40,
        *,
        restart: bool = False,
    ) -> None: ...

    async def set_stir_schedule(
        self,
        channel: int,
        points: Sequence[object],
        *,
        frequency: int = 127,
        active: bool = True,
        is_first_setting: bool = True,
    ) -> None: ...

    async def send_frame(self, frame: bytes | bytearray) -> None: ...

    async def set_dose_delay(self, enabled: bool) -> None: ...


class HeaterChihirosClient(BaseChihirosClient, Protocol):
    """Home Assistant-facing heater client surface."""

    @property
    def setting_temperature_celsius(self) -> float: ...

    @property
    def power_watts(self) -> int: ...

    @property
    def protector_temperature_celsius(self) -> float: ...

    @property
    def auto_default_temperature_celsius(self) -> float: ...

    @property
    def auto_default_power_watts(self) -> int: ...

    @property
    def auto_heating(self) -> bool: ...

    @property
    def is_celsius(self) -> bool: ...

    @property
    def backlight(self) -> bool: ...

    async def set_temperature(self, temperature_c: float) -> None: ...

    async def set_power(self, power_watts: int) -> None: ...

    async def set_manual_state(self, temperature_c: float, power_watts: int) -> None: ...

    async def set_manual_mode(self) -> None: ...

    async def apply_scene(self) -> None: ...

    async def set_auto_default_temperature(self, temperature_c: float) -> None: ...

    async def set_auto_default_power(self, power_watts: int) -> None: ...

    def restore_setting_temperature(self, temperature_c: float) -> None: ...

    def restore_manual_power(self, power_watts: int) -> None: ...

    def restore_auto_default_temperature(self, temperature_c: float) -> None: ...

    def restore_auto_default_power(self, power_watts: int) -> None: ...

    async def set_auto_heating(self, enabled: bool) -> None: ...

    async def set_temperature_unit(self, *, celsius: bool) -> None: ...

    async def set_backlight(self, enabled: bool) -> None: ...

    async def set_protector_temperature(self, temperature_c: float) -> None: ...

    async def calibrate(self, measured_temperature_c: float) -> None: ...

    async def reset_work_time(self) -> None: ...


@dataclass(frozen=True)
class ChihirosRuntime:
    """Resolved runtime device data for a config entry."""

    client: BaseChihirosClient
    address: str
    always_available: bool = False


def _resolve_fake_runtime(address: str, entry: ConfigEntry) -> ChihirosRuntime:
    return ChihirosRuntime(
        client=create_fake_device(address, entry_pump_count(entry)), address=address, always_available=True
    )


def _apply_entry_name(ble_device: BLEDevice, entry: ConfigEntry) -> None:
    entry_name = entry.data.get(CONF_NAME)
    if not entry_name:
        return
    try:
        ble_device.name = entry_name
    except Exception:
        pass


async def resolve_chihiros_runtime(hass: HomeAssistant, entry: ConfigEntry) -> ChihirosRuntime:
    """Resolve a config entry to a real BLE client or development fake."""
    if entry.unique_id is None:
        raise ConfigEntryNotReady(f"Entry doesn't have any unique_id {entry.title}")
    address: str = entry.unique_id
    if fake_devices_enabled() and is_fake_address(address):
        return _resolve_fake_runtime(address, entry)
    return await _resolve_ble_runtime(hass, entry, address)


async def _resolve_ble_runtime(hass: HomeAssistant, entry: ConfigEntry, address: str) -> ChihirosRuntime:
    """Resolve a config entry to a real BLE client."""
    ble_device = bluetooth.async_ble_device_from_address(hass, address.upper(), True)
    if not ble_device:
        raise ConfigEntryNotReady(f"Could not find Chihiros BLE device with address {address}")
    if not ble_device.name:
        raise ConfigEntryNotReady(f"Found Chihiros BLE device with address {address} but can not find its name")
    if needs_device_type(ble_device.name):
        _apply_entry_name(ble_device, entry)
    try:
        client = create_device(ble_device, device_type=entry.data.get("device_type"))
    except UnsupportedDeviceError as ex:
        raise ConfigEntryNotReady(str(ex)) from ex
    return ChihirosRuntime(client=client, address=ble_device.address)

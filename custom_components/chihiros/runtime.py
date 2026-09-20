"""Runtime device resolution for the Chihiros Home Assistant integration."""

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
from .vendor.chihiros_led_control.models import DeviceModel
from .vendor.chihiros_led_control.protocol import (
    FanStatusNotification,
    ParsedNotification,
    RuntimeNotification,
    ScheduleSnapshotNotification,
)
from .vendor.chihiros_led_control.weekday_encoding import WeekdaySelect

NotificationCallback = Callable[[ParsedNotification], None]


class DosingChihirosClient(Protocol):
    """Home Assistant-facing dosing pump client surface."""

    async def dose_ml(self, pump_idx: int, volume_ml: float) -> bytes:
        """Dose a volume in mL on a dosing pump channel and return the frame."""

    async def calibrate_channel(
        self,
        channel: int,
        *,
        seconds: int | None = None,
        volume_ml: float | None = None,
    ) -> bytes:
        """Run one channel's calibration test dose or record its measured volume."""


class StirrerChihirosClient(Protocol):
    """Home Assistant-facing magnetic stirrer client surface."""

    async def stir(self, channel: int, on: bool, *, seconds: int | None = None) -> None:
        """Manually start/stop one stir channel."""

    async def set_pre_second(
        self,
        channel: int,
        seconds: int,
        speed: int = 40,
        *,
        restart: bool = False,
    ) -> None:
        """Set a channel's pre-stir time and stir speed."""

    async def set_stir_schedule(
        self,
        channel: int,
        points: Sequence[object],
        *,
        frequency: int = 127,
        active: bool = True,
        is_first_setting: bool = True,
    ) -> None:
        """Replace one channel's timer-mode schedule."""

    async def program_channel(self, channel: int, **kwargs: Any) -> None:
        """Program one channel in a single transaction (mirror replay)."""

    async def send_frame(self, frame: bytes | bytearray) -> None:
        """Send a pre-built frame verbatim (broadcast replay)."""

    async def set_dose_delay(self, enabled: bool) -> None:
        """Set the dose-delay flag mirrored from the master."""


class HeaterChihirosClient(Protocol):
    """Home Assistant-facing Chihiros heater client surface."""

    @property
    def setting_temperature_celsius(self) -> float:
        """Return the last known target temperature."""

    @property
    def power_watts(self) -> int:
        """Return the tracked manual power in watts."""

    @property
    def protector_temperature_celsius(self) -> float:
        """Return the tracked overheat protection temperature."""

    @property
    def auto_default_temperature_celsius(self) -> float:
        """Return the tracked auto-mode default temperature."""

    @property
    def auto_default_power_watts(self) -> int:
        """Return the tracked auto-mode default power in watts."""

    @property
    def auto_heating(self) -> bool:
        """Return whether auto heating is enabled."""

    @property
    def is_celsius(self) -> bool:
        """Return whether the device displays Celsius (as opposed to Fahrenheit)."""

    @property
    def backlight(self) -> bool:
        """Return whether the tracked display-backlight state is on."""

    async def set_temperature(self, temperature_c: float) -> None:
        """Set the target temperature and switch to manual mode."""

    async def set_power(self, power_watts: int) -> None:
        """Set the manual power in watts and switch to manual mode."""

    async def set_manual_state(self, temperature_c: float, power_watts: int) -> None:
        """Atomically set both manual values and switch to manual mode."""

    async def set_manual_mode(self) -> None:
        """Switch to manual mode without writing a setpoint."""

    async def apply_scene(self) -> None:
        """Apply the stored auto schedule."""

    async def set_auto_default_temperature(self, temperature_c: float) -> None:
        """Set the auto-mode default temperature, resending the tracked power."""

    async def set_auto_default_power(self, power_watts: int) -> None:
        """Set the auto-mode default power, resending the tracked temperature."""

    def restore_setting_temperature(self, temperature_c: float) -> None:
        """Restore tracked manual target temperature without writing to the device."""

    def restore_manual_power(self, power_watts: int) -> None:
        """Restore tracked manual power without writing to the device."""

    def restore_auto_default_temperature(self, temperature_c: float) -> None:
        """Restore tracked auto temperature without writing to the device."""

    def restore_auto_default_power(self, power_watts: int) -> None:
        """Restore tracked auto power without writing to the device."""

    async def set_auto_heating(self, enabled: bool) -> None:
        """Enable or disable the heating element in auto mode."""

    async def set_temperature_unit(self, *, celsius: bool) -> None:
        """Set the device's display unit."""

    async def set_backlight(self, enabled: bool) -> None:
        """Turn the device's display backlight on or off."""

    async def set_protector_temperature(self, temperature_c: float) -> None:
        """Set the overheat protection temperature."""

    async def calibrate(self, measured_temperature_c: float) -> None:
        """Calibrate the sensor against a measured reference temperature."""

    async def reset_work_time(self) -> None:
        """Zero the runtime counter that drives the cleaning warning."""


class ChihirosClient(Protocol):
    """Home Assistant-facing device client surface."""

    model: DeviceModel
    last_runtime_notification: RuntimeNotification | None
    last_fan_status_notification: FanStatusNotification | None
    last_schedule_snapshot_notification: ScheduleSnapshotNotification | None

    @property
    def fan_auto(self) -> bool:
        """Return whether the fan is in temperature-controlled auto mode."""

    @property
    def fan_start_temp(self) -> int:
        """Return the last fan start temperature in whole degrees Celsius."""

    @property
    def fan_stop_temp(self) -> int:
        """Return the last fan stop temperature in whole degrees Celsius."""

    @property
    def address(self) -> str:
        """Return the device address."""

    @property
    def name(self) -> str:
        """Return the device name."""

    @property
    def model_name(self) -> str:
        """Return the model name."""

    @property
    def colors(self) -> dict[str, int]:
        """Return supported color channels."""

    def add_notification_callback(self, callback: NotificationCallback) -> Callable[[], None]:
        """Register a parsed notification callback."""

    async def query_status(self) -> None:
        """Request a current runtime/status snapshot."""

    async def set_brightness(self, brightness: int | Sequence[int] | Mapping[str | int, int]) -> None:
        """Set device brightness."""

    async def turn_on(self) -> None:
        """Turn the device on."""

    async def turn_off(self) -> None:
        """Turn the device off."""

    async def enable_auto_mode(self, timestamp: datetime | None = None) -> None:
        """Enable automatic mode."""

    async def set_manual_mode(self) -> None:
        """Enable manual mode."""

    async def set_auto_point(self, channel: int, minutes: int, level: int) -> None:
        """Write one auto-curve point for a channel."""

    async def set_auto_curve(self, points: Sequence[tuple[int, int, int]]) -> None:
        """Replace the device's auto curve in one transaction."""

    async def set_fan_speed(self, speed_percent: int) -> None:
        """Set the fan speed percentage on fan-equipped models."""

    async def set_fan_auto(self) -> None:
        """Switch the fan to temperature-controlled auto mode."""

    async def set_fan_start_stop_temp(self, start_temp: int, stop_temp: int) -> None:
        """Set the fan start/stop temperatures used by auto mode."""

    async def add_setting(
        self,
        sunrise: datetime,
        sunset: datetime,
        max_brightness: int | Sequence[int] | Mapping[str | int, int] = 100,
        ramp_up_in_minutes: int = 0,
        weekdays: list[WeekdaySelect] | None = None,
    ) -> None:
        """Add a schedule setting."""

    async def remove_setting(
        self,
        sunrise: datetime,
        sunset: datetime,
        ramp_up_in_minutes: int = 0,
        weekdays: list[WeekdaySelect] | None = None,
    ) -> None:
        """Remove a schedule setting."""

    async def reset_settings(self) -> None:
        """Reset schedule settings."""

    async def disconnect(self) -> None:
        """Disconnect the client."""


@dataclass(frozen=True)
class ChihirosRuntime:
    """Resolved runtime device data for a config entry."""

    client: ChihirosClient
    address: str
    always_available: bool = False


def _resolve_fake_runtime(address: str, entry: ConfigEntry) -> ChihirosRuntime:
    """Build a fake development client for a fake device address."""
    return ChihirosRuntime(
        client=create_fake_device(address, entry_pump_count(entry)),
        address=address,
        always_available=True,
    )


def _apply_entry_name(ble_device: BLEDevice, entry: ConfigEntry) -> None:
    """Fall back to the entry title for devices that advertise without a name."""
    entry_name = entry.data.get(CONF_NAME)
    if not entry_name:
        return
    try:
        ble_device.name = entry_name
    except Exception:
        pass


async def resolve_chihiros_runtime(hass: HomeAssistant, entry: ConfigEntry) -> ChihirosRuntime:
    """Resolve a config entry to either a real BLE client or a development fake client."""
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

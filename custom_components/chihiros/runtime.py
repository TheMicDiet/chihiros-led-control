"""Runtime device resolution for the Chihiros Home Assistant integration."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .dosing import CONF_PUMP_COUNT, normalize_pump_count
from .fake import create_fake_device, fake_devices_enabled, is_fake_address
from .vendor.chihiros_led_control import create_device, needs_device_type
from .vendor.chihiros_led_control.models import DeviceModel
from .vendor.chihiros_led_control.protocol import ParsedNotification, RuntimeNotification, ScheduleSnapshotNotification
from .vendor.chihiros_led_control.weekday_encoding import WeekdaySelect

NotificationCallback = Callable[[ParsedNotification], None]


class DosingChihirosClient(Protocol):
    """Home Assistant-facing dosing pump client surface."""

    async def dose_ml(self, pump_idx: int, volume_ml: float) -> None:
        """Dose a volume in mL on a dosing pump channel."""


class ChihirosClient(Protocol):
    """Home Assistant-facing device client surface."""

    model: DeviceModel
    last_runtime_notification: RuntimeNotification | None
    last_schedule_snapshot_notification: ScheduleSnapshotNotification | None

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


async def resolve_chihiros_runtime(hass: HomeAssistant, entry: ConfigEntry) -> ChihirosRuntime:
    """Resolve a config entry to either a real BLE client or a development fake client."""
    if entry.unique_id is None:
        raise ConfigEntryNotReady(f"Entry doesn't have any unique_id {entry.title}")

    address: str = entry.unique_id
    if fake_devices_enabled() and is_fake_address(address):
        return ChihirosRuntime(
            client=create_fake_device(address, normalize_pump_count(entry.data.get(CONF_PUMP_COUNT))),
            address=address,
            always_available=True,
        )

    ble_device = bluetooth.async_ble_device_from_address(hass, address.upper(), True)
    if not ble_device:
        raise ConfigEntryNotReady(f"Could not find Chihiros BLE device with address {address}")
    if not ble_device.name:
        raise ConfigEntryNotReady(f"Found Chihiros BLE device with address {address} but can not find its name")
    if needs_device_type(ble_device.name):
        entry_name = entry.data.get(CONF_NAME)
        if entry_name:
            try:
                ble_device.name = entry_name
            except Exception:
                pass

    return ChihirosRuntime(
        client=create_device(ble_device, device_type=entry.data.get("device_type")),
        address=ble_device.address,
    )

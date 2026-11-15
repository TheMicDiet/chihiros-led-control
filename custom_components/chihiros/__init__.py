"""Chihiros Home Assistant integration setup."""

from __future__ import annotations

from contextlib import AsyncExitStack

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_MASTER_ADDRESS, DOMAIN
from .coordinator import ChihirosDataUpdateCoordinator
from .dosing import (
    DosingCalibrationTracker,
    DosingDailyTotals,
    DosingProgrammingTracker,
    entry_pump_count,
    entry_stirrer_channel_count,
)
from .dosing_services import async_register_dosing_service, async_remove_dosing_service
from .master_slave_services import (
    async_register_pump_services,
    async_register_stirrer_link_services,
    async_remove_pump_services,
    async_remove_stirrer_link_services,
)
from .models import ChihirosData, DosingChihirosData, StirrerChannelState, StirrerChihirosData
from .runtime import is_device_kind, resolve_chihiros_runtime
from .schedule_services import async_register_schedule_services, async_remove_schedule_services
from .stirrer import set_stirrer_pre_run_entities_enabled
from .stirrer_services import async_register_stirrer_service, async_remove_stirrer_service
from .vendor.chihiros_led_control.models import DeviceKind

PLATFORMS: list[Platform] = [
    Platform.LIGHT,
    Platform.SWITCH,
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.BUTTON,
    Platform.FAN,
    Platform.SELECT,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Chihiros, releasing every acquired resource if setup fails."""
    runtime = await resolve_chihiros_runtime(hass, entry)
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(runtime.client.disconnect)
        coordinator = ChihirosDataUpdateCoordinator(
            hass,
            runtime.client,
            runtime.address,
            always_available=runtime.always_available,
        )
        cleanup.callback(coordinator.async_close)
        coordinator.async_start_bluetooth()
        data = await _async_create_entry_data(hass, entry, coordinator, cleanup)
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = data
        cleanup.callback(_async_remove_entry_data, hass, entry.entry_id)
        _async_update_services(hass)
        cleanup.push_async_callback(hass.config_entries.async_unload_platforms, entry, PLATFORMS)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        if isinstance(data, DosingChihirosData):
            coordinator.async_start_dosing_refresh()
        if isinstance(data, StirrerChihirosData) and entry.data.get(CONF_MASTER_ADDRESS):
            set_stirrer_pre_run_entities_enabled(hass, runtime.address, len(data.stirrer_states), enabled=True)
        cleanup.pop_all()
    return True


async def _async_create_entry_data(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: ChihirosDataUpdateCoordinator,
    cleanup: AsyncExitStack,
) -> ChihirosData:
    """Load family records while registering cleanup before their first await."""
    client = coordinator.api
    address = coordinator.address
    if is_device_kind(client, DeviceKind.MAG_STIRRER):
        states = [StirrerChannelState() for _ in range(entry_stirrer_channel_count(entry))]
        programming = DosingProgrammingTracker(hass, address)
        await programming.async_load()
        return StirrerChihirosData(entry.title, client, coordinator, states, programming)
    if is_device_kind(client, DeviceKind.DOSING_PUMP):
        totals = DosingDailyTotals(hass, address, entry_pump_count(entry))
        cleanup.callback(totals.async_close)
        await totals.async_load()
        programming = DosingProgrammingTracker(hass, address)
        await programming.async_load()
        calibration = DosingCalibrationTracker(hass, address)
        await calibration.async_load()
        return DosingChihirosData(
            entry.title, client, coordinator, totals, [1.0] * totals.pump_count, programming, calibration
        )
    return ChihirosData(entry.title, client, coordinator)


def _async_remove_entry_data(hass: HomeAssistant, entry_id: str) -> None:
    """Withdraw failed setup state and its capability-dependent services."""
    hass.data.get(DOMAIN, {}).pop(entry_id, None)
    _async_update_services(hass)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        chihiros_data: ChihirosData = hass.data[DOMAIN].pop(entry.entry_id)
        chihiros_data.coordinator.async_close()
        if isinstance(chihiros_data, DosingChihirosData):
            chihiros_data.dosing_totals.async_close()
        _async_update_services(hass)
        await chihiros_data.device.disconnect()
    return unload_ok


def _async_update_services(hass: HomeAssistant) -> None:
    """Register services for the capabilities of configured devices."""
    if _has_light_devices(hass):
        async_register_schedule_services(hass)
    else:
        async_remove_schedule_services(hass)
    if _has_dosing_devices(hass):
        async_register_dosing_service(hass)
        async_register_pump_services(hass)
    else:
        async_remove_dosing_service(hass)
        async_remove_pump_services(hass)
    if _has_stirrer_devices(hass):
        async_register_stirrer_service(hass)
        async_register_stirrer_link_services(hass)
    else:
        async_remove_stirrer_service(hass)
        async_remove_stirrer_link_services(hass)


def _has_light_devices(hass: HomeAssistant) -> bool:
    return any(is_device_kind(data.device, DeviceKind.LED) for data in hass.data.get(DOMAIN, {}).values())


def _has_dosing_devices(hass: HomeAssistant) -> bool:
    return any(is_device_kind(data.device, DeviceKind.DOSING_PUMP) for data in hass.data.get(DOMAIN, {}).values())


def _has_stirrer_devices(hass: HomeAssistant) -> bool:
    return any(is_device_kind(data.device, DeviceKind.MAG_STIRRER) for data in hass.data.get(DOMAIN, {}).values())

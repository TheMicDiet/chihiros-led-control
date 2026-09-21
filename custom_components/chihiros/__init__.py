"""Chihiros Home Assistant integration setup."""

from __future__ import annotations

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
    """Set up Chihiros from a config entry."""
    runtime = await resolve_chihiros_runtime(hass, entry)
    coordinator = ChihirosDataUpdateCoordinator(
        hass,
        runtime.client,
        runtime.address,
        always_available=runtime.always_available,
    )
    coordinator.async_start_bluetooth()

    if is_device_kind(runtime.client, DeviceKind.MAG_STIRRER):
        stirrer_states = [StirrerChannelState() for _ in range(entry_stirrer_channel_count(entry))]
        programming = DosingProgrammingTracker(hass, runtime.address)
        await programming.async_load()
        data: ChihirosData = StirrerChihirosData(entry.title, runtime.client, coordinator, stirrer_states, programming)
    elif is_device_kind(runtime.client, DeviceKind.DOSING_PUMP):
        totals = DosingDailyTotals(hass, runtime.address, entry_pump_count(entry))
        await totals.async_load()
        programming = DosingProgrammingTracker(hass, runtime.address)
        await programming.async_load()
        calibration = DosingCalibrationTracker(hass, runtime.address)
        await calibration.async_load()
        data = DosingChihirosData(
            entry.title,
            runtime.client,
            coordinator,
            totals,
            [1.0] * totals.pump_count,
            programming,
            calibration,
        )
    else:
        data = ChihirosData(entry.title, runtime.client, coordinator)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = data
    _async_update_services(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if isinstance(data, StirrerChihirosData) and entry.data.get(CONF_MASTER_ADDRESS):
        set_stirrer_pre_run_entities_enabled(hass, runtime.address, len(data.stirrer_states), enabled=True)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        chihiros_data: ChihirosData = hass.data[DOMAIN].pop(entry.entry_id)
        chihiros_data.coordinator.async_close()
        if isinstance(chihiros_data, DosingChihirosData):
            chihiros_data.dosing_totals.async_close()
        await chihiros_data.device.disconnect()
        _async_update_services(hass)
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

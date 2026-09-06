"""Chihiros Home Assistant integration setup."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import ChihirosDataUpdateCoordinator
from .dosing import (
    CONF_PUMP_COUNT,
    DosingDailyTotals,
    DosingProgrammingTracker,
    is_dosing_capable,
    normalize_pump_count,
)
from .dosing_services import (
    ATTR_ML,
    ATTR_PUMP,
    SERVICE_DOSE_ML,
    async_register_dosing_service,
    async_remove_dosing_service,
    async_trigger_dose_ml,
)
from .master_slave_services import (
    ATTR_COMPENSATE,
    ATTR_DAILY_ML,
    ATTR_DELAY,
    ATTR_ENABLE,
    ATTR_ENABLED,
    ATTR_MASTER_ADDRESS,
    ATTR_MASTER_ENTRY_ID,
    ATTR_MIRROR,
    ATTR_MODE,
    ATTR_POINTS,
    SERVICE_MIRROR_STIRRER,
    SERVICE_RESET_DOSING_CHANNEL,
    SERVICE_SET_CHANNEL_ACTIVE,
    SERVICE_SET_DOSE_DELAY,
    SERVICE_SET_DOSING_SCHEDULE,
    SERVICE_SET_STIRRER_MASTER,
    async_register_pump_services,
    async_register_stirrer_link_services,
    async_remove_pump_services,
    async_remove_stirrer_link_services,
)
from .master_slave_services import (
    async_broadcast_frame_to_linked_stirrers as _async_broadcast_frame_to_linked_stirrers,
)
from .master_slave_services import (
    build_work_points as _build_work_points,
)
from .models import ChihirosData, StirrerChannelState
from .runtime import resolve_chihiros_runtime
from .schedule_services import (
    ATTR_BRIGHTNESS,
    ATTR_CURVE,
    ATTR_END,
    ATTR_LEVELS,
    ATTR_PERIODS,
    ATTR_RAMP_UP_MINUTES,
    ATTR_START,
    ATTR_WEEKDAYS,
    SERVICE_ADD_SCHEDULE,
    SERVICE_REMOVE_SCHEDULE,
    SERVICE_RESET_SCHEDULE,
    SERVICE_SET_AUTO_CURVE,
    SERVICE_SET_SCHEDULE,
    async_register_schedule_services,
    async_remove_schedule_services,
)
from .schedule_services import (
    async_add_schedule_period as _async_add_schedule_period,
)
from .schedule_services import (
    async_refresh_status as _async_refresh_status,
)
from .schedule_services import (
    async_replace_schedule as _async_replace_schedule,
)
from .schedule_services import (
    brightness_from_service_data as _brightness_from_service_data,
)
from .schedule_services import (
    ensure_light_device as _ensure_light_device,
)
from .schedule_services import (
    parse_schedule_time as _parse_schedule_time,
)
from .schedule_services import (
    parse_weekdays as _parse_weekdays,
)
from .schedule_services import (
    validate_auto_curve as _validate_auto_curve,
)
from .schedule_services import (
    validate_schedule_period as _validate_schedule_period,
)
from .schedule_services import (
    validate_schedule_periods as _validate_schedule_periods,
)
from .service_utils import (
    ATTR_ADDRESS,
    ATTR_ENTRY_ID,
)
from .service_utils import (
    resolve_service_device as _resolve_service_device,
)
from .stirrer import STIRRER_CHANNEL_COUNT, is_stirrer_capable
from .stirrer_services import (
    ATTR_ACTIVE,
    ATTR_CHANNEL,
    ATTR_FIRST_SETTING,
    ATTR_FREQUENCY,
    ATTR_STIR_POINTS,
    SERVICE_SET_STIR_SCHEDULE,
    async_register_stirrer_service,
    async_remove_stirrer_service,
)
from .stirrer_services import validate_stir_points as _validate_stir_points

__all__ = [
    "ATTR_ACTIVE",
    "ATTR_ADDRESS",
    "ATTR_BRIGHTNESS",
    "ATTR_CHANNEL",
    "ATTR_COMPENSATE",
    "ATTR_CURVE",
    "ATTR_DAILY_ML",
    "ATTR_DELAY",
    "ATTR_ENABLE",
    "ATTR_ENABLED",
    "ATTR_END",
    "ATTR_ENTRY_ID",
    "ATTR_FIRST_SETTING",
    "ATTR_FREQUENCY",
    "ATTR_LEVELS",
    "ATTR_MASTER_ADDRESS",
    "ATTR_MASTER_ENTRY_ID",
    "ATTR_ML",
    "ATTR_MODE",
    "ATTR_MIRROR",
    "ATTR_PERIODS",
    "ATTR_POINTS",
    "ATTR_PUMP",
    "ATTR_RAMP_UP_MINUTES",
    "ATTR_START",
    "ATTR_STIR_POINTS",
    "ATTR_WEEKDAYS",
    "SERVICE_ADD_SCHEDULE",
    "SERVICE_DOSE_ML",
    "SERVICE_MIRROR_STIRRER",
    "SERVICE_REMOVE_SCHEDULE",
    "SERVICE_RESET_DOSING_CHANNEL",
    "SERVICE_RESET_SCHEDULE",
    "SERVICE_SET_AUTO_CURVE",
    "SERVICE_SET_CHANNEL_ACTIVE",
    "SERVICE_SET_DOSE_DELAY",
    "SERVICE_SET_DOSING_SCHEDULE",
    "SERVICE_SET_SCHEDULE",
    "SERVICE_SET_STIRRER_MASTER",
    "SERVICE_SET_STIR_SCHEDULE",
    "_async_add_schedule_period",
    "_async_broadcast_frame_to_linked_stirrers",
    "_async_refresh_status",
    "_async_replace_schedule",
    "_brightness_from_service_data",
    "_build_work_points",
    "_ensure_light_device",
    "_parse_schedule_time",
    "_parse_weekdays",
    "_resolve_service_device",
    "_validate_auto_curve",
    "_validate_schedule_period",
    "_validate_schedule_periods",
    "_validate_stir_points",
    "async_trigger_dose_ml",
]

PLATFORMS: list[Platform] = [
    Platform.LIGHT,
    Platform.SWITCH,
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.BUTTON,
    Platform.FAN,
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

    dosing_totals = None
    dosing_volumes: list[float] = []
    dosing_programming: DosingProgrammingTracker | None = None
    stirrer_states: list[StirrerChannelState] = []
    if is_stirrer_capable(runtime.client):
        stirrer_states = [StirrerChannelState() for _ in range(STIRRER_CHANNEL_COUNT)]
    elif is_dosing_capable(runtime.client):
        dosing_totals = DosingDailyTotals(hass, runtime.address, normalize_pump_count(entry.data.get(CONF_PUMP_COUNT)))
        await dosing_totals.async_load()
        dosing_volumes = [1.0] * dosing_totals.pump_count
        dosing_programming = DosingProgrammingTracker(hass, runtime.address)
        await dosing_programming.async_load()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = ChihirosData(
        entry.title, runtime.client, coordinator, dosing_totals, dosing_volumes, stirrer_states, dosing_programming
    )
    _async_update_services(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        chihiros_data: ChihirosData = hass.data[DOMAIN].pop(entry.entry_id)
        chihiros_data.coordinator.async_close()
        if chihiros_data.dosing_totals:
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
    """Return whether any configured device supports light services."""
    return any(data.device.colors for data in hass.data.get(DOMAIN, {}).values())


def _has_dosing_devices(hass: HomeAssistant) -> bool:
    """Return whether any configured device supports dosing services."""
    return any(data.dosing_totals for data in hass.data.get(DOMAIN, {}).values())


def _has_stirrer_devices(hass: HomeAssistant) -> bool:
    """Return whether any configured device is a magnetic stirrer."""
    return any(is_stirrer_capable(data.device) for data in hass.data.get(DOMAIN, {}).values())

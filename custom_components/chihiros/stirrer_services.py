"""Home Assistant service to program a magnetic stirrer's timer schedule."""

from __future__ import annotations

import datetime
import functools
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .dosing import derive_first_setting, serialize_points
from .models import ChihirosData
from .service_utils import (
    ATTR_WEEKDAYS,
    DEVICE_SELECTOR_SCHEMA,
    WEEKDAY_VALUES,
    frequency_from_service_data,
    parse_start_minutes,
    resolve_service_device,
)
from .stirrer import STIRRER_CHANNEL_MAX, is_stirrer_capable, stirrer_client
from .vendor.chihiros_led_control.commands import (
    DosingWorkPoint,
    stirrer_dosage_for_minutes,
    validate_stirrer_point_gaps,
)

SERVICE_SET_STIR_SCHEDULE = "set_stir_schedule"
SERVICE_STIR_FOR = "stir_for"
ATTR_CHANNEL = "channel"
ATTR_STIR_POINTS = "points"
ATTR_FREQUENCY = "frequency"
ATTR_DURATION = "duration"
ATTR_ACTIVE = "active"
ATTR_FIRST_SETTING = "first_setting"

# App-side run-time limit (DOSING_CONTROL.md §6.5): work points are capped at
# 999 minutes (duplicateJudge's iteration bound).
STIRRER_MAX_MINUTES = 999
# The tempRun duration field is [minutes, seconds] — one byte each, so a
# bounded manual stir is capped at 255 min 59 s (create_general_temp_run_command).
STIRRER_MAX_TEMP_RUN_SECONDS = 255 * 60 + 59

STIR_POINT_SCHEMA = vol.Schema(
    {
        vol.Required("start"): vol.Any(str, datetime.time),
        vol.Required("minutes"): vol.All(vol.Coerce(int), vol.Range(min=1, max=STIRRER_MAX_MINUTES)),
    }
)

_CHANNEL_VALIDATOR = vol.All(vol.Coerce(int), vol.Range(min=1, max=STIRRER_CHANNEL_MAX))

STIR_SCHEDULE_SCHEMA = vol.Schema(
    {
        **DEVICE_SELECTOR_SCHEMA,
        vol.Required(ATTR_CHANNEL): _CHANNEL_VALIDATOR,
        vol.Required(ATTR_STIR_POINTS): vol.All([dict], vol.Length(min=1), [STIR_POINT_SCHEMA]),
        vol.Exclusive(ATTR_WEEKDAYS, "repeat"): vol.All(list, [vol.In(WEEKDAY_VALUES)]),
        vol.Exclusive(ATTR_FREQUENCY, "repeat"): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
        vol.Optional(ATTR_ACTIVE, default=True): vol.Boolean(),
        # Derived from the programming record when omitted (first write of the
        # day for the channel); exposed for advanced/backwards-compatible use.
        vol.Optional(ATTR_FIRST_SETTING): vol.Boolean(),
    }
)

STIR_FOR_SCHEMA = vol.Schema(
    {
        **DEVICE_SELECTOR_SCHEMA,
        vol.Required(ATTR_CHANNEL): _CHANNEL_VALIDATOR,
        vol.Required(ATTR_DURATION): cv.time_period,
    }
)


def _parse_start_minutes(value: str | datetime.time) -> int:
    """Parse a start time into minutes since midnight (see :func:`parse_start_minutes`)."""
    return parse_start_minutes(value)


def _validate_channel(data: ChihirosData, channel: int) -> None:
    """Reject channels the configured stirrer does not expose."""
    configured = len(data.stirrer_states)
    if channel >= configured:
        raise HomeAssistantError(f"{data.device.name} has {configured} stir channels configured")


def validate_stir_points(points: list[dict[str, Any]]) -> list[DosingWorkPoint]:
    """Validate stir points and convert them to timer-mode work points.

    Start times may not be closer together than the app's 2-minute minimum
    gap (checked cyclically, so the wraparound from the last point to the
    first point of the next day counts too).
    """
    parsed: list[tuple[int, DosingWorkPoint]] = []
    for point in points:
        try:
            start = _parse_start_minutes(point["start"])
        except vol.Invalid as ex:
            raise HomeAssistantError(str(ex)) from ex
        minutes = int(point["minutes"])
        parsed.append(
            (
                start,
                DosingWorkPoint(
                    start // 60,
                    start % 60,
                    volume_ml=stirrer_dosage_for_minutes(minutes),
                ),
            )
        )
    parsed.sort(key=lambda item: item[0])
    _validate_point_gaps([start for start, _ in parsed])
    return [work_point for _, work_point in parsed]


def _validate_point_gaps(starts: list[int]) -> None:
    """Reject start times closer together than the app's cyclic gap rule."""
    try:
        validate_stirrer_point_gaps(starts)
    except ValueError as ex:
        raise HomeAssistantError(str(ex)) from ex


async def _async_set_stir_schedule(hass: HomeAssistant, call: ServiceCall) -> None:
    """Program one stirrer channel in timer mode."""
    data = resolve_service_device(hass, call.data)
    if not is_stirrer_capable(data.device):
        raise HomeAssistantError(f"{data.device.name} is not a magnetic stirrer")
    channel = int(call.data[ATTR_CHANNEL]) - 1
    _validate_channel(data, channel)
    points = validate_stir_points(call.data[ATTR_STIR_POINTS])
    frequency = frequency_from_service_data(call.data)
    active = bool(call.data[ATTR_ACTIVE])
    first_setting = derive_first_setting(data.dosing_programming, channel, call.data.get(ATTR_FIRST_SETTING))
    await stirrer_client(data.device).set_stir_schedule(
        channel,
        points,
        frequency=frequency,
        active=active,
        is_first_setting=first_setting,
    )
    if data.dosing_programming is not None:
        # Record exactly what was sent (set_stir_schedule always sends
        # dosingSet with a daily volume of 0 plus the timer frames), so the
        # record is complete enough for a verbatim replay via
        # _apply_channel_setup instead of a partial mode-without-points trap.
        await data.dosing_programming.async_record(
            channel,
            {
                "active": active,
                "compensate": False,
                "dose_per_day_ml": 0.0,
                "frequency": frequency,
                "mode": "timer",
                "points": serialize_points(points),
                "first_setting": first_setting,
            },
        )


async def _async_stir_for(hass: HomeAssistant, call: ServiceCall) -> None:
    """Stir one channel for a bounded duration (minutes + seconds)."""
    data = resolve_service_device(hass, call.data)
    if not is_stirrer_capable(data.device):
        raise HomeAssistantError(f"{data.device.name} is not a magnetic stirrer")
    channel = int(call.data[ATTR_CHANNEL]) - 1
    _validate_channel(data, channel)
    duration: datetime.timedelta = call.data[ATTR_DURATION]
    seconds = int(duration.total_seconds())
    if not 1 <= seconds <= STIRRER_MAX_TEMP_RUN_SECONDS:
        max_min, max_sec = divmod(STIRRER_MAX_TEMP_RUN_SECONDS, 60)
        raise HomeAssistantError(f"Duration must be between 1 second and {max_min} minutes {max_sec} seconds")
    await stirrer_client(data.device).stir(channel, True, seconds=seconds)


def async_register_stirrer_service(hass: HomeAssistant) -> None:
    """Register the stirrer services for configured stirrers."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_STIR_SCHEDULE):
        return
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_STIR_SCHEDULE,
        functools.partial(_async_set_stir_schedule, hass),
        schema=STIR_SCHEDULE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_STIR_FOR,
        functools.partial(_async_stir_for, hass),
        schema=STIR_FOR_SCHEMA,
    )


def async_remove_stirrer_service(hass: HomeAssistant) -> None:
    """Remove the stirrer services if registered."""
    for service in (SERVICE_SET_STIR_SCHEDULE, SERVICE_STIR_FOR):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)

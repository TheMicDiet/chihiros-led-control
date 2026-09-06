"""Home Assistant service to program a magnetic stirrer's timer schedule."""

from __future__ import annotations

import datetime
from typing import Any, cast

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .models import ChihirosData
from .service_utils import DEVICE_SELECTOR_SCHEMA, parse_start_minutes, resolve_service_device
from .stirrer import STIRRER_CHANNEL_COUNT, is_stirrer_capable, stirrer_client
from .vendor.chihiros_led_control.commands import DosingWorkPoint, stirrer_dosage_for_minutes

SERVICE_SET_STIR_SCHEDULE = "set_stir_schedule"
ATTR_CHANNEL = "channel"
ATTR_STIR_POINTS = "points"
ATTR_FREQUENCY = "frequency"
ATTR_ACTIVE = "active"
ATTR_FIRST_SETTING = "first_setting"

# App-side validation limits (DOSING_CONTROL.md §6.5): work points must be at
# least 2 minutes apart (stirrer_time_gap_warning / duplicateJudge) and run
# times are capped at 999 minutes (duplicateJudge's iteration bound).
STIRRER_MIN_POINT_GAP_MINUTES = 2
STIRRER_MAX_MINUTES = 999
MINUTES_PER_DAY = 24 * 60

STIR_POINT_SCHEMA = vol.Schema(
    {
        vol.Required("start"): vol.Any(str, datetime.time),
        vol.Required("minutes"): vol.All(vol.Coerce(int), vol.Range(min=1, max=STIRRER_MAX_MINUTES)),
    }
)

STIR_SCHEDULE_SCHEMA = vol.Schema(
    {
        **DEVICE_SELECTOR_SCHEMA,
        vol.Required(ATTR_CHANNEL): vol.All(vol.Coerce(int), vol.Range(min=1, max=STIRRER_CHANNEL_COUNT)),
        vol.Required(ATTR_STIR_POINTS): vol.All([dict], vol.Length(min=1), [STIR_POINT_SCHEMA]),
        vol.Optional(ATTR_FREQUENCY, default=127): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
        vol.Optional(ATTR_ACTIVE, default=True): vol.Boolean(),
        vol.Optional(ATTR_FIRST_SETTING, default=True): vol.Boolean(),
    }
)


def _parse_start_minutes(value: str | datetime.time) -> int:
    """Parse a start time into minutes since midnight (see :func:`parse_start_minutes`)."""
    return parse_start_minutes(value)


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
    """Reject start times closer together than the app's 2-minute minimum gap."""
    gap_error = HomeAssistantError(f"Stir points must be at least {STIRRER_MIN_POINT_GAP_MINUTES} minutes apart")
    for first, second in zip(starts, starts[1:], strict=False):
        if second - first < STIRRER_MIN_POINT_GAP_MINUTES:
            raise gap_error
    if len(starts) > 1 and starts[0] + MINUTES_PER_DAY - starts[-1] < STIRRER_MIN_POINT_GAP_MINUTES:
        raise gap_error


def async_register_stirrer_service(hass: HomeAssistant) -> None:
    """Register the stir schedule service for configured stirrers."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_STIR_SCHEDULE):
        return

    async def async_set_stir_schedule(call: ServiceCall) -> None:
        """Program one stirrer channel in timer mode."""
        data: ChihirosData = resolve_service_device(hass, call.data)
        if not is_stirrer_capable(data.device):
            raise HomeAssistantError(f"{data.device.name} is not a magnetic stirrer")
        points = validate_stir_points(call.data[ATTR_STIR_POINTS])
        device = stirrer_client(data.device)
        await cast(Any, device).set_stir_schedule(
            int(call.data[ATTR_CHANNEL]) - 1,
            points,
            frequency=int(call.data[ATTR_FREQUENCY]),
            active=bool(call.data[ATTR_ACTIVE]),
            is_first_setting=bool(call.data[ATTR_FIRST_SETTING]),
        )

    hass.services.async_register(
        DOMAIN, SERVICE_SET_STIR_SCHEDULE, async_set_stir_schedule, schema=STIR_SCHEDULE_SCHEMA
    )


def async_remove_stirrer_service(hass: HomeAssistant) -> None:
    """Remove the stir schedule service if registered."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_STIR_SCHEDULE):
        hass.services.async_remove(DOMAIN, SERVICE_SET_STIR_SCHEDULE)

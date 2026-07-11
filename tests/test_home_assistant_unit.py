"""Unit tests for isolated Home Assistant integration behavior."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.chihiros import (
    ATTR_ADDRESS,
    ATTR_BRIGHTNESS,
    ATTR_END,
    ATTR_ENTRY_ID,
    ATTR_LEVELS,
    ATTR_RAMP_UP_MINUTES,
    ATTR_START,
    ATTR_WEEKDAYS,
    _async_add_schedule_period,
    _async_refresh_status,
    _brightness_from_service_data,
    _ensure_light_device,
    _parse_schedule_time,
    _parse_weekdays,
    _resolve_service_device,
    _validate_schedule_period,
    _validate_schedule_periods,
)
from custom_components.chihiros.const import DOMAIN
from custom_components.chihiros.coordinator import _notification_to_debug_dict, _schedule_point_to_dict
from custom_components.chihiros.discovery import ChihirosDiscovery, discovery_title
from custom_components.chihiros.dosing import PUMP_COUNT, _coerce_total, is_dosing_capable, normalize_pump_count
from custom_components.chihiros.fake import FAKE_DEVICES, create_fake_device, is_fake_address
from custom_components.chihiros.vendor.chihiros_led_control.models import DOSING_PUMP
from custom_components.chihiros.vendor.chihiros_led_control.protocol import RuntimeNotification, SchedulePoint

pytestmark = pytest.mark.unit


def _data(*, colors: dict[str, int] | None = None, dosing: bool = False) -> SimpleNamespace:
    """Build the small data surface used by service helper functions."""
    device = SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        name="Test light",
        colors={"red": 1, "green": 2, "blue": 3} if colors is None else colors,
    )
    return SimpleNamespace(device=device, dosing_totals=object() if dosing else None)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, PUMP_COUNT), ("invalid", PUMP_COUNT), (3, PUMP_COUNT), ("2", 2), (4, 4)],
)
def test_normalize_pump_count(value: object, expected: int) -> None:
    """Only supported pump counts are accepted."""
    assert normalize_pump_count(value) == expected


@pytest.mark.parametrize(("value", "expected"), [(1, 1.0), ("2.26", 2.3), (None, 0.0), (object(), 0.0)])
def test_coerce_dosing_total(value: object, expected: float) -> None:
    """Persisted dosing values are normalized defensively."""
    assert _coerce_total(value) == expected


def test_dosing_capability_uses_model_name_or_name() -> None:
    """Dosing detection supports runtime clients and model objects."""
    assert is_dosing_capable(SimpleNamespace(model_name=DOSING_PUMP.name))
    assert is_dosing_capable(DOSING_PUMP)
    assert not is_dosing_capable(SimpleNamespace(name="A light"))


def test_resolve_service_device_by_entry_address_or_single_entry() -> None:
    """Service targets can be selected through every supported selector."""
    selected = _data()
    hass = SimpleNamespace(data={DOMAIN: {"entry": selected}})

    assert _resolve_service_device(hass, {ATTR_ENTRY_ID: "entry"}) is selected
    assert _resolve_service_device(hass, {ATTR_ADDRESS: "aa:bb:cc:dd:ee:ff"}) is selected
    assert _resolve_service_device(hass, {}) is selected


@pytest.mark.parametrize(
    ("entries", "selector", "message"),
    [
        ({}, {ATTR_ENTRY_ID: "missing"}, "config entry not found"),
        ({}, {ATTR_ADDRESS: "missing"}, "device address not found"),
        ({"one": _data(), "two": _data()}, {}, "Multiple Chihiros devices"),
    ],
)
def test_resolve_service_device_errors(entries: dict[str, Any], selector: dict[str, str], message: str) -> None:
    """Invalid and ambiguous service targets produce useful errors."""
    hass = SimpleNamespace(data={DOMAIN: entries})
    with pytest.raises(HomeAssistantError, match=message):
        _resolve_service_device(hass, selector)


def test_schedule_period_validation_normalizes_weekdays() -> None:
    """A valid schedule is parsed without crossing the HA service boundary."""
    result = _validate_schedule_period(
        _data(),
        {
            ATTR_START: "08:00",
            ATTR_END: "18:30",
            ATTR_BRIGHTNESS: {"red": 80},
            ATTR_WEEKDAYS: ["monday", "wednesday"],
        },
    )
    assert {weekday.value for weekday in result["weekdays"]} == {"monday", "wednesday"}


@pytest.mark.parametrize(
    ("period", "message"),
    [
        ({ATTR_START: "noon", ATTR_END: "18:00", ATTR_BRIGHTNESS: 50}, "Invalid schedule time"),
        ({ATTR_START: "18:00", ATTR_END: "08:00", ATTR_BRIGHTNESS: 50}, "must be before"),
        ({ATTR_START: "08:00", ATTR_END: "18:00", ATTR_LEVELS: {}}, "at least one channel"),
        ({ATTR_START: "08:00", ATTR_END: "18:00", ATTR_LEVELS: {"white": 50}}, "not supported"),
    ],
)
def test_schedule_period_validation_errors(period: dict[str, Any], message: str) -> None:
    """Schedule validation failures are unit-tested at their source."""
    with pytest.raises(HomeAssistantError, match=message):
        _validate_schedule_period(_data(), period)


def test_scalar_brightness_requires_a_controllable_channel() -> None:
    """A channel-less device cannot accept scalar light schedules."""
    with pytest.raises(HomeAssistantError, match="does not expose"):
        _validate_schedule_period(
            _data(colors={}),
            {ATTR_START: "08:00", ATTR_END: "18:00", ATTR_BRIGHTNESS: 50},
        )


def test_full_schedule_rejects_empty_and_duplicate_weekdays() -> None:
    """Replacement schedules must be non-empty and target a weekday once."""
    data = _data()
    with pytest.raises(HomeAssistantError, match="at least one period"):
        _validate_schedule_periods(data, [])

    periods = [
        {ATTR_START: "07:00", ATTR_END: "09:00", ATTR_BRIGHTNESS: 40, ATTR_WEEKDAYS: ["friday"]},
        {ATTR_START: "17:00", ATTR_END: "20:00", ATTR_BRIGHTNESS: 20, ATTR_WEEKDAYS: ["friday"]},
    ]
    with pytest.raises(HomeAssistantError, match="periods 1 and 2.*friday"):
        _validate_schedule_periods(data, periods)


def test_dosing_device_is_rejected_for_light_service() -> None:
    """Light-only service helpers reject dosing pumps."""
    with pytest.raises(HomeAssistantError, match="is not a light"):
        _ensure_light_device(_data(dosing=True))


@pytest.mark.asyncio
async def test_add_schedule_period_translates_service_data() -> None:
    """Service data is translated into the client API contract."""
    calls: list[tuple[Any, ...]] = []

    async def add_setting(*args: Any, **kwargs: Any) -> None:
        calls.append((*args, kwargs))

    data = _data()
    data.device.add_setting = add_setting
    await _async_add_schedule_period(
        data,
        {
            ATTR_START: "08:00",
            ATTR_END: "18:30",
            ATTR_LEVELS: {"red": 80},
            ATTR_RAMP_UP_MINUTES: 20,
            ATTR_WEEKDAYS: ["monday"],
        },
    )

    start, end, kwargs = calls[0]
    assert start.strftime("%H:%M") == "08:00"
    assert end.strftime("%H:%M") == "18:30"
    assert kwargs["max_brightness"] == {"red": 80}
    assert kwargs["ramp_up_in_minutes"] == 20
    assert [weekday.value for weekday in kwargs["weekdays"]] == ["monday"]


@pytest.mark.asyncio
async def test_status_refresh_is_best_effort() -> None:
    """A failed post-write refresh never turns a successful write into an error."""

    async def fail() -> None:
        raise RuntimeError("offline")

    await _async_refresh_status(SimpleNamespace(coordinator=SimpleNamespace(async_request_status=fail)))


def test_schedule_and_notification_debug_conversion() -> None:
    """Protocol values are converted into stable HA-friendly structures."""
    point = SchedulePoint(8, 5, {"red": 10})
    assert _schedule_point_to_dict(point) == {"time": "08:05", "levels": {"red": 10}}

    notification = RuntimeNotification(23, 511, bytes.fromhex("5b 17 0a 00 01 0a 01 ff"))
    assert _notification_to_debug_dict(notification, "runtime") == {
        "firmware_version": 23,
        "frame": "5b 17 0a 00 01 0a 01 ff",
        "payload": "01 ff",
        "mode": "0x0a",
        "parsed_type": "runtime",
    }


@pytest.mark.asyncio
async def test_fake_device_supports_all_brightness_shapes_and_callbacks() -> None:
    """The development fake behaves like the client surface it replaces."""
    device = create_fake_device(FAKE_DEVICES[0].address)
    notifications: list[object] = []
    remove = device.add_notification_callback(notifications.append)

    await device.set_brightness(25)
    await device.set_brightness({"red": 40, "unknown": 99})
    await device.set_brightness([1, 2, 3])
    await device.query_status()
    remove()

    assert len(notifications) == 2
    assert device.address == FAKE_DEVICES[0].address
    assert device.name == FAKE_DEVICES[0].name
    assert device.model_name == FAKE_DEVICES[0].model.name
    assert device.colors == dict(FAKE_DEVICES[0].model.color_channels)
    assert is_fake_address(device.address)


def test_discovery_helpers_for_fake_device() -> None:
    """Fake discovery metadata maps cleanly to config entry data and labels."""
    discovery = ChihirosDiscovery.from_fake(FAKE_DEVICES[0])
    assert discovery.is_fake
    assert discovery.entry_data() == {ATTR_ADDRESS: FAKE_DEVICES[0].address}
    assert discovery.display_name() == f"{FAKE_DEVICES[0].name} ({FAKE_DEVICES[0].address})"
    assert discovery_title(SimpleNamespace(name="Resolved name"), discovery) == "Resolved name"
    assert discovery_title(SimpleNamespace(name=""), discovery) == discovery.name


def test_small_schedule_parsing_helpers() -> None:
    """Small parsing helpers preserve their public input shapes."""
    assert _parse_schedule_time("09:15").time() == datetime.strptime("09:15", "%H:%M").time()
    assert _brightness_from_service_data({ATTR_LEVELS: {"red": 5}}) == {"red": 5}
    assert _brightness_from_service_data({ATTR_BRIGHTNESS: 10}) == 10
    assert _parse_weekdays(None) is None

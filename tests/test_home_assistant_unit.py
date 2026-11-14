"""Unit tests for isolated Home Assistant integration behavior."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("homeassistant", reason="Home Assistant test group is not installed")

import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError

from custom_components.chihiros.const import DOMAIN
from custom_components.chihiros.coordinator import _notification_to_debug_dict, _schedule_point_to_dict
from custom_components.chihiros.discovery import ChihirosDiscovery, discovery_title
from custom_components.chihiros.dosing import PUMP_COUNT, is_dosing_capable, normalize_pump_count
from custom_components.chihiros.fake import FAKE_DEVICES, create_fake_device, is_fake_address
from custom_components.chihiros.master_slave_services import (
    build_work_points as _build_work_points,
)
from custom_components.chihiros.schedule_services import (
    ATTR_BRIGHTNESS,
    ATTR_END,
    ATTR_LEVELS,
    ATTR_RAMP_UP_MINUTES,
    ATTR_START,
    ATTR_WEEKDAYS,
    SET_AUTO_CURVE_SCHEMA,
)
from custom_components.chihiros.schedule_services import (
    async_add_schedule_period as _async_add_schedule_period,
)
from custom_components.chihiros.schedule_services import (
    async_refresh_status as _async_refresh_status,
)
from custom_components.chihiros.schedule_services import (
    ensure_light_device as _ensure_light_device,
)
from custom_components.chihiros.schedule_services import (
    validate_auto_curve as _validate_auto_curve,
)
from custom_components.chihiros.schedule_services import (
    validate_schedule_period as _validate_schedule_period,
)
from custom_components.chihiros.schedule_services import (
    validate_schedule_periods as _validate_schedule_periods,
)
from custom_components.chihiros.sensor import _format_schedule_point, _format_schedule_state
from custom_components.chihiros.service_utils import (
    ATTR_ADDRESS,
    ATTR_ENTRY_ID,
)
from custom_components.chihiros.service_utils import (
    resolve_service_device as _resolve_service_device,
)
from custom_components.chihiros.stirrer import is_stirrer_capable
from custom_components.chihiros.stirrer_services import (
    validate_stir_points as _validate_stir_points,
)
from custom_components.chihiros.vendor.chihiros_led_control.models import DeviceKind, DeviceModel, LedFeature, LedSpec
from custom_components.chihiros.vendor.chihiros_led_control.protocol.led import (
    FanStatusNotification,
    RuntimeNotification,
    SchedulePoint,
)
from custom_components.chihiros.vendor.chihiros_led_control.registry import DOSING_PUMP, MAG_STIRRER

pytestmark = [pytest.mark.unit, pytest.mark.homeassistant]


def test_schedule_midnight_is_valid_but_other_reverse_ranges_are_not() -> None:
    """HA permits the scene-only midnight sentinel without accepting hh:00 reversal."""
    data = _data(colors={"white": 0})
    period = {ATTR_START: "08:00", ATTR_END: "00:00", ATTR_BRIGHTNESS: 50, ATTR_RAMP_UP_MINUTES: 0}
    _validate_schedule_period(data, period)
    with pytest.raises(HomeAssistantError, match="before end"):
        _validate_schedule_period(data, {**period, ATTR_END: "07:00"})


def _data(*, colors: dict[str, int] | None = None, dosing: bool = False) -> SimpleNamespace:
    """Build the small data surface used by service helper functions."""
    device = SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        name="Test light",
        colors={"red": 1, "green": 2, "blue": 3} if colors is None else colors,
        device_kind=DeviceKind.DOSING_PUMP if dosing else DeviceKind.LED,
    )
    device.model = DOSING_PUMP if dosing else DeviceModel("Test", (), LedSpec(device.colors))
    return SimpleNamespace(device=device, dosing_totals=object() if dosing else None)


@pytest.mark.parametrize(
    ("curve", "message"),
    [
        ({}, "at least one channel"),
        ({0: []}, "at least one point"),
        ({9: [[60, 50]]}, "Channel 9 is not supported"),
        ({0: [[2881, 50]]}, "minutes must be between 0 and 2880"),
        ({0: [[60, 101]]}, "level must be between 0 and 100"),
    ],
)
def test_validate_auto_curve_rejects_invalid_curves(curve: dict[int, list[list[int]]], message: str) -> None:
    """Auto curves reject empty input, unknown channels, and out-of-range points."""
    data = _data(colors={"red": 0, "green": 1, "blue": 2, "white": 3})

    with pytest.raises(HomeAssistantError, match=message):
        _validate_auto_curve(data, curve)


def test_validate_auto_curve_accepts_valid_curve() -> None:
    """A valid per-channel curve passes validation."""
    data = _data(colors={"red": 0, "green": 1, "blue": 2, "white": 3})

    _validate_auto_curve(data, {0: [[480, 100], [720, 0]], 3: [[480, 40]]})


def test_set_auto_curve_schema_coerces_channel_keys() -> None:
    """The service schema coerces string channel keys and validates point shape."""
    coerced = SET_AUTO_CURVE_SCHEMA({"curve": {"0": [[480, 100]], 1: [[720, 0]]}})

    assert coerced["curve"] == {0: [[480, 100]], 1: [[720, 0]]}

    with pytest.raises(vol.Invalid):
        SET_AUTO_CURVE_SCHEMA({"curve": {0: [[480]]}})


def test_dosing_capability_uses_typed_device_kind() -> None:
    """Dosing detection uses the vendored device-family discriminator."""
    assert is_dosing_capable(SimpleNamespace(device_kind=DeviceKind.DOSING_PUMP))
    assert is_dosing_capable(DOSING_PUMP)
    assert not is_dosing_capable(SimpleNamespace(device_kind=DeviceKind.LED))


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


@pytest.mark.parametrize(
    ("period", "message"),
    [
        ({ATTR_START: "noon", ATTR_END: "18:00", ATTR_BRIGHTNESS: 50}, "Invalid schedule time"),
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
            ATTR_WEEKDAYS: ["monday", "wednesday"],
        },
    )

    start, end, kwargs = calls[0]
    assert start.strftime("%H:%M") == "08:00"
    assert end.strftime("%H:%M") == "18:30"
    assert kwargs["max_brightness"] == {"red": 80}
    assert kwargs["ramp_up_in_minutes"] == 20
    assert [weekday.value for weekday in kwargs["weekdays"]] == ["monday", "wednesday"]


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

    fan_notification = FanStatusNotification(
        27, 600, 25, bytes.fromhex("5b 1b 10 00 01 0b 02 58 19 00 01 00 00 00 00 00 48 22")
    )
    assert _notification_to_debug_dict(fan_notification, "fan_status") == {
        "firmware_version": 27,
        "frame": "5b 1b 10 00 01 0b 02 58 19 00 01 00 00 00 00 00 48 22",
        "payload": "02 58 19 00 01 00 00 00 00 00 48 22",
        "mode": "0x0b",
        "parsed_type": "fan_status",
    }


@pytest.mark.asyncio
async def test_fake_fan_device_publishes_fan_status() -> None:
    """The fan-equipped fake device behaves like the fan client surface."""
    fan_device_info = next(
        device
        for device in FAKE_DEVICES
        if isinstance(device.model.spec, LedSpec) and LedFeature.FAN in device.model.spec.features
    )
    device = create_fake_device(fan_device_info.address)
    notifications: list[object] = []
    device.add_notification_callback(notifications.append)

    await device.set_fan_speed(50)

    assert device.last_fan_status_notification == FanStatusNotification(
        firmware_version=27, fan_rpm=1000, temperature_celsius=25
    )
    assert notifications == [device.last_fan_status_notification]

    with pytest.raises(ValueError, match="between 0 and 100"):
        await device.set_fan_speed(101)


@pytest.mark.asyncio
async def test_fake_heater_device_reports_its_state() -> None:
    """The development roster covers the heater, whose states arrive as notifications."""
    by_code = {code: info for info in FAKE_DEVICES for code in info.model.advertised_codes}
    assert by_code["DYHET"].model.device_kind.value == "heater"
    device = create_fake_device(by_code["DYHET"].address)

    await device.query_status()

    assert device.current_temperature_celsius is not None
    assert device.work_time_hours == 120
    assert device.heater_alarms == ()
    await device.reset_work_time()
    assert device.work_time_hours == 0


def test_discovery_helpers_for_fake_device() -> None:
    """Fake discovery metadata maps cleanly to config entry data and labels."""
    discovery = ChihirosDiscovery.from_fake(FAKE_DEVICES[0])
    assert discovery.is_fake
    assert discovery.entry_data() == {ATTR_ADDRESS: FAKE_DEVICES[0].address}
    assert discovery.display_name() == f"{FAKE_DEVICES[0].name} ({FAKE_DEVICES[0].address})"
    assert discovery_title(SimpleNamespace(name="Resolved name"), discovery) == "Resolved name"
    assert discovery_title(SimpleNamespace(name=""), discovery) == discovery.name


def test_format_schedule_state_covers_branches() -> None:
    """All schedule formatting branches produce readable state strings."""
    # empty -> No schedule
    assert _format_schedule_state(()) == "No schedule"

    # uniform levels collapse to "<time> <level>%"
    assert _format_schedule_point({"time": "08:05", "levels": {"red": 10, "blue": 10}}) == "08:05 10%"

    # distinct per-channel levels render as "<color initial><level>/..."
    assert _format_schedule_point({"time": "09:00", "levels": {"red": 10, "green": 20, "blue": 30}}) == (
        "09:00 R10/G20/B30"
    )

    # missing/invalid levels falls back to the time string
    assert _format_schedule_point({"time": "10:00", "levels": {}}) == "10:00"
    assert _format_schedule_point({"levels": {}}) == "unknown"


def test_format_schedule_state_truncates_long_payload() -> None:
    """A schedule string longer than the sensor max collapses to a count."""
    many_points = tuple({"time": "12:00", "levels": {"red": 50, "green": 50, "blue": 50}} for _ in range(200))
    state = _format_schedule_state(many_points)
    assert state == f"{len(many_points)} points"


def test_normalize_pump_count_defaults_invalid_values() -> None:
    """Unsupported or non-numeric values fall back to the default pump count."""
    assert normalize_pump_count("2") == 2
    assert normalize_pump_count("4") == 4
    assert normalize_pump_count("8") == 8
    assert normalize_pump_count("3") == PUMP_COUNT
    assert normalize_pump_count(None) == PUMP_COUNT
    assert normalize_pump_count("not a number") == PUMP_COUNT


def test_build_work_points_validation() -> None:
    """Point building validates mode-specific shapes."""
    points = _build_work_points("free", [{"start": "23:00", "end": "01:00", "count": 3}])
    assert points[0].start_hour == 23 and points[0].duration_minutes == 120 and points[0].number == 3

    with pytest.raises(HomeAssistantError, match="'ml'"):
        _build_work_points("timer", [{"start": "08:00"}])
    with pytest.raises(HomeAssistantError, match="Unknown dosing mode"):
        _build_work_points("bogus", [{"start": "08:00", "ml": 1.0}])


def test_validate_stir_points_unit() -> None:
    """Point validation converts times and enforces app interval overlaps."""
    points = _validate_stir_points(
        [
            {"start": "20:30", "minutes": 10},
            {"start": "8:05", "minutes": 45},
        ]
    )
    assert [(p.start_hour, p.start_minute, p.volume_ml) for p in points] == [
        (8, 5, pytest.approx(27.0)),
        (20, 30, pytest.approx(6.0)),
    ]

    with pytest.raises(HomeAssistantError, match="overlap"):
        _validate_stir_points([{"start": "08:00", "minutes": 5}, {"start": "08:01", "minutes": 5}])

    with pytest.raises(HomeAssistantError, match="overlap"):
        _validate_stir_points([{"start": "08:00", "minutes": 5}, {"start": "08:05", "minutes": 1}])


def test_stirrer_capability_and_fake_device() -> None:
    """The stirrer capability check matches only Mag Stirrer models."""
    assert is_stirrer_capable(SimpleNamespace(device_kind=DeviceKind.MAG_STIRRER))
    assert not is_stirrer_capable(SimpleNamespace(device_kind=DeviceKind.DOSING_PUMP))

    fake_address = "FA:CE:C0:00:00:0F"
    assert is_fake_address(fake_address)
    fake = create_fake_device(fake_address)
    assert fake.model is MAG_STIRRER

"""Home Assistant integration tests for the Chihiros config entry."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

import pytest

try:
    from homeassistant.components.bluetooth import update_coordinator as bluetooth_update
    from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
    from homeassistant.components.button import SERVICE_PRESS
    from homeassistant.components.light import ATTR_BRIGHTNESS
    from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
    from homeassistant.components.number import (
        ATTR_VALUE as ATTR_NUMBER_VALUE,
    )
    from homeassistant.components.number import (
        DOMAIN as NUMBER_DOMAIN,
    )
    from homeassistant.components.number import (
        SERVICE_SET_VALUE,
    )
    from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
    from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
    from homeassistant.config_entries import ConfigEntry, ConfigEntryState
    from homeassistant.const import ATTR_ENTITY_ID, CONF_ADDRESS, SERVICE_TURN_OFF, SERVICE_TURN_ON, STATE_OFF, STATE_ON
    from homeassistant.core import HomeAssistant
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    import custom_components.chihiros as chihiros_integration
    from custom_components.chihiros import (
        ATTR_ADDRESS,
        ATTR_END,
        ATTR_ENTRY_ID,
        ATTR_LEVELS,
        ATTR_ML,
        ATTR_PERIODS,
        ATTR_PUMP,
        ATTR_RAMP_UP_MINUTES,
        ATTR_START,
        ATTR_WEEKDAYS,
        SERVICE_ADD_SCHEDULE,
        SERVICE_DOSE_ML,
        SERVICE_REMOVE_SCHEDULE,
        SERVICE_RESET_SCHEDULE,
        SERVICE_SET_SCHEDULE,
    )
    from custom_components.chihiros import (
        ATTR_BRIGHTNESS as ATTR_SCHEDULE_BRIGHTNESS,
    )
    from custom_components.chihiros.const import DOMAIN
    from custom_components.chihiros.coordinator import ChihirosDataUpdateCoordinator
    from custom_components.chihiros.dosing import CONF_PUMP_COUNT
    from custom_components.chihiros.runtime import ChihirosRuntime
except ImportError as err:
    pytest.skip(
        f"Home Assistant test group is not installed or is incompatible: {err}",
        allow_module_level=True,
    )

from custom_components.chihiros.vendor.chihiros_led_control.models import RGB_CHANNELS, DeviceModel
from custom_components.chihiros.vendor.chihiros_led_control.protocol import (
    ParsedNotification,
    RuntimeNotification,
    SchedulePoint,
    ScheduleSnapshotNotification,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.usefixtures("enable_custom_integrations", "mock_bluetooth"),
]

TEST_ADDRESS = "FA:CE:C0:00:10:01"


class TrackingChihirosClient:
    """Mock external Chihiros client used behind the Home Assistant integration boundary."""

    def __init__(self) -> None:
        """Initialize the tracking client."""
        self.model = DeviceModel("Test RGB", ("TEST-RGB",), RGB_CHANNELS)
        self.last_runtime_notification: RuntimeNotification | None = None
        self.last_schedule_snapshot_notification: ScheduleSnapshotNotification | None = None
        self.query_status_calls = 0
        self.brightness_calls: list[int | Sequence[int] | Mapping[str | int, int]] = []
        self.auto_mode_calls: list[datetime | None] = []
        self.manual_mode_calls = 0
        self.add_setting_calls: list[dict[str, Any]] = []
        self.remove_setting_calls: list[dict[str, Any]] = []
        self.reset_settings_calls = 0
        self.disconnect_calls = 0
        self.dose_ml_calls: list[tuple[int, float]] = []
        self._callbacks: set[Callable[[ParsedNotification], None]] = set()

    @property
    def address(self) -> str:
        """Return the fake BLE address."""
        return TEST_ADDRESS

    @property
    def name(self) -> str:
        """Return the fake device name."""
        return "Test Chihiros"

    @property
    def model_name(self) -> str:
        """Return the fake model name."""
        return self.model.name

    @property
    def colors(self) -> dict[str, int]:
        """Return supported color channels."""
        return dict(self.model.color_channels)

    def add_notification_callback(self, callback: Callable[[ParsedNotification], None]) -> Callable[[], None]:
        """Register a parsed notification callback."""
        self._callbacks.add(callback)

        def remove_callback() -> None:
            self._callbacks.discard(callback)

        return remove_callback

    async def query_status(self) -> None:
        """Publish deterministic runtime and schedule snapshots."""
        self.query_status_calls += 1
        self.last_runtime_notification = RuntimeNotification(
            firmware_version=23,
            runtime_minutes=511,
            raw=bytes.fromhex("5b 17 0a 00 01 0a 01 ff ff ff ff 0c 36 2d"),
        )
        self.last_schedule_snapshot_notification = ScheduleSnapshotNotification(
            firmware_version=23,
            points=(
                SchedulePoint(8, 0, {"red": 15, "green": 15, "blue": 15}),
                SchedulePoint(12, 0, {"red": 70, "green": 70, "blue": 70}),
            ),
            raw=bytes.fromhex("5b 17 00 00 00 fe"),
        )
        self._notify(self.last_runtime_notification)
        self._notify(self.last_schedule_snapshot_notification)

    async def set_brightness(self, brightness: int | Sequence[int] | Mapping[str | int, int]) -> None:
        """Record a brightness write."""
        self.brightness_calls.append(brightness)

    async def turn_on(self) -> None:
        """Record a full-device turn-on."""
        await self.set_brightness(100)

    async def turn_off(self) -> None:
        """Record a full-device turn-off."""
        await self.set_brightness(0)

    async def enable_auto_mode(self, timestamp: datetime | None = None) -> None:
        """Record enabling auto mode."""
        self.auto_mode_calls.append(timestamp)

    async def set_manual_mode(self) -> None:
        """Record enabling manual mode."""
        self.manual_mode_calls += 1

    async def dose_ml(self, pump_idx: int, volume_ml: float) -> None:
        """Record a manual dose."""
        self.dose_ml_calls.append((pump_idx, volume_ml))

    async def add_setting(
        self,
        sunrise: datetime,
        sunset: datetime,
        max_brightness: int | Sequence[int] | Mapping[str | int, int] = 100,
        ramp_up_in_minutes: int = 0,
        weekdays: list[object] | None = None,
    ) -> None:
        """Record a schedule write."""
        self.add_setting_calls.append(
            {
                "sunrise": sunrise,
                "sunset": sunset,
                "max_brightness": max_brightness,
                "ramp_up_in_minutes": ramp_up_in_minutes,
                "weekdays": weekdays,
            }
        )

    async def remove_setting(
        self,
        sunrise: datetime,
        sunset: datetime,
        ramp_up_in_minutes: int = 0,
        weekdays: list[object] | None = None,
    ) -> None:
        """Record a schedule delete."""
        self.remove_setting_calls.append(
            {
                "sunrise": sunrise,
                "sunset": sunset,
                "ramp_up_in_minutes": ramp_up_in_minutes,
                "weekdays": weekdays,
            }
        )

    async def reset_settings(self) -> None:
        """Record a schedule reset."""
        self.reset_settings_calls += 1

    async def disconnect(self) -> None:
        """Record disconnect."""
        self.disconnect_calls += 1

    def _notify(self, notification: ParsedNotification) -> None:
        """Publish a notification to registered callbacks."""
        for callback in tuple(self._callbacks):
            callback(notification)


class TrackingDosingClient(TrackingChihirosClient):
    """Mock dosing pump client."""

    def __init__(self) -> None:
        """Initialize the tracking dosing pump client."""
        super().__init__()
        self.model = DeviceModel("Dosing Pump", ("DYDOSE",), {})

    @property
    def name(self) -> str:
        """Return the fake dosing pump name."""
        return "Test Dosing Pump"


async def _setup_entry(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    client: TrackingChihirosClient | None = None,
    entry_data: dict[str, Any] | None = None,
) -> tuple[ConfigEntry, TrackingChihirosClient]:
    """Set up the integration through Home Assistant's config entry interface."""
    client = client or TrackingChihirosClient()

    async def wait_for_step(label: str, awaitable: Any) -> Any:
        try:
            return await asyncio.wait_for(awaitable, timeout=30)
        except TimeoutError as err:
            raise TimeoutError(f"Timed out waiting for {label}") from err

    async def resolve_runtime(_hass: HomeAssistant, _entry: ConfigEntry) -> ChihirosRuntime:
        return ChihirosRuntime(client=client, address=TEST_ADDRESS, always_available=True)

    monkeypatch.setattr(chihiros_integration, "resolve_chihiros_runtime", resolve_runtime)
    monkeypatch.setattr(bluetooth_update, "async_address_present", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(ChihirosDataUpdateCoordinator, "async_start_bluetooth", lambda _self: None)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title=client.name,
        unique_id=TEST_ADDRESS,
        data={CONF_ADDRESS: TEST_ADDRESS, **(entry_data or {})},
    )
    entry.add_to_hass(hass)
    await wait_for_step("config entry setup", hass.config_entries.async_setup(entry.entry_id))
    await client.query_status()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert entry.state is ConfigEntryState.LOADED
    return entry, client


def _entity_id(
    entity_registry: er.EntityRegistry,
    platform: str,
    unique_id: str,
) -> str:
    """Return a registered entity id by unique id."""
    entity_id = entity_registry.async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id is not None
    return entity_id


async def _flush_ha_state_updates() -> None:
    """Yield to state-write callbacks without waiting for HA background tasks."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_config_entry_sets_up_entities_services_status_and_unloads(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Set up and unload the config entry through Home Assistant."""
    entry, client = await _setup_entry(hass, monkeypatch)
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    for service in (SERVICE_ADD_SCHEDULE, SERVICE_REMOVE_SCHEDULE, SERVICE_RESET_SCHEDULE, SERVICE_SET_SCHEDULE):
        assert hass.services.has_service(DOMAIN, service)

    rgb_light = _entity_id(entity_registry, LIGHT_DOMAIN, f"{TEST_ADDRESS}_rgb")
    auto_switch = _entity_id(entity_registry, SWITCH_DOMAIN, f"{TEST_ADDRESS}_auto_mode")
    firmware_sensor = _entity_id(entity_registry, SENSOR_DOMAIN, f"{TEST_ADDRESS}_firmware_version")
    schedule_sensor = _entity_id(entity_registry, SENSOR_DOMAIN, f"{TEST_ADDRESS}_schedule_points")
    notification_sensor = _entity_id(entity_registry, SENSOR_DOMAIN, f"{TEST_ADDRESS}_last_notification")

    assert hass.states.get(rgb_light) is not None
    assert hass.states.get(auto_switch).state == STATE_OFF
    assert hass.states.get(firmware_sensor).state == "23"
    assert hass.states.get(schedule_sensor).state == "08:00 15%; 12:00 70%"
    assert hass.states.get(notification_sensor).state == "0xfe"
    assert hass.states.get(notification_sensor).attributes["parsed_type"] == "schedule_snapshot"

    device = device_registry.async_get_device(connections={(dr.CONNECTION_BLUETOOTH, TEST_ADDRESS)})
    assert device is not None
    assert device.manufacturer == "Chihiros"
    assert device.model == "Test RGB"

    assert await hass.config_entries.async_unload(entry.entry_id)

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not hass.services.has_service(DOMAIN, SERVICE_ADD_SCHEDULE)
    assert client.disconnect_calls == 1


async def test_light_and_auto_mode_services_drive_client(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive light and switch behavior through Home Assistant services."""
    _entry, client = await _setup_entry(hass, monkeypatch)
    entity_registry = er.async_get(hass)
    rgb_light = _entity_id(entity_registry, LIGHT_DOMAIN, f"{TEST_ADDRESS}_rgb")
    auto_switch = _entity_id(entity_registry, SWITCH_DOMAIN, f"{TEST_ADDRESS}_auto_mode")

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: rgb_light, ATTR_BRIGHTNESS: 128},
        blocking=True,
    )
    await _flush_ha_state_updates()

    # Brightness scales the default white (255,255,255) equally across RGB channels.
    assert client.brightness_calls[-1] == {"red": 51, "green": 51, "blue": 51}
    assert hass.states.get(rgb_light).state == STATE_ON
    assert hass.states.get(rgb_light).attributes[ATTR_BRIGHTNESS] == 128
    assert hass.states.get(auto_switch).state == STATE_OFF

    await hass.services.async_call(SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: auto_switch}, blocking=True)
    await _flush_ha_state_updates()

    assert client.auto_mode_calls and isinstance(client.auto_mode_calls[-1], datetime)
    assert hass.states.get(auto_switch).state == STATE_ON

    await hass.services.async_call(LIGHT_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: rgb_light}, blocking=True)
    await _flush_ha_state_updates()

    assert client.brightness_calls[-1] == {"red": 0, "green": 0, "blue": 0}
    assert hass.states.get(rgb_light).state == STATE_OFF
    assert hass.states.get(auto_switch).state == STATE_OFF

    await hass.services.async_call(SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: auto_switch}, blocking=True)
    await _flush_ha_state_updates()

    assert client.manual_mode_calls == 1
    assert hass.states.get(auto_switch).state == STATE_OFF


async def test_manual_dose_service_updates_persisted_daily_total_sensor(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual dose service drives the dosing client and updates today's persisted local total."""
    dosing_client = TrackingDosingClient()
    entry, client = await _setup_entry(hass, monkeypatch, dosing_client)
    assert isinstance(client, TrackingDosingClient)
    entity_registry = er.async_get(hass)
    pump_2_sensor = _entity_id(entity_registry, SENSOR_DOMAIN, f"{TEST_ADDRESS}_dosing_pump_2_dosed_today")
    pump_2_lifetime_ml = _entity_id(entity_registry, SENSOR_DOMAIN, f"{TEST_ADDRESS}_dosing_pump_2_total_ml")
    pump_2_lifetime_cycles = _entity_id(entity_registry, SENSOR_DOMAIN, f"{TEST_ADDRESS}_dosing_pump_2_total_cycles")

    assert hass.services.has_service(DOMAIN, SERVICE_DOSE_ML)
    assert hass.states.get(pump_2_sensor).state == "0.0"
    assert hass.states.get(pump_2_lifetime_ml).state == "0.0"
    assert hass.states.get(pump_2_lifetime_cycles).state == "0"

    await hass.services.async_call(
        DOMAIN,
        SERVICE_DOSE_ML,
        {ATTR_ENTRY_ID: entry.entry_id, ATTR_PUMP: 2, ATTR_ML: 2.5},
        blocking=True,
    )
    await _flush_ha_state_updates()

    assert client.dose_ml_calls == [(1, 2.5)]
    assert hass.states.get(pump_2_sensor).state == "2.5"
    assert hass.states.get(pump_2_lifetime_ml).state == "2.5"
    assert hass.states.get(pump_2_lifetime_cycles).state == "1"

    # A second dose adds onto the lifetime counters and cycles increment per dose.
    await hass.services.async_call(
        DOMAIN,
        SERVICE_DOSE_ML,
        {ATTR_ENTRY_ID: entry.entry_id, ATTR_PUMP: 2, ATTR_ML: 1.0},
        blocking=True,
    )
    await _flush_ha_state_updates()

    assert hass.states.get(pump_2_sensor).state == "3.5"
    assert hass.states.get(pump_2_lifetime_ml).state == "3.5"
    assert hass.states.get(pump_2_lifetime_cycles).state == "2"


async def test_two_channel_dosing_pump_creates_two_sensors_and_rejects_pump_three(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured dosing channel count controls entities and service validation."""
    dosing_client = TrackingDosingClient()
    entry, client = await _setup_entry(hass, monkeypatch, dosing_client, {CONF_PUMP_COUNT: "2"})
    entity_registry = er.async_get(hass)

    pump_2_sensor = _entity_id(entity_registry, SENSOR_DOMAIN, f"{TEST_ADDRESS}_dosing_pump_2_dosed_today")
    assert hass.states.get(pump_2_sensor).state == "0.0"
    pump_3_sensor = entity_registry.async_get_entity_id(
        SENSOR_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_dosing_pump_3_dosed_today"
    )
    assert pump_3_sensor is None
    pump_3_total_ml = entity_registry.async_get_entity_id(
        SENSOR_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_dosing_pump_3_total_ml"
    )
    pump_3_total_cycles = entity_registry.async_get_entity_id(
        SENSOR_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_dosing_pump_3_total_cycles"
    )
    assert pump_3_total_ml is None
    assert pump_3_total_cycles is None

    with pytest.raises(HomeAssistantError, match="has 2 pumps"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_DOSE_ML,
            {ATTR_ENTRY_ID: entry.entry_id, ATTR_PUMP: 3, ATTR_ML: 2.5},
            blocking=True,
        )

    assert client.dose_ml_calls == []


async def test_schedule_services_are_not_registered_for_dosing_only(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Light schedule services are hidden when only dosing pumps are configured."""
    _entry, client = await _setup_entry(hass, monkeypatch, TrackingDosingClient())

    assert not hass.services.has_service(DOMAIN, SERVICE_ADD_SCHEDULE)
    assert not hass.services.has_service(DOMAIN, SERVICE_REMOVE_SCHEDULE)
    assert not hass.services.has_service(DOMAIN, SERVICE_RESET_SCHEDULE)
    assert not hass.services.has_service(DOMAIN, SERVICE_SET_SCHEDULE)
    assert hass.services.has_service(DOMAIN, SERVICE_DOSE_ML)
    assert client.remove_setting_calls == []
    assert client.reset_settings_calls == 0


async def test_schedule_services_drive_client(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive schedule services through Home Assistant's service registry."""
    entry, client = await _setup_entry(hass, monkeypatch)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_ADD_SCHEDULE,
        {
            ATTR_ENTRY_ID: entry.entry_id,
            ATTR_START: "08:00",
            ATTR_END: "18:30",
            ATTR_LEVELS: {"red": 80, "green": 70, "blue": 60},
            ATTR_RAMP_UP_MINUTES: 20,
            ATTR_WEEKDAYS: ["monday", "wednesday"],
        },
        blocking=True,
    )

    add_call = client.add_setting_calls[-1]
    assert add_call["sunrise"].strftime("%H:%M") == "08:00"
    assert add_call["sunset"].strftime("%H:%M") == "18:30"
    assert add_call["max_brightness"] == {"red": 80, "green": 70, "blue": 60}
    assert add_call["ramp_up_in_minutes"] == 20
    assert [weekday.value for weekday in add_call["weekdays"]] == ["monday", "wednesday"]

    await hass.services.async_call(
        DOMAIN,
        SERVICE_REMOVE_SCHEDULE,
        {
            ATTR_ADDRESS: TEST_ADDRESS.lower(),
            ATTR_START: "08:00",
            ATTR_END: "18:30",
            ATTR_RAMP_UP_MINUTES: 20,
            ATTR_WEEKDAYS: ["monday"],
        },
        blocking=True,
    )

    remove_call = client.remove_setting_calls[-1]
    assert remove_call["sunrise"].strftime("%H:%M") == "08:00"
    assert remove_call["sunset"].strftime("%H:%M") == "18:30"
    assert [weekday.value for weekday in remove_call["weekdays"]] == ["monday"]

    await hass.services.async_call(DOMAIN, SERVICE_RESET_SCHEDULE, {}, blocking=True)
    assert client.reset_settings_calls == 1

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_SCHEDULE,
        {
            ATTR_PERIODS: [
                {
                    ATTR_START: "07:00",
                    ATTR_END: "09:00",
                    ATTR_SCHEDULE_BRIGHTNESS: 40,
                    ATTR_WEEKDAYS: ["tuesday"],
                },
                {
                    ATTR_START: "17:00",
                    ATTR_END: "20:00",
                    ATTR_SCHEDULE_BRIGHTNESS: {"red": 10, "green": 20, "blue": 30},
                    ATTR_WEEKDAYS: ["thursday"],
                },
            ]
        },
        blocking=True,
    )

    assert client.reset_settings_calls == 2
    assert client.add_setting_calls[-2]["max_brightness"] == 40
    assert client.add_setting_calls[-1]["max_brightness"] == {"red": 10, "green": 20, "blue": 30}


async def test_dosing_button_press_triggers_dose_with_configured_volume(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing a pump's dose button doses the volume configured on its number."""
    dosing_client = TrackingDosingClient()
    entry, client = await _setup_entry(hass, monkeypatch, dosing_client)
    assert isinstance(client, TrackingDosingClient)
    entity_registry = er.async_get(hass)
    pump_1_number = _entity_id(entity_registry, NUMBER_DOMAIN, f"{TEST_ADDRESS}_dosing_pump_1_dose_volume")
    pump_1_button = _entity_id(entity_registry, BUTTON_DOMAIN, f"{TEST_ADDRESS}_dosing_pump_1_dose")

    # Configure pump 1's dose volume to 3.0 mL.
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: pump_1_number, ATTR_NUMBER_VALUE: 3.0},
        blocking=True,
    )
    await _flush_ha_state_updates()

    await hass.services.async_call(BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: pump_1_button}, blocking=True)
    await _flush_ha_state_updates()

    assert client.dose_ml_calls == [(0, 3.0)]
    # The dosing total sensor reflects the dosed volume.
    pump_1_sensor = _entity_id(entity_registry, SENSOR_DOMAIN, f"{TEST_ADDRESS}_dosing_pump_1_dosed_today")
    assert hass.states.get(pump_1_sensor).state == "3.0"


async def test_dosing_number_set_value_updates_runtime_volume(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting a pump dose volume number updates the persisted runtime volume."""
    dosing_client = TrackingDosingClient()
    entry, client = await _setup_entry(hass, monkeypatch, dosing_client)
    assert isinstance(client, TrackingDosingClient)
    entity_registry = er.async_get(hass)
    pump_1_number = _entity_id(entity_registry, NUMBER_DOMAIN, f"{TEST_ADDRESS}_dosing_pump_1_dose_volume")

    # Initial value is the integration default of 1.0 mL per pump.
    assert hass.states.get(pump_1_number).state == "1.0"

    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: pump_1_number, ATTR_NUMBER_VALUE: 5.5},
        blocking=True,
    )
    await _flush_ha_state_updates()

    assert hass.states.get(pump_1_number).state == "5.5"
    assert hass.data[DOMAIN][entry.entry_id].dosing_volumes[0] == 5.5


async def test_dosing_number_restore_skips_out_of_range_and_invalid(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restoring an out-of-range or non-numeric value leaves the default in place."""
    dosing_client = TrackingDosingClient()
    entry, client = await _setup_entry(hass, monkeypatch, dosing_client)
    assert isinstance(client, TrackingDosingClient)
    entity_registry = er.async_get(hass)
    pump_1_number = _entity_id(entity_registry, NUMBER_DOMAIN, f"{TEST_ADDRESS}_dosing_pump_1_dose_volume")
    pump_2_number = _entity_id(entity_registry, NUMBER_DOMAIN, f"{TEST_ADDRESS}_dosing_pump_2_dose_volume")

    await hass.config_entries.async_unload(entry.entry_id)
    await _flush_ha_state_updates()

    # Prime restore: out-of-range for pump 1, garbage string for pump 2.
    from homeassistant.core import State
    from homeassistant.helpers.restore_state import StoredState
    from homeassistant.helpers.restore_state import async_get as async_get_restore_data
    from homeassistant.util import dt as dt_util

    restore_data = async_get_restore_data(hass)
    restore_data.last_states[pump_1_number] = StoredState(State(pump_1_number, "9999.0"), None, dt_util.utcnow())
    restore_data.last_states[pump_2_number] = StoredState(State(pump_2_number, "not a number"), None, dt_util.utcnow())

    await hass.config_entries.async_setup(entry.entry_id)
    await _flush_ha_state_updates()
    assert entry.state is ConfigEntryState.LOADED

    # Both rejected restores keep the integration default of 1.0 mL.
    assert hass.states.get(pump_1_number).state == "1.0"
    assert hass.states.get(pump_2_number).state == "1.0"


async def test_dosing_number_restore_valid_value(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restoring a valid in-range volume is applied on reload."""
    dosing_client = TrackingDosingClient()
    entry, client = await _setup_entry(hass, monkeypatch, dosing_client)
    assert isinstance(client, TrackingDosingClient)
    entity_registry = er.async_get(hass)
    pump_3_number = _entity_id(entity_registry, NUMBER_DOMAIN, f"{TEST_ADDRESS}_dosing_pump_3_dose_volume")

    await hass.config_entries.async_unload(entry.entry_id)
    await _flush_ha_state_updates()

    from homeassistant.core import State
    from homeassistant.helpers.restore_state import StoredState
    from homeassistant.helpers.restore_state import async_get as async_get_restore_data
    from homeassistant.util import dt as dt_util

    restore_data = async_get_restore_data(hass)
    restore_data.last_states[pump_3_number] = StoredState(State(pump_3_number, "3.25"), None, dt_util.utcnow())

    await hass.config_entries.async_setup(entry.entry_id)
    await _flush_ha_state_updates()
    assert entry.state is ConfigEntryState.LOADED

    # Restored value is rounded to one decimal place.
    assert hass.states.get(pump_3_number).state == "3.2"

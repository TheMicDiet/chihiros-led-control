"""Home Assistant tests for heater support (entities, notifications, restore)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

try:
    from homeassistant.components.bluetooth import update_coordinator as bluetooth_update
    from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
    from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
    from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
    from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
    from homeassistant.config_entries import ConfigEntry, ConfigEntryState
    from homeassistant.const import ATTR_ENTITY_ID, CONF_ADDRESS, STATE_OFF, STATE_ON, UnitOfTemperature
    from homeassistant.core import HomeAssistant, State
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.restore_state import StoredState
    from homeassistant.helpers.restore_state import async_get as async_get_restore_data
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    import custom_components.chihiros as chihiros_integration
    from custom_components.chihiros.const import DOMAIN
    from custom_components.chihiros.coordinator import ChihirosDataUpdateCoordinator
    from custom_components.chihiros.fake import create_fake_device
    from custom_components.chihiros.runtime import ChihirosRuntime
except ImportError as err:
    pytest.skip(
        f"Home Assistant test group is not installed or is incompatible: {err}",
        allow_module_level=True,
    )

from custom_components.chihiros.vendor.chihiros_led_control.models import HEATER
from custom_components.chihiros.vendor.chihiros_led_control.protocol import (
    HeaterStatusNotification,
    HeaterTemperatureNotification,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.usefixtures("enable_custom_integrations", "mock_bluetooth"),
]

TEST_ADDRESS = "FA:CE:C0:00:50:01"


class _TrackingHeater:
    """Minimal mock heater client for integration tests."""

    def __init__(self) -> None:
        self.model = HEATER
        self.temperature_calls: list[float] = []
        self.power_calls: list[int] = []
        self.protector_calls: list[float] = []
        self.calibration_calls: list[float] = []
        self.auto_heating_calls: list[bool] = []
        self.unit_calls: list[bool] = []
        self.reset_work_time_calls = 0
        self.write_exception: Exception | None = None
        self._setting_temperature = 25.0
        self._power_watts = 200
        self._protector_temperature = 37.0
        self._auto_heating = False
        self._celsius = True
        self._callbacks: set[Callable[[Any], None]] = set()

    @property
    def address(self) -> str:
        return TEST_ADDRESS

    @property
    def name(self) -> str:
        return "DYHET-test"

    @property
    def model_name(self) -> str:
        return self.model.name

    @property
    def colors(self) -> dict[str, int]:
        return {}

    @property
    def setting_temperature_celsius(self) -> float:
        return self._setting_temperature

    @property
    def power_watts(self) -> int:
        return self._power_watts

    @property
    def protector_temperature_celsius(self) -> float:
        return self._protector_temperature

    @property
    def auto_heating(self) -> bool:
        return self._auto_heating

    @property
    def is_celsius(self) -> bool:
        return self._celsius

    def add_notification_callback(self, callback: Callable[[Any], None]) -> Callable[[], None]:
        """Register a notification callback."""
        self._callbacks.add(callback)

        def remove() -> None:
            self._callbacks.discard(callback)

        return remove

    def push_temperature(self, setting_celsius: float, current_celsius: float) -> None:
        """Deliver a heater temperature notification to the coordinator."""
        notification = HeaterTemperatureNotification(
            setting_temperature_celsius=setting_celsius,
            current_temperature_celsius=current_celsius,
            raw=b"",
        )
        for callback in tuple(self._callbacks):
            callback(notification)

    def push_status(self, *, work_time_hours: int, alarms: int, firmware_version: int = 23) -> None:
        """Deliver a heater status notification to the coordinator."""
        notification = HeaterStatusNotification(
            firmware_version=firmware_version,
            work_time_hours=work_time_hours,
            alarms=alarms,
            raw=b"",
        )
        for callback in tuple(self._callbacks):
            callback(notification)

    async def query_status(self) -> None:
        """Accept a status request."""
        await asyncio.sleep(0)

    async def disconnect(self) -> None:
        """Disconnect the fake client."""

    async def _write(self) -> None:
        """Raise the configured write failure, if any."""
        if self.write_exception is not None:
            raise self.write_exception

    async def set_temperature(self, temperature_c: float) -> None:
        """Record a target temperature write."""
        await self._write()
        self._setting_temperature = temperature_c
        self.temperature_calls.append(temperature_c)

    async def set_power(self, power_watts: int) -> None:
        """Record a power write."""
        await self._write()
        self._power_watts = power_watts
        self.power_calls.append(power_watts)

    async def set_protector_temperature(self, temperature_c: float) -> None:
        """Record a protection temperature write."""
        await self._write()
        self._protector_temperature = temperature_c
        self.protector_calls.append(temperature_c)

    async def calibrate(self, measured_temperature_c: float) -> None:
        """Record a calibration write."""
        await self._write()
        self.calibration_calls.append(measured_temperature_c)

    async def set_auto_heating(self, enabled: bool) -> None:
        """Record an auto-heating write."""
        await self._write()
        self._auto_heating = enabled
        self.auto_heating_calls.append(enabled)

    async def set_temperature_unit(self, *, celsius: bool) -> None:
        """Record a display-unit write."""
        await self._write()
        self._celsius = celsius
        self.unit_calls.append(celsius)

    async def reset_work_time(self) -> None:
        """Record a runtime reset."""
        await self._write()
        self.reset_work_time_calls += 1


async def _setup_heater(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    client: Any | None = None,
) -> tuple[ConfigEntry, Any]:
    """Set up the integration against a mock or fake heater client."""
    client = client or _TrackingHeater()

    async def resolve_runtime(_hass: HomeAssistant, _entry: ConfigEntry) -> ChihirosRuntime:
        return ChihirosRuntime(client=client, address=TEST_ADDRESS, always_available=True)

    monkeypatch.setattr(chihiros_integration, "resolve_chihiros_runtime", resolve_runtime)
    monkeypatch.setattr(bluetooth_update, "async_address_present", lambda *_a, **_k: True)
    monkeypatch.setattr(ChihirosDataUpdateCoordinator, "async_start_bluetooth", lambda _self: None)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title=client.name,
        unique_id=TEST_ADDRESS,
        data={CONF_ADDRESS: TEST_ADDRESS},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await _flush()
    assert entry.state is ConfigEntryState.LOADED
    return entry, client


async def _flush() -> None:
    """Yield to pending state-write callbacks."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def _entity_id(hass: HomeAssistant, domain: str, suffix: str) -> str:
    """Resolve an entity id from the entity registry by unique id suffix."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(domain, DOMAIN, f"{TEST_ADDRESS}_{suffix}")
    assert entity_id is not None, f"no {domain} entity registered for {suffix}"
    return entity_id


async def _reload_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    prime: Callable[[], None] | None = None,
) -> None:
    """Unload and re-setup a config entry so entities restore from stored state."""
    await hass.config_entries.async_unload(entry.entry_id)
    await _flush()
    if prime is not None:
        prime()
    await hass.config_entries.async_setup(entry.entry_id)
    await _flush()
    assert entry.state is ConfigEntryState.LOADED


def _prime_restore_state(hass: HomeAssistant, entity_id: str, state: State) -> None:
    """Inject a stored last-state so the next entity load restores from it."""
    async_get_restore_data(hass).last_states[entity_id] = StoredState(state, None, dt_util.utcnow())


async def test_heater_setup_creates_all_entities(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """A heater entry gets its numbers, switch, select, button and sensors."""
    await _setup_heater(hass, monkeypatch)
    await hass.async_block_till_done()

    registered = {
        domain: {
            entry.unique_id.split("_", 1)[1]
            for entry in er.async_get(hass).entities.values()
            if entry.domain == domain
        }
        for domain in (NUMBER_DOMAIN, SWITCH_DOMAIN, SELECT_DOMAIN, SENSOR_DOMAIN)
    }
    assert {
        "heater_temperature",
        "heater_power",
        "heater_protector_temperature",
        "heater_calibration_temperature",
    } <= registered[NUMBER_DOMAIN]
    assert "heater_auto_heating" in registered[SWITCH_DOMAIN]
    assert "heater_temperature_unit" in registered[SELECT_DOMAIN]
    assert {
        "heater_current_temperature_celsius",
        "heater_work_time_hours",
        "heater_alarms",
        "firmware_version",
    } <= registered[SENSOR_DOMAIN]
    assert _entity_id(hass, "button", "heater_reset_work_time")


async def test_heater_notifications_update_sensors(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pushed heater frames populate the temperature, runtime and alarm sensors."""
    _entry, client = await _setup_heater(hass, monkeypatch)
    await hass.async_block_till_done()

    client.push_temperature(26.6, 25.8)
    client.push_status(work_time_hours=1994, alarms=0x01 | 0x08, firmware_version=0x0F1B)
    await _flush()

    current = hass.states.get(_entity_id(hass, SENSOR_DOMAIN, "heater_current_temperature_celsius"))
    assert float(current.state) == pytest.approx(25.8)
    runtime = hass.states.get(_entity_id(hass, SENSOR_DOMAIN, "heater_work_time_hours"))
    assert float(runtime.state) == 1994
    alarms = hass.states.get(_entity_id(hass, SENSOR_DOMAIN, "heater_alarms"))
    assert alarms.state == "insufficient_water, needs_cleaning"
    assert alarms.attributes["alarm_bits"] == 0x09
    assert alarms.attributes["alarms"] == ["insufficient_water", "needs_cleaning"]
    firmware = hass.states.get(_entity_id(hass, SENSOR_DOMAIN, "firmware_version"))
    assert float(firmware.state) == 0x0F1B


async def test_heater_temperature_number_prefers_reported_value(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The temperature number writes to the client and follows the device afterwards."""
    _entry, client = await _setup_heater(hass, monkeypatch)
    entity_id = _entity_id(hass, NUMBER_DOMAIN, "heater_temperature")

    client.push_temperature(26.5, 26.0)
    await _flush()
    assert float(hass.states.get(entity_id).state) == pytest.approx(26.5)

    await hass.services.async_call(
        NUMBER_DOMAIN,
        "set_value",
        {ATTR_ENTITY_ID: entity_id, "value": 24.5},
        blocking=True,
    )
    await _flush()
    assert client.temperature_calls == [24.5]
    assert float(hass.states.get(entity_id).state) == pytest.approx(24.5)

    # A fresh device report replaces the value written through Home Assistant.
    client.push_temperature(28.0, 27.5)
    await _flush()
    assert float(hass.states.get(entity_id).state) == pytest.approx(28.0)


async def test_heater_power_number_writes_and_restores(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The power number writes watts and restores its value across a reload."""
    entry, client = await _setup_heater(hass, monkeypatch)
    entity_id = _entity_id(hass, NUMBER_DOMAIN, "heater_power")

    await hass.services.async_call(
        NUMBER_DOMAIN,
        "set_value",
        {ATTR_ENTITY_ID: entity_id, "value": 800},
        blocking=True,
    )
    await _flush()
    assert client.power_calls == [800]
    assert float(hass.states.get(entity_id).state) == pytest.approx(800)

    # A fresh client has no idea about the power, so the restored value is shown.
    await _reload_entry(
        hass,
        entry,
        prime=lambda: _prime_restore_state(hass, entity_id, State(entity_id, "800.0")),
    )
    assert float(hass.states.get(entity_id).state) == pytest.approx(800)

    # Writing through Home Assistant outranks the restored value, so a later
    # device notification must not roll the number back to 800 W.
    await hass.services.async_call(
        NUMBER_DOMAIN,
        "set_value",
        {ATTR_ENTITY_ID: entity_id, "value": 400},
        blocking=True,
    )
    client.push_status(work_time_hours=10, alarms=0)
    await _flush()

    assert client.power_calls == [800, 400]
    assert float(hass.states.get(entity_id).state) == pytest.approx(400)


async def test_heater_switch_restores_and_drives_client(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The auto-heating switch is optimistic, restored and writes to the client."""
    entry, client = await _setup_heater(hass, monkeypatch)
    entity_id = _entity_id(hass, SWITCH_DOMAIN, "heater_auto_heating")
    assert hass.states.get(entity_id).state == STATE_OFF

    await hass.services.async_call(
        SWITCH_DOMAIN,
        "turn_on",
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    await _flush()
    assert client.auto_heating_calls == [True]
    assert hass.states.get(entity_id).state == STATE_ON

    await _reload_entry(
        hass,
        entry,
        prime=lambda: _prime_restore_state(hass, entity_id, State(entity_id, STATE_ON)),
    )
    assert hass.states.get(entity_id).state == STATE_ON
    # Restoring must not silently rewrite the device.
    assert client.auto_heating_calls == [True]


async def test_heater_unit_select_writes_client(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """The unit select writes the display unit to the client."""
    _entry, client = await _setup_heater(hass, monkeypatch)
    entity_id = _entity_id(hass, SELECT_DOMAIN, "heater_temperature_unit")
    assert hass.states.get(entity_id).state == UnitOfTemperature.CELSIUS

    await hass.services.async_call(
        SELECT_DOMAIN,
        "select_option",
        {ATTR_ENTITY_ID: entity_id, "option": UnitOfTemperature.FAHRENHEIT},
        blocking=True,
    )
    await _flush()

    assert client.unit_calls == [False]
    assert hass.states.get(entity_id).state == UnitOfTemperature.FAHRENHEIT


async def test_heater_calibration_and_protection_numbers(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """The protection and calibration numbers write their configured temperatures."""
    _entry, client = await _setup_heater(hass, monkeypatch)

    await hass.services.async_call(
        NUMBER_DOMAIN,
        "set_value",
        {ATTR_ENTITY_ID: _entity_id(hass, NUMBER_DOMAIN, "heater_protector_temperature"), "value": 35.5},
        blocking=True,
    )
    await hass.services.async_call(
        NUMBER_DOMAIN,
        "set_value",
        {ATTR_ENTITY_ID: _entity_id(hass, NUMBER_DOMAIN, "heater_calibration_temperature"), "value": 26.0},
        blocking=True,
    )
    await _flush()

    assert client.protector_calls == [35.5]
    assert client.calibration_calls == [26.0]


async def test_heater_reset_runtime_button(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reset button zeroes the device's runtime counter."""
    _entry, client = await _setup_heater(hass, monkeypatch)
    entity_id = _entity_id(hass, "button", "heater_reset_work_time")

    await hass.services.async_call("button", "press", {ATTR_ENTITY_ID: entity_id}, blocking=True)
    await _flush()

    assert client.reset_work_time_calls == 1


async def test_heater_write_failure_raises_home_assistant_error(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing BLE write surfaces as HomeAssistantError and keeps the old state."""
    _entry, client = await _setup_heater(hass, monkeypatch)
    entity_id = _entity_id(hass, NUMBER_DOMAIN, "heater_temperature")
    client.write_exception = RuntimeError("ble write failed")

    with pytest.raises(HomeAssistantError, match="Failed to set"):
        await hass.services.async_call(
            NUMBER_DOMAIN,
            "set_value",
            {ATTR_ENTITY_ID: entity_id, "value": 30.0},
            blocking=True,
        )
    await _flush()

    assert client.temperature_calls == []
    assert hass.states.get(entity_id).state in ("unknown", "unavailable")


@pytest.mark.parametrize("name", ["DYHET-abc", "DYH1T-abc"])
async def test_bluetooth_discovery_reaches_confirm_step(hass: HomeAssistant, name: str) -> None:
    """Auto-discovered heaters only ask for confirmation, without a config form."""
    import time
    from types import SimpleNamespace

    from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

    discovery = BluetoothServiceInfoBleak(
        name=name,
        address=f"AA:BB:CC:DD:EE:{0x01 if name.startswith('DYHET') else 0x02:02X}",
        rssi=-60,
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        source="local",
        device=SimpleNamespace(name=name, address="AA:BB:CC:DD:EE:FF"),
        advertisement=None,
        connectable=True,
        time=time.time(),
        tx_power=None,
    )
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "bluetooth"}, data=discovery)

    assert result["type"] == "form"
    assert result["step_id"] == "bluetooth_confirm"
    assert result["description_placeholders"] == {"name": name}


async def test_fake_heater_device_exposes_working_entities(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The development fake heater drives the entities end to end."""
    fake = create_fake_device("FA:CE:C0:00:00:10")
    await _setup_heater(hass, monkeypatch, client=fake)
    await hass.async_block_till_done()

    temperature_id = _entity_id(hass, NUMBER_DOMAIN, "heater_temperature")
    runtime_id = _entity_id(hass, SENSOR_DOMAIN, "heater_work_time_hours")

    await hass.services.async_call(
        NUMBER_DOMAIN,
        "set_value",
        {ATTR_ENTITY_ID: temperature_id, "value": 27.0},
        blocking=True,
    )
    await _flush()

    assert fake.setting_temperature_celsius == pytest.approx(27.0)
    assert float(hass.states.get(temperature_id).state) == pytest.approx(27.0)
    assert float(hass.states.get(runtime_id).state) == 120

    await hass.services.async_call(
        "button",
        "press",
        {ATTR_ENTITY_ID: _entity_id(hass, "button", "heater_reset_work_time")},
        blocking=True,
    )
    await _flush()
    assert float(hass.states.get(runtime_id).state) == 0

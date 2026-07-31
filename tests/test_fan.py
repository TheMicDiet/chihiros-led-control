"""Integration tests for the Chihiros fan platform."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

try:
    from homeassistant.components.bluetooth import update_coordinator as bluetooth_update
    from homeassistant.components.fan import (
        ATTR_PERCENTAGE,
        SERVICE_SET_PERCENTAGE,
    )
    from homeassistant.components.fan import (
        DOMAIN as FAN_DOMAIN,
    )
    from homeassistant.config_entries import ConfigEntry, ConfigEntryState
    from homeassistant.const import ATTR_ENTITY_ID, CONF_ADDRESS, STATE_OFF, STATE_ON
    from homeassistant.core import HomeAssistant, State
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.restore_state import StoredState
    from homeassistant.helpers.restore_state import async_get as async_get_restore_data
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    import custom_components.chihiros as chihiros_integration
    from custom_components.chihiros.const import DOMAIN
    from custom_components.chihiros.coordinator import ATTR_FAN_RPM, ChihirosDataUpdateCoordinator
    from custom_components.chihiros.runtime import ChihirosRuntime
except ImportError as err:
    pytest.skip(
        f"Home Assistant test group is not installed or is incompatible: {err}",
        allow_module_level=True,
    )

from custom_components.chihiros.vendor.chihiros_led_control.models import (
    RGB_CHANNELS,
    WRGB_CHANNELS,
    DeviceModel,
)
from custom_components.chihiros.vendor.chihiros_led_control.protocol import (
    FanStatusNotification,
    ParsedNotification,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.usefixtures("enable_custom_integrations", "mock_bluetooth"),
]

TEST_ADDRESS = "FA:CE:C0:00:20:01"
FAN_MODEL = DeviceModel("WRGB VIVID III", ("DYVVD3",), WRGB_CHANNELS, has_fan=True)
RGB_MODEL = DeviceModel("Test RGB", ("TEST-RGB",), RGB_CHANNELS)


class _TrackingClient:
    """Minimal mock Chihiros client for fan entity tests."""

    def __init__(self, model: DeviceModel) -> None:
        self.model = model
        self.fan_speed_calls: list[int] = []
        self._callbacks: set[Callable[[ParsedNotification], None]] = set()
        self.set_fan_speed_exception: Exception | None = None

    @property
    def address(self) -> str:
        return TEST_ADDRESS

    @property
    def name(self) -> str:
        return "Test Chihiros"

    @property
    def model_name(self) -> str:
        return self.model.name

    @property
    def colors(self) -> dict[str, int]:
        return dict(self.model.color_channels)

    def add_notification_callback(self, callback: Callable[[ParsedNotification], None]) -> Callable[[], None]:
        self._callbacks.add(callback)

        def remove() -> None:
            self._callbacks.discard(callback)

        return remove

    async def set_fan_speed(self, speed_percent: int) -> None:
        if self.set_fan_speed_exception is not None:
            raise self.set_fan_speed_exception
        self.fan_speed_calls.append(speed_percent)

    async def disconnect(self) -> None:
        pass


async def _setup(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    model: DeviceModel = FAN_MODEL,
    *,
    always_available: bool = True,
) -> tuple[ConfigEntry, _TrackingClient, ChihirosDataUpdateCoordinator]:
    """Set up the integration with a fan-capable device model."""
    client = _TrackingClient(model)

    async def resolve_runtime(_hass: HomeAssistant, _entry: ConfigEntry) -> ChihirosRuntime:
        return ChihirosRuntime(client=client, address=TEST_ADDRESS, always_available=always_available)

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
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert entry.state is ConfigEntryState.LOADED

    coordinator: ChihirosDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id].coordinator
    return entry, client, coordinator


async def _flush() -> None:
    """Yield to pending state-write callbacks."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def _entity_id(registry: er.EntityRegistry) -> str:
    """Look up the fan entity id and assert it exists."""
    entity_id = registry.async_get_entity_id(FAN_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_fan")
    assert entity_id is not None
    return entity_id


def _prime_restore_state(hass: HomeAssistant, entity_id: str, state: State) -> None:
    """Inject a stored last-state so the next entity load restores from it."""
    async_get_restore_data(hass).last_states[entity_id] = StoredState(state, None, dt_util.utcnow())


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


async def test_fan_entity_created_for_fan_model(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fan-equipped model gets a fan entity."""
    _entry, _client, _coordinator = await _setup(hass, monkeypatch, FAN_MODEL)
    registry = er.async_get(hass)
    assert _entity_id(registry) is not None


async def test_fan_set_percentage_turn_on_and_turn_off_drive_client(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """set_percentage/turn_on/turn_off forward to set_fan_speed and update state.

    turn_on/turn_off are invoked through the entity object directly because the
    fan entity only advertises SET_SPEED (HA's fan services require explicit
    TURN_ON/TURN_OFF feature flags the entity does not set).
    """
    _entry, client, _coordinator = await _setup(hass, monkeypatch, FAN_MODEL)
    registry = er.async_get(hass)
    entity_id = _entity_id(registry)
    entity = hass.data[FAN_DOMAIN].get_entity(entity_id)
    assert entity is not None

    await hass.services.async_call(
        FAN_DOMAIN,
        SERVICE_SET_PERCENTAGE,
        {ATTR_ENTITY_ID: entity_id, ATTR_PERCENTAGE: 50},
        blocking=True,
    )
    await _flush()

    assert client.fan_speed_calls == [50]
    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_PERCENTAGE] == 50

    # turn_on without percentage restores the previous percentage
    await entity.async_turn_on()
    await _flush()
    assert client.fan_speed_calls == [50, 50]

    await entity.async_turn_off()
    await _flush()
    assert client.fan_speed_calls == [50, 50, 0]
    state = hass.states.get(entity_id)
    assert state.state == STATE_OFF
    assert state.attributes[ATTR_PERCENTAGE] == 0


async def test_fan_set_speed_failure_raises_and_keeps_previous_state(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A BLE failure during set_percentage raises HomeAssistantError and keeps state."""
    _entry, client, _coordinator = await _setup(hass, monkeypatch, FAN_MODEL)
    registry = er.async_get(hass)
    entity_id = _entity_id(registry)

    # Establish a known on state at 40% first.
    await hass.services.async_call(
        FAN_DOMAIN,
        SERVICE_SET_PERCENTAGE,
        {ATTR_ENTITY_ID: entity_id, ATTR_PERCENTAGE: 40},
        blocking=True,
    )
    await _flush()

    client.set_fan_speed_exception = RuntimeError("ble write failed")
    with pytest.raises(HomeAssistantError, match="Failed to set fan speed"):
        await hass.services.async_call(
            FAN_DOMAIN,
            SERVICE_SET_PERCENTAGE,
            {ATTR_ENTITY_ID: entity_id, ATTR_PERCENTAGE: 80},
            blocking=True,
        )
    await _flush()

    # The failed write must not have advanced the reported percentage.
    state = hass.states.get(entity_id)
    assert state.attributes[ATTR_PERCENTAGE] == 40


async def test_fan_restores_percentage_from_last_state(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fan entity restores its percentage on reload."""
    entry, _client, _coordinator = await _setup(hass, monkeypatch, FAN_MODEL)
    registry = er.async_get(hass)
    entity_id = _entity_id(registry)

    await _reload_entry(
        hass,
        entry,
        prime=lambda: _prime_restore_state(
            hass,
            entity_id,
            State(entity_id, STATE_ON, {"percentage": 65}),
        ),
    )

    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_PERCENTAGE] == 65


async def test_fan_extra_state_attributes_reflect_fan_status_notification(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fan status notification surfaces fan_rpm as an extra state attribute."""
    _entry, _client, coordinator = await _setup(hass, monkeypatch, FAN_MODEL)
    registry = er.async_get(hass)
    entity_id = _entity_id(registry)

    # No notification yet -> no fan_rpm attribute.
    assert hass.states.get(entity_id).attributes.get(ATTR_FAN_RPM) is None

    notification = FanStatusNotification(
        firmware_version=27,
        fan_rpm=1234,
        temperature_celsius=25,
        raw=bytes.fromhex("5b 1b 10 00 01 0b 04 d2 19 00 01 00 00 00 00 00 48 22"),
    )
    coordinator._async_handle_notification(notification)
    await _flush()

    assert hass.states.get(entity_id).attributes[ATTR_FAN_RPM] == 1234


async def test_fan_available_when_not_always_available(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fan entity delegates to coordinator availability when not always available."""
    _entry, _client, _coordinator = await _setup(hass, monkeypatch, FAN_MODEL, always_available=False)
    registry = er.async_get(hass)
    entity_id = _entity_id(registry)

    await _flush()

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state != "unavailable"

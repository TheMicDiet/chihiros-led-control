"""Integration tests for the unified RGB/RGBW light entity."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

try:
    from homeassistant.components.bluetooth import update_coordinator as bluetooth_update
    from homeassistant.components.light import (
        ATTR_BRIGHTNESS,
        ATTR_RGB_COLOR,
        ATTR_RGBW_COLOR,
    )
    from homeassistant.components.light import (
        DOMAIN as LIGHT_DOMAIN,
    )
    from homeassistant.config_entries import ConfigEntry, ConfigEntryState
    from homeassistant.const import ATTR_ENTITY_ID, CONF_ADDRESS, SERVICE_TURN_ON, STATE_OFF, STATE_ON
    from homeassistant.core import HomeAssistant, State
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.restore_state import StoredState
    from homeassistant.helpers.restore_state import async_get as async_get_restore_data
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    import custom_components.chihiros as chihiros_integration
    from custom_components.chihiros.const import DOMAIN
    from custom_components.chihiros.coordinator import ChihirosDataUpdateCoordinator
    from custom_components.chihiros.runtime import ChihirosRuntime
except ImportError as err:
    pytest.skip(
        f"Home Assistant test group is not installed or is incompatible: {err}",
        allow_module_level=True,
    )

from custom_components.chihiros.vendor.chihiros_led_control.models import (
    RGB_CHANNELS,
    WHITE_CHANNELS,
    WRGB_CHANNELS,
    DeviceKind,
    DeviceModel,
    LedSpec,
)
from custom_components.chihiros.vendor.chihiros_led_control.protocol.notifications import ParsedNotification

pytestmark = [
    pytest.mark.integration,
    pytest.mark.homeassistant,
    pytest.mark.asyncio,
    pytest.mark.usefixtures("enable_custom_integrations", "mock_bluetooth"),
]

TEST_ADDRESS = "FA:CE:C0:00:10:02"


class _TrackingClient:
    """Minimal mock Chihiros client for RGB entity tests."""

    def __init__(self, model: DeviceModel) -> None:
        self.model = model
        self.brightness_calls: list[Any] = []
        self._callbacks: set[Callable[[ParsedNotification], None]] = set()

    @property
    def address(self) -> str:
        return TEST_ADDRESS

    @property
    def name(self) -> str:
        return "Test Chihiros"

    @property
    def device_kind(self) -> DeviceKind:
        return self.model.device_kind

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

    async def set_brightness(self, brightness: Any) -> None:
        self.brightness_calls.append(brightness)

    async def query_status(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass


WRGB_MODEL = DeviceModel("WRGB II Pro", ("DYWPRO30",), LedSpec(WRGB_CHANNELS))
WHITE_MODEL = DeviceModel("A II", ("DYNA2",), LedSpec(WHITE_CHANNELS))


async def _setup(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    model: DeviceModel,
) -> tuple[ConfigEntry, _TrackingClient]:
    """Set up the integration with a specific device model."""
    client = _TrackingClient(model)

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
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert entry.state is ConfigEntryState.LOADED
    return entry, client


async def _flush() -> None:
    """Yield to pending state-write callbacks."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def _entity_id(registry: er.EntityRegistry, suffix: str) -> str:
    """Look up a light entity id by unique-id suffix and assert it exists."""
    entity_id = registry.async_get_entity_id(LIGHT_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_{suffix}")
    assert entity_id is not None
    return entity_id


def _prime_restore_state(hass: HomeAssistant, entity_id: str, state: State) -> None:
    """Inject a stored last-state so the next entity load restores from it."""
    async_get_restore_data(hass).last_states[entity_id] = StoredState(state, None, dt_util.utcnow())


def _light_state(entity_id: str, is_on: bool = False, **attributes: Any) -> State:
    """Build a restore-source State with the given attributes."""
    return State(entity_id, STATE_ON if is_on else STATE_OFF, attributes)


async def _reload_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    prime: Callable[[], None] | None = None,
) -> None:
    """Unload and re-setup a config entry so entities restore from stored state.

    ``prime`` is called between unload and re-setup so callers can inject a
    stored last-state (it must run after unload, because removing the entity
    overwrites ``last_states[entity_id]`` with the entity's current state).
    """
    await hass.config_entries.async_unload(entry.entry_id)
    await _flush()
    if prime is not None:
        prime()
    await hass.config_entries.async_setup(entry.entry_id)
    await _flush()
    assert entry.state is ConfigEntryState.LOADED


# --- turn_on / turn_off tests ---


async def test_rgb_turn_on_with_rgb_color(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning on with rgb_color sends all channels in one call."""
    _entry, client = await _setup(hass, monkeypatch, DeviceModel("Test RGB", (), LedSpec(RGB_CHANNELS)))
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(LIGHT_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_rgb")
    assert entity_id is not None

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id, ATTR_RGB_COLOR: [255, 0, 0]},
        blocking=True,
    )
    await _flush()

    assert len(client.brightness_calls) == 1
    call = client.brightness_calls[0]
    assert isinstance(call, dict)
    assert call["red"] == 100
    assert call["green"] == 0
    assert call["blue"] == 0
    assert hass.states.get(entity_id).state == STATE_ON


# --- RGBW-specific tests ---


async def test_rgbw_turn_on_with_rgbw_color(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning on with rgbw_color sends all four channels."""
    _entry, client = await _setup(hass, monkeypatch, WRGB_MODEL)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(LIGHT_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_rgbw")
    assert entity_id is not None

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id, ATTR_RGBW_COLOR: [255, 0, 0, 128]},
        blocking=True,
    )
    await _flush()

    call = client.brightness_calls[-1]
    assert call["red"] == 100
    assert call["green"] == 0
    assert call["blue"] == 0
    assert call["white"] == 51  # ceil(128/255*100)


async def test_rgbw_turn_on_with_rgb_color_does_not_touch_white(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sending rgb_color to a WRGB entity only updates RGB channels."""
    _entry, client = await _setup(hass, monkeypatch, WRGB_MODEL)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(LIGHT_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_rgbw")
    assert entity_id is not None

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id, ATTR_RGB_COLOR: [255, 0, 0]},
        blocking=True,
    )
    await _flush()

    call = client.brightness_calls[-1]
    # HA converts rgb_color to rgbw_color with white=0 for RGBW entities
    assert call == {"red": 100, "green": 0, "blue": 0, "white": 0}


# --- white-only per-channel entity (ChihirosLightEntity) ---


async def test_white_only_device_creates_per_channel_light_entity(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A white-only device gets a single per-channel brightness entity."""
    _entry, _client = await _setup(hass, monkeypatch, WHITE_MODEL)
    registry = er.async_get(hass)

    white_id = registry.async_get_entity_id(LIGHT_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_white")
    assert white_id is not None
    # No unified RGB/RGBW entity for white-only devices
    assert registry.async_get_entity_id(LIGHT_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_rgb") is None
    assert registry.async_get_entity_id(LIGHT_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_rgbw") is None


# --- restore-state tests ---


async def test_rgb_entity_restores_color_and_brightness_from_last_state(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unified RGB entity restores rgb_color and brightness on reload."""
    entry, _client = await _setup(hass, monkeypatch, DeviceModel("Test RGB", (), LedSpec(RGB_CHANNELS)))
    registry = er.async_get(hass)
    entity_id = _entity_id(registry, "rgb")

    await _reload_entry(
        hass,
        entry,
        prime=lambda: _prime_restore_state(
            hass,
            entity_id,
            _light_state(entity_id, is_on=True, brightness=120, rgb_color=[10, 20, 30]),
        ),
    )

    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] == 120
    assert state.attributes[ATTR_RGB_COLOR] == (10, 20, 30)


async def test_rgbw_entity_restores_rgbw_color_from_last_state(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unified RGBW entity restores rgbw_color (and rgb mirror) on reload."""
    entry, _client = await _setup(hass, monkeypatch, WRGB_MODEL)
    registry = er.async_get(hass)
    entity_id = _entity_id(registry, "rgbw")

    await _reload_entry(
        hass,
        entry,
        prime=lambda: _prime_restore_state(
            hass,
            entity_id,
            _light_state(
                entity_id,
                is_on=True,
                brightness=180,
                rgb_color=[15, 25, 35],
                rgbw_color=[15, 25, 35, 45],
            ),
        ),
    )

    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] == 180
    assert state.attributes[ATTR_RGBW_COLOR] == (15, 25, 35, 45)


async def test_white_entity_restores_brightness_and_on_state_from_last_state(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-channel white entity restores brightness and on/off on reload."""
    entry, _client = await _setup(hass, monkeypatch, WHITE_MODEL)
    registry = er.async_get(hass)
    entity_id = _entity_id(registry, "white")

    await _reload_entry(
        hass,
        entry,
        prime=lambda: _prime_restore_state(
            hass,
            entity_id,
            _light_state(entity_id, is_on=True, brightness=77),
        ),
    )

    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] == 77


async def test_rgb_entity_restore_off_state_when_last_state_off(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restoring from an OFF last state keeps the entity off."""
    entry, _client = await _setup(hass, monkeypatch, DeviceModel("Test RGB", (), LedSpec(RGB_CHANNELS)))
    registry = er.async_get(hass)
    entity_id = _entity_id(registry, "rgb")

    await _reload_entry(
        hass,
        entry,
        prime=lambda: _prime_restore_state(
            hass,
            entity_id,
            _light_state(entity_id, is_on=False, brightness=50, rgb_color=[100, 100, 100]),
        ),
    )

    state = hass.states.get(entity_id)
    assert state.state == STATE_OFF


async def test_rgb_entity_restore_without_color_keeps_default_color(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restoring only brightness (no color attributes) keeps the default color."""
    entry, _client = await _setup(hass, monkeypatch, DeviceModel("Test RGB", (), LedSpec(RGB_CHANNELS)))
    registry = er.async_get(hass)
    entity_id = _entity_id(registry, "rgb")

    await _reload_entry(
        hass,
        entry,
        prime=lambda: _prime_restore_state(
            hass,
            entity_id,
            _light_state(entity_id, is_on=True, brightness=200),
        ),
    )

    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] == 200
    # No rgb_color in the restore source, so the constructor default is kept.
    assert state.attributes[ATTR_RGB_COLOR] == (255, 255, 255)

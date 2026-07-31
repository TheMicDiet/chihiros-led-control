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
    from homeassistant.const import ATTR_ENTITY_ID, CONF_ADDRESS, SERVICE_TURN_OFF, SERVICE_TURN_ON, STATE_OFF, STATE_ON
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
    DeviceModel,
)
from custom_components.chihiros.vendor.chihiros_led_control.protocol import ParsedNotification

pytestmark = [
    pytest.mark.integration,
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
        self.set_brightness_exception: Exception | None = None

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

    async def set_brightness(self, brightness: Any) -> None:
        if self.set_brightness_exception is not None:
            raise self.set_brightness_exception
        self.brightness_calls.append(brightness)

    async def query_status(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass


WRGB_MODEL = DeviceModel("WRGB II Pro", ("DYWPRO30",), WRGB_CHANNELS)
WHITE_MODEL = DeviceModel("A II", ("DYNA2",), WHITE_CHANNELS)


async def _setup(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    model: DeviceModel,
    *,
    always_available: bool = True,
) -> tuple[ConfigEntry, _TrackingClient]:
    """Set up the integration with a specific device model."""
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


# --- entity creation tests ---


async def test_rgb_entity_created_for_rgb_device(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An RGB device gets a unified RGB light entity."""
    _entry, _client = await _setup(hass, monkeypatch, DeviceModel("Test RGB", (), RGB_CHANNELS))
    registry = er.async_get(hass)

    entity_id = registry.async_get_entity_id(LIGHT_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_rgb")
    assert entity_id is not None


async def test_rgbw_entity_created_for_wrgb_device(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A WRGB device gets a unified RGBW light entity."""
    _entry, _client = await _setup(hass, monkeypatch, WRGB_MODEL)
    registry = er.async_get(hass)

    entity_id = registry.async_get_entity_id(LIGHT_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_rgbw")
    assert entity_id is not None


async def test_no_rgb_entity_for_white_only_device(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A white-only device does not get an RGB entity."""
    _entry, _client = await _setup(hass, monkeypatch, WHITE_MODEL)
    registry = er.async_get(hass)

    rgb_id = registry.async_get_entity_id(LIGHT_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_rgb")
    rgbw_id = registry.async_get_entity_id(LIGHT_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_rgbw")
    assert rgb_id is None
    assert rgbw_id is None


# --- turn_on / turn_off tests ---


async def test_rgb_turn_on_with_rgb_color(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning on with rgb_color sends all channels in one call."""
    _entry, client = await _setup(hass, monkeypatch, DeviceModel("Test RGB", (), RGB_CHANNELS))
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


async def test_rgb_turn_on_with_brightness_scales_all_channels(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Brightness scales each channel proportionally."""
    _entry, client = await _setup(hass, monkeypatch, DeviceModel("Test RGB", (), RGB_CHANNELS))
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(LIGHT_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_rgb")
    assert entity_id is not None

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id, ATTR_RGB_COLOR: [255, 128, 0], ATTR_BRIGHTNESS: 128},
        blocking=True,
    )
    await _flush()

    call = client.brightness_calls[-1]
    # scale_channel(255, 128) = ceil(255*128/255/255*100) = ceil(128/255*100) = 51
    assert call["red"] == 51
    # scale_channel(128, 128) = ceil(128*128/255/255*100) = 26
    assert call["green"] == 26
    assert call["blue"] == 0


async def test_rgb_turn_on_without_color_defaults_to_white(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning on without a color sends full-brightness white."""
    _entry, client = await _setup(hass, monkeypatch, DeviceModel("Test RGB", (), RGB_CHANNELS))
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(LIGHT_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_rgb")
    assert entity_id is not None

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    await _flush()

    call = client.brightness_calls[-1]
    assert call == {"red": 100, "green": 100, "blue": 100}


async def test_rgb_turn_off_zeros_all_channels(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning off sends zero for all channels."""
    _entry, client = await _setup(hass, monkeypatch, DeviceModel("Test RGB", (), RGB_CHANNELS))
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(LIGHT_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_rgb")
    assert entity_id is not None

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    await _flush()

    call = client.brightness_calls[-1]
    assert call == {"red": 0, "green": 0, "blue": 0}


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


async def test_rgbw_turn_on_without_color_defaults_to_full_rgbw(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning on without a color sends full-brightness RGBW."""
    _entry, client = await _setup(hass, monkeypatch, WRGB_MODEL)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(LIGHT_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_rgbw")
    assert entity_id is not None

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    await _flush()

    call = client.brightness_calls[-1]
    assert call == {"red": 100, "green": 100, "blue": 100, "white": 100}


async def test_rgbw_turn_off_zeros_all_four_channels(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning off a WRGB entity zeros all four channels."""
    _entry, client = await _setup(hass, monkeypatch, WRGB_MODEL)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(LIGHT_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_rgbw")
    assert entity_id is not None

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    await _flush()

    call = client.brightness_calls[-1]
    assert call == {"red": 0, "green": 0, "blue": 0, "white": 0}


async def test_rgbw_brightness_only_scales_all_channels(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing brightness without color scales all four channels."""
    _entry, client = await _setup(hass, monkeypatch, WRGB_MODEL)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(LIGHT_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_rgbw")
    assert entity_id is not None

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id, ATTR_BRIGHTNESS: 128},
        blocking=True,
    )
    await _flush()

    call = client.brightness_calls[-1]
    # default color is (255,255,255,255), all channels should scale equally
    expected = 51  # ceil(128/255*100)
    assert call["red"] == expected
    assert call["green"] == expected
    assert call["blue"] == expected
    assert call["white"] == expected


# --- brightness-only / scale edge cases ---


async def test_rgb_brightness_only_scales_default_white(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Brightness without color scales the default (255,255,255) RGB color."""
    _entry, client = await _setup(hass, monkeypatch, DeviceModel("Test RGB", (), RGB_CHANNELS))
    registry = er.async_get(hass)
    entity_id = _entity_id(registry, "rgb")

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id, ATTR_BRIGHTNESS: 128},
        blocking=True,
    )
    await _flush()

    call = client.brightness_calls[-1]
    assert call == {"red": 51, "green": 51, "blue": 51}
    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] == 128
    assert state.attributes[ATTR_RGB_COLOR] == (255, 255, 255)


async def test_rgb_scale_channel_zero_value(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 0 channel value stays 0 through the brightness scaling."""
    _entry, client = await _setup(hass, monkeypatch, DeviceModel("Test RGB", (), RGB_CHANNELS))
    registry = er.async_get(hass)
    entity_id = _entity_id(registry, "rgb")

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id, ATTR_RGB_COLOR: [0, 128, 255]},
        blocking=True,
    )
    await _flush()

    call = client.brightness_calls[-1]
    assert call == {"red": 0, "green": 51, "blue": 100}
    state = hass.states.get(entity_id)
    assert state.attributes[ATTR_RGB_COLOR] == (0, 128, 255)


async def test_rgb_toggle_off_on_restores_last_brightness(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning on without brightness after a turn-off restores the last level."""
    _entry, client = await _setup(hass, monkeypatch, DeviceModel("Test RGB", (), RGB_CHANNELS))
    registry = er.async_get(hass)
    entity_id = _entity_id(registry, "rgb")

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id, ATTR_RGB_COLOR: [255, 0, 0], ATTR_BRIGHTNESS: 128},
        blocking=True,
    )
    await _flush()
    await hass.services.async_call(LIGHT_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: entity_id}, blocking=True)
    await _flush()

    await hass.services.async_call(LIGHT_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: entity_id}, blocking=True)
    await _flush()

    # scale_channel(255, 128) = ceil(128/255*100) = 51
    assert client.brightness_calls[-1] == {"red": 51, "green": 0, "blue": 0}
    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] == 128
    assert state.attributes[ATTR_RGB_COLOR] == (255, 0, 0)


async def test_rgbw_toggle_off_on_restores_rgbw_color_and_brightness(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning on without color after a turn-off restores rgbw color and level."""
    _entry, client = await _setup(hass, monkeypatch, WRGB_MODEL)
    registry = er.async_get(hass)
    entity_id = _entity_id(registry, "rgbw")

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id, ATTR_RGBW_COLOR: [255, 0, 0, 0], ATTR_BRIGHTNESS: 128},
        blocking=True,
    )
    await _flush()
    await hass.services.async_call(LIGHT_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: entity_id}, blocking=True)
    await _flush()

    await hass.services.async_call(LIGHT_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: entity_id}, blocking=True)
    await _flush()

    assert client.brightness_calls[-1] == {"red": 51, "green": 0, "blue": 0, "white": 0}
    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] == 128
    assert state.attributes[ATTR_RGBW_COLOR] == (255, 0, 0, 0)


# --- error paths ---


async def test_rgb_turn_on_set_brightness_failure_keeps_state_consistent(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed color change raises and keeps the previously applied state."""
    _entry, client = await _setup(hass, monkeypatch, DeviceModel("Test RGB", (), RGB_CHANNELS))
    registry = er.async_get(hass)
    entity_id = _entity_id(registry, "rgb")

    # Establish a known on state first.
    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id, ATTR_RGB_COLOR: [255, 0, 0], ATTR_BRIGHTNESS: 128},
        blocking=True,
    )
    await _flush()

    client.set_brightness_exception = RuntimeError("ble write failed")

    with pytest.raises(HomeAssistantError, match="Failed to set brightness"):
        await hass.services.async_call(
            LIGHT_DOMAIN,
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: entity_id, ATTR_RGB_COLOR: [0, 255, 0]},
            blocking=True,
        )
    await _flush()

    # The entity must still report the previously applied color and brightness
    # instead of the color that was never written to the device.
    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] == 128
    assert state.attributes[ATTR_RGB_COLOR] == (255, 0, 0)


async def test_rgb_turn_off_set_brightness_failure_keeps_state_on(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A BLE failure during turn_off raises HomeAssistantError and keeps the entity on."""
    _entry, client = await _setup(hass, monkeypatch, DeviceModel("Test RGB", (), RGB_CHANNELS))
    registry = er.async_get(hass)
    entity_id = _entity_id(registry, "rgb")

    # Turn on successfully first so we have a known state.
    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id, ATTR_RGB_COLOR: [255, 0, 0]},
        blocking=True,
    )
    await _flush()

    client.set_brightness_exception = RuntimeError("ble write failed")

    with pytest.raises(HomeAssistantError, match="Failed to turn off"):
        await hass.services.async_call(
            LIGHT_DOMAIN,
            SERVICE_TURN_OFF,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
        )
    await _flush()

    # is_on and the reported brightness/color must be unchanged after the failed write
    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] == 255
    assert state.attributes[ATTR_RGB_COLOR] == (255, 0, 0)


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


async def test_white_turn_on_with_brightness_scales_to_device_level(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-channel white entity maps 0-255 brightness to 1-100 device level."""
    _entry, client = await _setup(hass, monkeypatch, WHITE_MODEL)
    registry = er.async_get(hass)
    entity_id = _entity_id(registry, "white")

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id, ATTR_BRIGHTNESS: 128},
        blocking=True,
    )
    await _flush()

    assert client.brightness_calls[-1] == {"white": 51}
    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] == 128


async def test_white_turn_on_without_brightness_defaults_to_full(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning on without brightness sends 100 to the device and reports 255."""
    _entry, client = await _setup(hass, monkeypatch, WHITE_MODEL)
    registry = er.async_get(hass)
    entity_id = _entity_id(registry, "white")

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    await _flush()

    assert client.brightness_calls[-1] == {"white": 100}
    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] == 255


async def test_white_turn_off_zeros_channel(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning off a white entity sends 0 to the device channel."""
    _entry, client = await _setup(hass, monkeypatch, WHITE_MODEL)
    registry = er.async_get(hass)
    entity_id = _entity_id(registry, "white")

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id, ATTR_BRIGHTNESS: 200},
        blocking=True,
    )
    await _flush()

    await hass.services.async_call(LIGHT_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: entity_id}, blocking=True)
    await _flush()

    assert client.brightness_calls[-1] == {"white": 0}
    state = hass.states.get(entity_id)
    assert state.state == STATE_OFF


async def test_white_turn_on_set_brightness_failure_keeps_state_consistent(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed brightness change raises and keeps the previously applied level."""
    _entry, client = await _setup(hass, monkeypatch, WHITE_MODEL)
    registry = er.async_get(hass)
    entity_id = _entity_id(registry, "white")

    # Establish a known on state first.
    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id, ATTR_BRIGHTNESS: 128},
        blocking=True,
    )
    await _flush()

    client.set_brightness_exception = RuntimeError("ble write failed")

    with pytest.raises(HomeAssistantError, match="Failed to set brightness"):
        await hass.services.async_call(
            LIGHT_DOMAIN,
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: entity_id, ATTR_BRIGHTNESS: 200},
            blocking=True,
        )
    await _flush()

    # Brightness must remain at the previously applied level.
    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] == 128


async def test_white_toggle_off_on_restores_last_brightness(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning on without brightness after a turn-off restores the last level."""
    _entry, client = await _setup(hass, monkeypatch, WHITE_MODEL)
    registry = er.async_get(hass)
    entity_id = _entity_id(registry, "white")

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id, ATTR_BRIGHTNESS: 200},
        blocking=True,
    )
    await _flush()
    await hass.services.async_call(LIGHT_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: entity_id}, blocking=True)
    await _flush()

    await hass.services.async_call(LIGHT_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: entity_id}, blocking=True)
    await _flush()

    # ceil(200/255*100) = 79
    assert client.brightness_calls[-1] == {"white": 79}
    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] == 200


# --- restore-state tests ---


async def test_rgb_entity_restores_color_and_brightness_from_last_state(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unified RGB entity restores rgb_color and brightness on reload."""
    entry, _client = await _setup(hass, monkeypatch, DeviceModel("Test RGB", (), RGB_CHANNELS))
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
    entry, _client = await _setup(hass, monkeypatch, DeviceModel("Test RGB", (), RGB_CHANNELS))
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
    entry, _client = await _setup(hass, monkeypatch, DeviceModel("Test RGB", (), RGB_CHANNELS))
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


# --- availability (not always available) ---


async def test_white_entity_available_when_not_always_available(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-channel entity delegates to coordinator availability when not always available."""
    _entry, _client = await _setup(hass, monkeypatch, WHITE_MODEL, always_available=False)
    registry = er.async_get(hass)
    entity_id = _entity_id(registry, "white")

    await _flush()

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state != "unavailable"


async def test_rgb_entity_available_when_not_always_available(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unified RGB entity delegates to coordinator availability when not always available."""
    _entry, _client = await _setup(hass, monkeypatch, DeviceModel("Test RGB", (), RGB_CHANNELS), always_available=False)
    registry = er.async_get(hass)
    entity_id = _entity_id(registry, "rgb")

    await _flush()

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state != "unavailable"

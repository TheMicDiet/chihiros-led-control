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
    from homeassistant.const import ATTR_ENTITY_ID, CONF_ADDRESS, SERVICE_TURN_OFF, SERVICE_TURN_ON, STATE_ON
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import entity_registry as er
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

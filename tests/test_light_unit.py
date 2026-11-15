"""Isolated entity commands and arithmetic, without HA setup or Bluetooth."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest

pytest.importorskip("homeassistant", reason="Home Assistant test group is not installed")

from homeassistant.exceptions import HomeAssistantError

from custom_components.chihiros.light import ChihirosLightEntity, ChihirosRGBLightEntity
from custom_components.chihiros.vendor.chihiros_led_control.models import (
    RGB_CHANNELS,
    WHITE_CHANNELS,
    WRGB_CHANNELS,
    DeviceModel,
    LedSpec,
)

pytestmark = [pytest.mark.unit, pytest.mark.homeassistant, pytest.mark.asyncio]

RGB_MODEL = DeviceModel("Test RGB", (), LedSpec(RGB_CHANNELS))
RGBW_MODEL = DeviceModel("WRGB II Pro", ("DYWPRO30",), LedSpec(WRGB_CHANNELS))
WHITE_MODEL = DeviceModel("A II", ("DYNA2",), LedSpec(WHITE_CHANNELS))
EXTENDED_RGB_MODEL = DeviceModel(
    "RGB VIVID II", ("DYRGBV",), LedSpec(RGB_CHANNELS, channel_maxima={0: 115, 1: 130, 2: 200})
)


class _Client:
    """Record the device-write boundary without a driver or transport."""

    name = "Test Chihiros"

    def __init__(self, model: DeviceModel) -> None:
        self.model = model
        self.model_name = model.name
        self.colors = dict(model.color_channels)
        self.brightness_calls: list[dict[str, int]] = []
        self.set_brightness_exception: Exception | None = None

    async def set_brightness(self, brightness: dict[str, int]) -> None:
        if self.set_brightness_exception is not None:
            raise self.set_brightness_exception
        self.brightness_calls.append(brightness)


class _Coordinator:
    """Expose only the entity's address and auto-mode update boundary."""

    address = "FA:CE:C0:00:10:02"

    def __init__(self) -> None:
        self.auto_mode_calls: list[bool] = []

    def async_set_auto_mode(self, enabled: bool) -> None:
        self.auto_mode_calls.append(enabled)


def _entity(
    monkeypatch: pytest.MonkeyPatch, model: DeviceModel
) -> tuple[ChihirosLightEntity | ChihirosRGBLightEntity, _Client, _Coordinator, Mock]:
    """Construct the real entity, replacing only external collaborators."""
    client = _Client(model)
    coordinator = _Coordinator()
    if "red" in client.colors:
        entity = ChihirosRGBLightEntity(coordinator, client)
    else:
        entity = ChihirosLightEntity(coordinator, client, "white")
    state_write = Mock()
    monkeypatch.setattr(entity, "schedule_update_ha_state", state_write)
    return entity, client, coordinator, state_write


@pytest.mark.parametrize(
    ("model", "kwargs", "expected", "brightness", "color"),
    [
        (
            RGB_MODEL,
            {"rgb_color": (255, 128, 0), "brightness": 128},
            {"red": 51, "green": 26, "blue": 0},
            128,
            (255, 128, 0),
        ),
        (RGB_MODEL, {}, {"red": 100, "green": 100, "blue": 100}, 255, (255, 255, 255)),
        (RGB_MODEL, {"brightness": 128}, {"red": 51, "green": 51, "blue": 51}, 128, (255, 255, 255)),
        (RGB_MODEL, {"rgb_color": (0, 128, 255)}, {"red": 0, "green": 51, "blue": 100}, 255, (0, 128, 255)),
        (RGBW_MODEL, {}, {"red": 100, "green": 100, "blue": 100, "white": 100}, 255, (255, 255, 255, 255)),
        (RGBW_MODEL, {"brightness": 128}, {"red": 51, "green": 51, "blue": 51, "white": 51}, 128, (255, 255, 255, 255)),
        (WHITE_MODEL, {"brightness": 128}, {"white": 51}, 128, None),
        (WHITE_MODEL, {}, {"white": 100}, 255, None),
    ],
    ids=[
        "rgb-scaled-color",
        "rgb-default",
        "rgb-brightness",
        "rgb-zero-channel",
        "rgbw-default",
        "rgbw-brightness",
        "white-brightness",
        "white-default",
    ],
)
async def test_turn_on_scales_channels_and_persists_applied_state(
    monkeypatch: pytest.MonkeyPatch,
    model: DeviceModel,
    kwargs: dict[str, Any],
    expected: dict[str, int],
    brightness: int,
    color: tuple[int, ...] | None,
) -> None:
    """Channel conversion commits the applied color, brightness and manual mode."""
    entity, client, coordinator, state_write = _entity(monkeypatch, model)

    await entity.async_turn_on(**kwargs)

    assert client.brightness_calls == [expected]
    assert entity.is_on is True
    assert entity.brightness == brightness
    if isinstance(entity, ChihirosRGBLightEntity):
        assert entity.rgb_color == color[:3]
        assert entity.rgbw_color == (color if "white" in client.colors else None)
    assert coordinator.auto_mode_calls == [False]
    state_write.assert_called_once_with()


@pytest.mark.parametrize("model", [RGB_MODEL, RGBW_MODEL], ids=["rgb", "rgbw"])
async def test_turn_off_zeros_all_channels(monkeypatch: pytest.MonkeyPatch, model: DeviceModel) -> None:
    """An off command still clears hardware channels before local state is known."""
    entity, client, coordinator, state_write = _entity(monkeypatch, model)

    await entity.async_turn_off()

    assert client.brightness_calls[-1] == dict.fromkeys(client.colors, 0)
    assert entity.is_on is False
    assert coordinator.auto_mode_calls == [False]
    state_write.assert_called_once_with()


@pytest.mark.parametrize(
    ("model", "kwargs", "expected"),
    [
        (RGB_MODEL, {"rgb_color": (255, 0, 0), "brightness": 128}, {"red": 51, "green": 0, "blue": 0}),
        (RGBW_MODEL, {"rgbw_color": (255, 0, 0, 0), "brightness": 128}, {"red": 51, "green": 0, "blue": 0, "white": 0}),
        (WHITE_MODEL, {"brightness": 200}, {"white": 79}),
    ],
    ids=["rgb", "rgbw", "white"],
)
async def test_toggle_preserves_last_color_and_brightness(
    monkeypatch: pytest.MonkeyPatch, model: DeviceModel, kwargs: dict[str, Any], expected: dict[str, int]
) -> None:
    """An off/on cycle reapplies the last successful color and brightness."""
    entity, client, coordinator, state_write = _entity(monkeypatch, model)

    await entity.async_turn_on(**kwargs)
    await entity.async_turn_off()
    assert entity.is_on is False
    assert coordinator.auto_mode_calls == [False, False]
    assert state_write.call_count == 2
    await entity.async_turn_on()

    assert client.brightness_calls == [expected, dict.fromkeys(client.colors, 0), expected]
    assert entity.is_on is True
    assert entity.brightness == kwargs["brightness"]
    if isinstance(entity, ChihirosRGBLightEntity):
        assert entity.rgb_color == (255, 0, 0)
        if model == RGBW_MODEL:
            assert entity.rgbw_color == (255, 0, 0, 0)
    assert coordinator.auto_mode_calls == [False, False, False]
    assert state_write.call_count == 3


@pytest.mark.parametrize(
    ("model", "turn_off", "initial", "change", "message"),
    [
        (
            RGB_MODEL,
            False,
            {"rgb_color": (255, 0, 0), "brightness": 128},
            {"rgb_color": (0, 255, 0)},
            "Failed to set brightness",
        ),
        (RGB_MODEL, True, {"rgb_color": (255, 0, 0)}, {}, "Failed to turn off"),
        (WHITE_MODEL, False, {"brightness": 128}, {"brightness": 200}, "Failed to set brightness"),
    ],
    ids=["rgb-on", "rgb-off", "white-on"],
)
async def test_failed_write_preserves_applied_state(
    monkeypatch: pytest.MonkeyPatch,
    model: DeviceModel,
    turn_off: bool,
    initial: dict[str, Any],
    change: dict[str, Any],
    message: str,
) -> None:
    """Rejected device writes preserve applied state without publishing a change."""
    entity, client, coordinator, state_write = _entity(monkeypatch, model)
    await entity.async_turn_on(**initial)
    applied_calls = list(client.brightness_calls)
    client.set_brightness_exception = RuntimeError("ble write failed")
    state_write.reset_mock()
    coordinator.auto_mode_calls.clear()

    with pytest.raises(HomeAssistantError, match=message):
        if turn_off:
            await entity.async_turn_off()
        else:
            await entity.async_turn_on(**change)

    assert entity.is_on is True
    assert entity.brightness == initial.get("brightness", 255)
    if isinstance(entity, ChihirosRGBLightEntity):
        assert entity.rgb_color == (255, 0, 0)
    assert client.brightness_calls == applied_calls
    assert coordinator.auto_mode_calls == []
    state_write.assert_not_called()


@pytest.mark.parametrize(
    ("brightness", "color", "expected"),
    [
        (255, (255, 255, 255), {"red": 115, "green": 130, "blue": 200}),
        (128, (255, 255, 255), {"red": 58, "green": 66, "blue": 101}),
        (1, (255, 255, 255), {"red": 1, "green": 1, "blue": 1}),
        (128, (255, 128, 0), {"red": 58, "green": 33, "blue": 0}),
    ],
    ids=["full", "half", "minimum", "scaled-color"],
)
async def test_extended_model_uses_each_channel_maximum(
    monkeypatch: pytest.MonkeyPatch, brightness: int, color: tuple[int, int, int], expected: dict[str, int]
) -> None:
    """Extended models scale each channel using its own raw maximum."""
    entity, client, _coordinator, _state_write = _entity(monkeypatch, EXTENDED_RGB_MODEL)

    await entity.async_turn_on(rgb_color=color, brightness=brightness)

    assert client.brightness_calls == [expected]
    assert entity.brightness == brightness


async def test_explicit_single_channel_maximum(monkeypatch: pytest.MonkeyPatch) -> None:
    """Single-channel lights honor explicit maxima for on and off commands."""
    model = DeviceModel("Explicit maximum", (), LedSpec(WHITE_CHANNELS, channel_maxima={0: 125}))
    entity, client, _coordinator, _state_write = _entity(monkeypatch, model)

    await entity.async_turn_on(brightness=128)
    assert client.brightness_calls[-1] == {"white": 63}
    await entity.async_turn_off()
    assert client.brightness_calls[-1] == {"white": 0}

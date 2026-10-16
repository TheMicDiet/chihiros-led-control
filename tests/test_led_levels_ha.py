"""Home Assistant percentage inputs translated through actual LED drivers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant", reason="Home Assistant test group is not installed")

from homeassistant.exceptions import HomeAssistantError

from chihiros_led_control import testing as testing_module
from chihiros_led_control.testing import ScriptedBLEDevice, ScriptedTransport
from custom_components.chihiros import schedule_services as services
from custom_components.chihiros.vendor.chihiros_led_control.devices import ChihirosDevice
from custom_components.chihiros.vendor.chihiros_led_control.devices import base as device_base
from custom_components.chihiros.vendor.chihiros_led_control.registry import detect_model

pytestmark = pytest.mark.unit


def _device_data(
    monkeypatch: pytest.MonkeyPatch, name: str = "DYRGBV-test"
) -> tuple[SimpleNamespace, ScriptedTransport]:
    monkeypatch.setattr(device_base, "COMMAND_NOTIFICATION_WAIT", 0.0)
    monkeypatch.setattr(device_base, "STATUS_NOTIFICATION_WAIT", 0.0)
    monkeypatch.setattr(testing_module, "BATCH_WRITE_DELAY", 0.0)
    transport = ScriptedTransport(name=name)
    device = ChihirosDevice(
        ScriptedBLEDevice(transport.name, transport.address), detect_model(transport.name), transport=transport
    )
    return SimpleNamespace(device=device), transport


@pytest.mark.asyncio
async def test_schedule_service_percentages_encode_extended_model_units(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing levels fields are percentages, not unscaled raw channel values."""
    data, transport = _device_data(monkeypatch)
    period = {
        services.ATTR_START: "08:00",
        services.ATTR_END: "18:00",
        services.ATTR_LEVELS: {"red": 100, "green": 50, "blue": 0},
        services.ATTR_RAMP_UP_MINUTES: 0,
    }
    services.validate_schedule_brightness(data, period)
    await services.async_add_schedule_period(data, period)
    scenes = [frame for frame in transport.writes if frame[0] == 0xA5 and frame[5] == 25]
    assert len(scenes) == 1
    assert scenes[0][12:15] == bytes([115, 65, 0])
    assert services.model_brightness_from_service_data(data, {services.ATTR_BRIGHTNESS: 100}) == 115
    with pytest.raises(HomeAssistantError, match="percentages must be between 0 and 100"):
        services.validate_schedule_brightness(data, {services.ATTR_LEVELS: {"blue": 101}})


@pytest.mark.asyncio
async def test_curve_service_percentages_reach_production_point_encoder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Firmware >=20 keeps custom points, with percentage-to-raw channel conversion."""
    data, transport = _device_data(monkeypatch)
    transport.expect(90, 4, [1], respond=[bytes.fromhex("5b 1b 08 00 01 0a 00 01")])

    async def refresh() -> None:
        pass

    auto_mode = []
    data.coordinator = SimpleNamespace(async_request_status=refresh, async_set_auto_mode=auto_mode.append)
    monkeypatch.setattr(services, "resolve_service_device", lambda *_args: data)
    call = SimpleNamespace(
        hass=None,
        data={services.ATTR_CURVE: {0: [[480, 100]], 1: [[510, 50]], 2: [[540, 0], [570, 1], [600, 100]]}},
    )
    await services._async_set_auto_curve_service(call)
    points = [frame[6:9] for frame in transport.writes if frame[5] == 6]
    assert points == [
        bytes([0, 16, 115]),
        bytes([1, 17, 65]),
        bytes([2, 18, 0]),
        bytes([2, 19, 2]),
        bytes([2, 20, 200]),
    ]
    assert not any(frame[0] == 0xA5 and frame[5] == 25 for frame in transport.writes)
    assert auto_mode == [True]
    with pytest.raises(HomeAssistantError, match="level must be between 0 and 100"):
        services.validate_auto_curve(data, {2: [[480, 101]]})


@pytest.mark.parametrize(
    ("name", "firmware", "weekdays", "message"),
    [
        ("DYRGBV-test", 19, None, "firmware 20"),
        ("DYNA2-test", 27, ["monday"], "weekday selection"),
    ],
)
async def test_rejected_scene_replacement_preserves_unknown_device_schedule(
    monkeypatch: pytest.MonkeyPatch, name: str, firmware: int, weekdays: list[str] | None, message: str
) -> None:
    """Model/firmware validation must not trigger a destructive cleanup reset."""
    data, transport = _device_data(monkeypatch, name)
    transport.expect(90, 4, [1], respond=[bytes([0x5B, firmware, 8, 0, 1, 10, 0, 1])])
    await data.device.query_status()
    before = list(transport.writes)
    period = {
        services.ATTR_START: "08:00",
        services.ATTR_END: "18:00",
        services.ATTR_BRIGHTNESS: 50,
        services.ATTR_RAMP_UP_MINUTES: 0,
        services.ATTR_WEEKDAYS: weekdays,
    }
    with pytest.raises(HomeAssistantError, match=message):
        await services.async_replace_schedule(data, [period])
    assert transport.writes == before
    await data.device.disconnect()

"""Tests for Chihiros sensor formatting and notification-sensor error handling."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

try:
    from homeassistant.components.bluetooth import update_coordinator as bluetooth_update
    from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
    from homeassistant.config_entries import ConfigEntry, ConfigEntryState
    from homeassistant.const import CONF_ADDRESS
    from homeassistant.core import HomeAssistant
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    import custom_components.chihiros as chihiros_integration
    from custom_components.chihiros.const import DOMAIN
    from custom_components.chihiros.coordinator import ChihirosDataUpdateCoordinator
    from custom_components.chihiros.runtime import ChihirosRuntime
    from custom_components.chihiros.sensor import (
        _format_schedule_point,
        _format_schedule_state,
    )
except ImportError as err:
    pytest.skip(
        f"Home Assistant test group is not installed or is incompatible: {err}",
        allow_module_level=True,
    )

from custom_components.chihiros.vendor.chihiros_led_control.models import RGB_CHANNELS, DeviceModel
from custom_components.chihiros.vendor.chihiros_led_control.protocol import ParsedNotification

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("enable_custom_integrations", "mock_bluetooth"),
]

_ASYNC_TESTS = pytest.mark.asyncio

MAX_SENSOR_STATE_LENGTH = 255


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
    assert len(_format_schedule_state(many_points[:2])) <= MAX_SENSOR_STATE_LENGTH


# --- integration: notification sensor async_update error path ---


class _FailingQueryClient:
    """Minimal client whose query_status always fails."""

    def __init__(self) -> None:
        self.model = DeviceModel("Test RGB", ("TEST-RGB",), RGB_CHANNELS)
        self._callbacks: set[Callable[[ParsedNotification], None]] = set()

    @property
    def address(self) -> str:
        return "FA:CE:C0:00:40:01"

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

    async def query_status(self) -> None:
        raise RuntimeError("device offline")

    async def disconnect(self) -> None:
        pass


@_ASYNC_TESTS
async def test_notification_sensor_async_update_raises_homeassistant_error(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing status request surfaces as HomeAssistantError from async_update."""
    client = _FailingQueryClient()
    address = client.address

    async def resolve_runtime(_hass: HomeAssistant, _entry: ConfigEntry) -> ChihirosRuntime:
        return ChihirosRuntime(client=client, address=address, always_available=True)

    monkeypatch.setattr(chihiros_integration, "resolve_chihiros_runtime", resolve_runtime)
    monkeypatch.setattr(bluetooth_update, "async_address_present", lambda *_a, **_k: True)
    monkeypatch.setattr(ChihirosDataUpdateCoordinator, "async_start_bluetooth", lambda _self: None)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title=client.name,
        unique_id=address,
        data={CONF_ADDRESS: address},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert entry.state is ConfigEntryState.LOADED

    registry = er.async_get(hass)
    sensor_id = registry.async_get_entity_id(SENSOR_DOMAIN, DOMAIN, f"{address}_last_notification")
    assert sensor_id is not None
    entity = hass.data[SENSOR_DOMAIN].get_entity(sensor_id)
    assert entity is not None

    with pytest.raises(HomeAssistantError, match="Failed to request status"):
        await entity.async_update()

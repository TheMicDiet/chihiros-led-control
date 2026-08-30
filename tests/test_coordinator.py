"""Integration tests for ChihirosDataUpdateCoordinator behavior."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

try:
    from homeassistant.components.bluetooth import update_coordinator as bluetooth_update
    from homeassistant.config_entries import ConfigEntry, ConfigEntryState
    from homeassistant.const import CONF_ADDRESS
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    import custom_components.chihiros as chihiros_integration
    from custom_components.chihiros.const import DOMAIN
    from custom_components.chihiros.coordinator import (
        ATTR_DOSING_DAILY_UL,
        ATTR_DOSING_LIFETIME_UL,
        ATTR_FAN_RPM,
        ATTR_FAN_TEMPERATURE_CELSIUS,
        ATTR_FIRMWARE_VERSION,
        ATTR_LAST_NOTIFICATION,
        ATTR_RUNTIME_MINUTES,
        ATTR_SCHEDULE_POINTS,
        ChihirosDataUpdateCoordinator,
    )
    from custom_components.chihiros.runtime import ChihirosRuntime

    # Capture the real unbound method before any per-test monkeypatching so the
    # idempotency test can restore it after the setup harness patches it out.
    _REAL_ASYNC_START_BLUETOOTH = ChihirosDataUpdateCoordinator.async_start_bluetooth
except ImportError as err:
    pytest.skip(
        f"Home Assistant test group is not installed or is incompatible: {err}",
        allow_module_level=True,
    )

from custom_components.chihiros.vendor.chihiros_led_control.models import RGB_CHANNELS, DeviceModel
from custom_components.chihiros.vendor.chihiros_led_control.protocol import (
    DosingDailyNotification,
    DosingTotalsNotification,
    ParsedNotification,
    RuntimeNotification,
    SchedulePoint,
    ScheduleSnapshotNotification,
    Vivid3FanStatusNotification,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.usefixtures("enable_custom_integrations", "mock_bluetooth"),
]

TEST_ADDRESS = "FA:CE:C0:00:30:01"


class _TrackingClient:
    """Minimal mock Chihiros client for coordinator tests."""

    def __init__(self) -> None:
        self.model = DeviceModel("Test RGB", ("TEST-RGB",), RGB_CHANNELS)
        self.query_status_calls = 0
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

    async def query_status(self) -> None:
        self.query_status_calls += 1

    async def disconnect(self) -> None:
        pass


async def _setup(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ConfigEntry, _TrackingClient, ChihirosDataUpdateCoordinator]:
    """Set up the integration and return the tracking coordinator."""
    client = _TrackingClient()

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

    coordinator: ChihirosDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id].coordinator
    return entry, client, coordinator


async def _flush() -> None:
    """Yield to pending callbacks."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_async_request_status_calls_client_query_status(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """async_request_status forwards to the client query_status."""
    _entry, client, coordinator = await _setup(hass, monkeypatch)

    before = client.query_status_calls
    await coordinator.async_request_status()
    assert client.query_status_calls == before + 1


async def test_async_set_auto_mode_noop_when_unchanged_and_updates_when_changed(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting the same mode is a no-op; changing it notifies listeners."""
    _entry, _client, coordinator = await _setup(hass, monkeypatch)
    updates = 0

    def _listener() -> None:
        nonlocal updates
        updates += 1

    coordinator.async_add_listener(_listener)

    # Initial mode is False; setting False again is a no-op (no listener update).
    coordinator.async_set_auto_mode(False)
    assert updates == 0
    assert coordinator.auto_mode is False

    # Setting True flips the mode and notifies listeners.
    coordinator.async_set_auto_mode(True)
    assert coordinator.auto_mode is True
    assert updates == 1


async def test_async_start_bluetooth_is_idempotent(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """async_start_bluetooth only registers the bluetooth callback once."""
    _entry, _client, coordinator = await _setup(hass, monkeypatch)

    # Restore the real async_start_bluetooth (the setup harness patches it to a
    # no-op) so the idempotency guard can be exercised.
    monkeypatch.setattr(ChihirosDataUpdateCoordinator, "async_start_bluetooth", _REAL_ASYNC_START_BLUETOOTH)
    starts: list[bool] = []

    def _fake_start() -> Callable[[], None]:
        starts.append(True)
        return lambda: None

    monkeypatch.setattr(coordinator, "async_start", _fake_start)
    coordinator._remove_bluetooth_callback = None

    coordinator.async_start_bluetooth()
    coordinator.async_start_bluetooth()
    assert starts == [True]


async def test_async_close_unregisters_callbacks_and_drops_notifications(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing the coordinator removes callbacks and ignores later notifications."""
    _entry, _client, coordinator = await _setup(hass, monkeypatch)

    captured: list[Any] = []
    coordinator.async_add_listener(lambda: captured.append("update"))

    # Idempotent start so a real bluetooth callback is registered, then close.
    monkeypatch.setattr(coordinator, "async_start", lambda: None)
    coordinator.async_start_bluetooth()

    coordinator.async_close()
    # A second close must not raises (handles the None bluetooth callback).
    coordinator.async_close()

    # Notifications queued after close are dropped (no listener update).
    coordinator._async_handle_notification(RuntimeNotification(23, 511, bytes.fromhex("5b 17 0a 00 01 0a 01 ff")))
    assert captured == []


async def test_handle_runtime_notification_populates_data(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runtime notification stores firmware/runtime diagnostic data."""
    _entry, _client, coordinator = await _setup(hass, monkeypatch)

    coordinator._async_handle_notification(RuntimeNotification(23, 511, bytes.fromhex("5b 17 0a 00 01 0a 01 ff")))
    await _flush()

    assert coordinator.data[ATTR_FIRMWARE_VERSION] == 23
    assert coordinator.data[ATTR_RUNTIME_MINUTES] == 511


async def test_handle_schedule_snapshot_notification_populates_data(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A schedule snapshot notification stores schedule points."""
    _entry, _client, coordinator = await _setup(hass, monkeypatch)

    coordinator._async_handle_notification(
        ScheduleSnapshotNotification(
            23,
            (SchedulePoint(8, 5, {"red": 10}),),
            bytes.fromhex("5b 17 0a 00 01 0a 01 ff"),
        )
    )
    await _flush()

    assert coordinator.data[ATTR_FIRMWARE_VERSION] == 23
    points = coordinator.data[ATTR_SCHEDULE_POINTS]
    assert points == ({"time": "08:05", "levels": {"red": 10}},)


async def test_handle_newer_notifications_populates_data_without_firmware(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Newer pump and VIVID3 notifications do not require a firmware field."""
    _entry, _client, coordinator = await _setup(hass, monkeypatch)
    updates = 0

    def _listener() -> None:
        nonlocal updates
        updates += 1

    coordinator.async_add_listener(_listener)
    coordinator._async_handle_notification(
        DosingTotalsNotification((105500, 0), bytes.fromhex("b6 10 10 00 01 3c 04 1f 00 00"))
    )
    coordinator._async_handle_notification(
        DosingDailyNotification((10000, 40000), bytes.fromhex("b6 10 0e 00 01 44 00 64 01 90"))
    )
    coordinator._async_handle_notification(
        Vivid3FanStatusNotification(600, 25, bytes.fromhex("b6 00 00 00 01 16 02 58 19"))
    )
    await _flush()

    assert coordinator.data[ATTR_DOSING_LIFETIME_UL] == (105500, 0)
    assert coordinator.data[ATTR_DOSING_DAILY_UL] == (10000, 40000)
    assert coordinator.data[ATTR_FAN_RPM] == 600
    assert coordinator.data[ATTR_FAN_TEMPERATURE_CELSIUS] == 25
    assert coordinator.data[ATTR_LAST_NOTIFICATION]["firmware_version"] is None
    assert updates == 3

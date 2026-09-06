"""Home Assistant tests for magnetic-stirrer support (entities and service)."""

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
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    import custom_components.chihiros as chihiros_integration
    from custom_components.chihiros import (
        ATTR_ADDRESS,
        ATTR_CHANNEL,
        ATTR_STIR_POINTS,
        SERVICE_SET_STIR_SCHEDULE,
        _validate_stir_points,
    )
    from custom_components.chihiros.const import DOMAIN
    from custom_components.chihiros.fake import create_fake_device, is_fake_address
    from custom_components.chihiros.runtime import ChihirosRuntime
    from custom_components.chihiros.stirrer import STIRRER_CHANNEL_COUNT, is_stirrer_capable

except ImportError as err:
    pytest.skip(
        f"Home Assistant test group is not installed or is incompatible: {err}",
        allow_module_level=True,
    )

from custom_components.chihiros.vendor.chihiros_led_control.models import MAG_STIRRER

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.usefixtures("enable_custom_integrations", "mock_bluetooth"),
]

TEST_ADDRESS = "FA:CE:C0:00:40:01"


class _TrackingStirrer:
    """Minimal mock stirrer client for integration tests."""

    def __init__(self) -> None:
        self.model = MAG_STIRRER
        self.stir_calls: list[tuple[int, bool]] = []
        self.pre_second_calls: list[tuple[int, int, int]] = []
        self.schedule_calls: list[dict[str, Any]] = []
        self._callbacks: set[Callable[[object], None]] = set()

    @property
    def address(self) -> str:
        return TEST_ADDRESS

    @property
    def name(self) -> str:
        return "DYMIXR-test"

    @property
    def model_name(self) -> str:
        return self.model.name

    @property
    def colors(self) -> dict[str, int]:
        return {}

    def add_notification_callback(self, callback: Callable[[object], None]) -> Callable[[], None]:
        self._callbacks.add(callback)

        def remove() -> None:
            self._callbacks.discard(callback)

        return remove

    async def query_status(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def stir(self, channel: int, on: bool, *, seconds: int | None = None) -> None:
        self.stir_calls.append((channel, on))

    async def set_pre_second(self, channel: int, seconds: int, speed: int = 40) -> None:
        self.pre_second_calls.append((channel, seconds, speed))

    async def set_stir_schedule(self, channel: int, points: Any, **kwargs: Any) -> None:
        self.schedule_calls.append({"channel": channel, "points": points, **kwargs})


async def _setup_stirrer(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ConfigEntry, _TrackingStirrer]:
    """Set up the integration against a mock stirrer client."""
    client = _TrackingStirrer()

    async def resolve_runtime(_hass: HomeAssistant, _entry: ConfigEntry) -> ChihirosRuntime:
        return ChihirosRuntime(client=client, address=TEST_ADDRESS, always_available=True)

    monkeypatch.setattr(chihiros_integration, "resolve_chihiros_runtime", resolve_runtime)
    monkeypatch.setattr(bluetooth_update, "async_address_present", lambda *_a, **_k: True)
    from custom_components.chihiros.coordinator import ChihirosDataUpdateCoordinator

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


def _entity_id(hass: HomeAssistant, domain: str, unique_id: str) -> str | None:
    """Resolve an entity id from the entity registry by unique id."""
    registry = er.async_get(hass)
    return registry.async_get_entity_id(domain, DOMAIN, f"{TEST_ADDRESS}_{unique_id}")


async def test_stirrer_setup_creates_switches_and_numbers(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stirrer entry gets one switch and two numbers per channel."""
    _entry, _client = await _setup_stirrer(hass, monkeypatch)
    await hass.async_block_till_done()

    switch_ids = [_entity_id(hass, "switch", f"stir_channel_{n}") for n in range(1, STIRRER_CHANNEL_COUNT + 1)]
    speed_ids = [_entity_id(hass, "number", f"stir_channel_{n}_speed") for n in range(1, STIRRER_CHANNEL_COUNT + 1)]
    prerun_ids = [_entity_id(hass, "number", f"stir_channel_{n}_pre_run") for n in range(1, STIRRER_CHANNEL_COUNT + 1)]
    assert all(switch_ids) and all(speed_ids) and all(prerun_ids)

    # The stirrer must not expose light/dosing entities.
    assert _entity_id(hass, "switch", "auto_mode") is None
    assert _entity_id(hass, "button", "dosing_pump_1_dose") is None

    # No dose_ml service for a stirrer-only setup.
    assert not hass.services.has_service(DOMAIN, "dose_ml")


async def test_stir_switch_drives_client_and_tracks_state(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Turning the switch on/off calls the client and updates the state."""
    _entry, client = await _setup_stirrer(hass, monkeypatch)
    await hass.async_block_till_done()

    entity_id = _entity_id(hass, "switch", "stir_channel_3")
    assert entity_id is not None

    await hass.services.async_call("switch", "turn_on", {"entity_id": entity_id}, blocking=True)
    assert client.stir_calls == [(2, True)]
    assert hass.states.is_state(entity_id, "on")

    await hass.services.async_call("switch", "turn_off", {"entity_id": entity_id}, blocking=True)
    assert client.stir_calls == [(2, True), (2, False)]
    assert hass.states.is_state(entity_id, "off")


async def test_stir_numbers_write_pre_second_frame(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Speed and pre-run numbers share one (0xA5, 42) write with both values."""
    _entry, client = await _setup_stirrer(hass, monkeypatch)
    await hass.async_block_till_done()

    speed_id = _entity_id(hass, "number", "stir_channel_1_speed")
    prerun_id = _entity_id(hass, "number", "stir_channel_1_pre_run")
    assert speed_id is not None and prerun_id is not None
    assert float(hass.states.get(speed_id).state) == 40
    assert float(hass.states.get(prerun_id).state) == 0

    await hass.services.async_call("number", "set_value", {"entity_id": speed_id, "value": 55}, blocking=True)
    assert client.pre_second_calls == [(0, 0, 55)]
    assert float(hass.states.get(speed_id).state) == 55

    await hass.services.async_call("number", "set_value", {"entity_id": prerun_id, "value": 90}, blocking=True)
    # The pre-run write re-sends the current speed from the shared state.
    assert client.pre_second_calls == [(0, 0, 55), (0, 90, 55)]


async def test_set_stir_schedule_service(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """The set_stir_schedule service resolves the device and programs timer points."""
    _entry, client = await _setup_stirrer(hass, monkeypatch)
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_STIR_SCHEDULE,
        {
            ATTR_ADDRESS: TEST_ADDRESS,
            ATTR_CHANNEL: 1,
            ATTR_STIR_POINTS: [
                {"start": "08:00", "minutes": 30},
                {"start": "20:00", "minutes": 15},
            ],
        },
        blocking=True,
    )
    assert len(client.schedule_calls) == 1
    call = client.schedule_calls[0]
    assert call["channel"] == 0
    assert call["frequency"] == 127
    assert call["active"] is True
    points = call["points"]
    assert [(p.start_hour, p.start_minute) for p in points] == [(8, 0), (20, 0)]
    assert [p.volume_ml for p in points] == [pytest.approx(18.0), pytest.approx(9.0)]


async def test_set_stir_schedule_service_validates_points(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Too-close points and invalid times are rejected before touching the device."""
    _entry, client = await _setup_stirrer(hass, monkeypatch)
    await hass.async_block_till_done()

    with pytest.raises(HomeAssistantError, match="2 minutes apart"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_STIR_SCHEDULE,
            {
                ATTR_ADDRESS: TEST_ADDRESS,
                ATTR_CHANNEL: 1,
                ATTR_STIR_POINTS: [
                    {"start": "08:00", "minutes": 30},
                    {"start": "08:01", "minutes": 15},
                ],
            },
            blocking=True,
        )
    assert client.schedule_calls == []

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_STIR_SCHEDULE,
            {
                ATTR_ADDRESS: TEST_ADDRESS,
                ATTR_CHANNEL: 1,
                ATTR_STIR_POINTS: [{"start": "24:99", "minutes": 30}],
            },
            blocking=True,
        )

    # The wraparound gap counts too: 23:59 and 00:00 are only 1 minute apart.
    with pytest.raises(HomeAssistantError, match="2 minutes apart"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_STIR_SCHEDULE,
            {
                ATTR_ADDRESS: TEST_ADDRESS,
                ATTR_CHANNEL: 1,
                ATTR_STIR_POINTS: [
                    {"start": "00:00", "minutes": 30},
                    {"start": "23:59", "minutes": 15},
                ],
            },
            blocking=True,
        )


async def test_set_stir_schedule_service_rejects_non_stirrer(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dosing pump target is rejected with a clear error."""
    from types import SimpleNamespace

    from custom_components.chihiros.stirrer_services import async_register_stirrer_service

    async_register_stirrer_service(hass)
    pump_data = SimpleNamespace(
        device=SimpleNamespace(name="DYDOSE-test", address=TEST_ADDRESS),
        stirrer_states=[],
    )
    hass.data.setdefault(DOMAIN, {})["entry"] = pump_data

    with pytest.raises(HomeAssistantError, match="not a magnetic stirrer"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_STIR_SCHEDULE,
            {
                ATTR_ADDRESS: TEST_ADDRESS,
                ATTR_CHANNEL: 1,
                ATTR_STIR_POINTS: [{"start": "08:00", "minutes": 30}],
            },
            blocking=True,
        )


async def test_validate_stir_points_unit() -> None:
    """Point validation converts times and enforces the gap rule in isolation."""
    points = _validate_stir_points(
        [
            {"start": "20:30", "minutes": 10},
            {"start": "8:05", "minutes": 45},
        ]
    )
    assert [(p.start_hour, p.start_minute, p.volume_ml) for p in points] == [
        (8, 5, pytest.approx(27.0)),
        (20, 30, pytest.approx(6.0)),
    ]

    with pytest.raises(HomeAssistantError, match="2 minutes apart"):
        _validate_stir_points([{"start": "08:00", "minutes": 5}, {"start": "08:01", "minutes": 5}])


async def test_stirrer_capability_and_fake_device() -> None:
    """The stirrer capability check matches only Mag Stirrer models."""
    assert is_stirrer_capable(SimpleNamespaceDevice("Mag Stirrer"))
    assert not is_stirrer_capable(SimpleNamespaceDevice("Dosing Pump"))

    fake_address = "FA:CE:C0:00:00:0F"
    assert is_fake_address(fake_address)
    fake = create_fake_device(fake_address)
    assert fake.model is MAG_STIRRER


class SimpleNamespaceDevice:
    """Tiny device stand-in for capability checks."""

    def __init__(self, model_name: str) -> None:
        """Initialize the stand-in with a model name."""
        self.model_name = model_name
        self.name = model_name

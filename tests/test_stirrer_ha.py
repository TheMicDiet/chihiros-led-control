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
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    import custom_components.chihiros as chihiros_integration
    from custom_components.chihiros.const import DOMAIN
    from custom_components.chihiros.fake import create_fake_device, is_fake_address
    from custom_components.chihiros.runtime import ChihirosRuntime
    from custom_components.chihiros.service_utils import ATTR_ADDRESS
    from custom_components.chihiros.stirrer import STIRRER_CHANNEL_COUNT, is_stirrer_capable
    from custom_components.chihiros.stirrer_services import (
        ATTR_CHANNEL,
        ATTR_STIR_POINTS,
        SERVICE_SET_STIR_SCHEDULE,
        SERVICE_STIR_FOR,
    )
    from custom_components.chihiros.stirrer_services import (
        validate_stir_points as _validate_stir_points,
    )

except ImportError as err:
    pytest.skip(
        f"Home Assistant test group is not installed or is incompatible: {err}",
        allow_module_level=True,
    )

from custom_components.chihiros.vendor.chihiros_led_control.models import DeviceKind, DeviceModel
from custom_components.chihiros.vendor.chihiros_led_control.registry import DOSING_PUMP, MAG_STIRRER

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
        self.restart_calls: list[tuple[int, int, int]] = []
        self.schedule_calls: list[dict[str, Any]] = []
        self._callbacks: set[Callable[[object], None]] = set()

    @property
    def address(self) -> str:
        return TEST_ADDRESS

    @property
    def name(self) -> str:
        return "DYMIXR-test"

    @property
    def device_kind(self) -> DeviceKind:
        return self.model.device_kind

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
        self.stir_calls.append((channel, on, seconds))

    async def set_pre_second(self, channel: int, seconds: int, speed: int = 40, *, restart: bool = False) -> None:
        self.pre_second_calls.append((channel, seconds, speed))
        if restart:
            self.restart_calls.append((channel, seconds, speed))

    async def set_stir_schedule(self, channel: int, points: Any, **kwargs: Any) -> None:
        self.schedule_calls.append({"channel": channel, "points": points, **kwargs})


async def _setup_stirrer(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    *,
    channel_count: int | None = None,
    master_address: str | None = None,
) -> tuple[ConfigEntry, _TrackingStirrer]:
    """Set up the integration against a mock stirrer client."""
    client = _TrackingStirrer()

    async def resolve_runtime(_hass: HomeAssistant, _entry: ConfigEntry) -> ChihirosRuntime:
        return ChihirosRuntime(client=client, address=TEST_ADDRESS, always_available=True)

    monkeypatch.setattr(chihiros_integration, "resolve_chihiros_runtime", resolve_runtime)
    monkeypatch.setattr(bluetooth_update, "async_address_present", lambda *_a, **_k: True)
    from custom_components.chihiros.coordinator import ChihirosDataUpdateCoordinator

    monkeypatch.setattr(ChihirosDataUpdateCoordinator, "async_start_bluetooth", lambda _self: None)

    data: dict[str, Any] = {CONF_ADDRESS: TEST_ADDRESS}
    if channel_count is not None:
        data["stirrer_channel_count"] = channel_count
    if master_address is not None:
        data["master_address"] = master_address
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=client.name,
        unique_id=TEST_ADDRESS,
        data=data,
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

    # Pre-run numbers only matter for slave operation, so they start disabled.
    registry = er.async_get(hass)
    assert all(registry.async_get(entity_id).disabled_by is not None for entity_id in prerun_ids)
    assert all(registry.async_get(entity_id).disabled_by is None for entity_id in switch_ids + speed_ids)

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
    assert client.stir_calls == [(2, True, None)]
    assert hass.states.is_state(entity_id, "on")

    await hass.services.async_call("switch", "turn_off", {"entity_id": entity_id}, blocking=True)
    assert client.stir_calls == [(2, True, None), (2, False, None)]
    assert hass.states.is_state(entity_id, "off")


async def test_stir_for_service(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """The stir_for service runs a channel for a bounded duration."""
    _entry, client = await _setup_stirrer(hass, monkeypatch)
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_STIR_FOR,
        {ATTR_ADDRESS: TEST_ADDRESS, ATTR_CHANNEL: 2, "duration": 300},
        blocking=True,
    )
    assert client.stir_calls == [(1, True, 300)]

    # A HH:MM:SS string is accepted too.
    await hass.services.async_call(
        DOMAIN,
        SERVICE_STIR_FOR,
        {ATTR_ADDRESS: TEST_ADDRESS, ATTR_CHANNEL: 2, "duration": "00:01:30"},
        blocking=True,
    )
    assert client.stir_calls == [(1, True, 300), (1, True, 90)]

    # Durations beyond the device's [minutes, seconds] wire cap are rejected.
    with pytest.raises(HomeAssistantError, match="255 minutes 59 seconds"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_STIR_FOR,
            {ATTR_ADDRESS: TEST_ADDRESS, ATTR_CHANNEL: 2, "duration": 256 * 60},
            blocking=True,
        )
    assert client.stir_calls == [(1, True, 300), (1, True, 90)]


async def test_stirrer_channel_count_config(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stirrer entry configured with 2 channels exposes only 2 channels."""
    entry, client = await _setup_stirrer(hass, monkeypatch, channel_count=2)
    await hass.async_block_till_done()

    assert _entity_id(hass, "switch", "stir_channel_1") is not None
    assert _entity_id(hass, "switch", "stir_channel_3") is None

    with pytest.raises(HomeAssistantError, match="has 2 stir channels configured"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_STIR_FOR,
            {ATTR_ADDRESS: TEST_ADDRESS, ATTR_CHANNEL: 3, "duration": 300},
            blocking=True,
        )
    assert client.stir_calls == []


async def test_stir_numbers_write_pre_second_frame(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Speed and pre-run numbers share one (0xA5, 42) write with both values."""
    entry, client = await _setup_stirrer(hass, monkeypatch)
    await hass.async_block_till_done()

    speed_id = _entity_id(hass, "number", "stir_channel_1_speed")
    prerun_id = _entity_id(hass, "number", "stir_channel_1_pre_run")
    assert speed_id is not None and prerun_id is not None

    # The pre-run number is disabled by default; enable and reload it.
    er.async_get(hass).async_update_entity(prerun_id, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    speed_state = hass.states.get(speed_id)
    assert speed_state is not None
    assert float(speed_state.state) == 40
    assert speed_state.attributes["min"] == 0
    assert speed_state.attributes["max"] == 40
    assert speed_state.attributes.get("unit_of_measurement") is None
    assert float(hass.states.get(prerun_id).state) == 0

    await hass.services.async_call("number", "set_value", {"entity_id": speed_id, "value": 16}, blocking=True)
    assert client.pre_second_calls == [(0, 0, 16)]
    assert client.restart_calls == []
    assert float(hass.states.get(speed_id).state) == 16

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _entity_id(hass, "switch", "stir_channel_1")}, blocking=True
    )
    await hass.services.async_call("number", "set_value", {"entity_id": speed_id, "value": 40}, blocking=True)
    assert client.restart_calls == [(0, 0, 40)]

    await hass.services.async_call("number", "set_value", {"entity_id": prerun_id, "value": 90}, blocking=True)
    # The pre-run write re-sends the current speed without restarting the channel.
    assert client.pre_second_calls == [(0, 0, 16), (0, 0, 40), (0, 90, 40)]
    assert client.restart_calls == [(0, 0, 40)]


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
    assert call["is_first_setting"] is True
    points = call["points"]
    assert [(p.start_hour, p.start_minute) for p in points] == [(8, 0), (20, 0)]
    assert [p.volume_ml for p in points] == [pytest.approx(18.0), pytest.approx(9.0)]

    # A weekday selection is encoded as the vendor's repetition bitmask.
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_STIR_SCHEDULE,
        {
            ATTR_ADDRESS: TEST_ADDRESS,
            ATTR_CHANNEL: 1,
            "weekdays": ["monday", "friday"],
            ATTR_STIR_POINTS: [{"start": "08:00", "minutes": 30}],
        },
        blocking=True,
    )
    assert client.schedule_calls[1]["frequency"] == 68  # monday=64, friday=4

    # The second write on the same day is no longer the channel's first
    # setting (derived from the integration's programming record).
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
    assert client.schedule_calls[2]["is_first_setting"] is False
    # A different channel keeps the derived first-setting flag.
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_STIR_SCHEDULE,
        {
            ATTR_ADDRESS: TEST_ADDRESS,
            ATTR_CHANNEL: 2,
            ATTR_STIR_POINTS: [{"start": "08:00", "minutes": 30}],
        },
        blocking=True,
    )
    assert client.schedule_calls[3]["is_first_setting"] is True


async def test_set_stir_schedule_service_validates_points(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Overlapping work intervals and invalid times are rejected before writing."""
    _entry, client = await _setup_stirrer(hass, monkeypatch)
    await hass.async_block_till_done()

    with pytest.raises(HomeAssistantError, match="overlap"):
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

    # The app uses ordinary wall-clock coordinates; it does not apply a
    # cyclic last-point-to-first-point comparison across midnight.
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_STIR_SCHEDULE,
        {
            ATTR_ADDRESS: TEST_ADDRESS,
            ATTR_CHANNEL: 1,
            ATTR_STIR_POINTS: [
                {"start": "00:00", "minutes": 1},
                {"start": "23:59", "minutes": 1},
            ],
        },
        blocking=True,
    )
    assert len(client.schedule_calls) == 1


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


async def test_stir_services_accept_device_id_target(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Services accept the Home Assistant device selector value (``device_id``)."""
    _entry, client = await _setup_stirrer(hass, monkeypatch)
    await hass.async_block_till_done()
    device = dr.async_get(hass).async_get_device(connections={(dr.CONNECTION_BLUETOOTH, TEST_ADDRESS)})
    assert device is not None

    await hass.services.async_call(
        DOMAIN,
        SERVICE_STIR_FOR,
        {"device_id": device.id, ATTR_CHANNEL: 1, "duration": 30},
        blocking=True,
    )
    assert client.stir_calls == [(0, True, 30)]


async def test_options_flow_changes_stirrer_channel_count(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """The options flow changes the exposed stir channel count and reloads."""
    entry, _client = await _setup_stirrer(hass, monkeypatch, channel_count=2)
    await hass.async_block_till_done()
    assert _entity_id(hass, "switch", "stir_channel_3") is None

    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(result["flow_id"], user_input={"stirrer_channel_count": 4})
    await hass.async_block_till_done()

    assert entry.options["stirrer_channel_count"] == 4
    assert _entity_id(hass, "switch", "stir_channel_3") is not None


async def test_linked_stirrer_enables_pre_run_entities(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linking a master pump enables the pre-run numbers (slave mode only)."""
    _entry, _client = await _setup_stirrer(hass, monkeypatch, master_address="FA:CE:C0:00:00:04")
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    pre_run_ids = [_entity_id(hass, "number", f"stir_channel_{n}_pre_run") for n in range(1, STIRRER_CHANNEL_COUNT + 1)]
    assert all(entity_id is not None for entity_id in pre_run_ids)
    assert all(registry.async_get(entity_id).disabled_by is None for entity_id in pre_run_ids)


async def test_unlinking_master_disables_pre_run_entities(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicitly unlinking the master restores the pre-run numbers' disabled default."""
    entry, _client = await _setup_stirrer(hass, monkeypatch, master_address="FA:CE:C0:00:00:04")
    await hass.async_block_till_done()
    pre_run_id = _entity_id(hass, "number", "stir_channel_1_pre_run")
    assert pre_run_id is not None
    assert er.async_get(hass).async_get(pre_run_id).disabled_by is None

    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"stirrer_channel_count": STIRRER_CHANNEL_COUNT, "master_address": "none"},
    )
    await hass.async_block_till_done()

    assert er.async_get(hass).async_get(pre_run_id).disabled_by is not None


async def test_validate_stir_points_unit() -> None:
    """Point validation converts times and enforces app interval overlaps."""
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

    with pytest.raises(HomeAssistantError, match="overlap"):
        _validate_stir_points([{"start": "08:00", "minutes": 5}, {"start": "08:01", "minutes": 5}])

    with pytest.raises(HomeAssistantError, match="overlap"):
        _validate_stir_points([{"start": "08:00", "minutes": 5}, {"start": "08:05", "minutes": 1}])


async def test_stirrer_capability_and_fake_device() -> None:
    """The stirrer capability check matches only Mag Stirrer models."""
    assert is_stirrer_capable(SimpleNamespaceDevice(MAG_STIRRER))
    assert not is_stirrer_capable(SimpleNamespaceDevice(DOSING_PUMP))

    fake_address = "FA:CE:C0:00:00:0F"
    assert is_fake_address(fake_address)
    fake = create_fake_device(fake_address)
    assert fake.model is MAG_STIRRER


class SimpleNamespaceDevice:
    """Tiny typed device stand-in for capability checks."""

    def __init__(self, model: DeviceModel) -> None:
        """Initialize the stand-in with typed model metadata."""
        self.model = model

    @property
    def device_kind(self) -> DeviceKind:
        """Return the typed family discriminator."""
        return self.model.device_kind

    @property
    def name(self) -> str:
        """Return the typed model name."""
        return self.model.name


@pytest.mark.parametrize("name", ["DYDOSE-abc", "DYMIXR-abc"])
async def test_bluetooth_discovery_reaches_channel_config_step(hass: HomeAssistant, name: str) -> None:
    """Auto-discovered pumps/stirrers reach their config form without asserting."""
    import time
    from types import SimpleNamespace

    from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

    discovery = BluetoothServiceInfoBleak(
        name=name,
        address=f"AA:BB:CC:DD:EE:{0x01 if name.startswith('DYDOSE') else 0x02:02X}",
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
    assert result["step_id"] in ("dosing_config", "stirrer_config")

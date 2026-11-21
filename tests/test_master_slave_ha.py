"""Home Assistant tests for pump programming and stirrer master/slave mirroring."""

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
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    import custom_components.chihiros as chihiros_integration
    from custom_components.chihiros.const import DOMAIN
    from custom_components.chihiros.dosing_services import ATTR_ML, ATTR_PUMP, SERVICE_DOSE_ML
    from custom_components.chihiros.master_slave_services import (
        ATTR_MASTER_ADDRESS,
        ATTR_MODE,
        ATTR_POINTS,
        SERVICE_MIRROR_STIRRER,
        SERVICE_RESET_DOSING_CHANNEL,
        SERVICE_SET_CHANNEL_ACTIVE,
        SERVICE_SET_DOSE_DELAY,
        SERVICE_SET_DOSING_SCHEDULE,
        SERVICE_SET_STIRRER_MASTER,
    )
    from custom_components.chihiros.runtime import ChihirosRuntime
    from custom_components.chihiros.service_utils import ATTR_ADDRESS
    from custom_components.chihiros.stirrer_services import ATTR_CHANNEL
except ImportError as err:
    pytest.skip(
        f"Home Assistant test group is not installed or is incompatible: {err}",
        allow_module_level=True,
    )

from custom_components.chihiros.vendor.chihiros_led_control.models import DeviceKind
from custom_components.chihiros.vendor.chihiros_led_control.protocol.dosing import DosingMode
from custom_components.chihiros.vendor.chihiros_led_control.registry import DOSING_PUMP, MAG_STIRRER

pytestmark = [
    pytest.mark.integration,
    pytest.mark.homeassistant,
    pytest.mark.asyncio,
    pytest.mark.usefixtures("enable_custom_integrations", "mock_bluetooth"),
]

PUMP_ADDRESS = "FA:CE:C0:00:50:01"
STIRRER_ADDRESS = "FA:CE:C0:00:50:02"


class _TrackingPump:
    """Mock dosing pump client recording programming writes."""

    def __init__(self) -> None:
        self.model = DOSING_PUMP
        self.program_calls: list[dict[str, Any]] = []
        self.active_calls: list[tuple[int, bool, bool]] = []
        self.fail_next: Exception | None = None
        self.delay_calls: list[bool] = []
        self.dose_calls: list[tuple[int, float]] = []
        self.reset_calls: list[int] = []
        self.broadcast_frames: list[bytes] = []
        self._callbacks: set[Callable[[object], None]] = set()

    @property
    def address(self) -> str:
        return PUMP_ADDRESS

    @property
    def name(self) -> str:
        return "DYDOSE-test"

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

    async def set_channel_active(self, channel: int, *, active: bool = True, compensate: bool = False) -> None:
        if self.fail_next:
            raise self.fail_next
        self.active_calls.append((channel, active, compensate))

    async def program_channel(
        self,
        channel: int,
        *,
        active: bool,
        compensate: bool = False,
        dose_per_day_ml: float | None = None,
        frequency: int | None = None,
        is_first_setting: bool = True,
        mode: DosingMode | None = None,
        points: Any = (),
    ) -> None:
        if self.fail_next:
            raise self.fail_next
        self.program_calls.append(
            {
                "channel": channel,
                "active": active,
                "compensate": compensate,
                "ml": dose_per_day_ml,
                "frequency": frequency,
                "first_setting": is_first_setting,
                "mode": mode,
                "points": tuple(points),
            }
        )

    async def set_dose_delay(self, enabled: bool) -> None:
        self.delay_calls.append(enabled)

    async def dose_ml(self, pump_idx: int, volume_ml: float) -> bytes:
        self.dose_calls.append((pump_idx, volume_ml))
        return b"5a 01 manual-dose-frame"

    async def reset_channel(self, channel: int) -> bytes:
        self.reset_calls.append(channel)
        return b"5a 01 reset-frame"

    async def send_frame(self, frame: bytes | bytearray) -> None:
        self.broadcast_frames.append(bytes(frame))


class _TrackingStirrer(_TrackingPump):
    """Mock stirrer client (same recording surface, stirrer model)."""

    def __init__(self, *, name: str = "DYMIXR-test", address: str = STIRRER_ADDRESS) -> None:
        super().__init__()
        self.model = MAG_STIRRER
        self._address = address
        self._name = name

    @property
    def address(self) -> str:
        return self._address

    @property
    def name(self) -> str:
        return self._name

    @property
    def model_name(self) -> str:
        return self.model.name

    async def set_stir_schedule(self, channel: int, points: Any, **kwargs: Any) -> None:
        """Record direct stirrer programming using the shared programming surface."""
        await self.program_channel(channel, points=points, **kwargs)


class _ObservedProgrammingLock(asyncio.Lock):
    """Expose contention without depending on wall-clock scheduling delays."""

    def __init__(self) -> None:
        """Initialize the lock and its contention barrier."""
        super().__init__()
        self.contended = asyncio.Event()

    async def acquire(self) -> bool:
        """Signal an overlapping programming operation before blocking."""
        if self.locked():
            self.contended.set()
        return await super().acquire()


async def _setup_pair(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    *,
    pump_count: int | None = None,
    stirrer_channel_count: int | None = None,
) -> tuple[_TrackingPump, _TrackingStirrer]:
    """Set up one dosing pump and one stirrer config entry."""
    pump = _TrackingPump()
    stirrer = _TrackingStirrer()
    clients = {PUMP_ADDRESS: pump, STIRRER_ADDRESS: stirrer}

    async def resolve_runtime(_hass: HomeAssistant, entry: ConfigEntry) -> ChihirosRuntime:
        return ChihirosRuntime(client=clients[entry.unique_id], address=entry.unique_id, always_available=True)

    monkeypatch.setattr(chihiros_integration, "resolve_chihiros_runtime", resolve_runtime)
    monkeypatch.setattr(bluetooth_update, "async_address_present", lambda *_a, **_k: True)
    from custom_components.chihiros.coordinator import ChihirosDataUpdateCoordinator

    monkeypatch.setattr(ChihirosDataUpdateCoordinator, "async_start_bluetooth", lambda _self: None)

    for address, title in ((PUMP_ADDRESS, "DYDOSE-test"), (STIRRER_ADDRESS, "DYMIXR-test")):
        data: dict[str, Any] = {CONF_ADDRESS: address}
        if pump_count is not None and address == PUMP_ADDRESS:
            data["pump_count"] = pump_count
        if stirrer_channel_count is not None and address == STIRRER_ADDRESS:
            data["stirrer_channel_count"] = stirrer_channel_count
        entry = MockConfigEntry(domain=DOMAIN, title=title, unique_id=address, data=data)
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert all(entry.state is ConfigEntryState.LOADED for entry in hass.config_entries.async_entries(DOMAIN))
    return pump, stirrer


async def test_set_dosing_schedule_programs_pump_and_records(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service programs the pump and records the channel setup."""
    pump, stirrer = await _setup_pair(hass, monkeypatch)
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_DOSING_SCHEDULE,
        {
            ATTR_ADDRESS: PUMP_ADDRESS,
            ATTR_CHANNEL: 2,
            ATTR_MODE: "timer",
            ATTR_POINTS: [{"start": "08:00", "ml": 2.5}, {"start": "20:30", "ml": 1.0}],
            "daily_ml": 60.0,
        },
        blocking=True,
    )
    assert len(pump.program_calls) == 1
    call = pump.program_calls[0]
    assert call["channel"] == 1
    assert call["active"] is True
    assert call["compensate"] is False
    assert call["ml"] == 60.0
    assert call["frequency"] == 127
    assert call["first_setting"] is True
    assert call["mode"] is DosingMode.TIMER
    assert [(p.start_hour, p.start_minute, p.volume_ml) for p in call["points"]] == [
        (8, 0, 2.5),
        (20, 30, 1.0),
    ]
    record = _pump_programming(hass).channels[1]
    assert record["mode"] == "timer"
    assert record["dose_per_day_ml"] == 60.0
    # No stirrer linked yet: nothing mirrored.
    assert stirrer.program_calls == []


async def test_set_stirrer_master_mirrors_programming(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linking with mirroring replays the recorded pump programming."""
    pump, stirrer = await _setup_pair(hass, monkeypatch)
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_DOSING_SCHEDULE,
        {
            ATTR_ADDRESS: PUMP_ADDRESS,
            ATTR_CHANNEL: 1,
            ATTR_MODE: "timer",
            ATTR_POINTS: [{"start": "08:00", "ml": 2.5}],
        },
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_STIRRER_MASTER,
        {ATTR_ADDRESS: STIRRER_ADDRESS, ATTR_MASTER_ADDRESS: PUMP_ADDRESS},
        blocking=True,
    )
    assert stirrer.program_calls and stirrer.program_calls[0]["mode"] is DosingMode.TIMER
    assert stirrer.program_calls[0]["channel"] == 0
    # Full replay includes dosingSet even when the pump recorded no daily volume.
    assert stirrer.program_calls[0]["ml"] == 0.0
    assert stirrer.program_calls[0]["first_setting"] is True
    assert stirrer.delay_calls == [False]

    # The link persists on the stirrer's config entry data.
    entries = [e for e in hass.config_entries.async_entries(DOMAIN) if e.unique_id == STIRRER_ADDRESS]
    assert entries[0].data[ATTR_MASTER_ADDRESS] == PUMP_ADDRESS


async def test_set_stirrer_master_replays_recorded_dose_delay(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linking with mirroring replays the pump's recorded dose-delay flag.

    Regression: the service schema used to default ``delay`` to False, so
    linking a stirrer after enabling the pump's dose delay silently turned the
    flag off on the stirrer instead of replaying the recorded value.
    """
    pump, stirrer = await _setup_pair(hass, monkeypatch)
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN, SERVICE_SET_DOSE_DELAY, {ATTR_ADDRESS: PUMP_ADDRESS, "enabled": True}, blocking=True
    )
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_DOSING_SCHEDULE,
        {ATTR_ADDRESS: PUMP_ADDRESS, ATTR_CHANNEL: 1, ATTR_MODE: "timer", ATTR_POINTS: [{"start": "08:00", "ml": 2.0}]},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_STIRRER_MASTER,
        {ATTR_ADDRESS: STIRRER_ADDRESS, ATTR_MASTER_ADDRESS: PUMP_ADDRESS, "mirror": True},
        blocking=True,
    )
    assert pump.delay_calls == [True]
    assert stirrer.delay_calls == [True]


async def test_options_flow_links_and_unlinks_master_pump(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """The stirrer Configure dialog links/unlinks the master and mirrors on link."""
    pump, stirrer = await _setup_pair(hass, monkeypatch)
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_DOSING_SCHEDULE,
        {ATTR_ADDRESS: PUMP_ADDRESS, ATTR_CHANNEL: 1, ATTR_MODE: "timer", ATTR_POINTS: [{"start": "08:00", "ml": 2.0}]},
        blocking=True,
    )
    stirrer_entry = next(e for e in hass.config_entries.async_entries(DOMAIN) if e.unique_id == STIRRER_ADDRESS)

    result = await hass.config_entries.options.async_init(stirrer_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"stirrer_channel_count": 8, ATTR_MASTER_ADDRESS: PUMP_ADDRESS},
    )
    await hass.async_block_till_done()

    assert stirrer_entry.data[ATTR_MASTER_ADDRESS] == PUMP_ADDRESS
    # Selecting the master replayed the pump's recorded channel 1 immediately.
    assert stirrer.program_calls and stirrer.program_calls[0]["channel"] == 0

    # Later pump writes are still mirrored live.
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_DOSING_SCHEDULE,
        {ATTR_ADDRESS: PUMP_ADDRESS, ATTR_CHANNEL: 2, ATTR_MODE: "timer", ATTR_POINTS: [{"start": "09:00", "ml": 1.0}]},
        blocking=True,
    )
    assert stirrer.program_calls[-1]["channel"] == 1

    # Unlink through the same dialog.
    result = await hass.config_entries.options.async_init(stirrer_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"stirrer_channel_count": 8, ATTR_MASTER_ADDRESS: "none"},
    )
    await hass.async_block_till_done()
    assert ATTR_MASTER_ADDRESS not in stirrer_entry.data

    mirrored = len(stirrer.program_calls)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_DOSING_SCHEDULE,
        {ATTR_ADDRESS: PUMP_ADDRESS, ATTR_CHANNEL: 3, ATTR_MODE: "timer", ATTR_POINTS: [{"start": "10:00", "ml": 1.0}]},
        blocking=True,
    )
    assert len(stirrer.program_calls) == mirrored


def _schema_default(schema: object, name: str) -> object:
    """Return the pre-filled default for one field of a flow schema."""
    for marker in schema.schema:  # type: ignore[attr-defined]
        if str(marker) != name:
            continue
        default = getattr(marker, "default", None)
        return default() if callable(default) else default
    raise AssertionError(f"field {name!r} not in schema")


async def test_pump_options_flow_reopen_shows_saved_count(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reopening the pump dialog pre-fills the saved channel count (string select)."""
    pump, _stirrer = await _setup_pair(hass, monkeypatch, pump_count=4)
    await hass.async_block_till_done()
    entry = next(e for e in hass.config_entries.async_entries(DOMAIN) if e.unique_id == PUMP_ADDRESS)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(result["flow_id"], user_input={"pump_count": 2})
    await hass.async_block_till_done()
    assert entry.options["pump_count"] == 2
    assert pump.program_calls == []

    reopened = await hass.config_entries.options.async_init(entry.entry_id)
    assert _schema_default(reopened["data_schema"], "pump_count") == "2"


async def test_pump_write_live_mirrors_to_linked_stirrer(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """After linking, every pump programming write is replayed to the stirrer."""
    pump, stirrer = await _setup_pair(hass, monkeypatch)
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_STIRRER_MASTER,
        {ATTR_ADDRESS: STIRRER_ADDRESS, ATTR_MASTER_ADDRESS: PUMP_ADDRESS, "mirror": False},
        blocking=True,
    )
    assert stirrer.program_calls == []

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_DOSING_SCHEDULE,
        {
            ATTR_ADDRESS: PUMP_ADDRESS,
            ATTR_CHANNEL: 3,
            ATTR_MODE: "single",
            ATTR_POINTS: [{"start": "09:15", "ml": 5.0}],
        },
        blocking=True,
    )
    assert stirrer.program_calls[-1]["channel"] == 2
    assert stirrer.program_calls[-1]["mode"] is DosingMode.SINGLE

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_CHANNEL_ACTIVE,
        {ATTR_ADDRESS: PUMP_ADDRESS, ATTR_CHANNEL: 3, "enable": False, "compensate": True},
        blocking=True,
    )
    assert stirrer.program_calls[-1]["channel"] == 2
    assert stirrer.program_calls[-1]["active"] is False
    assert stirrer.program_calls[-1]["compensate"] is True

    # An active-only write must not wipe the stored schedule: the record keeps
    # the mode/points for later full mirrors.
    pump_data = hass.data[DOMAIN][
        next(e.entry_id for e in hass.config_entries.async_entries(DOMAIN) if e.unique_id == PUMP_ADDRESS)
    ]
    record = pump_data.dosing_programming.channels[2]
    assert record["mode"] == "single"


async def test_unlink_stops_mirroring(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unlinking clears the persisted master and stops live mirroring."""
    pump, stirrer = await _setup_pair(hass, monkeypatch)
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_STIRRER_MASTER,
        {ATTR_ADDRESS: STIRRER_ADDRESS, ATTR_MASTER_ADDRESS: PUMP_ADDRESS, "mirror": False},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_STIRRER_MASTER,
        {ATTR_ADDRESS: STIRRER_ADDRESS},
        blocking=True,
    )
    entries = [e for e in hass.config_entries.async_entries(DOMAIN) if e.unique_id == STIRRER_ADDRESS]
    assert ATTR_MASTER_ADDRESS not in entries[0].data

    calls_before = len(stirrer.program_calls)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_DOSING_SCHEDULE,
        {ATTR_ADDRESS: PUMP_ADDRESS, ATTR_CHANNEL: 1, ATTR_MODE: "auto", ATTR_POINTS: [{"start": "10:00", "ml": 3.0}]},
        blocking=True,
    )
    assert len(stirrer.program_calls) == calls_before


async def test_mirror_stirrer_service_and_missing_programming(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mirror_stirrer replays all channels; without programming it fails cleanly."""
    pump, stirrer = await _setup_pair(hass, monkeypatch)
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_STIRRER_MASTER,
        {ATTR_ADDRESS: STIRRER_ADDRESS, ATTR_MASTER_ADDRESS: PUMP_ADDRESS, "mirror": False},
        blocking=True,
    )
    with pytest.raises(HomeAssistantError, match="No channel programming recorded"):
        await hass.services.async_call(DOMAIN, SERVICE_MIRROR_STIRRER, {ATTR_ADDRESS: STIRRER_ADDRESS}, blocking=True)

    for channel, start in ((1, "08:00"), (2, "12:00")):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_DOSING_SCHEDULE,
            {
                ATTR_ADDRESS: PUMP_ADDRESS,
                ATTR_CHANNEL: channel,
                ATTR_MODE: "timer",
                ATTR_POINTS: [{"start": start, "ml": 2.0}],
            },
            blocking=True,
        )
    # Ignore earlier live writes so only the explicit full replay can satisfy this.
    stirrer.program_calls.clear()
    await hass.services.async_call(
        DOMAIN, SERVICE_MIRROR_STIRRER, {ATTR_ADDRESS: STIRRER_ADDRESS, "delay": True}, blocking=True
    )
    mirrored_channels = {call["channel"] for call in stirrer.program_calls}
    assert mirrored_channels == {0, 1}
    assert stirrer.delay_calls == [True]


async def test_invalid_time_string_becomes_service_error(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad start time surfaces as a service error, not a raw vol.Invalid."""
    pump, _ = await _setup_pair(hass, monkeypatch)
    await hass.async_block_till_done()

    with pytest.raises(HomeAssistantError, match="Invalid start time"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_DOSING_SCHEDULE,
            {
                ATTR_ADDRESS: PUMP_ADDRESS,
                ATTR_CHANNEL: 1,
                ATTR_MODE: "timer",
                ATTR_POINTS: [{"start": "8am", "ml": 1.0}],
            },
            blocking=True,
        )
    assert pump.program_calls == []


async def test_mirror_of_corrupt_record_is_a_service_error(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt stored record raises HomeAssistantError, not KeyError."""
    pump, stirrer = await _setup_pair(hass, monkeypatch)
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_STIRRER_MASTER,
        {ATTR_ADDRESS: STIRRER_ADDRESS, ATTR_MASTER_ADDRESS: PUMP_ADDRESS, "mirror": False},
        blocking=True,
    )
    pump_entry_id = next(e.entry_id for e in hass.config_entries.async_entries(DOMAIN) if e.unique_id == PUMP_ADDRESS)
    hass.data[DOMAIN][pump_entry_id].dosing_programming._channels[0] = {"mode": "not-a-mode"}
    with pytest.raises(HomeAssistantError, match="Corrupt programming record"):
        await hass.services.async_call(DOMAIN, SERVICE_MIRROR_STIRRER, {ATTR_ADDRESS: STIRRER_ADDRESS}, blocking=True)
    del pump


async def test_multi_slave_mirror_collects_failures(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """With two linked stirrers, one failing slave does not block the other."""
    pump, stirrer = await _setup_pair(hass, monkeypatch)

    # Link a second stirrer that fails on every write.
    failing = _TrackingStirrer(name="DYMIXR-failing", address="FA:CE:C0:00:50:03")
    clients_by_address = {
        PUMP_ADDRESS: pump,
        STIRRER_ADDRESS: stirrer,
        "FA:CE:C0:00:50:03": failing,
    }

    async def resolve_runtime(_hass: HomeAssistant, entry: ConfigEntry) -> ChihirosRuntime:
        return ChihirosRuntime(
            client=clients_by_address[entry.unique_id], address=entry.unique_id, always_available=True
        )

    monkeypatch.setattr(chihiros_integration, "resolve_chihiros_runtime", resolve_runtime)

    async def failing_program(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("radio lost")

    failing.program_channel = failing_program  # type: ignore[method-assign]

    from homeassistant.const import CONF_ADDRESS as CONF_ADDR
    from pytest_homeassistant_custom_component.common import MockConfigEntry as _Entry

    entry = _Entry(
        domain=DOMAIN, title="DYMIXR-failing", unique_id="FA:CE:C0:00:50:03", data={CONF_ADDR: "FA:CE:C0:00:50:03"}
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    for address in (STIRRER_ADDRESS, "FA:CE:C0:00:50:03"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_STIRRER_MASTER,
            {ATTR_ADDRESS: address, ATTR_MASTER_ADDRESS: PUMP_ADDRESS, "mirror": False},
            blocking=True,
        )

    with pytest.raises(HomeAssistantError, match="DYMIXR-failing.*radio lost"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_DOSING_SCHEDULE,
            {
                ATTR_ADDRESS: PUMP_ADDRESS,
                ATTR_CHANNEL: 1,
                ATTR_MODE: "timer",
                ATTR_POINTS: [{"start": "08:00", "ml": 2.0}],
            },
            blocking=True,
        )
    # The healthy stirrer was mirrored despite the failing one.
    assert stirrer.program_calls and stirrer.program_calls[0]["channel"] == 0


async def test_set_dose_delay_records_and_mirrors(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """The dose-delay flag is recorded and mirrored to the linked stirrer."""
    pump, stirrer = await _setup_pair(hass, monkeypatch)
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_STIRRER_MASTER,
        {ATTR_ADDRESS: STIRRER_ADDRESS, ATTR_MASTER_ADDRESS: PUMP_ADDRESS, "mirror": False},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN, SERVICE_SET_DOSE_DELAY, {ATTR_ADDRESS: PUMP_ADDRESS, "enabled": True}, blocking=True
    )
    assert pump.delay_calls == [True]
    assert stirrer.delay_calls == [True]

    # A later full mirror replays the *recorded* flag without an override.
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_DOSING_SCHEDULE,
        {ATTR_ADDRESS: PUMP_ADDRESS, ATTR_CHANNEL: 1, ATTR_MODE: "timer", ATTR_POINTS: [{"start": "08:00", "ml": 2.0}]},
        blocking=True,
    )
    await hass.services.async_call(DOMAIN, SERVICE_MIRROR_STIRRER, {ATTR_ADDRESS: STIRRER_ADDRESS}, blocking=True)
    assert stirrer.delay_calls == [True, True]


async def test_manual_dose_broadcasts_verbatim_frame(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """A manual dose on a linked pump replays the exact frame to the stirrer.

    App parity: tempDosing applies the same broadcast ternary as startWork
    (slave linked -> device: null, DOSING_CONTROL.md §5 @ 0xa62d78).
    """
    pump, stirrer = await _setup_pair(hass, monkeypatch)
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_STIRRER_MASTER,
        {ATTR_ADDRESS: STIRRER_ADDRESS, ATTR_MASTER_ADDRESS: PUMP_ADDRESS, "mirror": False},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN,
        SERVICE_DOSE_ML,
        {ATTR_ADDRESS: PUMP_ADDRESS, ATTR_PUMP: 2, ATTR_ML: 3.5},
        blocking=True,
    )
    assert pump.dose_calls == [(1, 3.5)]
    # The stirrer received the identical frame bytes (verbatim broadcast).
    assert stirrer.broadcast_frames == [b"5a 01 manual-dose-frame"]


async def test_reset_dosing_channel_broadcasts_and_clears_record(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reset service drops the record and broadcasts the reset frame."""
    pump, stirrer = await _setup_pair(hass, monkeypatch)
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_STIRRER_MASTER,
        {ATTR_ADDRESS: STIRRER_ADDRESS, ATTR_MASTER_ADDRESS: PUMP_ADDRESS, "mirror": False},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_DOSING_SCHEDULE,
        {ATTR_ADDRESS: PUMP_ADDRESS, ATTR_CHANNEL: 4, ATTR_MODE: "timer", ATTR_POINTS: [{"start": "06:00", "ml": 1.0}]},
        blocking=True,
    )
    pump_entry_id = next(e.entry_id for e in hass.config_entries.async_entries(DOMAIN) if e.unique_id == PUMP_ADDRESS)
    assert 3 in hass.data[DOMAIN][pump_entry_id].dosing_programming.channels

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_DOSING_CHANNEL,
        {ATTR_ADDRESS: PUMP_ADDRESS, ATTR_CHANNEL: 4},
        blocking=True,
    )
    assert pump.reset_calls == [3]
    assert 3 not in hass.data[DOMAIN][pump_entry_id].dosing_programming.channels
    assert stirrer.broadcast_frames == [b"5a 01 reset-frame"]


async def test_set_stirrer_master_accepts_device_id(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """The master pump can be selected with the Home Assistant device selector."""
    _pump, _stirrer = await _setup_pair(hass, monkeypatch)
    await hass.async_block_till_done()
    pump_device = dr.async_get(hass).async_get_device(connections={(dr.CONNECTION_BLUETOOTH, PUMP_ADDRESS)})
    assert pump_device is not None

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_STIRRER_MASTER,
        {ATTR_ADDRESS: STIRRER_ADDRESS, "master_device_id": pump_device.id, "mirror": False},
        blocking=True,
    )
    entry = next(e for e in hass.config_entries.async_entries(DOMAIN) if e.unique_id == STIRRER_ADDRESS)
    assert entry.data[ATTR_MASTER_ADDRESS] == PUMP_ADDRESS


async def test_set_dosing_schedule_rejects_unconfigured_channel(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 2-pump entry rejects programming channel 3 before touching the device."""
    pump, _stirrer = await _setup_pair(hass, monkeypatch, pump_count=2)
    await hass.async_block_till_done()

    with pytest.raises(HomeAssistantError, match="has 2 pump channels configured"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_DOSING_SCHEDULE,
            {
                ATTR_ADDRESS: PUMP_ADDRESS,
                ATTR_CHANNEL: 3,
                ATTR_MODE: "timer",
                ATTR_POINTS: [{"start": "08:00", "ml": 1.0}],
            },
            blocking=True,
        )
    assert pump.program_calls == []


async def test_schedule_rejects_weekdays_and_frequency(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    """``weekdays`` and the raw ``frequency`` bitmask are mutually exclusive."""
    pump, _stirrer = await _setup_pair(hass, monkeypatch)
    await hass.async_block_till_done()
    base = {
        ATTR_ADDRESS: PUMP_ADDRESS,
        ATTR_CHANNEL: 1,
        ATTR_MODE: "timer",
        ATTR_POINTS: [{"start": "08:00", "ml": 1.0}],
    }

    with pytest.raises(Exception, match="exclusion"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_DOSING_SCHEDULE,
            {**base, "weekdays": ["monday"], "frequency": 1},
            blocking=True,
        )

    # Supplying only the raw bitmask still works for advanced/legacy callers.
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_DOSING_SCHEDULE,
        {**base, "daily_ml": 1.0, "frequency": 4},
        blocking=True,
    )
    assert pump.program_calls[-1]["frequency"] == 4


@pytest.mark.parametrize("repeat", [{"weekdays": ["monday"]}, {"weekdays": []}, {"frequency": 127}])
async def test_schedule_repeat_requires_daily_volume(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, repeat: dict[str, Any]
) -> None:
    """An explicit repeat update without daily volume never writes or changes the tracker."""
    pump, stirrer = await _setup_pair(hass, monkeypatch)
    tracker = _pump_programming(hass)
    base = {
        ATTR_ADDRESS: PUMP_ADDRESS,
        ATTR_CHANNEL: 1,
        ATTR_MODE: "timer",
        ATTR_POINTS: [{"start": "08:00", "ml": 1.0}],
    }
    await hass.services.async_call(
        DOMAIN, SERVICE_SET_DOSING_SCHEDULE, {**base, "daily_ml": 1.0, "weekdays": ["tuesday"]}, blocking=True
    )
    original = dict(tracker.channels[0])
    assert original["frequency"] == 32

    with pytest.raises(HomeAssistantError, match="daily_ml"):
        await hass.services.async_call(DOMAIN, SERVICE_SET_DOSING_SCHEDULE, {**base, **repeat}, blocking=True)
    assert len(pump.program_calls) == 1
    assert tracker.channels[0] == original
    assert not stirrer.program_calls

    # A points-only update leaves the previously programmed weekdays and volume alone.
    await hass.services.async_call(DOMAIN, SERVICE_SET_DOSING_SCHEDULE, base, blocking=True)
    assert len(pump.program_calls) == 2
    assert tracker.channels[0]["frequency"] == 32
    assert tracker.channels[0]["dose_per_day_ml"] == 1.0


async def test_mirror_respects_configured_stirrer_channel_count(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Channels outside a stirrer's configured count are not mirrored."""
    pump, stirrer = await _setup_pair(hass, monkeypatch, stirrer_channel_count=2)
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_STIRRER_MASTER,
        {ATTR_ADDRESS: STIRRER_ADDRESS, ATTR_MASTER_ADDRESS: PUMP_ADDRESS, "mirror": False},
        blocking=True,
    )
    for channel in (1, 2, 3):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_DOSING_SCHEDULE,
            {
                ATTR_ADDRESS: PUMP_ADDRESS,
                ATTR_CHANNEL: channel,
                ATTR_MODE: "timer",
                ATTR_POINTS: [{"start": "08:00", "ml": 1.0}],
            },
            blocking=True,
        )
    # Live mirroring already skipped the out-of-range channel 3.
    assert {call["channel"] for call in stirrer.program_calls} == {0, 1}
    assert pump.program_calls[-1]["channel"] == 2  # the pump itself was still programmed

    # A full replay skips it too.
    stirrer.program_calls.clear()
    await hass.services.async_call(DOMAIN, SERVICE_MIRROR_STIRRER, {ATTR_ADDRESS: STIRRER_ADDRESS}, blocking=True)
    assert {call["channel"] for call in stirrer.program_calls} == {0, 1}
    assert stirrer.delay_calls == [False]


def _pump_programming(hass: HomeAssistant) -> Any:
    """Return the real programming tracker for the configured pump."""
    entry = next(entry for entry in hass.config_entries.async_entries(DOMAIN) if entry.unique_id == PUMP_ADDRESS)
    return hass.data[DOMAIN][entry.entry_id].dosing_programming


async def _write_schedule(hass: HomeAssistant, *, daily_ml: float | None = 10.0) -> None:
    """Program a single pump channel through the registered service."""
    payload: dict[str, Any] = {
        ATTR_ADDRESS: PUMP_ADDRESS,
        ATTR_CHANNEL: 1,
        ATTR_MODE: "timer",
        ATTR_POINTS: [{"start": "08:00", "ml": 2.0}],
    }
    if daily_ml is not None:
        payload["daily_ml"] = daily_ml
    await hass.services.async_call(DOMAIN, SERVICE_SET_DOSING_SCHEDULE, payload, blocking=True)


@pytest.mark.parametrize("stirrer", [False, True], ids=["pump", "stirrer"])
async def test_concurrent_first_day_schedule_writes_reset_only_once(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, stirrer: bool
) -> None:
    """Derivation, hardware programming and its record share one channel lock."""
    pump, slave = await _setup_pair(hass, monkeypatch)
    client = slave if stirrer else pump
    entry = next(entry for entry in hass.config_entries.async_entries(DOMAIN) if entry.unique_id == client.address)
    tracker = hass.data[DOMAIN][entry.entry_id].dosing_programming
    lock = _ObservedProgrammingLock()
    monkeypatch.setattr(tracker, "channel_lock", lambda _channel: lock)
    entered, release = asyncio.Event(), asyncio.Event()
    original = client.program_channel

    async def blocked_program(*args: Any, **kwargs: Any) -> None:
        """Hold the hardware boundary after recording the first accepted call."""
        await original(*args, **kwargs)
        entered.set()
        await release.wait()

    monkeypatch.setattr(client, "program_channel", blocked_program)

    async def program() -> None:
        """Exercise either registered programming service."""
        if stirrer:
            await hass.services.async_call(
                DOMAIN,
                "set_stir_schedule",
                {ATTR_ADDRESS: STIRRER_ADDRESS, ATTR_CHANNEL: 1, "points": [{"start": "08:00", "minutes": 5}]},
                blocking=True,
            )
        else:
            await _write_schedule(hass)

    first = asyncio.create_task(program())
    await asyncio.wait_for(entered.wait(), timeout=5)
    second = asyncio.create_task(program())
    try:
        await asyncio.wait_for(lock.contended.wait(), timeout=5)
    finally:
        release.set()
        await asyncio.gather(first, second)
    assert [call["first_setting"] for call in client.program_calls] == [True, False]
    assert tracker.channel_programmed_today(0)


async def test_schedule_without_daily_ml_preserves_first_daily_setting(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schedule-only write cannot consume the first actual daily-setting stamp."""
    pump, _stirrer = await _setup_pair(hass, monkeypatch)
    await _write_schedule(hass, daily_ml=None)
    assert not _pump_programming(hass).channel_programmed_today(0)
    await _write_schedule(hass)
    assert pump.program_calls[-1]["first_setting"] is True
    await _write_schedule(hass)
    assert pump.program_calls[-1]["first_setting"] is False


@pytest.mark.parametrize("operation", ["schedule", "active", "delay", "reset"])
@pytest.mark.parametrize("mirror_fails", [False, True], ids=["storage-only", "storage-and-mirror"])
async def test_storage_failure_still_updates_linked_slave(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, operation: str, mirror_fails: bool
) -> None:
    """Accepted pump writes propagate despite persistence errors; both failures remain visible."""
    pump, stirrer = await _setup_pair(hass, monkeypatch)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_STIRRER_MASTER,
        {ATTR_ADDRESS: STIRRER_ADDRESS, ATTR_MASTER_ADDRESS: PUMP_ADDRESS, "mirror": False},
        blocking=True,
    )
    tracker = _pump_programming(hass)

    async def fail_save(_data: dict[str, Any]) -> None:
        """Reject local persistence, not hardware programming."""
        raise OSError("storage unavailable")

    async def mirror_failure(*_args: Any, **_kwargs: Any) -> None:
        """Report a distinct slave failure after pump success."""
        raise OSError("slave unavailable")

    monkeypatch.setattr(tracker._store, "async_save", fail_save)
    methods = {
        "schedule": "program_channel",
        "active": "program_channel",
        "delay": "set_dose_delay",
        "reset": "send_frame",
    }
    if mirror_fails:
        monkeypatch.setattr(stirrer, methods[operation], mirror_failure)
    services = {
        "active": SERVICE_SET_CHANNEL_ACTIVE,
        "delay": SERVICE_SET_DOSE_DELAY,
        "reset": SERVICE_RESET_DOSING_CHANNEL,
    }
    with pytest.raises(HomeAssistantError, match="Pump .* succeeded.*local storage: storage unavailable") as error:
        if operation == "schedule":
            await _write_schedule(hass)
        else:
            payloads = {
                "delay": {ATTR_ADDRESS: PUMP_ADDRESS},
                "active": {ATTR_ADDRESS: PUMP_ADDRESS, ATTR_CHANNEL: 1},
                "reset": {ATTR_ADDRESS: PUMP_ADDRESS, ATTR_CHANNEL: 1},
            }
            await hass.services.async_call(DOMAIN, services[operation], payloads[operation], blocking=True)
    assert ("slave unavailable" in str(error.value)) is mirror_fails
    pump_writes = {
        "schedule": pump.program_calls,
        "active": pump.active_calls,
        "delay": pump.delay_calls,
        "reset": pump.reset_calls,
    }
    slave_writes = {
        "schedule": stirrer.program_calls,
        "active": stirrer.program_calls,
        "delay": stirrer.delay_calls,
        "reset": stirrer.broadcast_frames,
    }
    assert len(pump_writes[operation]) == 1
    assert len(slave_writes[operation]) == (0 if mirror_fails else 1)
    assert tracker.channel_programmed_today(0) is (operation == "schedule")


async def test_cancelled_programming_releases_channel_without_committing(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ambiguous cancellation releases serialization without claiming hardware success."""
    pump, _stirrer = await _setup_pair(hass, monkeypatch)
    original = pump.program_channel
    entered = asyncio.Event()

    async def cancelled_program(*_args: Any, **_kwargs: Any) -> None:
        """Suspend at a boundary that may already have accepted a partial write."""
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(pump, "program_channel", cancelled_program)
    task = asyncio.create_task(_write_schedule(hass))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    tracker = _pump_programming(hass)
    assert not tracker.channel_lock(0).locked()
    assert tracker.channels == {}
    monkeypatch.setattr(pump, "program_channel", original)
    await _write_schedule(hass)
    assert len(pump.program_calls) == 1
    assert pump.program_calls[0]["first_setting"] is True

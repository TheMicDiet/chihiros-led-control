"""Integration tests for the Chihiros fan platform."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

import pytest

try:
    from homeassistant.components.bluetooth import update_coordinator as bluetooth_update
    from homeassistant.components.fan import (
        ATTR_PERCENTAGE,
        SERVICE_SET_PERCENTAGE,
        SERVICE_TURN_OFF,
    )
    from homeassistant.components.fan import (
        DOMAIN as FAN_DOMAIN,
    )
    from homeassistant.config_entries import ConfigEntry, ConfigEntryState
    from homeassistant.const import ATTR_ENTITY_ID, CONF_ADDRESS, STATE_OFF, STATE_ON
    from homeassistant.core import HomeAssistant, State
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers.restore_state import StoredState
    from homeassistant.helpers.restore_state import async_get as async_get_restore_data
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    import custom_components.chihiros as chihiros_integration
    from custom_components.chihiros.const import DOMAIN
    from custom_components.chihiros.coordinator import ATTR_FAN_RPM, ChihirosDataUpdateCoordinator
    from custom_components.chihiros.runtime import ChihirosRuntime
except ImportError as err:
    pytest.skip(
        f"Home Assistant test group is not installed or is incompatible: {err}",
        allow_module_level=True,
    )

from custom_components.chihiros.vendor.chihiros_led_control.models import (
    RGB_CHANNELS,
    WRGB_CHANNELS,
    DeviceKind,
    DeviceModel,
    LedFeature,
    LedSpec,
)
from custom_components.chihiros.vendor.chihiros_led_control.protocol.led import (
    FanStatusNotification,
)
from custom_components.chihiros.vendor.chihiros_led_control.protocol.notifications import ParsedNotification

pytestmark = [
    pytest.mark.integration,
    pytest.mark.homeassistant,
    pytest.mark.asyncio,
    pytest.mark.usefixtures("enable_custom_integrations", "mock_bluetooth"),
]

TEST_ADDRESS = "FA:CE:C0:00:20:01"
FAN_MODEL = DeviceModel(
    "WRGB VIVID III", ("DYVVD3",), LedSpec(WRGB_CHANNELS, features=frozenset({LedFeature.FAN}), min_fan_speed=25)
)
RGB_MODEL = DeviceModel("Test RGB", ("TEST-RGB",), LedSpec(RGB_CHANNELS))


class _TrackingClient:
    """Minimal mock Chihiros client for fan entity tests."""

    def __init__(self, model: DeviceModel) -> None:
        self.model = model
        self.fan_speed_calls: list[int] = []
        self.fan_auto_calls: int = 0
        self.fan_temp_calls: list[tuple[int, int]] = []
        self._callbacks: set[Callable[[ParsedNotification], None]] = set()
        self.set_fan_speed_exception: Exception | None = None
        self.before_fan_temp: Callable[[], Awaitable[None]] | None = None
        self._temperature_lock = asyncio.Lock()
        self._fan_auto = False
        self._fan_start_temp = 38
        self._fan_stop_temp = 33

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

    @property
    def fan_auto(self) -> bool:
        return self._fan_auto

    @property
    def fan_start_temp(self) -> int:
        return self._fan_start_temp

    @property
    def fan_stop_temp(self) -> int:
        return self._fan_stop_temp

    def add_notification_callback(self, callback: Callable[[ParsedNotification], None]) -> Callable[[], None]:
        self._callbacks.add(callback)

        def remove() -> None:
            self._callbacks.discard(callback)

        return remove

    async def set_fan_speed(self, speed_percent: int) -> None:
        if self.set_fan_speed_exception is not None:
            raise self.set_fan_speed_exception
        self.fan_speed_calls.append(speed_percent)
        self._fan_auto = False

    async def set_fan_auto(self) -> None:
        self.fan_auto_calls += 1
        self._fan_auto = True

    async def set_fan_start_stop_temp(self, start_temp: int, stop_temp: int) -> None:
        async with self._temperature_lock:
            if self.before_fan_temp is not None:
                await self.before_fan_temp()
            self.fan_temp_calls.append((start_temp, stop_temp))
            self._fan_start_temp = start_temp
            self._fan_stop_temp = stop_temp

    async def disconnect(self) -> None:
        pass


def _install_client(monkeypatch: pytest.MonkeyPatch, client: _TrackingClient) -> None:
    """Resolve a chosen client for setup, including fresh-client reloads."""

    async def resolve_runtime(_hass: HomeAssistant, _entry: ConfigEntry) -> ChihirosRuntime:
        """Return the test's selected runtime client."""
        return ChihirosRuntime(client=client, address=TEST_ADDRESS, always_available=True)

    monkeypatch.setattr(chihiros_integration, "resolve_chihiros_runtime", resolve_runtime)


async def _setup(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    model: DeviceModel = FAN_MODEL,
) -> tuple[ConfigEntry, _TrackingClient, ChihirosDataUpdateCoordinator]:
    """Set up the integration with a fan-capable device model."""
    client = _TrackingClient(model)
    _install_client(monkeypatch, client)
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
    """Yield to pending state-write callbacks."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def _entity_id(registry: er.EntityRegistry) -> str:
    """Look up the fan entity id and assert it exists."""
    entity_id = registry.async_get_entity_id(FAN_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_fan")
    assert entity_id is not None
    return entity_id


def _prime_restore_state(hass: HomeAssistant, entity_id: str, state: State) -> None:
    """Inject a stored last-state so the next entity load restores from it."""
    async_get_restore_data(hass).last_states[entity_id] = StoredState(state, None, dt_util.utcnow())


async def _reload_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    prime: Callable[[], None] | None = None,
) -> None:
    """Unload and re-setup a config entry so entities restore from stored state."""
    await hass.config_entries.async_unload(entry.entry_id)
    await _flush()
    if prime is not None:
        prime()
    await hass.config_entries.async_setup(entry.entry_id)
    await _flush()
    assert entry.state is ConfigEntryState.LOADED


def _entity(hass: HomeAssistant, domain: str, suffix: str) -> Any:
    """Return a loaded entity using its existing registry identity."""
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{TEST_ADDRESS}_{suffix}")
    assert entity_id is not None
    entity = hass.data[domain].get_entity(entity_id)
    assert entity is not None
    return entity


class _DeferredRestore:
    """Hold only a named startup coroutine without blocking HA setup."""

    def __init__(self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, method: str) -> None:
        """Intercept the selected deferred task while leaving other HA tasks alone."""
        self.release = asyncio.Event()
        self.created = asyncio.Event()
        self.tasks: list[asyncio.Task[None]] = []
        original = hass.async_create_task

        def create(coro: Coroutine[Any, Any, None], *args: Any, **kwargs: Any) -> asyncio.Task[None]:
            """Create a held restore task, or delegate ordinary HA task creation."""
            if getattr(getattr(coro, "cr_code", None), "co_name", None) != method:
                return original(coro, *args, **kwargs)

            async def deferred() -> None:
                """Wait until the test allows the real startup operation to run."""
                await self.release.wait()
                await coro

            task = asyncio.create_task(deferred())
            task.add_done_callback(lambda _task: coro.close())
            self.tasks.append(task)
            self.created.set()
            return task

        monkeypatch.setattr(hass, "async_create_task", create)

    async def finish(self) -> None:
        """Release deferred work and collect cancellation during lifecycle tests."""
        self.release.set()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await _flush()


async def test_fan_set_percentage_turn_on_and_turn_off_drive_client(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Percentage services and direct on/off calls preserve the last manual speed."""
    _entry, client, _coordinator = await _setup(hass, monkeypatch, FAN_MODEL)
    registry = er.async_get(hass)
    entity_id = _entity_id(registry)
    entity = hass.data[FAN_DOMAIN].get_entity(entity_id)
    assert entity is not None

    await hass.services.async_call(
        FAN_DOMAIN,
        SERVICE_SET_PERCENTAGE,
        {ATTR_ENTITY_ID: entity_id, ATTR_PERCENTAGE: 50},
        blocking=True,
    )
    await _flush()

    assert client.fan_speed_calls == [50]
    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_PERCENTAGE] == 50

    # turn_on without percentage restores the previous percentage
    await entity.async_turn_on()
    await _flush()
    assert client.fan_speed_calls == [50, 50]

    await entity.async_turn_off()
    await _flush()
    assert client.fan_speed_calls == [50, 50, 0]
    state = hass.states.get(entity_id)
    assert state.state == STATE_OFF
    assert state.attributes[ATTR_PERCENTAGE] == 0

    # Turning on without a requested percentage restores the previous manual speed.
    await entity.async_turn_on()
    await _flush()
    assert client.fan_speed_calls == [50, 50, 0, 50]
    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_PERCENTAGE] == 50


async def test_fan_turn_off_service_disables_fan(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The standard fan.turn_off service is available and sends zero speed."""
    _entry, client, _coordinator = await _setup(hass, monkeypatch, FAN_MODEL)
    registry = er.async_get(hass)
    entity_id = _entity_id(registry)

    await hass.services.async_call(
        FAN_DOMAIN,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    await _flush()

    assert client.fan_speed_calls == [0]
    assert hass.states.get(entity_id).state == STATE_OFF


async def test_fan_set_speed_failure_raises_and_keeps_previous_state(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A BLE failure during set_percentage raises HomeAssistantError and keeps state."""
    _entry, client, _coordinator = await _setup(hass, monkeypatch, FAN_MODEL)
    registry = er.async_get(hass)
    entity_id = _entity_id(registry)

    # Establish a known on state at 40% first.
    await hass.services.async_call(
        FAN_DOMAIN,
        SERVICE_SET_PERCENTAGE,
        {ATTR_ENTITY_ID: entity_id, ATTR_PERCENTAGE: 40},
        blocking=True,
    )
    await _flush()

    client.set_fan_speed_exception = RuntimeError("ble write failed")
    with pytest.raises(HomeAssistantError, match="Failed to set fan speed"):
        await hass.services.async_call(
            FAN_DOMAIN,
            SERVICE_SET_PERCENTAGE,
            {ATTR_ENTITY_ID: entity_id, ATTR_PERCENTAGE: 80},
            blocking=True,
        )
    await _flush()

    # The failed write must not have advanced the reported percentage.
    state = hass.states.get(entity_id)
    assert state.attributes[ATTR_PERCENTAGE] == 40


async def test_fan_restores_percentage_from_last_state(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fan entity restores its percentage on reload."""
    entry, _client, _coordinator = await _setup(hass, monkeypatch, FAN_MODEL)
    registry = er.async_get(hass)
    entity_id = _entity_id(registry)

    await _reload_entry(
        hass,
        entry,
        prime=lambda: _prime_restore_state(
            hass,
            entity_id,
            State(entity_id, STATE_ON, {"percentage": 65}),
        ),
    )

    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_PERCENTAGE] == 65


async def test_fan_extra_state_attributes_reflect_fan_status_notification(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fan status notification surfaces fan_rpm as an extra state attribute."""
    _entry, _client, coordinator = await _setup(hass, monkeypatch, FAN_MODEL)
    registry = er.async_get(hass)
    entity_id = _entity_id(registry)

    # No notification yet -> no fan_rpm attribute.
    assert hass.states.get(entity_id).attributes.get(ATTR_FAN_RPM) is None

    notification = FanStatusNotification(
        firmware_version=27,
        fan_rpm=1234,
        temperature_celsius=25,
        raw=bytes.fromhex("5b 1b 10 00 01 0b 04 d2 19 00 01 00 00 00 00 00 48 22"),
    )
    coordinator._async_handle_notification(notification)
    await _flush()

    assert hass.states.get(entity_id).attributes[ATTR_FAN_RPM] == 1234


async def test_fan_auto_preset_drives_client_and_updates_state(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selecting the Auto preset enables temperature-controlled auto mode."""
    from homeassistant.components.fan import ATTR_PRESET_MODE, SERVICE_SET_PRESET_MODE

    _entry, client, _coordinator = await _setup(hass, monkeypatch, FAN_MODEL)
    registry = er.async_get(hass)
    entity_id = _entity_id(registry)

    await hass.services.async_call(
        FAN_DOMAIN,
        SERVICE_SET_PRESET_MODE,
        {ATTR_ENTITY_ID: entity_id, ATTR_PRESET_MODE: "Auto"},
        blocking=True,
    )
    await _flush()

    assert client.fan_auto_calls == 1
    assert client.fan_auto is True
    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_PRESET_MODE] == "Auto"

    await hass.services.async_call(
        FAN_DOMAIN,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    await _flush()
    assert client.fan_speed_calls == [0]
    assert hass.states.get(entity_id).state == STATE_OFF


async def test_fan_percentage_leaves_auto_preset(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting a manual percentage exits temperature-controlled auto mode."""
    from homeassistant.components.fan import ATTR_PRESET_MODE, SERVICE_SET_PRESET_MODE

    _entry, client, _coordinator = await _setup(hass, monkeypatch, FAN_MODEL)
    registry = er.async_get(hass)
    entity_id = _entity_id(registry)

    await hass.services.async_call(
        FAN_DOMAIN,
        SERVICE_SET_PRESET_MODE,
        {ATTR_ENTITY_ID: entity_id, ATTR_PRESET_MODE: "Auto"},
        blocking=True,
    )
    await _flush()

    await hass.services.async_call(
        FAN_DOMAIN,
        SERVICE_SET_PERCENTAGE,
        {ATTR_ENTITY_ID: entity_id, ATTR_PERCENTAGE: 40},
        blocking=True,
    )
    await _flush()

    assert client.fan_auto is False
    state = hass.states.get(entity_id)
    assert state.attributes[ATTR_PRESET_MODE] == "Manual"
    assert state.attributes[ATTR_PERCENTAGE] == 40


async def test_fan_manual_preset_reapplies_speed(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selecting the Manual preset re-sends the last speed to leave auto control."""
    from homeassistant.components.fan import ATTR_PRESET_MODE, SERVICE_SET_PRESET_MODE

    _entry, client, _coordinator = await _setup(hass, monkeypatch, FAN_MODEL)
    registry = er.async_get(hass)
    entity_id = _entity_id(registry)

    await hass.services.async_call(
        FAN_DOMAIN,
        SERVICE_SET_PERCENTAGE,
        {ATTR_ENTITY_ID: entity_id, ATTR_PERCENTAGE: 60},
        blocking=True,
    )
    await _flush()

    await hass.services.async_call(
        FAN_DOMAIN,
        SERVICE_SET_PRESET_MODE,
        {ATTR_ENTITY_ID: entity_id, ATTR_PRESET_MODE: "Auto"},
        blocking=True,
    )
    await _flush()

    await hass.services.async_call(
        FAN_DOMAIN,
        SERVICE_SET_PRESET_MODE,
        {ATTR_ENTITY_ID: entity_id, ATTR_PRESET_MODE: "Manual"},
        blocking=True,
    )
    await _flush()

    assert client.fan_speed_calls == [60, 60]
    state = hass.states.get(entity_id)
    assert state.attributes[ATTR_PRESET_MODE] == "Manual"


async def test_fan_manual_preset_after_auto_restores_last_manual_speed(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leaving Auto for Manual re-applies the last manual speed, not full speed."""
    from homeassistant.components.fan import ATTR_PRESET_MODE, SERVICE_SET_PRESET_MODE

    _entry, client, _coordinator = await _setup(hass, monkeypatch, FAN_MODEL)
    registry = er.async_get(hass)
    entity_id = _entity_id(registry)

    await hass.services.async_call(
        FAN_DOMAIN,
        SERVICE_SET_PERCENTAGE,
        {ATTR_ENTITY_ID: entity_id, ATTR_PERCENTAGE: 60},
        blocking=True,
    )
    await _flush()

    # Fan off (percentage 0) then auto; the stale percentage must not turn the
    # fan on at 100% when the user switches back to manual.
    await hass.services.async_call(
        FAN_DOMAIN,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    await _flush()

    await hass.services.async_call(
        FAN_DOMAIN,
        SERVICE_SET_PRESET_MODE,
        {ATTR_ENTITY_ID: entity_id, ATTR_PRESET_MODE: "Auto"},
        blocking=True,
    )
    await _flush()

    await hass.services.async_call(
        FAN_DOMAIN,
        SERVICE_SET_PRESET_MODE,
        {ATTR_ENTITY_ID: entity_id, ATTR_PRESET_MODE: "Manual"},
        blocking=True,
    )
    await _flush()

    assert client.fan_speed_calls == [60, 0, 60]
    state = hass.states.get(entity_id)
    assert state.attributes[ATTR_PRESET_MODE] == "Manual"
    assert state.attributes[ATTR_PERCENTAGE] == 60


async def test_fan_temp_numbers_restore_pair_on_reload(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restored fan temperatures are re-applied once with the stored pair."""
    from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN

    _entry, client, _coordinator = await _setup(hass, monkeypatch, FAN_MODEL)
    registry = er.async_get(hass)
    start_id = registry.async_get_entity_id(NUMBER_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_fan_start_temp")
    stop_id = registry.async_get_entity_id(NUMBER_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_fan_stop_temp")
    assert start_id is not None
    assert stop_id is not None

    def _prime() -> None:
        _prime_restore_state(hass, start_id, State(start_id, "38.0"))
        _prime_restore_state(hass, stop_id, State(stop_id, "36.0"))

    await _reload_entry(hass, _entry, prime=_prime)

    # A single deferred write re-applies the full stored pair (both halves
    # restored, not the device defaults).
    assert client.fan_temp_calls == [(38, 36)]
    assert hass.states.get(start_id).state == "38.0"
    assert hass.states.get(stop_id).state == "36.0"


async def test_fan_temp_numbers_restore_preserves_hysteresis_on_reload(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invalid restored pair is normalized to the required hysteresis gap."""
    from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN

    _entry, client, _coordinator = await _setup(hass, monkeypatch, FAN_MODEL)
    registry = er.async_get(hass)
    start_id = registry.async_get_entity_id(NUMBER_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_fan_start_temp")
    stop_id = registry.async_get_entity_id(NUMBER_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_fan_stop_temp")
    assert start_id is not None
    assert stop_id is not None

    def _prime() -> None:
        _prime_restore_state(hass, start_id, State(start_id, "30.0"))
        _prime_restore_state(hass, stop_id, State(stop_id, "29.0"))

    await _reload_entry(hass, _entry, prime=_prime)

    assert client.fan_temp_calls == [(31, 29)]
    assert hass.states.get(start_id).state == "31.0"
    assert hass.states.get(stop_id).state == "29.0"


async def test_fan_temp_numbers_restore_start_without_stop_state(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A saved start temperature is restored even when the stop value is absent."""
    from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN

    _entry, client, _coordinator = await _setup(hass, monkeypatch, FAN_MODEL)
    registry = er.async_get(hass)
    start_id = registry.async_get_entity_id(NUMBER_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_fan_start_temp")
    stop_id = registry.async_get_entity_id(NUMBER_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_fan_stop_temp")
    assert start_id is not None
    assert stop_id is not None

    def _prime() -> None:
        _prime_restore_state(hass, start_id, State(start_id, "40.0"))
        async_get_restore_data(hass).last_states.pop(stop_id, None)

    await _reload_entry(hass, _entry, prime=_prime)

    assert client.fan_temp_calls == [(40, 33)]
    assert hass.states.get(start_id).state == "40.0"
    assert hass.states.get(stop_id).state == "33.0"


async def test_fan_temp_numbers_drive_client_and_enforce_hysteresis(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fan start/stop temperature numbers write the device and keep a hysteresis gap."""
    from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
    from homeassistant.components.number import SERVICE_SET_VALUE
    from homeassistant.helpers import entity_registry as er

    _entry, client, _coordinator = await _setup(hass, monkeypatch, FAN_MODEL)
    registry = er.async_get(hass)
    start_id = registry.async_get_entity_id(NUMBER_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_fan_start_temp")
    stop_id = registry.async_get_entity_id(NUMBER_DOMAIN, DOMAIN, f"{TEST_ADDRESS}_fan_stop_temp")
    assert start_id is not None
    assert stop_id is not None

    # Defaults from the vendor app.
    assert hass.states.get(start_id).state == "38.0"
    assert hass.states.get(stop_id).state == "33.0"
    await _flush()
    assert client.fan_temp_calls == []

    # Raising the stop temperature pushes the start temperature along.
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: stop_id, "value": 40},
        blocking=True,
    )
    await _flush()

    assert client.fan_temp_calls == [(42, 40)]
    assert hass.states.get(start_id).state == "42.0"
    assert hass.states.get(stop_id).state == "40.0"

    # Lowering the start temperature pulls the stop temperature down with it.
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: start_id, "value": 30},
        blocking=True,
    )
    await _flush()

    assert client.fan_temp_calls == [(42, 40), (30, 28)]
    assert hass.states.get(start_id).state == "30.0"
    assert hass.states.get(stop_id).state == "28.0"


async def test_concurrent_fan_temperature_changes_preserve_both_requests(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pair serialization must cover deriving the sibling's temperature."""
    _entry, client, _coordinator = await _setup(hass, monkeypatch)
    start = _entity(hass, "number", "fan_start_temp")
    stop = _entity(hass, "number", "fan_stop_temp")
    entered, release, requested = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def hold_write() -> None:
        """Hold the first transport write until both services have started."""
        entered.set()
        await release.wait()

    async def change_stop() -> None:
        """Signal that the sibling command has reached its first await."""
        requested.set()
        await stop.async_set_native_value(36)

    client.before_fan_temp = hold_write
    first = asyncio.create_task(start.async_set_native_value(45))
    await entered.wait()
    second = asyncio.create_task(change_stop())
    await requested.wait()
    release.set()
    await asyncio.gather(first, second)
    await _flush()

    assert client.fan_temp_calls == [(45, 33), (45, 36)]
    assert (start.native_value, stop.native_value) == (45, 36)


@pytest.mark.parametrize(
    ("suffix", "value", "expected"),
    [("fan_start_temp", 45, (45, 33)), ("fan_stop_temp", 36, (38, 36))],
)
async def test_deferred_temperature_restore_respects_live_pair(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    value: int,
    expected: tuple[int, int],
) -> None:
    """A queued restore must not overwrite a live value or reread a stale partner."""
    entry, client, _coordinator = await _setup(hass, monkeypatch)
    start_id = _entity(hass, "number", "fan_start_temp").entity_id
    stop_id = _entity(hass, "number", "fan_stop_temp").entity_id
    deferred = _DeferredRestore(hass, monkeypatch, "_restore_temperatures")

    def prime() -> None:
        """Store a pair different from the client's current values."""
        _prime_restore_state(hass, start_id, State(start_id, "42.0"))
        _prime_restore_state(hass, stop_id, State(stop_id, "35.0"))

    try:
        await _reload_entry(hass, entry, prime)
        await deferred.created.wait()
        await _entity(hass, "number", suffix).async_set_native_value(value)
    finally:
        await deferred.finish()

    assert len(deferred.tasks) == 1
    assert client.fan_temp_calls == [expected]
    assert (
        _entity(hass, "number", "fan_start_temp").native_value,
        _entity(hass, "number", "fan_stop_temp").native_value,
    ) == expected


async def test_enabled_fan_start_restores_with_disabled_stop(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The enabled start control alone restores both stored temperatures."""
    entry, client, _coordinator = await _setup(hass, monkeypatch)
    start_id = _entity(hass, "number", "fan_start_temp").entity_id
    stop_id = _entity(hass, "number", "fan_stop_temp").entity_id

    def prime() -> None:
        """Disable the stop entity while retaining its saved temperature."""
        er.async_get(hass).async_update_entity(stop_id, disabled_by=er.RegistryEntryDisabler.USER)
        _prime_restore_state(hass, start_id, State(start_id, "45.0"))
        _prime_restore_state(hass, stop_id, State(stop_id, "36.0"))

    await _reload_entry(hass, entry, prime)
    assert hass.data["number"].get_entity(stop_id) is None
    assert client.fan_temp_calls == [(45, 36)]
    assert _entity(hass, "number", "fan_start_temp").native_value == 45


class _RestoreWriteGate:
    """Block a restore write and expose cancellation to lifecycle assertions."""

    def __init__(self) -> None:
        """Initialize explicit write-entry, release and cancellation signals."""
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def __call__(self) -> None:
        """Wait for release while recording cancellation of the pending write."""
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


def _prime_fan_temperature_restore(hass: HomeAssistant, start_id: str, stop_id: str, partner_enabled: bool) -> None:
    """Store both thresholds, optionally disabling the stop number."""
    if not partner_enabled:
        er.async_get(hass).async_update_entity(stop_id, disabled_by=er.RegistryEntryDisabler.USER)
    _prime_restore_state(hass, start_id, State(start_id, "45.0"))
    _prime_restore_state(hass, stop_id, State(stop_id, "36.0"))


@pytest.mark.parametrize("partner_enabled", [True, False], ids=["both-enabled", "start-only"])
@pytest.mark.parametrize("inflight", [False, True], ids=["queued", "inflight"])
async def test_fan_temperature_restore_survives_entity_rename(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    partner_enabled: bool,
    inflight: bool,
) -> None:
    """A registry rename preserves the shared snapshot, including a disabled sibling."""
    entry, client, _coordinator = await _setup(hass, monkeypatch)
    start_id = _entity(hass, "number", "fan_start_temp").entity_id
    stop_id = _entity(hass, "number", "fan_stop_temp").entity_id
    deferred = _DeferredRestore(hass, monkeypatch, "_restore_temperatures")
    gate = _RestoreWriteGate()
    readded = asyncio.Event()

    try:
        await _reload_entry(
            hass, entry, lambda: _prime_fan_temperature_restore(hass, start_id, stop_id, partner_enabled)
        )
        await deferred.created.wait()
        start = _entity(hass, "number", "fan_start_temp")
        original_added = start.async_added_to_hass

        async def added() -> None:
            """Observe completion of HA's real same-instance remove/add lifecycle."""
            await original_added()
            readded.set()

        monkeypatch.setattr(start, "async_added_to_hass", added)
        if inflight:
            client.before_fan_temp = gate
            deferred.release.set()
            await gate.entered.wait()

        # A rename must reuse the original snapshot, not consult a changed restore store.
        _prime_restore_state(hass, stop_id, State(stop_id, "30.0"))
        renamed_id = "number.renamed_fan_start_temperature"
        er.async_get(hass).async_update_entity(start_id, new_entity_id=renamed_id)
        await readded.wait()
        await _flush()

        assert entry.state is ConfigEntryState.LOADED
        assert hass.data["number"].get_entity(renamed_id) is start
        assert start.native_value == 45
        if partner_enabled:
            assert not deferred.tasks[0].done()
            assert len(deferred.tasks) == 1
            assert _entity(hass, "number", "fan_stop_temp").native_value == 36
        else:
            assert deferred.tasks[0].cancelled()
            assert len(deferred.tasks) == 2
            assert not deferred.tasks[1].done()
            assert hass.data["number"].get_entity(stop_id) is None
    finally:
        gate.release.set()
        await deferred.finish()

    assert client.fan_temp_calls == [(45, 36)]
    assert (client.fan_start_temp, client.fan_stop_temp) == (45, 36)
    assert hass.states.get(renamed_id).state == "45.0"
    if partner_enabled:
        assert hass.states.get(stop_id).state == "36.0"


@pytest.mark.parametrize("percentage", [0, 60], ids=["off", "manual"])
async def test_deferred_fan_auto_restore_respects_explicit_command(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    percentage: int,
) -> None:
    """Explicit manual and off commands supersede deferred Auto restoration."""
    entry, client, _coordinator = await _setup(hass, monkeypatch)
    entity_id = _entity_id(er.async_get(hass))
    deferred = _DeferredRestore(hass, monkeypatch, "_restore_auto_mode")
    try:
        await _reload_entry(
            hass,
            entry,
            lambda: _prime_restore_state(
                hass, entity_id, State(entity_id, STATE_ON, {"percentage": 40, "preset_mode": "Auto"})
            ),
        )
        await deferred.created.wait()
        fan = _entity(hass, "fan", "fan")
        if percentage:
            await fan.async_set_percentage(percentage)
        else:
            await fan.async_turn_off()
    finally:
        await deferred.finish()

    assert fan.preset_mode == "Manual"
    assert fan.percentage == percentage
    assert client.fan_auto_calls == 0
    assert client.fan_auto is False


async def test_off_state_reload_remembers_manual_fan_speed(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HA restore store preserves the last nonzero speed while the fan is off."""
    entry, client, _coordinator = await _setup(hass, monkeypatch)
    old_fan = _entity(hass, "fan", "fan")
    await old_fan.async_set_percentage(60)
    await old_fan.async_turn_off()
    await _flush()
    state = hass.states.get(old_fan.entity_id)
    assert state.attributes["percentage"] == 0
    assert state.attributes["last_manual_percentage"] == 60

    fresh_client = _TrackingClient(FAN_MODEL)
    _install_client(monkeypatch, fresh_client)
    await _reload_entry(hass, entry)
    fan = _entity(hass, "fan", "fan")
    assert fan is not old_fan
    assert fan.is_on is False
    await fan.async_turn_on()
    assert fresh_client.fan_speed_calls == [60]
    assert fan.percentage == 60


@pytest.mark.parametrize("method", ["_restore_auto_mode", "_restore_temperatures"])
async def test_unload_cancels_deferred_fan_restoration(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    """Queued startup work cannot target an old runtime after unload and reload."""
    entry, client, _coordinator = await _setup(hass, monkeypatch)
    fan_id = _entity_id(er.async_get(hass))
    start_id = _entity(hass, "number", "fan_start_temp").entity_id
    deferred = _DeferredRestore(hass, monkeypatch, method)

    def prime() -> None:
        """Store only the configuration whose startup task is held."""
        if method == "_restore_auto_mode":
            _prime_restore_state(hass, fan_id, State(fan_id, STATE_ON, {"preset_mode": "Auto"}))
        else:
            _prime_restore_state(hass, start_id, State(start_id, "45.0"))

    try:
        await _reload_entry(hass, entry, prime)
        await deferred.created.wait()

        async def disconnect() -> None:
            """Require the platform to cancel queued work before disconnecting."""
            assert all(task.cancelled() for task in deferred.tasks)

        monkeypatch.setattr(client, "disconnect", disconnect)
        old_writes = (client.fan_auto_calls, tuple(client.fan_temp_calls))
        expected_temperatures = (
            (45, 33) if method == "_restore_temperatures" else (client.fan_start_temp, client.fan_stop_temp)
        )
        await hass.config_entries.async_unload(entry.entry_id)
        assert all(task.cancelled() for task in deferred.tasks)
        fresh_client = _TrackingClient(FAN_MODEL)
        _install_client(monkeypatch, fresh_client)
        await hass.config_entries.async_setup(entry.entry_id)
        await _flush()
    finally:
        await deferred.finish()

    assert (client.fan_auto_calls, tuple(client.fan_temp_calls)) == old_writes
    assert fresh_client.fan_auto is (method == "_restore_auto_mode")
    assert (fresh_client.fan_start_temp, fresh_client.fan_stop_temp) == expected_temperatures


@pytest.mark.parametrize("method", ["set_fan_auto", "set_fan_start_stop_temp"])
async def test_unload_cancels_inflight_fan_restoration(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    """Unload cancels an active restore rather than leaving an old-runtime write."""
    entry, client, _coordinator = await _setup(hass, monkeypatch)
    fan_id = _entity_id(er.async_get(hass))
    start_id = _entity(hass, "number", "fan_start_temp").entity_id
    gate = _RestoreWriteGate()
    original = getattr(client, method)

    async def hold_write(*args: int) -> None:
        """Keep the restore in flight until removal cancels its transport await."""
        await gate()
        await original(*args)

    def prime() -> None:
        """Store only the configuration whose transport operation is held."""
        if method == "set_fan_auto":
            _prime_restore_state(hass, fan_id, State(fan_id, STATE_ON, {"preset_mode": "Auto"}))
        else:
            _prime_restore_state(hass, start_id, State(start_id, "45.0"))

    monkeypatch.setattr(client, method, hold_write)
    await _reload_entry(hass, entry, prime)
    await gate.entered.wait()
    old_writes = (client.fan_auto_calls, tuple(client.fan_temp_calls))

    async def disconnect() -> None:
        """Require transport cancellation to finish before disconnecting."""
        assert gate.cancelled.is_set()

    monkeypatch.setattr(client, "disconnect", disconnect)
    try:
        await hass.config_entries.async_unload(entry.entry_id)
        assert gate.cancelled.is_set()
    finally:
        gate.release.set()
        await _flush()
    assert (client.fan_auto_calls, tuple(client.fan_temp_calls)) == old_writes


@pytest.mark.parametrize("cancelled", [False, True], ids=["failure", "cancellation"])
async def test_fan_temperature_write_failure_keeps_pair_and_releases_lock(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    cancelled: bool,
) -> None:
    """An unsuccessful pair write preserves both values and permits a later write."""
    _entry, client, _coordinator = await _setup(hass, monkeypatch)
    start = _entity(hass, "number", "fan_start_temp")
    stop = _entity(hass, "number", "fan_stop_temp")
    await start.async_set_native_value(45)

    async def fail_write() -> None:
        """Abort the device write before it commits either temperature."""
        if cancelled:
            raise asyncio.CancelledError
        raise RuntimeError("Temperature write failed")

    client.before_fan_temp = fail_write
    with pytest.raises(asyncio.CancelledError if cancelled else HomeAssistantError):
        await stop.async_set_native_value(36)
    assert (start.native_value, stop.native_value) == (45, 33)
    assert client.fan_temp_calls == [(45, 33)]

    client.before_fan_temp = None
    await stop.async_set_native_value(37)
    assert (start.native_value, stop.native_value) == (45, 37)
    assert client.fan_temp_calls == [(45, 33), (45, 37)]

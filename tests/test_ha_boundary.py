"""Home Assistant journeys through config flows, services, and published state."""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

pytest.importorskip("homeassistant")

from bleak.exc import BleakError  # noqa: E402
from ha_peripheral import PeripheralProxy  # noqa: E402
from homeassistant.bootstrap import async_load_base_functionality  # noqa: E402
from homeassistant.components.bluetooth import async_register_scanner  # noqa: E402
from homeassistant.components.repairs import repairs_flow_manager  # noqa: E402
from homeassistant.config_entries import SOURCE_USER, ConfigEntries, ConfigEntry, ConfigEntryState  # noqa: E402
from homeassistant.const import CONF_ADDRESS  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.core_config import async_process_ha_core_config  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType, InvalidData  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.helpers import issue_registry as ir  # noqa: E402
from homeassistant.helpers.storage import Store  # noqa: E402
from homeassistant.loader import async_setup as setup_loader  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402

from chihiros_led_control.testing import ScriptedResponder  # noqa: E402

LIGHT = "AA:BB:CC:00:00:02"
PUMP = "AA:BB:CC:00:00:04"
FAN = "AA:BB:CC:00:00:05"
STIRRER = "AA:BB:CC:00:00:0F"
HEATER = "AA:BB:CC:00:00:10"

pytestmark = [pytest.mark.homeassistant, pytest.mark.integration]
DOMAIN = "chihiros"


@pytest.fixture
async def ha(tmp_path: Path) -> AsyncIterator[HomeAssistant]:
    """Run real HA with its loader, services, config entries and disk storage."""
    repo = Path(__file__).resolve().parents[1]
    (tmp_path / "custom_components").symlink_to(repo / "custom_components", target_is_directory=True)
    hass = HomeAssistant(str(tmp_path))
    hass.config.skip_pip = True
    setup_loader(hass)
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        config = {"http": {"server_host": "127.0.0.1", "server_port": port}}
        hass.config_entries = ConfigEntries(hass, config)
        await async_load_base_functionality(hass)
        await async_process_ha_core_config(hass, {})
        assert await async_setup_component(hass, DOMAIN, config)
        await hass.async_start()
        await hass.async_block_till_done()
        yield hass
    finally:
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_stop(force=True)


async def _add_device(hass: HomeAssistant, address: str, **options: int) -> ConfigEntry:
    """Pick a peripheral and complete the user flow, including any channel form."""
    flow = hass.config_entries.flow
    result = await flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] == FlowResultType.FORM
    result = await flow.async_configure(result["flow_id"], {CONF_ADDRESS: address})
    if options:
        assert result["type"] == FlowResultType.FORM
        result = await flow.async_configure(result["flow_id"], options)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry = result["result"]
    assert entry.state is ConfigEntryState.LOADED
    return entry


@pytest.fixture
async def proxy(ha: HomeAssistant) -> AsyncIterator[PeripheralProxy]:
    """Use the same Bluetooth proxy registration API as remote adapters."""
    adapter = PeripheralProxy()
    cancel = async_register_scanner(ha, adapter)
    try:
        yield adapter
    finally:
        cancel()


def _entity(hass: HomeAssistant, entry: ConfigEntry, domain: str, suffix: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{entry.unique_id}_{suffix}")
    assert entity_id is not None, (domain, suffix)
    return entity_id


async def _call(hass: HomeAssistant, domain: str, service: str, **data: object) -> None:
    await hass.services.async_call(domain, service, data, blocking=True)
    await hass.async_block_till_done()


def _payloads(ble: ScriptedResponder, mode: int) -> list[str]:
    return [frame[6:-1].hex(" ") for frame in ble.writes if frame[5] == mode]


def _pump(proxy: PeripheralProxy) -> ScriptedResponder:
    ble = ScriptedResponder()
    ble.expect(165, 4, [4], respond=[bytes.fromhex("5b 10 10 00 01 1e 00 14 00 b8")])
    ble.expect(165, 4, [5], respond=[bytes.fromhex("5b 10 0e 00 01 22 00 64 00 1b")])
    proxy.advertise(PUMP, "DYDOSE-test", ble)
    return ble


async def test_light_curve_manual_override_and_unload(ha: HomeAssistant, proxy: PeripheralProxy) -> None:
    """HA routes schedule and RGBW services and removes them once the light is unloaded."""
    ble = ScriptedResponder()
    proxy.advertise(LIGHT, "DYWPRO60-test", ble)
    entry = await _add_device(ha, LIGHT)
    light = _entity(ha, entry, "light", "rgbw")
    auto = _entity(ha, entry, "switch", "auto_mode")

    await _call(ha, DOMAIN, "set_auto_curve", entry_id=entry.entry_id, curve={"0": [[480, 50]]})
    assert _payloads(ble, 6) == ["00 08 00 32"]
    assert ha.states.is_state(auto, "on")
    await _call(ha, "light", "turn_on", entity_id=light, rgbw_color=[255, 0, 0, 128])
    assert _payloads(ble, 7) == ["00 64", "01 00", "02 00", "03 33"]
    assert (ha.states.get(light).state, ha.states.get(auto).state) == ("on", "off")
    await _call(ha, "light", "turn_off", entity_id=light)
    assert ha.states.is_state(light, "off")

    assert await ha.config_entries.async_unload(entry.entry_id)
    assert not ha.services.has_service(DOMAIN, "set_auto_curve")


async def test_white_light_scene_service_rejects_duplicate_weekdays_before_replacing(
    ha: HomeAssistant, proxy: PeripheralProxy
) -> None:
    """An invalid replacement leaves the active scene alone; midnight encodes as 23:59."""
    ble = ScriptedResponder()
    proxy.advertise(LIGHT, "DYNA2-test", ble)
    entry = await _add_device(ha, LIGHT)
    light = _entity(ha, entry, "light", "white")
    auto = _entity(ha, entry, "switch", "auto_mode")
    await _call(ha, "light", "turn_on", entity_id=light, brightness=128)
    assert _payloads(ble, 7) == ["00 33"]
    assert ha.states.is_state(light, "on")

    selector = {"entry_id": entry.entry_id}
    period = {"start": "08:30", "end": "00:00", "brightness": 90}
    await _call(ha, DOMAIN, "set_schedule", **selector, periods=[period])
    assert _payloads(ble, 25) == ["08 1e 17 3b 00 7f 5a ff ff ff ff ff ff ff"]
    assert ha.states.is_state(auto, "on")
    before = list(ble.writes)
    with pytest.raises(HomeAssistantError, match="only one schedule period per weekday"):
        await _call(ha, DOMAIN, "set_schedule", **selector, periods=[period, {"start": "10:00", "end": "16:00"}])
    assert ble.writes == before
    await _call(ha, DOMAIN, "reset_schedule", **selector)
    assert _payloads(ble, 5)[-1] == "28 ff ff"


async def test_partial_curve_failure_is_reported_and_cleaned_up(ha: HomeAssistant, proxy: PeripheralProxy) -> None:
    """Invalid data cannot touch the device; a partially written curve is cleared."""
    ble = ScriptedResponder()
    proxy.advertise(LIGHT, "DYRGBV-test", ble)
    entry = await _add_device(ha, LIGHT)
    auto = _entity(ha, entry, "switch", "auto_mode")
    original = list(ble.writes)
    with pytest.raises(HomeAssistantError, match="level must be between 0 and 100"):
        await _call(ha, DOMAIN, "set_auto_curve", entry_id=entry.entry_id, curve={"2": [[480, 101]]})
    assert ble.writes == original

    ble.expect(90, 6, [2, 17], fail=True)
    with pytest.raises(HomeAssistantError, match="previous curve may be lost"):
        await _call(ha, DOMAIN, "set_auto_curve", entry_id=entry.entry_id, curve={"2": [[480, 50], [510, 100]]})
    assert _payloads(ble, 6) == ["02 10 64", "02 11 c8"] * 3
    assert _payloads(ble, 5)[-1] == "05 ff ff"  # custom curve reset, not an auto-mode switch
    assert ha.states.is_state(auto, "off")


async def test_dosing_distinguishes_reported_volume_from_persisted_local_total(
    ha: HomeAssistant, proxy: PeripheralProxy
) -> None:
    """A local dose updates only its channel and survives unloading the entry."""
    ble = _pump(proxy)
    entry = await _add_device(ha, PUMP, pump_count=2)
    local = _entity(ha, entry, "sensor", "dosing_pump_2_dosed_today")
    reported = _entity(ha, entry, "sensor", "dosing_pump_2_dosing_daily_ul")
    selector = {"entry_id": entry.entry_id}
    assert not ha.services.has_service(DOMAIN, "set_schedule")
    with pytest.raises(HomeAssistantError, match="has 2 pumps"):
        await _call(ha, DOMAIN, "dose_ml", **selector, pump=3, ml=2.5)
    assert _payloads(ble, 27) == []
    await _call(ha, DOMAIN, "dose_ml", **selector, pump=2, ml=2.5)
    assert _payloads(ble, 27) == ["01 00 02 00 19"]
    assert (ha.states.get(local).state, ha.states.get(reported).state) == ("2.5", "2.7")

    assert await ha.config_entries.async_reload(entry.entry_id)
    await ha.async_block_till_done()
    assert (ha.states.get(local).state, ha.states.get(reported).state) == ("2.5", "2.7")
    assert _payloads(ble, 27) == ["01 00 02 00 19"]


async def test_fan_remembers_speed_across_reload(ha: HomeAssistant, proxy: PeripheralProxy) -> None:
    """Auto, off and reload do not erase the last explicitly selected fan speed."""
    ble = ScriptedResponder()
    proxy.advertise(FAN, "DYVVD3-test", ble)
    entry = await _add_device(ha, FAN)
    fan = _entity(ha, entry, "fan", "fan")
    await _call(ha, "fan", "set_percentage", entity_id=fan, percentage=53)
    await _call(ha, "fan", "set_preset_mode", entity_id=fan, preset_mode="Auto")
    assert ha.states.get(fan).attributes["preset_mode"] == "Auto"
    await _call(ha, "fan", "turn_off", entity_id=fan)
    assert ha.states.is_state(fan, "off")
    assert await ha.config_entries.async_reload(entry.entry_id)
    await _call(ha, "fan", "turn_on", entity_id=fan)
    assert (ha.states.get(fan).state, ha.states.get(fan).attributes["percentage"]) == ("on", 53)
    assert _payloads(ble, 15) == ["35", "00", "35"]


async def test_fan_thresholds_restore_after_reload_with_hysteresis(ha: HomeAssistant, proxy: PeripheralProxy) -> None:
    """A pair is restored through HA state without losing the configured separation."""
    ble = ScriptedResponder()
    proxy.advertise(FAN, "DYVVD3-test", ble)
    entry = await _add_device(ha, FAN)
    start = _entity(ha, entry, "number", "fan_start_temp")
    stop = _entity(ha, entry, "number", "fan_stop_temp")
    await _call(ha, "number", "set_value", entity_id=start, value=40)
    await _call(ha, "number", "set_value", entity_id=stop, value=36)
    assert _payloads(ble, 45) == ["28 21", "28 24"]
    assert await ha.config_entries.async_reload(entry.entry_id)
    await ha.async_block_till_done()
    assert _payloads(ble, 45)[-1] == "28 24"
    assert (float(ha.states.get(start).state), float(ha.states.get(stop).state)) == (40, 36)


async def test_heater_measurement_and_configured_power_are_independent(
    ha: HomeAssistant, proxy: PeripheralProxy
) -> None:
    """Captured reports update sensors without replacing requested setpoints."""
    ble = ScriptedResponder()
    boot_reports = [
        bytes.fromhex("5b 06 0e 00 01 0a 00 00 cb ff ff ff 22 05 00 22"),
        bytes.fromhex("5b 01 10 00 01 0b 00 00 00 00 00 00 14 00 0b 33 50 22"),
    ]
    temperature_report = bytes.fromhex("5b 06 0e 01 7b 25 00 1e 0b ff 00 fa 00")
    ble.expect(90, 4, [1], respond=boot_reports)
    ble.expect(90, 43, respond=[temperature_report])
    ble.expect(165, 43, respond=[temperature_report])
    proxy.advertise(HEATER, "DYHET-test", ble)
    entry = await _add_device(ha, HEATER)
    target = _entity(ha, entry, "number", "heater_temperature")
    power = _entity(ha, entry, "number", "heater_power")
    measured_power = _entity(ha, entry, "sensor", "heater_measured_power_watts")
    water = _entity(ha, entry, "sensor", "heater_current_temperature_celsius")
    await _call(ha, "number", "set_value", entity_id=target, value=27.5)
    await _call(ha, "number", "set_value", entity_id=power, value=400)
    assert _payloads(ble, 43) == ["00 1b 32 14", "00 1b 32 28"]
    assert tuple(float(ha.states.get(entity).state) for entity in (target, power, water, measured_power)) == (
        27.5,
        400,
        25,
        30,
    )


async def test_linked_stirrer_mirrors_program_and_verbatim_dose(ha: HomeAssistant, proxy: PeripheralProxy) -> None:
    """Link replays stored programming, then forwards an actual manual-run frame unchanged."""
    pump = _pump(proxy)
    stirrer = ScriptedResponder()
    proxy.advertise(STIRRER, "DYMIXR-test", stirrer)
    await _add_device(ha, PUMP, pump_count=2)
    stir = await _add_device(ha, STIRRER, stirrer_channel_count=2)
    schedule = {"address": PUMP, "channel": 2, "mode": "timer", "points": [{"start": "08:30", "ml": 2.5}]}
    await _call(ha, DOMAIN, "set_dosing_schedule", **schedule, daily_ml=2.5, weekdays=["monday"])
    assert _payloads(stirrer, 21) == []
    await _call(ha, DOMAIN, "set_stirrer_master", address=STIRRER, master_address=PUMP)
    assert stir.data["master_address"] == PUMP
    assert _payloads(stirrer, 21) == _payloads(pump, 21) == ["01 03 08 1e 00 19"]
    assert _payloads(pump, 27) == ["01 20 01 00 00 19"]  # Monday on a legacy pump.
    assert _payloads(stirrer, 27) == ["01 40 01 00 00 19"]  # Monday on an ISO stirrer.

    stirrer.expect(165, 27, [1], fail=True)
    await _call(ha, DOMAIN, "dose_ml", address=PUMP, pump=2, ml=3.5)
    assert [frame for frame in stirrer.writes if frame[5] == 27][-1] == pump.writes[-1]
    assert _payloads(stirrer, 27).count("01 00 02 00 23") == 1  # No replay after losing the slave's ACK.
    await _call(ha, DOMAIN, "set_stirrer_master", address=STIRRER)
    assert "master_address" not in stir.data
    before = list(stirrer.writes)
    await _call(ha, DOMAIN, "dose_ml", address=PUMP, pump=2, ml=3.5)
    assert stirrer.writes == before


async def test_legacy_pump_stages_and_resumes_schedule(ha: HomeAssistant, proxy: PeripheralProxy) -> None:
    """Disabled schedules stay dormant on both devices and survive a reload."""
    pump, stirrer = _pump(proxy), ScriptedResponder()
    proxy.advertise(STIRRER, "DYMIXR-test", stirrer)
    entry = await _add_device(ha, PUMP, pump_count=2)
    await _add_device(ha, STIRRER, stirrer_channel_count=2)
    await _call(ha, DOMAIN, "set_stirrer_master", address=STIRRER, master_address=PUMP, mirror=False)
    selector = {"address": PUMP, "channel": 2}
    before = [list(ble.writes) for ble in (pump, stirrer)]
    with pytest.raises(HomeAssistantError, match="No stored schedule"):
        await _call(ha, DOMAIN, "set_channel_active", **selector)
    assert [ble.writes for ble in (pump, stirrer)] == before

    await _call(
        ha,
        DOMAIN,
        "set_dosing_schedule",
        **selector,
        mode="timer",
        points=[{"start": "08:30", "ml": 2.5}],
        active=False,
        daily_ml=2.5,
        weekdays=["sunday", "monday"],
    )
    assert _payloads(pump, 32) == _payloads(stirrer, 32) == ["01 00 00"]
    await _call(ha, DOMAIN, "mirror_stirrer", address=STIRRER)
    assert _payloads(pump, 21) == _payloads(stirrer, 21) == []
    assert _payloads(pump, 27) == ["01 60 01 00 00 19"]  # Daily settings don't install runnable slots.
    assert _payloads(stirrer, 27) == ["01 41 01 00 00 19"] * 2  # Live and full replay both translate.

    await _call(ha, DOMAIN, "set_channel_active", **selector, compensate=True)
    assert _payloads(pump, 32)[-1] == _payloads(stirrer, 32)[-1] == "01 01 01"
    assert _payloads(pump, 21) == _payloads(stirrer, 21) == ["01 03 08 1e 00 19"]
    assert _payloads(pump, 27)[-1] == "01 60 01 00 00 19"
    assert _payloads(stirrer, 27)[-1].split()[1] == "41"

    before = [len(ble.writes) for ble in (pump, stirrer)]
    await _call(ha, DOMAIN, "set_channel_active", **selector, enable=False)
    for ble, start in zip((pump, stirrer), before, strict=True):
        assert [(frame[5], frame[6:-1].hex(" ")) for frame in ble.writes[start:] if frame[0] == 165] == [
            (32, "01 00 00")
        ]
    assert await ha.config_entries.async_reload(entry.entry_id)
    await ha.async_block_till_done()
    await _call(ha, DOMAIN, "set_channel_active", **selector)
    assert _payloads(pump, 21) == _payloads(stirrer, 21) == ["01 03 08 1e 00 19"] * 2
    assert _payloads(pump, 27)[-1] == "01 60 01 00 00 19"

    # Same-day reprogramming must clear the legacy inhibit latch even with first_setting=False.
    # Raw frequency, including its high bit, passes through to the pump unchanged.
    await _call(
        ha,
        DOMAIN,
        "set_dosing_schedule",
        **selector,
        mode="free",
        points=[{"start": "10:00", "end": "12:00"}],
        daily_ml=4,
        frequency=0xA0,
        first_setting=False,
    )
    assert _payloads(pump, 27)[-1] == "01 a0 01 00 00 28"
    assert _payloads(stirrer, 27)[-1] == "01 c0 01 01 00 28"
    assert _payloads(pump, 23) == _payloads(stirrer, 23) == ["01 0a 00 0c 00 02"]


async def test_legacy_pump_resumes_old_iso_record_without_changing_stirrer_days(
    ha: HomeAssistant, proxy: PeripheralProxy
) -> None:
    """An unmarked record stays ISO on a stirrer and converts on a legacy pump."""
    pump, stirrer = _pump(proxy), ScriptedResponder()
    proxy.advertise(STIRRER, "DYMIXR-test", stirrer)
    key = f"{DOMAIN}_dosing_programming_{PUMP.lower().replace(':', '_')}"
    await Store(ha, 1, key).async_save(
        {
            "channels": {
                "1": {
                    "active": True,
                    "mode": "timer",
                    "points": [{"start_hour": 8, "start_minute": 30, "volume_ml": 2.5}],
                    "dose_per_day_ml": 2.5,
                    "frequency": 0xC1,
                }
            }
        }
    )
    await _add_device(ha, PUMP, pump_count=2)
    await _add_device(ha, STIRRER, stirrer_channel_count=2)
    await _call(ha, DOMAIN, "set_stirrer_master", address=STIRRER, master_address=PUMP)
    assert _payloads(stirrer, 27) == ["01 c1 01 00 00 19"]
    await _call(ha, DOMAIN, "set_channel_active", address=PUMP, channel=2)
    assert _payloads(pump, 27) == ["01 e0 01 00 00 19"]
    await _call(ha, DOMAIN, "mirror_stirrer", address=STIRRER)
    assert _payloads(stirrer, 27) == ["01 c1 01 00 00 19"] * 3
    assert _payloads(stirrer, 21) == ["01 03 08 1e 00 19"] * 3


@pytest.mark.parametrize("prefix", ["DYDOSE", "DYDOSED"])
async def test_legacy_pump_limits_channel_options_and_stale_entries(
    ha: HomeAssistant, proxy: PeripheralProxy, prefix: str
) -> None:
    """Real legacy pumps reject eight-channel config and clamp older entries at setup."""
    pump = ScriptedResponder()
    proxy.advertise(PUMP, f"{prefix}-test", pump)
    flow = ha.config_entries.flow
    result = await flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await flow.async_configure(result["flow_id"], {CONF_ADDRESS: PUMP})
    assert result["step_id"] == "dosing_config"
    with pytest.raises(InvalidData):
        await flow.async_configure(result["flow_id"], {"pump_count": 8})
    result = await flow.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.data["pump_count"] == 4
    await ha.async_block_till_done()

    ha.config_entries.async_update_entry(entry, options={"pump_count": 8})
    assert await ha.config_entries.async_reload(entry.entry_id)
    await ha.async_block_till_done()
    with pytest.raises(HomeAssistantError, match="has 4 pumps"):
        await _call(ha, DOMAIN, "dose_ml", address=PUMP, pump=5, ml=4)
    assert _payloads(pump, 27) == []
    options = ha.config_entries.options
    result = await options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await options.async_configure(result["flow_id"], {"pump_count": "8"})
    result = await options.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["pump_count"] == 4
    await ha.async_block_till_done()


async def test_generic_pump_keeps_eight_channels_and_iso_programming(ha: HomeAssistant, proxy: PeripheralProxy) -> None:
    """Legacy-only changes leave the generic pump's service and frame behavior intact."""
    pump = ScriptedResponder()
    proxy.advertise(PUMP, "DYNDOS-test", pump)
    await _add_device(ha, PUMP, pump_count=8)
    selector = {"address": PUMP, "channel": 8}
    await _call(ha, DOMAIN, "set_channel_active", **selector)
    assert _payloads(pump, 32) == ["07 00 01"]
    await _call(
        ha,
        DOMAIN,
        "set_dosing_schedule",
        **selector,
        mode="timer",
        points=[{"start": "08:30", "ml": 2.5}],
        active=False,
        daily_ml=2.5,
        weekdays=["monday"],
    )
    assert _payloads(pump, 32)[-1] == "07 00 00"
    assert _payloads(pump, 21) == ["07 03 08 1e 00 19"]
    assert _payloads(pump, 27) == ["07 40 01 00 00 19"]
    await _call(ha, DOMAIN, "dose_ml", address=PUMP, pump=8, ml=2.5)
    assert _payloads(pump, 27)[-1] == "07 00 00 00 19"


async def test_stirrer_service_respects_configured_channels(ha: HomeAssistant, proxy: PeripheralProxy) -> None:
    """The HA channel limit protects the device, and an accepted schedule is translated."""
    ble = ScriptedResponder()
    proxy.advertise(STIRRER, "DYMIXR-test", ble)
    entry = await _add_device(ha, STIRRER, stirrer_channel_count=2)
    switch = _entity(ha, entry, "switch", "stir_channel_2")
    speed = _entity(ha, entry, "number", "stir_channel_2_speed")
    await _call(ha, "number", "set_value", entity_id=speed, value=16)
    await _call(ha, "switch", "turn_on", entity_id=switch)
    assert ha.states.is_state(switch, "on")
    assert float(ha.states.get(speed).state) == 16
    assert _payloads(ble, 42) == ["01 00 00 10"]
    points = [{"start": "08:00", "minutes": 5}]
    before = list(ble.writes)
    with pytest.raises(HomeAssistantError, match="has 2 stir channels configured"):
        await _call(ha, DOMAIN, "set_stir_schedule", address=STIRRER, channel=3, points=points)
    assert ble.writes == before
    await _call(ha, DOMAIN, "set_stir_schedule", address=STIRRER, channel=2, points=points, weekdays=["monday"])
    assert _payloads(ble, 21) == ["01 03 08 00 00 1e"]


@pytest.mark.parametrize("decision", ["retry", "continue"])
async def test_calibration_requires_decision_after_ambiguous_timed_run(
    ha: HomeAssistant, proxy: PeripheralProxy, decision: str
) -> None:
    """A possibly delivered dose is not resent without another explicit user submission."""
    ble = _pump(proxy)
    entry = await _add_device(ha, PUMP, pump_count=2)
    assert await async_setup_component(ha, "repairs", {})
    manager = repairs_flow_manager(ha)
    assert manager is not None
    await _call(ha, "button", "press", entity_id=_entity(ha, entry, "button", "dosing_pump_calibrate"))
    issue_id = f"chihiros_calibrate_{entry.entry_id}"
    assert ir.async_get(ha).async_get_issue(DOMAIN, issue_id) is not None
    result = await manager.async_init(DOMAIN, data={"issue_id": issue_id})
    assert result["step_id"] == "calibrate_pump"
    flow_id = result["flow_id"]
    result = await manager.async_configure(flow_id, {"pump": "2"})
    assert result["step_id"] == "calibrate_run"

    timed = "01 05 ff ff"

    def fail_first_timed_write(_frame: bytes) -> list[bytes]:
        if _payloads(ble, 22).count(timed) == 1:
            raise BleakError("acknowledgement lost")
        return []

    ble.expect(165, 22, [1, 5], respond=fail_first_timed_write)
    result = await manager.async_configure(flow_id, {})
    assert result["step_id"] == "calibrate_dose_unclear"
    assert _payloads(ble, 22) == [timed]
    result = await manager.async_configure(flow_id, {"dose_choice": decision})
    assert _payloads(ble, 22) == [timed]
    if decision == "retry":
        assert result["step_id"] == "calibrate_run"
        result = await manager.async_configure(flow_id, {})
        assert _payloads(ble, 22) == [timed, timed]
    assert result["step_id"] == "calibrate_measure"

    result = await manager.async_configure(flow_id, {"volume_ml": 4.05})
    assert result["step_id"] == "calibrate_test"
    assert _payloads(ble, 22)[-1] == "01 ff 04 05"
    test_dose_fails = decision == "continue"
    if test_dose_fails:
        ble.expect(165, 27, [1], fail=True)
        result = await manager.async_configure(flow_id, {})
        assert result["step_id"] == "calibrate_dose_unclear"
        result = await manager.async_configure(flow_id, {"dose_choice": "continue"})
    else:
        result = await manager.async_configure(flow_id, {})
    assert result["step_id"] == "calibrate_accuracy"
    assert _payloads(ble, 27) == ["01 00 00 00 28"]  # Calibration verification is not an extra dose.
    result = await manager.async_configure(flow_id, {"accurate": "yes"})
    await ha.async_block_till_done()
    assert result["reason"] == "calibration_complete"
    assert ir.async_get(ha).async_get_issue(DOMAIN, issue_id) is None
    total = _entity(ha, entry, "sensor", "dosing_pump_2_dosed_today")
    assert ha.states.is_state(total, "0.0" if test_dose_fails else "4.0")

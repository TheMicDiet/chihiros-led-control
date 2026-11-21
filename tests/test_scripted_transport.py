"""Public device operations against an in-memory BLE peripheral."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from bleak_retry_connector import BleakError

from chihiros_led_control.const import DFU_CHAR_UUID, HM10_RX_CHAR_UUID, UART_RX_CHAR_UUID, UART_TX_CHAR_UUID
from chihiros_led_control.devices import ChihirosDevice, ChihirosDosingPump, ChihirosMagStirrer
from chihiros_led_control.exceptions import CharacteristicMissingError
from chihiros_led_control.protocol.dosing import DosingMode, DosingWorkPoint
from chihiros_led_control.protocol.notifications import DosingTotalsNotification, RuntimeNotification
from chihiros_led_control.registry import DOSING_PUMP, MAG_STIRRER, detect_model
from chihiros_led_control.testing import ScriptedBLEDevice, ScriptedTransport
from chihiros_led_control.weekday_encoding import WeekdaySelect

pytestmark = pytest.mark.component
RUNTIME_FRAME = bytes.fromhex("5b 1b 0a 00 01 0a 01 ff")


def _light(ble: ScriptedTransport) -> ChihirosDevice:
    return ChihirosDevice(ScriptedBLEDevice(ble.name, ble.address), detect_model(ble.name), transport=ble)


def _pump(ble: ScriptedTransport) -> ChihirosDosingPump:
    return ChihirosDosingPump(ScriptedBLEDevice(ble.name, ble.address), DOSING_PUMP, transport=ble)


def _trace(ble: ScriptedTransport) -> list[str]:
    """Observe commands in order, omitting length, sequence IDs and checksums."""
    return [f"{frame[0]:02x}: {frame[5:-1].hex(' ')}" for frame in ble.writes]


@pytest.mark.parametrize(
    ("endpoints", "subscription"),
    [
        ({HM10_RX_CHAR_UUID: ("write", "notify"), UART_RX_CHAR_UUID: ("write",)}, HM10_RX_CHAR_UUID),
        ({UART_RX_CHAR_UUID: ("write",), UART_TX_CHAR_UUID: ("notify",)}, UART_TX_CHAR_UUID),
        ({HM10_RX_CHAR_UUID: ("write-without-response",)}, None),
    ],
    ids=["ffe1-preferred", "nordic-uart", "write-only"],
)
async def test_status_uses_paired_notification_endpoint(
    endpoints: dict[str, tuple[str, ...]], subscription: str | None
) -> None:
    """DFU is never a control endpoint, even on a write-only device."""
    ble = ScriptedTransport(realtime=False, characteristics={**endpoints, DFU_CHAR_UUID: ("notify",)})
    light = _light(ble)
    seen = []
    light.add_notification_callback(seen.append)

    def reply(_frame: bytes) -> list[bytes]:
        return [] if seen else [RUNTIME_FRAME]

    ble.expect(90, 4, [1], respond=reply)
    await light.query_status()
    await light.disconnect()

    expected = RuntimeNotification(27, 511, RUNTIME_FRAME) if subscription else None
    assert light.last_runtime_notification == expected
    assert seen == ([expected] if subscription else [])
    assert ble.subscriptions == ([subscription] if subscription else [])
    assert [frame[5] for frame in ble.writes] == [4, 9, 4]


@pytest.mark.parametrize("response", [True, False], ids=["acknowledged", "write-only"])
async def test_writes_use_supported_ack_and_do_not_interleave(response: bool) -> None:
    """An outstanding GATT write holds its transaction ahead of the next client command."""
    props = ("write", "write-without-response") if response else ("write-without-response",)
    ble = ScriptedTransport(realtime=False, characteristics={HM10_RX_CHAR_UUID: props})
    light = _light(ble)
    hold = ble.pause("write", mode=5)
    turning_on = asyncio.create_task(light.turn_on())
    await hold.wait()
    turning_off = asyncio.create_task(light.turn_off())
    try:
        await asyncio.sleep(0)
        assert _trace(ble)[2:] == ["5a: 05 0b ff ff"]
    finally:
        hold.release.set()
        await asyncio.gather(turning_on, turning_off)
        await light.disconnect()
    assert ble.write_responses == [response] * 6
    assert ble.connections == 1
    assert _trace(ble)[2:] == ["5a: 05 0b ff ff", "5a: 07 00 64", "5a: 05 0b ff ff", "5a: 07 00 00"]


async def test_no_control_endpoint_closes_session() -> None:
    """A peripheral without a control endpoint fails before writing any frames."""
    ble = ScriptedTransport(realtime=False, characteristics={DFU_CHAR_UUID: ("notify",)})
    with pytest.raises(CharacteristicMissingError, match="Write characteristic missing"):
        await _light(ble).query_status()
    assert ble.writes == []
    assert [client.is_connected for client in ble.clients] == [False]


async def test_retry_reconnects_and_resends_prelude() -> None:
    """Idempotent status can recover from an error during the connection prelude."""
    ble = ScriptedTransport(realtime=False)
    light = _light(ble)

    def reply(_frame: bytes) -> list[bytes]:
        if ble.connections == 1:
            raise BleakError("transient failure")
        return [] if light.last_runtime_notification else [RUNTIME_FRAME]

    ble.expect(90, 4, [1], respond=reply)
    await light.query_status()
    await light.disconnect()

    assert ble.connections == 2
    assert [client.is_connected for client in ble.clients] == [False, False]
    assert [frame[5] for frame in ble.writes] == [4, 4, 9, 4]
    assert light.last_runtime_notification == RuntimeNotification(27, 511, RUNTIME_FRAME)


@pytest.mark.parametrize("stage", ["subscribe", "unsubscribe", "disconnect"])
async def test_cancellation_cannot_orphan_a_connection(stage: str) -> None:
    """Cancel at a pending BLE setup or teardown operation, then reconnect."""
    ble = ScriptedTransport(realtime=False)
    light = _light(ble)
    if stage != "subscribe":
        await light.query_status()
    hold = ble.pause(stage)
    task = asyncio.create_task(light.query_status() if stage == "subscribe" else light.disconnect())
    try:
        await hold.wait()
        task.cancel()
        await asyncio.sleep(0)
        if stage != "subscribe":
            task.cancel()
            await asyncio.sleep(0)
    finally:
        hold.release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)

    assert [client.is_connected for client in ble.clients] == [False]
    await light.query_status()
    assert ble.connections == 2
    await light.disconnect()


async def test_fan_auto_then_manual_speed() -> None:
    """The VIVID III changes mode in the same connection and clamps manual speed."""
    ble = ScriptedTransport(name="DYVVD3-test", realtime=False)
    light = _light(ble)
    assert not light.fan_auto
    await light.set_fan_auto()
    assert light.fan_auto
    await light.set_fan_speed(10)
    assert not light.fan_auto
    await light.disconnect()

    assert ble.connections == 1
    assert _trace(ble)[2:] == ["5a: 05 11 ff ff", "5a: 0f 19"]


@pytest.mark.parametrize(
    ("method", "state", "on", "off"),
    [("set_temp_protect", "temp_protect", 0x30, 0x31), ("set_bluetooth_led", "bluetooth_led", 0x33, 0x32)],
)
async def test_vivid_switch_polarity_and_failed_write_state(method: str, state: str, on: int, off: int) -> None:
    """Both switches encode their own polarity and only commit successful writes."""
    ble = ScriptedTransport(name="DYVVD3-test", realtime=False)
    light = _light(ble)
    switch = getattr(light, method)
    await switch(True)
    assert getattr(light, state) is True
    await switch(False)
    assert getattr(light, state) is False
    assert _trace(ble)[2:] == [f"5a: 05 {on:02x} ff ff", f"5a: 05 {off:02x} ff ff"]
    ble.expect(90, 5, [on, 255, 255], fail=True)
    with pytest.raises(BleakError, match="scripted write failure"):
        await switch(True)
    assert getattr(light, state) is False
    assert _trace(ble).count(f"5a: 05 {on:02x} ff ff") == 4
    assert not ble.is_connected


async def test_auto_mode_syncs_supplied_local_clock_before_switch() -> None:
    """The command uses the caller's wall clock, not time at the BLE peripheral."""
    ble = ScriptedTransport(name="DYNW60-test", realtime=False)
    light = _light(ble)
    await light.enable_auto_mode(datetime(2026, 6, 16, 20, 30, 45))
    await light.disconnect()
    assert _trace(ble)[2:] == ["5a: 09 1a 06 02 14 1e 2d", "5a: 05 12 ff ff"]


async def test_pump_doses_then_queries_counters_in_order() -> None:
    """Pump auth, dosing, and counter refresh traverse one session."""
    ble = ScriptedTransport(name="DYDOSE-test", realtime=False)
    pump = _pump(ble)
    totals = bytes.fromhex("5b 10 10 00 01 1e 04 1f 00 00")
    ble.expect(165, 4, [4], respond=[totals])
    await pump.dose_ml(1, 2.0)
    await pump.query_status()
    await pump.disconnect()
    assert ble.connections == 1
    assert _trace(ble)[2:] == [
        "a5: 04 04",
        "a5: 04 05",
        "a5: 1b 01 00 02 00 14",
        "a5: 04 04",
        "a5: 04 05",
        "5a: 04 01",
    ]
    assert pump.last_dosing_totals_notification == DosingTotalsNotification((105500, 0), totals)


@pytest.mark.parametrize("timed_calibration", [False, True], ids=["manual-dose", "timed-calibration"])
async def test_pump_never_replays_an_ambiguous_run(timed_calibration: bool) -> None:
    """The write may have reached the peripheral before it reported an error."""
    ble = ScriptedTransport(name="DYDOSE-test", realtime=False)
    pump = _pump(ble)
    mode, params = (22, [1, 5]) if timed_calibration else (27, [1])
    ble.expect(165, mode, params, fail=True)
    with pytest.raises(BleakError, match="scripted write failure"):
        if timed_calibration:
            await pump.calibrate_channel(1, seconds=5)
        else:
            await pump.dose_ml(1, 2.0)
    assert ble.connections == 1
    assert sum(frame[0] == 165 and frame[5] == mode for frame in ble.writes) == 1
    assert not ble.is_connected


async def test_recording_calibration_volume_can_retry() -> None:
    """A calibration setting, unlike a pump run, is safe to repeat."""
    ble = ScriptedTransport(name="DYDOSE-test", realtime=False)

    def reply(_frame: bytes) -> list[bytes]:
        if ble.connections == 1:
            raise BleakError("transient failure")
        return []

    ble.expect(165, 22, [0, 255], respond=reply)
    pump = _pump(ble)
    await pump.calibrate_channel(0, volume_ml=4.05)
    await pump.disconnect()
    assert ble.connections == 2
    assert _trace(ble).count("a5: 16 00 ff 04 05") == 2


async def test_pump_program_weekdays_require_daily_volume() -> None:
    """No weekday mask is silently dropped; a valid mask reaches the wire."""
    ble = ScriptedTransport(name="DYDOSE-test", realtime=False)
    pump = _pump(ble)
    points = [DosingWorkPoint(8, 0, volume_ml=1.0)]
    with pytest.raises(ValueError, match="daily volume"):
        await pump.program_channel(0, active=True, frequency=64, mode=DosingMode.TIMER, points=points)
    assert not ble.writes
    assert ble.connections == 0
    await pump.program_channel(0, active=True, frequency=64, dose_per_day_ml=1.0, mode=DosingMode.TIMER, points=points)
    await pump.disconnect()
    assert [line for line in _trace(ble) if line.startswith("a5: 1b")] == ["a5: 1b 00 40 01 00 00 0a"]


@pytest.mark.parametrize("name", ["DYDOSE-test", "DYDOSED-test"])
async def test_legacy_pump_uses_sunday_first_clock_and_correct_dose_subtypes(name: str) -> None:
    """Real legacy driver writes Sunday-first time and distinguishes extra/test doses."""
    ble = ScriptedTransport(name=name, realtime=False)
    pump = ChihirosDosingPump(
        ScriptedBLEDevice(ble.name, ble.address), transport=ble, clock=lambda: datetime(2026, 11, 23, 7, 59)
    )
    await pump.dose_ml(0, 2.5)
    await pump.dose_ml(0, 4.0, extra=False)
    await pump.disconnect()
    trace = _trace(ble)
    assert trace[1] == "5a: 09 1a 0b 02 07 3b 00"  # Monday is weekday 2 on DYDOSE.
    assert [line for line in trace if line.startswith("a5: 1b")] == [
        "a5: 1b 00 00 02 00 19",
        "a5: 1b 00 00 00 00 28",
    ]


async def test_legacy_pump_protects_other_channels_and_rejects_unrepresentable_schedules() -> None:
    """Errors are local; an invalid replacement never clears the existing schedule."""
    ble = ScriptedTransport(name="DYDOSE-test", realtime=False)
    pump = ChihirosDosingPump(ScriptedBLEDevice(ble.name, ble.address), DOSING_PUMP, transport=ble)
    with pytest.raises(ValueError, match="between 0 and 3"):
        await pump.reset_total_dosed(4)  # Without this guard, selector 25 resets channel 1.
    with pytest.raises(ValueError, match="between 0 and 3"):
        await pump.reset_channel(4)
    with pytest.raises(ValueError, match="without reprogramming"):
        await pump.set_channel_active(0)
    with pytest.raises(ValueError, match="5-second"):
        await pump.calibrate_channel(0, seconds=10)
    with pytest.raises(ValueError, match="at most 24"):
        await pump.set_schedule(0, DosingMode.TIMER, [DosingWorkPoint(8, i, volume_ml=1) for i in range(25)])
    with pytest.raises(ValueError, match="same day"):
        await pump.set_schedule(0, DosingMode.FREE, [DosingWorkPoint(23, 0, duration_minutes=120)])
    with pytest.raises(ValueError, match="distinct dose minutes"):
        window = DosingWorkPoint(8, 0, duration_minutes=120)
        await pump.set_schedule(0, DosingMode.FREE, [window, window])
    assert not ble.writes
    await pump.set_schedule(0, DosingMode.TIMER, [DosingWorkPoint(8, 0, volume_ml=1)])
    await pump.program_channel(0, active=True, dose_per_day_ml=1.5, is_first_setting=False)
    await pump.disconnect()
    assert _trace(ble)[2:] == ["a5: 20 00 00 01", "a5: 15 00 03 08 00 00 0a", "a5: 1b 00 7f 01 00 00 0f"]


async def test_stirrer_status_does_not_contact_peripheral() -> None:
    """Unlike a pump, a stirrer does not expose a counter status query."""
    ble = ScriptedTransport(name="DYMIXR-test", realtime=False)
    stirrer = ChihirosMagStirrer(ScriptedBLEDevice(ble.name, ble.address), MAG_STIRRER, transport=ble)
    await stirrer.query_status()
    assert ble.connections == 0
    assert ble.writes == []


async def test_scene_remove_writes_schedule_key_and_inactive_levels() -> None:
    """Removing a period keeps its time, ramp and weekday selection on the wire."""
    ble = ScriptedTransport(name="DYNW60-test", realtime=False)
    light = _light(ble)
    await light.remove_setting(datetime(2026, 1, 1, 2, 30), datetime(2026, 1, 1, 5, 10), 10, [WeekdaySelect.monday])
    await light.disconnect()
    assert _trace(ble)[-1] == "a5: 19 02 1e 05 0a 0a 40 ff ff ff ff ff ff ff ff"

"""Tests for magnetic-stirrer support: model detection and client API."""

from __future__ import annotations

import asyncio

import pytest
from bleak_retry_connector import BleakError

from chihiros_led_control.devices import ChihirosDevice, ChihirosDosingPump, ChihirosMagStirrer
from chihiros_led_control.factory import create_device, detect_model
from chihiros_led_control.protocol import dosing as dosing_commands
from chihiros_led_control.protocol.dosing import DosingMode, DosingWorkPoint
from chihiros_led_control.protocol.stirrer import stirrer_dosage_for_minutes
from chihiros_led_control.registry import DOSING_PUMP, MAG_STIRRER
from chihiros_led_control.testing import ScriptedBLEDevice, ScriptedTransport


@pytest.mark.unit
def test_detect_model_recognizes_stirrer_prefix() -> None:
    """DYMIXR advertisement names resolve to the Mag Stirrer model."""
    model = detect_model("DYMIXRCFAA123456")
    assert model.name == "Mag Stirrer"
    assert model is MAG_STIRRER


@pytest.mark.unit
def test_create_device_builds_stirrer_client() -> None:
    """DYMIXR devices get an independent magnetic-stirrer client."""

    async def run() -> ChihirosMagStirrer:
        return create_device(ScriptedBLEDevice("DYMIXR-test", "AA:BB:CC:DD:EE:FF"))

    device = asyncio.run(run())
    assert isinstance(device, ChihirosMagStirrer)
    assert not isinstance(device, ChihirosDosingPump)
    assert not isinstance(device, ChihirosDevice)
    assert device.model_name == "Mag Stirrer"


def _make_stirrer(transport: ScriptedTransport) -> ChihirosMagStirrer:
    return ChihirosMagStirrer(ScriptedBLEDevice(transport.name, transport.address), MAG_STIRRER, transport=transport)


@pytest.mark.component
def test_scripted_stirrer_pre_second_and_manual_stir() -> None:
    """Speed/pre-stir uses (0xA5, 42); manual start/stop uses (0xA5, 20)."""
    transport = ScriptedTransport(realtime=False, name="DYMIXR-test")

    async def run() -> None:
        device = _make_stirrer(transport)
        await device.set_pre_second(0, 90)
        await device.stir(2, True)
        await device.stir(2, False, seconds=300)

        modes = [frame[5] for frame in transport.writes]
        assert modes.count(42) == 1
        assert modes.count(20) == 2
        pre_second = next(frame for frame in transport.writes if frame[5] == 42)
        assert pre_second[6:10] == bytes([0, 0, 90, 20])
        start, stop = (frame for frame in transport.writes if frame[5] == 20)
        assert list(start[6:-1]) == [255, 255, 255, 255, 1, 255, 255, 255, 255, 255]
        assert list(stop[6:-1]) == [5, 0, 255, 255, 0, 255, 255, 255, 255, 255]

    asyncio.run(run())


@pytest.mark.component
def test_scripted_stirrer_schedule_sequence() -> None:
    """set_stir_schedule mirrors the app: active frame, dosingSet, timer points."""
    transport = ScriptedTransport(realtime=False, name="DYMIXR-test")

    async def run() -> None:
        device = _make_stirrer(transport)
        points = [
            DosingWorkPoint(8, 0, volume_ml=stirrer_dosage_for_minutes(10)),
            DosingWorkPoint(20, 30, volume_ml=stirrer_dosage_for_minutes(5)),
        ]
        await device.set_stir_schedule(1, points, frequency=127)

        dosing_frames = [frame for frame in transport.writes if frame[0] == 165]
        assert [frame[5] for frame in dosing_frames] == [32, 27, 21]
        active_frame, dosing_set, schedule = dosing_frames
        assert active_frame[6:9] == bytes([1, 0, 1])  # channel, compensate=0, active=1
        assert dosing_set[6:12] == bytes([1, 127, 1, 0, 0, 0])  # daily volume 0, first setting
        # Timer points: 10 minutes -> 6.0 mL -> 60 tenths; 5 minutes -> 30 tenths.
        assert list(schedule[6:-1]) == [1, 3, 8, 0, 0, 60, 20, 30, 0, 30]

    asyncio.run(run())


@pytest.mark.component
def test_scripted_stirrer_inactive_channel_skips_schedule() -> None:
    """An inactive stirrer channel gets no schedule frames (§6.2: only if is_active != 0)."""
    transport = ScriptedTransport(realtime=False, name="DYMIXR-test")

    async def run() -> None:
        device = _make_stirrer(transport)
        points = [DosingWorkPoint(8, 0, volume_ml=stirrer_dosage_for_minutes(10))]
        await device.set_stir_schedule(0, points, frequency=127, active=False)

        dosing_frames = [frame for frame in transport.writes if frame[0] == 165]
        assert [frame[5] for frame in dosing_frames] == [32, 27]
        assert dosing_frames[0][6:9] == bytes([0, 0, 0])  # active=0

    asyncio.run(run())


@pytest.mark.component
def test_program_channel_replays_inactive_stirrer_schedule() -> None:
    """Master/slave replay sends settings and schedule even for an inactive channel."""
    transport = ScriptedTransport(realtime=False, name="DYMIXR-test")
    points = [DosingWorkPoint(8, 0, volume_ml=stirrer_dosage_for_minutes(10))]

    async def run() -> None:
        device = _make_stirrer(transport)
        await device.program_channel(
            2,
            active=False,
            compensate=True,
            dose_per_day_ml=2.5,
            frequency=9,
            is_first_setting=False,
            mode=DosingMode.TIMER,
            points=points,
        )

    asyncio.run(run())
    frames = [frame for frame in transport.writes if frame[0] == 165]
    assert [frame[5] for frame in frames] == [32, 27, 21]
    assert frames[0][6:9] == bytes([2, 1, 0])
    assert frames[1][6:12] == bytes([2, 9, 1, 1, 0, 25])
    assert frames[2][6:-1] == bytes([2, 3, 8, 0, 0, 60])


@pytest.mark.component
def test_scripted_pump_schedule_and_settings() -> None:
    """Pump schedule/settings/calibration/reset commands write the right frames."""
    transport = ScriptedTransport(realtime=False, name="DYDOSE-test")

    async def run() -> None:
        device = ChihirosDosingPump(
            ScriptedBLEDevice(transport.name, transport.address),
            DOSING_PUMP,
            transport=transport,
        )
        await device.set_channel_active(0, active=True, compensate=True)
        await device.apply_dosing_settings(0, 10.0, 127, is_first_setting=False)
        await device.set_schedule(
            0,
            DosingMode.SINGLE,
            [DosingWorkPoint(8, 30, volume_ml=2.5)],
        )
        await device.calibrate_channel(0, seconds=10)
        await device.reset_channel(0)
        await device.reset_total_dosed(0)
        await device.set_dose_delay(True)

        dosing_frames = [frame for frame in transport.writes if frame[0] == 165]
        assert [(frame[5], list(frame[6:-1])) for frame in dosing_frames] == [
            (32, [0, 1, 1]),
            (27, [0, 127, 1, 1, 0, 100]),
            (21, [0, 0, 8, 30, 0, 25]),
            (22, [0, 10, 255, 255]),
            (5, [25, 255, 255]),
            (5, [21, 255, 255]),
            (31, [1]),
        ]

    asyncio.run(run())


@pytest.mark.unit
def test_calibrate_channel_requires_arguments() -> None:
    """calibrate_channel refuses to send a no-op calibration frame."""
    transport = ScriptedTransport(realtime=False, name="DYDOSE-test")

    async def run() -> None:
        device = ChihirosDosingPump(ScriptedBLEDevice(transport.name, transport.address), DOSING_PUMP)
        with pytest.raises(ValueError, match="seconds or volume_ml"):
            await device.calibrate_channel(0)

    asyncio.run(run())


@pytest.mark.component
@pytest.mark.parametrize("device_type", [ChihirosDosingPump, ChihirosMagStirrer], ids=["pump", "stirrer"])
@pytest.mark.parametrize("operation", ["manual-dose", "timed-calibration"])
def test_raw_broadcast_does_not_repeat_accepted_run_frame(
    device_type: type[ChihirosDosingPump] | type[ChihirosMagStirrer],
    operation: str,
) -> None:
    """Opaque broadcasts attempt once after an ambiguous accepted wire write.

    Accepted bytes are modeled, not firmware behavior or physical stirrer runs.
    """
    transport = ScriptedTransport(realtime=False, name="DYMIXR-test")
    frame = (
        dosing_commands.create_manual_dose_command((0, 1), 0, 1.0)
        if operation == "manual-dose"
        else dosing_commands.create_dosing_calibrate_command((0, 1), 0, seconds=10)
    )
    accepted: list[bytes] = []

    def accept_then_fail(data: bytes) -> tuple[bytes, ...]:
        """Accept the frame before losing acknowledgement at the BLE boundary."""
        accepted.append(data)
        raise BleakError("failure after acceptance")

    transport.expect(frame[0], frame[5], respond=accept_then_fail)

    async def run() -> None:
        """Exercise real device dispatch, retries, batching and GATT frame writes."""
        device = device_type(ScriptedBLEDevice(transport.name, transport.address), transport=transport)
        with pytest.raises(BleakError, match="failure after acceptance"):
            await device.send_frame(frame)
        await device.disconnect()

    asyncio.run(run())
    assert accepted == [bytes(frame)]
    assert transport.writes.count(bytes(frame)) == 1

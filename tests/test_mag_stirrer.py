"""Tests for magnetic-stirrer support: model detection, client API, and CLI."""

from __future__ import annotations

import asyncio

import pytest
from typer.testing import CliRunner

from chihiros_led_control import cli
from chihiros_led_control.client import ChihirosDevice, ChihirosDosingPump, ChihirosMagStirrer
from chihiros_led_control.commands import DosingMode, DosingWorkPoint, stirrer_dosage_for_minutes
from chihiros_led_control.factory import create_device, detect_model
from chihiros_led_control.models import DOSING_PUMP, MAG_STIRRER
from chihiros_led_control.testing import ScriptedBLEDevice, ScriptedTransport

RUNNER = CliRunner()
TEST_ADDRESS = "AA:BB:CC:DD:EE:FF"


class FakeBLEDevice:
    """Small BLEDevice stand-in for factory/CLI tests."""

    def __init__(self, name: str = "DYMIXR-test", address: str = TEST_ADDRESS) -> None:
        """Initialize fake BLE metadata."""
        self.name = name
        self.address = address


def test_detect_model_recognizes_stirrer_prefix() -> None:
    """DYMIXR advertisement names resolve to the Mag Stirrer model."""
    model = detect_model("DYMIXRCFAA123456")
    assert model.name == "Mag Stirrer"
    assert model is MAG_STIRRER


def test_create_device_builds_stirrer_client() -> None:
    """DYMIXR devices get a ChihirosMagStirrer, which is a dosing pump client."""

    async def run() -> ChihirosDevice:
        return create_device(FakeBLEDevice())  # type: ignore[arg-type]

    device = asyncio.run(run())
    assert isinstance(device, ChihirosMagStirrer)
    assert isinstance(device, ChihirosDosingPump)
    assert isinstance(device, ChihirosDevice)
    assert device.model_name == "Mag Stirrer"


def _fast_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove notification sleeps so scripted sessions run quickly."""
    from chihiros_led_control import client as client_module

    monkeypatch.setattr(client_module, "COMMAND_NOTIFICATION_WAIT", 0.0)
    monkeypatch.setattr(client_module, "STATUS_NOTIFICATION_WAIT", 0.0)
    monkeypatch.setattr(client_module, "BATCH_WRITE_DELAY", 0.0)


def _make_stirrer(transport: ScriptedTransport) -> ChihirosMagStirrer:
    return ChihirosMagStirrer(ScriptedBLEDevice(transport.name, transport.address), MAG_STIRRER)


def test_scripted_stirrer_pre_second_and_manual_stir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Speed/pre-stir uses (0xA5, 42); manual start/stop uses (0xA5, 20)."""
    transport = ScriptedTransport(name="DYMIXR-test")
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = _make_stirrer(transport)
        with transport.patch_establish_connection():
            await device.set_pre_second(0, 90, speed=60)
            await device.stir(2, True)
            await device.stir(2, False, seconds=300)

        modes = [frame[5] for frame in transport.writes]
        assert modes.count(42) == 1
        assert modes.count(20) == 2
        pre_second = next(frame for frame in transport.writes if frame[5] == 42)
        assert pre_second[6:10] == bytes([0, 0, 90, 60])
        start, stop = (frame for frame in transport.writes if frame[5] == 20)
        assert list(start[6:-1]) == [255, 255, 1, 255, 255, 255, 255, 255, 255, 255]
        assert list(stop[6:-1]) == [255, 255, 0, 255, 255, 255, 255, 255, 5, 0]

    asyncio.run(run())


def test_scripted_stirrer_schedule_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    """set_stir_schedule mirrors the app: active frame, dosingSet, timer points."""
    transport = ScriptedTransport(name="DYMIXR-test")
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = _make_stirrer(transport)
        points = [
            DosingWorkPoint(8, 0, volume_ml=stirrer_dosage_for_minutes(10)),
            DosingWorkPoint(20, 30, volume_ml=stirrer_dosage_for_minutes(5)),
        ]
        with transport.patch_establish_connection():
            await device.set_stir_schedule(1, points, frequency=127)

        dosing_frames = [frame for frame in transport.writes if frame[0] == 165]
        assert [frame[5] for frame in dosing_frames] == [32, 27, 21]
        active_frame, dosing_set, schedule = dosing_frames
        assert active_frame[6:9] == bytes([1, 0, 1])  # channel, compensate=0, active=1
        assert dosing_set[6:12] == bytes([1, 127, 1, 0, 0, 0])  # daily volume 0, first setting
        # Timer points: 10 minutes -> 6.0 mL -> 60 tenths; 5 minutes -> 30 tenths.
        assert list(schedule[6:-1]) == [1, 3, 8, 0, 0, 60, 20, 30, 0, 30]

    asyncio.run(run())


def test_scripted_stirrer_inactive_channel_skips_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    """An inactive stirrer channel gets no schedule frames (§6.2: only if is_active != 0)."""
    transport = ScriptedTransport(name="DYMIXR-test")
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = _make_stirrer(transport)
        points = [DosingWorkPoint(8, 0, volume_ml=stirrer_dosage_for_minutes(10))]
        with transport.patch_establish_connection():
            await device.set_stir_schedule(0, points, frequency=127, active=False)

        dosing_frames = [frame for frame in transport.writes if frame[0] == 165]
        assert [frame[5] for frame in dosing_frames] == [32, 27]
        assert dosing_frames[0][6:9] == bytes([0, 0, 0])  # active=0

    asyncio.run(run())


def test_scripted_pump_schedule_and_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pump schedule/settings/calibration/reset commands write the right frames."""
    transport = ScriptedTransport(name="DYDOSE-test")
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = ChihirosDosingPump(ScriptedBLEDevice(transport.name, transport.address), DOSING_PUMP)
        with transport.patch_establish_connection():
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


def test_calibrate_channel_requires_arguments() -> None:
    """calibrate_channel refuses to send a no-op calibration frame."""
    transport = ScriptedTransport(name="DYDOSE-test")

    async def run() -> None:
        device = ChihirosDosingPump(ScriptedBLEDevice(transport.name, transport.address), DOSING_PUMP)
        with pytest.raises(ValueError, match="seconds or volume_ml"):
            await device.calibrate_channel(0)

    asyncio.run(run())


def test_stir_cli_commands_drive_stirrer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stir commands convert user channel numbers and call the client API."""
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def _record(name: str):
        async def method(*args: object, **kwargs: object) -> None:
            calls.append((name, args, kwargs))

        return method

    async def get_device_from_address(address: str) -> ChihirosDevice:
        assert address == TEST_ADDRESS
        device = ChihirosMagStirrer(FakeBLEDevice(), MAG_STIRRER)  # type: ignore[arg-type]
        device.stir = _record("stir")  # type: ignore[method-assign]
        device.set_pre_second = _record("set_pre_second")  # type: ignore[method-assign]
        device.set_stir_schedule = _record("set_stir_schedule")  # type: ignore[method-assign]
        return device

    monkeypatch.setattr(cli, "get_device_from_address", get_device_from_address)

    assert RUNNER.invoke(cli.app, ["stir-on", TEST_ADDRESS, "3"]).exit_code == 0
    assert RUNNER.invoke(cli.app, ["stir-on", TEST_ADDRESS, "3", "--seconds", "600"]).exit_code == 0
    assert RUNNER.invoke(cli.app, ["stir-off", TEST_ADDRESS, "3"]).exit_code == 0
    assert RUNNER.invoke(cli.app, ["stir-speed", TEST_ADDRESS, "1", "60", "--pre-seconds", "30"]).exit_code == 0
    result = RUNNER.invoke(cli.app, ["stir-schedule", TEST_ADDRESS, "2", "08:00:10", "20:30:5"])
    assert result.exit_code == 0

    assert calls[0] == ("stir", (2, True), {"seconds": None})
    assert calls[1] == ("stir", (2, True), {"seconds": 600})
    assert calls[2] == ("stir", (2, False), {})
    assert calls[3] == ("set_pre_second", (0, 30, 60), {})
    assert calls[4][0] == "set_stir_schedule"
    points = calls[4][1][1]
    assert [point.start_hour for point in points] == [8, 20]  # type: ignore[attr-defined]
    assert points[0].volume_ml == pytest.approx(6.0)  # type: ignore[attr-defined]


def test_stir_cli_commands_reject_dosing_pump(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stir commands fail clearly when pointed at a dosing pump."""

    async def get_device_from_address(address: str) -> ChihirosDevice:
        assert address == TEST_ADDRESS
        return ChihirosDosingPump(FakeBLEDevice(name="DYDOSE-test"), DOSING_PUMP)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "get_device_from_address", get_device_from_address)

    result = RUNNER.invoke(cli.app, ["stir-on", TEST_ADDRESS, "1"])

    assert result.exit_code != 0
    assert "not a magnetic stirrer" in result.output


def test_doser_cli_commands_drive_pump(monkeypatch: pytest.MonkeyPatch) -> None:
    """Doser CLI commands parse arguments and call the client API."""
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def _record(name: str):
        async def method(*args: object, **kwargs: object) -> None:
            calls.append((name, args, kwargs))

        return method

    async def get_device_from_address(address: str) -> ChihirosDevice:
        assert address == TEST_ADDRESS
        device = ChihirosDosingPump(FakeBLEDevice(name="DYDOSE-test"), DOSING_PUMP)  # type: ignore[arg-type]
        device.set_schedule = _record("set_schedule")  # type: ignore[method-assign]
        device.reset_channel = _record("reset_channel")  # type: ignore[method-assign]
        device.calibrate_channel = _record("calibrate_channel")  # type: ignore[method-assign]
        device.set_dose_delay = _record("set_dose_delay")  # type: ignore[method-assign]
        return device

    monkeypatch.setattr(cli, "get_device_from_address", get_device_from_address)

    timer = RUNNER.invoke(cli.app, ["doser-schedule", TEST_ADDRESS, "1", "08:00:5.5", "20:00:5.5"])
    free = RUNNER.invoke(cli.app, ["doser-schedule", TEST_ADDRESS, "2", "08:00-10:00:3", "--mode", "free"])
    assert timer.exit_code == 0 and free.exit_code == 0
    assert RUNNER.invoke(cli.app, ["doser-reset-channel", TEST_ADDRESS, "4"]).exit_code == 0
    assert RUNNER.invoke(cli.app, ["doser-calibrate", TEST_ADDRESS, "1", "--seconds", "10"]).exit_code == 0
    assert RUNNER.invoke(cli.app, ["doser-delay", TEST_ADDRESS, "--disable"]).exit_code == 0

    _channel, mode, points = calls[0][1]
    assert mode is DosingMode.TIMER  # type: ignore[comparison-overlap]
    assert [(point.start_hour, point.volume_ml) for point in points] == [(8, 5.5), (20, 5.5)]  # type: ignore[attr-defined]
    _channel, mode, points = calls[1][1]
    assert mode is DosingMode.FREE  # type: ignore[comparison-overlap]
    assert points[0].duration_minutes == 120 and points[0].number == 3  # type: ignore[attr-defined]
    assert calls[2] == ("reset_channel", (3,), {})
    assert calls[3] == ("calibrate_channel", (0,), {"seconds": 10, "volume_ml": None})
    assert calls[4] == ("set_dose_delay", (False,), {})


def test_doser_cli_rejects_bad_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """Doser CLI commands validate mode names, point syntax, and device type."""

    async def get_pump(address: str) -> ChihirosDevice:
        assert address == TEST_ADDRESS
        return ChihirosDosingPump(FakeBLEDevice(name="DYDOSE-test"), DOSING_PUMP)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "get_device_from_address", get_pump)

    bad_mode = RUNNER.invoke(cli.app, ["doser-schedule", TEST_ADDRESS, "1", "08:00:5", "--mode", "daily"])
    bad_point = RUNNER.invoke(cli.app, ["doser-schedule", TEST_ADDRESS, "1", "8am"])
    bad_calibrate = RUNNER.invoke(cli.app, ["doser-calibrate", TEST_ADDRESS, "1"])

    async def get_light(address: str) -> ChihirosDevice:
        assert address == TEST_ADDRESS
        return ChihirosDevice(FakeBLEDevice(name="DYA-test"), detect_model("DYA-test"))  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "get_device_from_address", get_light)
    wrong_device = RUNNER.invoke(cli.app, ["doser-delay", TEST_ADDRESS])

    assert bad_mode.exit_code != 0 and "single/auto/free/timer" in bad_mode.output
    assert bad_point.exit_code != 0 and "HH:MM:ML" in bad_point.output
    assert bad_calibrate.exit_code != 0
    assert wrong_device.exit_code != 0 and "not a dosing pump" in wrong_device.output


def test_scripted_program_channel_is_one_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """program_channel batches active + daily + schedule frames in one session."""
    transport = ScriptedTransport(name="DYMIXR-test")
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = _make_stirrer(transport)
        with transport.patch_establish_connection():
            await device.program_channel(
                1,
                active=True,
                compensate=False,
                dose_per_day_ml=60.0,
                frequency=127,
                is_first_setting=True,
                mode=DosingMode.TIMER,
                points=[DosingWorkPoint(8, 0, volume_ml=2.5), DosingWorkPoint(20, 30, volume_ml=1.0)],
            )

        # All four frames go out in a single _send_command transaction: the
        # sequence shares one message-id run and one connection (prelude
        # frames from the connection setup are filtered out). Timer mode
        # batches both points into ONE (0xA5, 21) frame (flush only when the
        # accumulator exceeds 50 bytes).
        command_frames = [frame for frame in transport.writes if frame[5] in (32, 27, 21)]
        modes = [frame[5] for frame in command_frames]
        assert modes == [32, 27, 21]
        daily = command_frames[1]
        assert list(daily[6:-1]) == [1, 127, 1, 0, 2, 88]  # 60.0 mL -> 600 tenths
        schedule = command_frames[2]
        assert list(schedule[6:-1]) == [1, 3, 8, 0, 0, 25, 20, 30, 0, 10]

    asyncio.run(run())

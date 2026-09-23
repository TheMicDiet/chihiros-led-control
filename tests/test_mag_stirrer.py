"""Tests for magnetic-stirrer support: model detection, client API, and CLI."""

from __future__ import annotations

import asyncio

import pytest
from typer.testing import CliRunner

from chihiros_led_control import cli
from chihiros_led_control.devices import ChihirosDevice, ChihirosDosingPump, ChihirosMagStirrer
from chihiros_led_control.factory import create_device, detect_model
from chihiros_led_control.protocol.dosing import DosingMode, DosingWorkPoint
from chihiros_led_control.protocol.stirrer import stirrer_dosage_for_minutes
from chihiros_led_control.registry import DOSING_PUMP, MAG_STIRRER
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
    """DYMIXR devices get an independent magnetic-stirrer client."""

    async def run() -> ChihirosMagStirrer:
        return create_device(ScriptedBLEDevice("DYMIXR-test", "AA:BB:CC:DD:EE:FF"))

    device = asyncio.run(run())
    assert isinstance(device, ChihirosMagStirrer)
    assert not isinstance(device, ChihirosDosingPump)
    assert not isinstance(device, ChihirosDevice)
    assert device.model_name == "Mag Stirrer"


def _fast_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove notification sleeps so scripted sessions run quickly."""
    from chihiros_led_control import transport as transport_module
    from chihiros_led_control.devices import base as client_module

    monkeypatch.setattr(client_module, "COMMAND_NOTIFICATION_WAIT", 0.0)
    monkeypatch.setattr(client_module, "STATUS_NOTIFICATION_WAIT", 0.0)
    monkeypatch.setattr(transport_module, "BATCH_WRITE_DELAY", 0.0)


def _make_stirrer(transport: ScriptedTransport) -> ChihirosMagStirrer:
    return ChihirosMagStirrer(ScriptedBLEDevice(transport.name, transport.address), MAG_STIRRER, transport=transport)


def test_scripted_stirrer_pre_second_and_manual_stir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Speed/pre-stir uses (0xA5, 42); manual start/stop uses (0xA5, 20)."""
    transport = ScriptedTransport(name="DYMIXR-test")
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = _make_stirrer(transport)
        if transport:
            await device.set_pre_second(0, 90, speed=60)
            await device.stir(2, True)
            await device.stir(2, False, seconds=300)

        modes = [frame[5] for frame in transport.writes]
        assert modes.count(42) == 1
        assert modes.count(20) == 2
        pre_second = next(frame for frame in transport.writes if frame[5] == 42)
        assert pre_second[6:10] == bytes([0, 0, 90, 60])
        start, stop = (frame for frame in transport.writes if frame[5] == 20)
        assert list(start[6:-1]) == [255, 255, 255, 255, 1, 255, 255, 255, 255, 255]
        assert list(stop[6:-1]) == [5, 0, 255, 255, 0, 255, 255, 255, 255, 255]

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
        if transport:
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
        if transport:
            await device.set_stir_schedule(0, points, frequency=127, active=False)

        dosing_frames = [frame for frame in transport.writes if frame[0] == 165]
        assert [frame[5] for frame in dosing_frames] == [32, 27]
        assert dosing_frames[0][6:9] == bytes([0, 0, 0])  # active=0

    asyncio.run(run())


def test_program_channel_replays_inactive_stirrer_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    """Master/slave replay sends settings and schedule even for an inactive channel."""
    transport = ScriptedTransport(name="DYMIXR-test")
    _fast_waits(monkeypatch)
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


def test_scripted_pump_schedule_and_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pump schedule/settings/calibration/reset commands write the right frames."""
    transport = ScriptedTransport(name="DYDOSE-test")
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = ChihirosDosingPump(
            ScriptedBLEDevice(transport.name, transport.address),
            DOSING_PUMP,
            transport=transport,
        )
        if transport:
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

    assert RUNNER.invoke(cli.app, ["stirrer", "on", TEST_ADDRESS, "3"]).exit_code == 0
    assert RUNNER.invoke(cli.app, ["stirrer", "on", TEST_ADDRESS, "3", "--seconds", "600"]).exit_code == 0
    assert RUNNER.invoke(cli.app, ["stirrer", "off", TEST_ADDRESS, "3"]).exit_code == 0
    assert RUNNER.invoke(cli.app, ["stirrer", "speed", TEST_ADDRESS, "1", "60", "--pre-seconds", "30"]).exit_code == 0
    result = RUNNER.invoke(cli.app, ["stirrer", "schedule", TEST_ADDRESS, "2", "08:00:10", "20:30:5"])
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

    result = RUNNER.invoke(cli.app, ["stirrer", "on", TEST_ADDRESS, "1"])

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
        device.reset_total_dosed = _record("reset_total_dosed")  # type: ignore[method-assign]
        device.calibrate_channel = _record("calibrate_channel")  # type: ignore[method-assign]
        device.set_dose_delay = _record("set_dose_delay")  # type: ignore[method-assign]
        return device

    monkeypatch.setattr(cli, "get_device_from_address", get_device_from_address)

    timer = RUNNER.invoke(cli.app, ["dosing", "schedule", TEST_ADDRESS, "1", "08:00:5.5", "20:00:5.5"])
    free = RUNNER.invoke(cli.app, ["dosing", "schedule", TEST_ADDRESS, "2", "08:00-10:00:3", "--mode", "free"])
    assert timer.exit_code == 0 and free.exit_code == 0
    assert RUNNER.invoke(cli.app, ["dosing", "reset", TEST_ADDRESS, "4"]).exit_code == 0
    assert RUNNER.invoke(cli.app, ["dosing", "reset", TEST_ADDRESS, "2", "--totals"]).exit_code == 0
    assert RUNNER.invoke(cli.app, ["dosing", "calibrate", TEST_ADDRESS, "1", "--seconds", "10"]).exit_code == 0
    assert RUNNER.invoke(cli.app, ["dosing", "delay", TEST_ADDRESS, "--disable"]).exit_code == 0

    _channel, mode, points = calls[0][1]
    assert mode is DosingMode.TIMER  # type: ignore[comparison-overlap]
    assert [(point.start_hour, point.volume_ml) for point in points] == [(8, 5.5), (20, 5.5)]  # type: ignore[attr-defined]
    _channel, mode, points = calls[1][1]
    assert mode is DosingMode.FREE  # type: ignore[comparison-overlap]
    assert points[0].duration_minutes == 120 and points[0].number == 3  # type: ignore[attr-defined]
    assert calls[2] == ("reset_channel", (3,), {})
    assert calls[3] == ("reset_total_dosed", (1,), {})
    assert calls[4] == ("calibrate_channel", (0,), {"seconds": 10, "volume_ml": None})
    assert calls[5] == ("set_dose_delay", (False,), {})


def test_dosing_program_cli_batches_in_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """The program command forwards active/daily/schedule options to program_channel."""
    calls: list[tuple[int, dict[str, object]]] = []

    async def get_device(address: str) -> ChihirosDevice:
        assert address == TEST_ADDRESS
        device = ChihirosDosingPump(FakeBLEDevice(name="DYDOSE-test"), DOSING_PUMP)  # type: ignore[arg-type]

        async def program_channel(channel: int, **kwargs: object) -> None:
            calls.append((channel, kwargs))

        device.program_channel = program_channel  # type: ignore[method-assign]
        return device

    monkeypatch.setattr(cli, "get_device_from_address", get_device)

    result = RUNNER.invoke(
        cli.app,
        [
            "dosing",
            "program",
            TEST_ADDRESS,
            "2",
            "08:00:5.5",
            "--daily-ml",
            "60",
            "--weekdays",
            "monday",
            "--weekdays",
            "friday",
            "--disable",
            "--compensate",
        ],
    )

    assert result.exit_code == 0
    channel, kwargs = calls[0]
    assert channel == 1
    assert kwargs["active"] is False
    assert kwargs["compensate"] is True
    assert kwargs["dose_per_day_ml"] == 60.0
    assert kwargs["frequency"] == 68  # monday (64) + friday (4)
    assert kwargs["is_first_setting"] is True
    assert kwargs["mode"] is DosingMode.TIMER
    points = kwargs["points"]
    assert [(point.start_hour, point.volume_ml) for point in points] == [(8, 5.5)]  # type: ignore[attr-defined]


def test_dosing_cli_rejects_stirrer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pump commands refuse a stirrer, which speaks the dosing protocol but is not a pump."""

    async def get_device(address: str) -> ChihirosDevice:
        assert address == TEST_ADDRESS
        return ChihirosMagStirrer(FakeBLEDevice(), MAG_STIRRER)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "get_device_from_address", get_device)

    result = RUNNER.invoke(cli.app, ["dosing", "delay", TEST_ADDRESS])

    assert result.exit_code != 0
    assert "not a dosing pump" in result.output


def test_stirrer_schedule_cli_encodes_weekdays(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stirrer schedule converts the selected weekdays to the repetition bitmask."""
    calls: list[dict[str, object]] = []

    async def get_device(address: str) -> ChihirosDevice:
        assert address == TEST_ADDRESS
        device = ChihirosMagStirrer(FakeBLEDevice(), MAG_STIRRER)  # type: ignore[arg-type]

        async def set_stir_schedule(channel: int, points: object, **kwargs: object) -> None:
            calls.append(kwargs)

        device.set_stir_schedule = set_stir_schedule  # type: ignore[method-assign]
        return device

    monkeypatch.setattr(cli, "get_device_from_address", get_device)

    result = RUNNER.invoke(
        cli.app,
        ["stirrer", "schedule", TEST_ADDRESS, "1", "08:00:10", "--weekdays", "monday", "--weekdays", "friday"],
    )

    assert result.exit_code == 0
    assert calls[0]["frequency"] == 68  # monday (64) + friday (4)
    assert calls[0]["active"] is True


def test_doser_query_cli_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    """doser-totals/doser-today query the device and print the reported volumes."""
    from chihiros_led_control.protocol.notifications import DosingDailyNotification, DosingTotalsNotification

    queried: list[str] = []

    async def get_device(address: str) -> ChihirosDevice:
        assert address == TEST_ADDRESS
        device = ChihirosDosingPump(FakeBLEDevice(name="DYDOSE-test"), DOSING_PUMP)  # type: ignore[arg-type]

        async def query_totals() -> None:
            queried.append("totals")
            device.last_dosing_totals_notification = DosingTotalsNotification((105500, 0), b"")

        async def query_today() -> None:
            queried.append("today")
            device.last_dosing_daily_notification = DosingDailyNotification((2500, 0), b"")

        device.query_dosed_totals = query_totals  # type: ignore[method-assign]
        device.query_dosed_today = query_today  # type: ignore[method-assign]
        return device

    monkeypatch.setattr(cli, "get_device_from_address", get_device)

    totals = RUNNER.invoke(cli.app, ["dosing", "totals", TEST_ADDRESS])
    today = RUNNER.invoke(cli.app, ["dosing", "today", TEST_ADDRESS])

    assert totals.exit_code == 0 and today.exit_code == 0
    assert queried == ["totals", "today"]
    assert "105.5" in totals.output and "2.5" in today.output


def test_doser_cli_rejects_out_of_range_points(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point volumes/minutes outside the wire ranges fail cleanly, without a traceback."""

    async def get_device(address: str) -> ChihirosDevice:
        assert address == TEST_ADDRESS
        return ChihirosDosingPump(FakeBLEDevice(name="DYDOSE-test"), DOSING_PUMP)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "get_device_from_address", get_device)

    bad_ml = RUNNER.invoke(cli.app, ["dosing", "schedule", TEST_ADDRESS, "1", "08:00:99999"])
    bad_stir = RUNNER.invoke(cli.app, ["stirrer", "schedule", TEST_ADDRESS, "1", "08:00:0"])

    assert bad_ml.exit_code != 0 and "Dose volume" in bad_ml.output
    assert bad_stir.exit_code != 0 and "Stir run time" in bad_stir.output


def test_doser_cli_rejects_bad_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """Doser CLI commands validate mode names, point syntax, and device type."""

    async def get_pump(address: str) -> ChihirosDevice:
        assert address == TEST_ADDRESS
        return ChihirosDosingPump(FakeBLEDevice(name="DYDOSE-test"), DOSING_PUMP)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "get_device_from_address", get_pump)

    bad_mode = RUNNER.invoke(cli.app, ["dosing", "schedule", TEST_ADDRESS, "1", "08:00:5", "--mode", "daily"])
    bad_point = RUNNER.invoke(cli.app, ["dosing", "schedule", TEST_ADDRESS, "1", "8am"])
    bad_calibrate = RUNNER.invoke(cli.app, ["dosing", "calibrate", TEST_ADDRESS, "1"])

    async def get_light(address: str) -> ChihirosDevice:
        assert address == TEST_ADDRESS
        return ChihirosDevice(FakeBLEDevice(name="DYA-test"), detect_model("DYA-test"))  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "get_device_from_address", get_light)
    wrong_device = RUNNER.invoke(cli.app, ["dosing", "delay", TEST_ADDRESS])

    assert bad_mode.exit_code != 0 and "single/auto/free/timer" in bad_mode.output
    assert bad_point.exit_code != 0 and "HH:MM:ML" in bad_point.output
    assert bad_calibrate.exit_code != 0
    assert wrong_device.exit_code != 0 and "not a dosing pump" in wrong_device.output

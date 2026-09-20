"""Tests for heater support: model detection, client API, protocol, and CLI."""

from __future__ import annotations

import asyncio

import pytest
from typer.testing import CliRunner

from chihiros_led_control import cli
from chihiros_led_control.client import ChihirosDevice, ChihirosHeater
from chihiros_led_control.commands import (
    create_heater_backlight_command,
    create_heater_calibrate_command,
    create_heater_protector_temperature_command,
    create_heater_set_command,
    encode_heater_power_watts,
    split_heater_temperature,
)
from chihiros_led_control.factory import create_device, detect_model
from chihiros_led_control.models import HEATER
from chihiros_led_control.protocol import (
    HeaterStatusNotification,
    HeaterTemperatureNotification,
    heater_alarm_names,
    parse_notification,
)
from chihiros_led_control.testing import ScriptedBLEDevice, ScriptedTransport

RUNNER = CliRunner()
TEST_ADDRESS = "AA:BB:CC:DD:EE:FF"


class FakeBLEDevice:
    """Small BLEDevice stand-in for factory/CLI tests."""

    def __init__(self, name: str = "DYHET-test", address: str = TEST_ADDRESS) -> None:
        """Initialize fake BLE metadata."""
        self.name = name
        self.address = address


def _heater_temp_frame(setting_tenths: int, current_tenths: int) -> bytes:
    """Build a 0x5B/0x25 heater temperature frame."""
    frame = bytearray(13)
    frame[0] = 0x5B
    frame[1] = 0x0F
    frame[5] = 0x25
    frame[6] = setting_tenths >> 8
    frame[7] = setting_tenths & 0xFF
    frame[10] = current_tenths >> 8
    frame[11] = current_tenths & 0xFF
    return bytes(frame)


def _heater_status_frame(*, firmware: int, work_hours: int, alarms: int) -> bytes:
    """Build a 16-byte 0x5B/0x0A heater status frame."""
    frame = bytearray(16)
    frame[0] = 0x5B
    frame[5] = 0x0A
    frame[7] = work_hours >> 8
    frame[8] = work_hours & 0xFF
    frame[11] = firmware >> 8
    frame[12] = firmware & 0xFF
    frame[14] = alarms
    return bytes(frame)


def test_detect_model_recognizes_heater_prefixes() -> None:
    """Both DYHET and DYH1T advertisement names resolve to the Heater model."""
    for name in ("DYHET1234567890AB", "DYH1T123456"):
        model = detect_model(name)
        assert model.name == "Heater"
        assert model is HEATER
        assert model.is_heater
        assert dict(model.color_channels) == {}


def test_create_device_builds_heater_client() -> None:
    """DYHET devices get a ChihirosHeater client."""

    async def run() -> ChihirosDevice:
        return create_device(FakeBLEDevice())  # type: ignore[arg-type]

    device = asyncio.run(run())
    assert isinstance(device, ChihirosHeater)
    assert isinstance(device, ChihirosDevice)
    assert device.model_name == "Heater"


def test_heater_temperature_and_power_encoding() -> None:
    """Temperatures split into whole degrees plus hundredths; power rides as watts ÷ 10."""
    assert split_heater_temperature(25.5) == (25, 50)
    assert split_heater_temperature(36.9) == (36, 90)
    assert split_heater_temperature(24.0) == (24, 0)
    assert split_heater_temperature(25.999) == (26, 0)
    assert encode_heater_power_watts(800) == 80
    with pytest.raises(ValueError, match="divisible by 10"):
        encode_heater_power_watts(805)
    with pytest.raises(ValueError, match="temperature"):
        split_heater_temperature(-1.0)
    with pytest.raises(ValueError, match="power"):
        encode_heater_power_watts(3000)


def test_heater_frames_match_captured_app_traffic() -> None:
    """The heater frames reproduce the byte-exact frames captured from the app.

    Source: a capture of the vendor app driving a DYHET heater
    (``setHeaterCode`` defaults, the protection-temperature slider, calibration
    and the backlight toggle).
    """
    assert (
        create_heater_set_command((0, 5), auto=False, temperature_c=25.0, power_watts=200).hex(" ")
        == "5a 01 09 00 05 2b 00 19 00 14 2b"
    )
    assert (
        create_heater_set_command((0, 5), auto=True, temperature_c=20.0, power_watts=500).hex(" ")
        == "5a 01 09 00 05 2b 01 14 00 32 01"
    )
    assert create_heater_protector_temperature_command((0, 5), 36.9).hex(" ") == "5a 01 07 00 05 2f 24 5a 52"
    assert create_heater_protector_temperature_command((0, 5), 36.5).hex(" ") == "5a 01 07 00 05 2f 24 32 3a"
    assert create_heater_calibrate_command((0, 5), 23.0).hex(" ") == "5a 01 07 00 05 30 17 00 24"
    assert create_heater_backlight_command((0, 7), enabled=True).hex(" ") == "a5 01 0a 00 07 38 64 64 64 64 7f 4b"
    assert create_heater_backlight_command((0, 7), enabled=False).hex(" ") == "a5 01 0a 00 07 38 c8 c8 c8 c8 7f 4b"


def _fast_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove notification sleeps so scripted sessions run quickly."""
    from chihiros_led_control import client as client_module

    monkeypatch.setattr(client_module, "COMMAND_NOTIFICATION_WAIT", 0.0)
    monkeypatch.setattr(client_module, "STATUS_NOTIFICATION_WAIT", 0.0)
    monkeypatch.setattr(client_module, "BATCH_WRITE_DELAY", 0.0)


def _make_heater(transport: ScriptedTransport) -> ChihirosHeater:
    return ChihirosHeater(ScriptedBLEDevice(transport.name, transport.address), HEATER)


def _sent_frames(transport: ScriptedTransport) -> list[tuple[int, list[int]]]:
    """Return the (mode, payload) pairs of the heater commands actually sent.

    The connection prelude (``(0x5A, 4)`` device info and ``(0x5A, 9)`` time
    sync) is skipped so assertions only see the commands under test.
    """
    return [(frame[5], list(frame[6:-1])) for frame in transport.writes if frame[5] not in (4, 9)]


def test_scripted_heater_manual_state_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting the temperature or power sends switchToManual plus the state frame."""
    transport = ScriptedTransport(name="DYHET-test")
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = _make_heater(transport)
        with transport.patch_establish_connection():
            await device.set_temperature(26.5)
            await device.set_power(800)

        state_frames = [frame for frame in transport.writes if frame[5] == 43]
        assert [list(frame[6:-1]) for frame in state_frames] == [
            [0, 26, 50, 20],  # manual flag, 26.5 °C, default 200 W
            [0, 26, 50, 80],  # 26.5 °C is remembered across the power write
        ]
        # Every state write is preceded by switchToManual (mode 5, sub 11).
        manual_frames = [frame for frame in transport.writes if frame[5] == 5]
        assert [list(frame[6:-1]) for frame in manual_frames] == [[11, 255, 255], [11, 255, 255]]

    asyncio.run(run())


def test_concurrent_manual_setters_preserve_both_values() -> None:
    """A paired manual update captures its sibling only after prior commits."""

    async def run() -> None:
        device = ChihirosHeater(FakeBLEDevice(), HEATER)  # type: ignore[arg-type]
        sent_states: list[list[int]] = []
        first_write_started = asyncio.Event()
        release_first_write = asyncio.Event()

        async def capture_locked(commands_to_send: list[bytes], attempts: int, notification_wait: float) -> None:
            del attempts, notification_wait
            state_frame = next(command for command in commands_to_send if command[5] == 43)
            sent_states.append(list(state_frame[6:-1]))
            if len(sent_states) == 1:
                first_write_started.set()
                await release_first_write.wait()

        device._send_command_locked = capture_locked  # type: ignore[method-assign]

        temperature_task = asyncio.create_task(device.set_temperature(26.5))
        await first_write_started.wait()
        power_task = asyncio.create_task(device.set_power(800))
        await asyncio.sleep(0)
        release_first_write.set()
        await asyncio.gather(temperature_task, power_task)

        assert sent_states == [
            [0, 26, 50, 20],
            [0, 26, 50, 80],
        ]
        assert device.setting_temperature_celsius == pytest.approx(26.5)
        assert device.power_watts == 800

    asyncio.run(run())


def test_scripted_heater_settings_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auto defaults, mode switches, unit, protector and reset write their frames."""
    transport = ScriptedTransport(name="DYHET-test")
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = _make_heater(transport)
        with transport.patch_establish_connection():
            await device.set_auto_defaults(24.0, 1000)
            await device.set_manual_mode()
            await device.set_auto_mode()
            await device.apply_scene()
            await device.set_auto_heating(True)
            await device.set_auto_heating(False)
            await device.set_temperature_unit(celsius=False)
            await device.set_backlight(False)
            await device.set_backlight(True)
            await device.set_protector_temperature(37.0)
            await device.calibrate(25.5)
            await device.reset_work_time()

        assert _sent_frames(transport) == [
            (43, [1, 24, 0, 100]),
            (5, [11, 255, 255]),
            (5, [3, 255, 255]),
            (5, [18, 255, 255]),
            (5, [46, 255, 255]),
            (5, [47, 255, 255]),
            (5, [45, 255, 255]),
            (56, [200, 200, 200, 200, 127]),
            (56, [100, 100, 100, 100, 127]),
            (47, [37, 0]),
            (48, [25, 50]),
            (5, [58, 255, 255]),
        ]
        assert device.auto_heating is False
        assert device.is_celsius is False
        assert device.backlight is True
        assert device.protector_temperature_celsius == 37.0

    asyncio.run(run())


def test_scripted_heater_auto_defaults_track_the_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-value auto-default writes resend the sibling from the app's defaults."""
    transport = ScriptedTransport(name="DYHET-test")
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = _make_heater(transport)
        with transport.patch_establish_connection():
            await device.set_auto_default_temperature(22.5)
            await device.set_auto_default_power(800)

        assert _sent_frames(transport) == [
            (43, [1, 22, 50, 50]),  # 22.5 °C at the app's 500 W default
            (43, [1, 22, 50, 80]),  # the temperature is resent with the new power
        ]
        assert device.auto_default_temperature_celsius == pytest.approx(22.5)
        assert device.auto_default_power_watts == 800

    asyncio.run(run())


def test_concurrent_auto_default_setters_preserve_both_values() -> None:
    """A paired auto-default update captures its sibling after prior commits."""

    async def run() -> None:
        device = ChihirosHeater(FakeBLEDevice(), HEATER)  # type: ignore[arg-type]
        sent_states: list[list[int]] = []
        first_write_started = asyncio.Event()
        release_first_write = asyncio.Event()

        async def capture_locked(commands_to_send: list[bytes], attempts: int, notification_wait: float) -> None:
            del attempts, notification_wait
            sent_states.append(list(commands_to_send[0][6:-1]))
            if len(sent_states) == 1:
                first_write_started.set()
                await release_first_write.wait()

        device._send_command_locked = capture_locked  # type: ignore[method-assign]

        temperature_task = asyncio.create_task(device.set_auto_default_temperature(22.5))
        await first_write_started.wait()
        power_task = asyncio.create_task(device.set_auto_default_power(800))
        await asyncio.sleep(0)
        release_first_write.set()
        await asyncio.gather(temperature_task, power_task)

        assert sent_states == [
            [1, 22, 50, 50],
            [1, 22, 50, 80],
        ]
        assert device.auto_default_temperature_celsius == pytest.approx(22.5)
        assert device.auto_default_power_watts == 800

    asyncio.run(run())


def test_restored_heater_pairs_drive_real_client_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restored write-only values replace fresh defaults without touching BLE."""
    transport = ScriptedTransport(name="DYHET-test")
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = _make_heater(transport)
        with transport.patch_establish_connection():
            device.restore_setting_temperature(27.5)
            device.restore_manual_power(800)
            device.restore_auto_default_temperature(22.5)
            device.restore_auto_default_power(900)
            assert transport.connections == 0
            assert transport.writes == []

            await device.set_power(600)
            await device.set_temperature(26.5)
            await device.set_auto_default_temperature(23.0)

        assert _sent_frames(transport) == [
            (5, [11, 255, 255]),
            (43, [0, 27, 50, 60]),
            (5, [11, 255, 255]),
            (43, [0, 26, 50, 60]),
            (43, [1, 23, 0, 90]),
        ]

    asyncio.run(run())


def test_scripted_heater_notifications_track_device_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two 0x5B heater frames populate the client's reported state."""
    transport = ScriptedTransport(name="DYHET-test")
    _fast_waits(monkeypatch)
    temperature_frame = _heater_temp_frame(266, 258)
    status_frame = _heater_status_frame(firmware=0x0F1B, work_hours=1994, alarms=0x09)
    transport.expect(90, 4, [1], respond=[temperature_frame, status_frame])

    async def run() -> None:
        device = _make_heater(transport)
        with transport.patch_establish_connection():
            await device.query_status()

        assert device.last_heater_temperature_notification is not None
        assert device.setting_temperature_celsius == pytest.approx(26.6)
        assert device.current_temperature_celsius == pytest.approx(25.8)
        assert device.last_heater_status_notification is not None
        assert device.firmware_version == 0x0F1B
        assert device.work_time_hours == 1994
        assert device.heater_alarms == ("insufficient_water", "needs_cleaning")

    asyncio.run(run())


def test_scripted_heater_reported_setting_is_resent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reported setting temperature overrides the default the next power write uses."""
    transport = ScriptedTransport(name="DYHET-test")
    _fast_waits(monkeypatch)
    transport.expect(90, 4, [1], respond=[_heater_temp_frame(285, 280)])

    async def run() -> None:
        device = _make_heater(transport)
        with transport.patch_establish_connection():
            await device.query_status()
            await device.set_power(400)

        state_frame = next(frame for frame in transport.writes if frame[5] == 43)
        assert list(state_frame[6:-1]) == [0, 28, 50, 40]

    asyncio.run(run())


def test_heater_notification_parsing_is_family_specific() -> None:
    """Heater frames only decode as heater notifications when the family is known."""
    temperature = parse_notification(_heater_temp_frame(255, 250), heater=True)
    assert isinstance(temperature, HeaterTemperatureNotification)
    assert temperature.setting_temperature_celsius == 25.5
    assert temperature.current_temperature_celsius == 25.0

    status = parse_notification(_heater_status_frame(firmware=23, work_hours=120, alarms=0x40), heater=True)
    assert isinstance(status, HeaterStatusNotification)
    assert (status.firmware_version, status.work_time_hours, status.alarms) == (23, 120, 0x40)

    # The 16-byte/0x0A frame is the LED runtime frame without the heater flag…
    assert not isinstance(
        parse_notification(_heater_status_frame(firmware=23, work_hours=120, alarms=0)),
        HeaterStatusNotification,
    )
    # …and unknown heater frames are ignored instead of guessed at.
    assert parse_notification(_heater_temp_frame(255, 250)[:10], heater=True) is None
    assert heater_alarm_names(0x01 | 0x40) == ("insufficient_water", "sensor_failure")


def test_heater_validates_before_touching_the_device() -> None:
    """Invalid settings are rejected before the client opens a BLE connection."""
    transport = ScriptedTransport(name="DYHET-test")

    async def run() -> None:
        device = _make_heater(transport)
        with transport.patch_establish_connection():
            with pytest.raises(ValueError, match="temperature"):
                await device.set_temperature(-5.0)
            with pytest.raises(ValueError, match="power"):
                await device.set_power(3000)
            with pytest.raises(ValueError, match="divisible by 10"):
                await device.set_power(805)
            with pytest.raises(ValueError, match="divisible by 10"):
                await device.set_auto_defaults(20.0, 805)
            with pytest.raises(ValueError, match="temperature"):
                await device.set_protector_temperature(200.0)
        assert transport.connections == 0
        assert transport.writes == []

    asyncio.run(run())


def test_heater_cli_commands_drive_heater(monkeypatch: pytest.MonkeyPatch) -> None:
    """Heater CLI commands convert arguments and call the client API."""
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def _record(name: str):
        async def method(*args: object, **kwargs: object) -> None:
            calls.append((name, args, kwargs))

        return method

    async def get_device_from_address(address: str) -> ChihirosDevice:
        assert address == TEST_ADDRESS
        device = ChihirosHeater(FakeBLEDevice(), HEATER)  # type: ignore[arg-type]
        for name in (
            "set_manual_state",
            "set_manual_mode",
            "set_auto_defaults",
            "set_auto_mode",
            "apply_scene",
            "set_auto_heating",
            "set_temperature_unit",
            "set_backlight",
            "set_protector_temperature",
            "calibrate",
            "reset_work_time",
        ):
            setattr(device, name, _record(name))
        return device

    monkeypatch.setattr(cli, "get_device_from_address", get_device_from_address)

    assert RUNNER.invoke(cli.app, ["heater", "manual-set", TEST_ADDRESS, "26.5", "800"]).exit_code == 0
    assert RUNNER.invoke(cli.app, ["heater", "auto-defaults", TEST_ADDRESS, "24", "1000"]).exit_code == 0
    assert RUNNER.invoke(cli.app, ["heater", "mode", TEST_ADDRESS, "manual"]).exit_code == 0
    assert RUNNER.invoke(cli.app, ["heater", "mode", TEST_ADDRESS, "auto"]).exit_code == 0
    assert RUNNER.invoke(cli.app, ["heater", "mode", TEST_ADDRESS, "scene"]).exit_code == 0
    assert RUNNER.invoke(cli.app, ["heater", "auto-heating", TEST_ADDRESS, "--disable"]).exit_code == 0
    assert RUNNER.invoke(cli.app, ["heater", "unit", TEST_ADDRESS, "f"]).exit_code == 0
    assert RUNNER.invoke(cli.app, ["heater", "backlight", TEST_ADDRESS, "--disable"]).exit_code == 0
    assert RUNNER.invoke(cli.app, ["heater", "protector", TEST_ADDRESS, "37"]).exit_code == 0
    assert RUNNER.invoke(cli.app, ["heater", "calibrate", TEST_ADDRESS, "25.5"]).exit_code == 0
    assert RUNNER.invoke(cli.app, ["heater", "reset-work-time", TEST_ADDRESS]).exit_code == 0

    assert calls == [
        ("set_manual_state", (26.5, 800), {}),
        ("set_auto_defaults", (24.0, 1000), {}),
        ("set_manual_mode", (), {}),
        ("set_auto_mode", (), {}),
        ("apply_scene", (), {}),
        ("set_auto_heating", (False,), {}),
        ("set_temperature_unit", (), {"celsius": False}),
        ("set_backlight", (False,), {}),
        ("set_protector_temperature", (37.0,), {}),
        ("calibrate", (25.5,), {}),
        ("reset_work_time", (), {}),
    ]


def test_heater_cli_rejects_unrepresentable_power_before_device_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI power arguments that cannot reach the wire never start BLE resolution."""
    resolutions = 0

    async def get_device_from_address(_address: str) -> ChihirosDevice:
        nonlocal resolutions
        resolutions += 1
        raise AssertionError("device resolution must not run")

    monkeypatch.setattr(cli, "get_device_from_address", get_device_from_address)

    manual = RUNNER.invoke(cli.app, ["heater", "manual-set", TEST_ADDRESS, "26.5", "805"])
    auto = RUNNER.invoke(cli.app, ["heater", "auto-defaults", TEST_ADDRESS, "20", "805"])

    assert manual.exit_code != 0
    assert auto.exit_code != 0
    assert "divisible by 10" in manual.output
    assert "divisible by 10" in auto.output
    assert resolutions == 0


def test_heater_cli_status_does_not_present_write_only_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Status omits power and protection values because the heater cannot report them."""

    async def get_device_from_address(_address: str) -> ChihirosDevice:
        device = ChihirosHeater(FakeBLEDevice(), HEATER)  # type: ignore[arg-type]

        async def query_status() -> None:
            return None

        device.query_status = query_status  # type: ignore[method-assign]
        return device

    monkeypatch.setattr(cli, "get_device_from_address", get_device_from_address)

    result = RUNNER.invoke(cli.app, ["heater", "status", TEST_ADDRESS])

    assert result.exit_code == 0
    assert "Power" not in result.output
    assert "Protection temperature" not in result.output
    assert "Setting temperature" in result.output
    assert "unknown" in result.output


def test_heater_cli_commands_reject_other_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    """Heater commands fail clearly when pointed at another device family."""

    async def get_device_from_address(address: str) -> ChihirosDevice:
        assert address == TEST_ADDRESS
        return create_device(FakeBLEDevice(name="DYMIXR-test"))  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "get_device_from_address", get_device_from_address)

    result = RUNNER.invoke(cli.app, ["heater", "manual-set", TEST_ADDRESS, "25", "200"])

    assert result.exit_code != 0
    assert "not a heater" in result.output

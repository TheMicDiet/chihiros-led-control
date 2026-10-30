"""Tests for heater support: model detection, client API, protocol, and CLI."""

from __future__ import annotations

import asyncio

import pytest
from bleak.exc import BleakError
from typer.testing import CliRunner

from chihiros_led_control import cli
from chihiros_led_control.devices import ChihirosDevice, ChihirosHeater
from chihiros_led_control.factory import create_device, detect_model
from chihiros_led_control.protocol.heater import (
    HeaterBacklightSchedule,
    create_heater_backlight_command,
    create_heater_backlight_schedule_command,
    create_heater_calibrate_command,
    create_heater_protector_temperature_command,
    create_heater_set_command,
    encode_heater_power_watts,
    heater_alarm_names,
    split_heater_temperature,
)
from chihiros_led_control.protocol.heater import (
    parse_notification as parse_heater_notification,
)
from chihiros_led_control.protocol.led import parse_notification as parse_led_notification
from chihiros_led_control.protocol.notifications import (
    HeaterPowerNotification,
    HeaterStatusNotification,
    HeaterTemperatureNotification,
)
from chihiros_led_control.registry import HEATER
from chihiros_led_control.testing import ScriptedBLEDevice, ScriptedTransport

RUNNER = CliRunner()
TEST_ADDRESS = "AA:BB:CC:DD:EE:FF"


class FakeBLEDevice:
    """Small BLEDevice stand-in for factory/CLI tests."""

    def __init__(self, name: str = "DYHET-test", address: str = TEST_ADDRESS) -> None:
        """Initialize fake BLE metadata."""
        self.name = name
        self.address = address


def _heater_temp_frame(measured_power_watts: int, current_tenths: int) -> bytes:
    """Build a 13-byte 0x5B/0x25 periodic measurement frame."""
    frame = bytearray(13)
    frame[0] = 0x5B
    frame[1] = 0x0F
    frame[5] = 0x25
    frame[6] = measured_power_watts >> 8
    frame[7] = measured_power_watts & 0xFF
    frame[10] = current_tenths >> 8
    frame[11] = current_tenths & 0xFF
    return bytes(frame)


def _heater_status_frame(*, work_hours: int, alarms: int) -> bytes:
    """Build a 16-byte 0x5B/0x0A heater status frame."""
    frame = bytearray(16)
    frame[0] = 0x5B
    frame[5] = 0x0A
    frame[7] = work_hours >> 8
    frame[8] = work_hours & 0xFF
    frame[11:13] = b"\xff\x22"
    frame[14] = alarms
    return bytes(frame)


def _heater_power_frame(power_watts: int, heating_time_seconds: int) -> bytes:
    """Build an 18-byte 0x5B/0x0B configured-power response."""
    frame = bytearray(18)
    frame[0] = 0x5B
    frame[5] = 0x0B
    frame[12] = power_watts // 10
    frame[13:17] = heating_time_seconds.to_bytes(4, "big")
    return bytes(frame)


def test_detect_model_recognizes_heater_prefixes() -> None:
    """Both DYHET and DYH1T advertisement names resolve to the Heater model."""
    for name in ("DYHET1234567890AB", "DYH1T123456"):
        model = detect_model(name)
        assert model.name == "Heater"
        assert model is HEATER
        assert model.device_kind.value == "heater"
        assert dict(model.color_channels) == {}


def test_create_device_builds_heater_client() -> None:
    """DYHET devices get an independent ChihirosHeater client."""

    async def run() -> ChihirosHeater:
        return create_device(FakeBLEDevice())  # type: ignore[arg-type]

    device = asyncio.run(run())
    assert isinstance(device, ChihirosHeater)
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
    """Reproduce captured heater frames and the APK-verified backlight polarity.

    Temperature, protection and calibration examples come from vendor-app
    traffic on DYHET; backlight Boolean meaning is verified by issue #133's
    executed My Chihiros 2.8.59 builders.
    """
    assert (
        create_heater_set_command((0, 5), manual=True, temperature_c=25.0, power_watts=200).hex(" ")
        == "5a 01 09 00 05 2b 00 19 00 14 2b"
    )
    assert (
        create_heater_set_command((0, 5), manual=False, temperature_c=20.0, power_watts=500).hex(" ")
        == "5a 01 09 00 05 2b 01 14 00 32 01"
    )
    assert create_heater_protector_temperature_command((0, 5), 36.9).hex(" ") == "5a 01 07 00 05 2f 24 5a 52"
    assert create_heater_protector_temperature_command((0, 5), 36.5).hex(" ") == "5a 01 07 00 05 2f 24 32 3a"
    assert create_heater_calibrate_command((0, 5), 23.0).hex(" ") == "5a 01 07 00 05 30 17 00 24"
    assert create_heater_backlight_command((0, 7), enabled=True).hex(" ") == "a5 01 0a 00 07 38 c8 c8 c8 c8 7f 4b"
    assert create_heater_backlight_command((0, 7), enabled=False).hex(" ") == "a5 01 0a 00 07 38 64 64 64 64 7f 4b"


@pytest.mark.parametrize(
    ("schedule", "payload"),
    [
        (HeaterBacklightSchedule(8, 15, 21, 30, 81), [8, 15, 21, 30, 81]),
        (HeaterBacklightSchedule(0, 0, 23, 59), [0, 0, 23, 59, 127]),
        (HeaterBacklightSchedule(23, 59, 0, 0, 0), [23, 59, 0, 0, 0]),
    ],
)
def test_heater_backlight_schedule_encoding(schedule: HeaterBacklightSchedule, payload: list[int]) -> None:
    """Schedules preserve clock boundaries, overnight windows and all seven mask bits."""
    frame = create_heater_backlight_schedule_command((0, 7), schedule)
    assert frame[0] == 0xA5
    assert frame[5] == 0x38
    assert list(frame[6:-1]) == payload
    if schedule.weekdays == 81:
        assert frame.hex(" ") == "a5 01 0a 00 07 38 08 0f 15 1e 51 69"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("start_hour", -1),
        ("start_hour", 24),
        ("end_hour", -1),
        ("end_hour", 24),
        ("start_minute", -1),
        ("start_minute", 60),
        ("end_minute", -1),
        ("end_minute", 60),
        ("weekdays", -1),
        ("weekdays", 128),
        ("start_hour", 8.5),
        ("weekdays", True),
    ],
)
def test_heater_backlight_schedule_rejects_invalid_fields(field: str, value: object) -> None:
    """Sentinel levels cannot be passed off as schedule clock fields."""
    fields = dict(start_hour=8, start_minute=15, end_hour=21, end_minute=30, weekdays=81)
    fields[field] = value
    with pytest.raises(ValueError, match=field):
        HeaterBacklightSchedule(**fields)


def _fast_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove notification sleeps so scripted sessions run quickly."""
    from chihiros_led_control import transport as transport_module
    from chihiros_led_control.devices import base as client_module
    from chihiros_led_control.devices import heater as heater_module

    monkeypatch.setattr(client_module, "COMMAND_NOTIFICATION_WAIT", 0.0)
    monkeypatch.setattr(client_module, "STATUS_NOTIFICATION_WAIT", 0.0)
    monkeypatch.setattr(heater_module, "COMMAND_NOTIFICATION_WAIT", 0.0)
    monkeypatch.setattr(heater_module, "STATUS_NOTIFICATION_WAIT", 0.0)
    monkeypatch.setattr(transport_module, "BATCH_WRITE_DELAY", 0.0)


def _make_heater(transport: ScriptedTransport) -> ChihirosHeater:
    return ChihirosHeater(ScriptedBLEDevice(transport.name, transport.address), HEATER, transport=transport)


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
        if transport:
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

        async def capture_locked(
            commands_to_send: bytes | bytearray | list[bytes | bytearray], attempts: int, notification_wait: float
        ) -> None:
            del attempts, notification_wait
            frames = [commands_to_send] if isinstance(commands_to_send, (bytes, bytearray)) else commands_to_send
            state_frame = next((command for command in frames if command[5] == 43), None)
            if state_frame is None:
                return
            sent_states.append(list(state_frame[6:-1]))
            if len(sent_states) == 1:
                first_write_started.set()
                await release_first_write.wait()

        device._send_command = capture_locked  # type: ignore[method-assign]

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
    """State, mode switches, automatic power, unit, protector and reset write their frames."""
    transport = ScriptedTransport(name="DYHET-test")
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = _make_heater(transport)
        if transport:
            await device.set_state(24.0, 1000)
            await device.set_manual_mode()
            await device.set_auto_mode()
            await device.set_automatic_power(True)
            await device.set_automatic_power(False)
            await device.set_temperature_unit(celsius=False)
            await device.set_backlight(False)
            await device.set_backlight(True)
            await device.set_protector_temperature(37.0)
            await device.calibrate(25.5)
            await device.reset_work_time()

        assert _sent_frames(transport) == [
            (43, [1, 24, 0, 100]),
            (5, [11, 255, 255]),
            (5, [18, 255, 255]),
            (5, [46, 255, 255]),
            (5, [47, 255, 255]),
            (5, [45, 255, 255]),
            (56, [100, 100, 100, 100, 127]),
            (56, [200, 200, 200, 200, 127]),
            (47, [37, 0]),
            (48, [25, 50]),
            (5, [58, 255, 255]),
        ]
        assert device.automatic_power is False
        assert device.is_celsius is False
        assert device.backlight is True
        assert device.protector_temperature_celsius == 37.0

    asyncio.run(run())


def test_scripted_heater_backlight_schedule_and_always_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production transport writes each form and tracks scheduled state distinctly."""
    transport = ScriptedTransport(name="DYHET-test")
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = _make_heater(transport)
        try:
            await device.set_backlight_schedule(8, 15, 21, 30, weekdays=81)
            assert device.backlight is None
            assert device.backlight_schedule == HeaterBacklightSchedule(8, 15, 21, 30, 81)
            await device.set_backlight(False)
            assert device.backlight is False
            assert device.backlight_schedule is None
            await device.set_backlight_schedule(0, 0, 23, 59)
            assert device.backlight is None
            await device.set_backlight(True)
            assert device.backlight is True
            assert device.backlight_schedule is None
            assert _sent_frames(transport) == [
                (56, [8, 15, 21, 30, 81]),
                (56, [100, 100, 100, 100, 127]),
                (56, [0, 0, 23, 59, 127]),
                (56, [200, 200, 200, 200, 127]),
            ]
            assert all(frame[0] == 0xA5 for frame in transport.writes if frame[5] == 56)
        finally:
            await device.disconnect()

    asyncio.run(run())


@pytest.mark.parametrize("scheduled", [True, False])
@pytest.mark.parametrize("enabled", [True, False, None])
def test_scripted_heater_failed_backlight_write_preserves_state(
    monkeypatch: pytest.MonkeyPatch, enabled: bool | None, scheduled: bool
) -> None:
    """Failed schedule and Boolean writes never replace the last successful setting."""
    transport = ScriptedTransport(name="DYHET-test")
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = _make_heater(transport)
        try:
            if scheduled:
                await device.set_backlight_schedule(8, 15, 21, 30, weekdays=81)
            else:
                await device.set_backlight(False)
            original = device.backlight_schedule
            original_backlight = device.backlight
            payload = [9, 0, 20, 0, 127] if enabled is None else [200 if enabled else 100] * 4 + [127]
            transport.expect(165, 56, payload, fail=True)
            with pytest.raises(BleakError, match="scripted write failure"):
                if enabled is None:
                    await device.set_backlight_schedule(9, 0, 20, 0)
                else:
                    await device.set_backlight(enabled)
            assert device.backlight is original_backlight
            assert device.backlight_schedule == original
        finally:
            await device.disconnect()

    asyncio.run(run())


def test_shared_state_drives_manual_mutations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mode-preserving and manual writes always use one shared target/power pair."""
    transport = ScriptedTransport(name="DYHET-test")
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = _make_heater(transport)
        try:
            await device.set_auto_mode()
            await device.set_state(22.5, 800)
            assert device.setting_temperature_celsius == 22.5
            assert device.power_watts == 800
            await device.set_temperature(26.5)
            await device.set_state(24.0, 600)
            await device.set_power(400)
            await device.set_manual_state(27.0, 1000)
            await device.set_auto_mode()
            await device.set_manual_mode()
            assert _sent_frames(transport) == [
                (5, [18, 255, 255]),
                (43, [1, 22, 50, 80]),
                (5, [11, 255, 255]),
                (43, [0, 26, 50, 80]),
                (43, [1, 24, 0, 60]),
                (5, [11, 255, 255]),
                (43, [0, 24, 0, 40]),
                (5, [11, 255, 255]),
                (43, [0, 27, 0, 100]),
                (5, [18, 255, 255]),
                (5, [11, 255, 255]),
            ]
            assert device.setting_temperature_celsius == 27.0
            assert device.power_watts == 1000
        finally:
            await device.disconnect()

    asyncio.run(run())


def test_concurrent_preserving_and_manual_writes_share_the_lock() -> None:
    """A manual mutation resends the mode-preserving write's committed target."""

    async def run() -> None:
        device = ChihirosHeater(FakeBLEDevice(), HEATER)  # type: ignore[arg-type]
        sent_states: list[list[int]] = []
        first_write_started = asyncio.Event()
        release_first_write = asyncio.Event()

        async def capture_locked(
            commands_to_send: bytes | bytearray | list[bytes | bytearray], attempts: int, notification_wait: float
        ) -> None:
            del attempts, notification_wait
            frames = [commands_to_send] if isinstance(commands_to_send, (bytes, bytearray)) else commands_to_send
            state_frame = next((command for command in frames if command[5] == 43), None)
            if state_frame is None:
                return
            sent_states.append(list(state_frame[6:-1]))
            if len(sent_states) == 1:
                first_write_started.set()
                await release_first_write.wait()

        device._send_command = capture_locked  # type: ignore[method-assign]
        state_task = asyncio.create_task(device.set_state(22.5, 600))
        await first_write_started.wait()
        power_task = asyncio.create_task(device.set_power(800))
        await asyncio.sleep(0)
        release_first_write.set()
        await asyncio.gather(state_task, power_task)
        assert sent_states == [[1, 22, 50, 60], [0, 22, 50, 80]]
        assert device.setting_temperature_celsius == 22.5
        assert device.power_watts == 800

    asyncio.run(run())


def test_restored_heater_pairs_drive_real_client_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restored write-only values replace fresh defaults without touching BLE."""
    transport = ScriptedTransport(name="DYHET-test")
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = _make_heater(transport)
        if transport:
            device.restore_setting_temperature(27.5)
            device.restore_power(800)
            assert transport.connections == 0
            assert transport.writes == []

            await device.set_power(600)
            await device.set_temperature(26.5)
            await device.set_state(23.0, 900)

        assert _sent_frames(transport) == [
            (5, [11, 255, 255]),
            (43, [0, 27, 50, 60]),
            (5, [11, 255, 255]),
            (43, [0, 26, 50, 60]),
            (43, [1, 23, 0, 90]),
        ]

    asyncio.run(run())


def test_scripted_heater_notifications_track_device_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Query responses report configured power/runtime; periodic measurements stay separate."""
    transport = ScriptedTransport(name="DYHET-test")
    _fast_waits(monkeypatch)
    temperature_frame = _heater_temp_frame(30, 258)
    status_frame = _heater_status_frame(work_hours=1994, alarms=0x09)
    power_frame = _heater_power_frame(800, 734032)
    transport.expect(90, 4, [1], respond=[status_frame, power_frame])

    async def run() -> None:
        device = _make_heater(transport)
        try:
            assert device.measured_power_watts is None
            assert device.heating_time_seconds is None
            await device.query_status()
            assert device.last_heater_temperature_notification is None
            assert device.current_temperature_celsius is None
            assert device.power_watts == 800
            assert device.heating_time_seconds == 734032
            assert device.last_heater_power_notification is not None
            device._notification_handler(None, bytearray(temperature_frame))
            assert device.measured_power_watts == 30
            assert device.current_temperature_celsius == pytest.approx(25.8)
            assert device.setting_temperature_celsius == 25.0
            assert device.power_watts == 800
            assert device.last_heater_status_notification is not None
            await device.set_temperature(26.5)
            state_frame = next(frame for frame in transport.writes if frame[5] == 43)
            assert list(state_frame[6:-1]) == [0, 26, 50, 80]
        finally:
            await device.disconnect()
        assert device.work_time_hours == 1994
        assert device.heater_alarms == ("insufficient_water", "needs_cleaning")

    asyncio.run(run())


def test_real_measured_power_never_becomes_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real 30 W/25 °C measurement must not turn the next target into 3 °C."""
    transport = ScriptedTransport(name="DYHET-test")
    _fast_waits(monkeypatch)
    measurement = bytes.fromhex("5b060e017b25001e0bff00fa00")

    async def run() -> None:
        device = _make_heater(transport)
        try:
            await device.set_manual_mode()
            device._notification_handler(None, bytearray(measurement))
            assert device.measured_power_watts == 30
            assert device.current_temperature_celsius == 25.0
            assert device.setting_temperature_celsius == 25.0
            await device.set_power(400)
            state_frame = next(frame for frame in transport.writes if frame[5] == 43)
            assert list(state_frame[6:-1]) == [0, 25, 0, 40]
            assert device.setting_temperature_celsius == 25.0
        finally:
            await device.disconnect()

    asyncio.run(run())


def test_heater_notification_parsing_is_family_specific() -> None:
    """Heater frames only decode as heater notifications when the family is known."""
    temperature = parse_heater_notification(_heater_temp_frame(30, 250))
    assert isinstance(temperature, HeaterTemperatureNotification)
    assert temperature.measured_power_watts == 30
    assert temperature.current_temperature_celsius == 25.0

    status = parse_heater_notification(_heater_status_frame(work_hours=120, alarms=0x40))
    assert isinstance(status, HeaterStatusNotification)
    assert (status.work_time_hours, status.alarms) == (120, 0x40)

    # The 16-byte/0x0A frame is the LED runtime frame without the heater codec.
    assert not isinstance(
        parse_led_notification(_heater_status_frame(work_hours=120, alarms=0)),
        HeaterStatusNotification,
    )
    # Unknown or truncated heater frames are ignored instead of guessed at.
    assert parse_heater_notification(_heater_temp_frame(255, 250)[:10]) is None
    assert heater_alarm_names(0x01 | 0x40) == ("insufficient_water", "sensor_failure")


@pytest.mark.parametrize(
    ("capture", "expected"),
    [
        (
            "5b060e017b25001e0bff00fa00",
            HeaterTemperatureNotification(measured_power_watts=30, current_temperature_celsius=25.0),
        ),
        (
            "5b060e00010a0000cbffffff22050022",
            HeaterStatusNotification(work_time_hours=203, alarms=0),
        ),
        (
            "5b011000010b00000000000014000b335022",
            HeaterPowerNotification(power_watts=200, heating_time_seconds=734032),
        ),
    ],
)
def test_heater_real_captures_require_exact_frame_lengths(
    capture: str,
    expected: HeaterTemperatureNotification | HeaterStatusNotification | HeaterPowerNotification,
) -> None:
    """Only complete, exact-length firmware frames are accepted."""
    frame = bytes.fromhex(capture)
    parsed = parse_heater_notification(frame)
    assert parsed == expected
    assert parsed.raw == frame
    assert parse_heater_notification(bytearray(frame)) == expected
    for length in range(len(frame)):
        assert parse_heater_notification(frame[:length]) is None
    assert parse_heater_notification(frame + b"\x00") is None
    assert parse_heater_notification(b"\x5a" + frame[1:]) is None


@pytest.mark.parametrize(
    ("power_watts", "heating_time_seconds"),
    [(0, 0), (2000, 0xFFFFFFFF), (10, 0x01020304)],
)
def test_heater_configured_power_and_lifetime_byte_boundaries(power_watts: int, heating_time_seconds: int) -> None:
    """Power is a ten-watt byte and lifetime heating time is an unsigned big-endian word."""
    parsed = parse_heater_notification(_heater_power_frame(power_watts, heating_time_seconds))
    assert parsed == HeaterPowerNotification(power_watts, heating_time_seconds)


@pytest.mark.parametrize(
    ("method", "args", "mode", "payload"),
    [
        ("set_state", (26.5, 600), 43, [1, 26, 50, 60]),
        ("set_manual_state", (26.5, 600), 43, [0, 26, 50, 60]),
        ("set_manual_state", (26.5, 600), 5, [11, 255, 255]),
        ("set_temperature", (26.5,), 43, [0, 26, 50, 80]),
        ("set_power", (600,), 43, [0, 27, 50, 60]),
        ("set_automatic_power", (False,), 5, [47, 255, 255]),
    ],
)
def test_failed_heater_writes_never_commit_tracked_state(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    args: tuple[float | int | bool, ...],
    mode: int,
    payload: list[int],
) -> None:
    """Failed shared writes, including partially sent manual batches, preserve committed state."""
    transport = ScriptedTransport(name="DYHET-test")
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = _make_heater(transport)
        try:
            await device.set_state(27.5, 800)
            await device.set_automatic_power(True)
            transport.expect(90, mode, payload, fail=True)
            with pytest.raises(BleakError, match="scripted write failure"):
                await getattr(device, method)(*args)
            assert device.setting_temperature_celsius == 27.5
            assert device.power_watts == 800
            assert device.automatic_power is True
        finally:
            await device.disconnect()

    asyncio.run(run())


@pytest.mark.parametrize(("method", "flag"), [("set_state", 1), ("set_manual_state", 0)])
@pytest.mark.parametrize("reported_power", [200, 800])
def test_power_readback_during_write_stays_authoritative(
    monkeypatch: pytest.MonkeyPatch, method: str, flag: int, reported_power: int
) -> None:
    """A 0x0B received during a successful write beats the requested optimistic power."""
    transport = ScriptedTransport(name="DYHET-test")
    _fast_waits(monkeypatch)
    transport.expect(
        90,
        43,
        [flag, 26, 50, 40],
        respond=[
            _heater_power_frame(reported_power, 734032),
            bytes.fromhex("5b060e017b25001e0bff00fa00"),
        ],
    )

    async def run() -> None:
        device = _make_heater(transport)
        try:
            await getattr(device, method)(26.5, 400)
            assert device.setting_temperature_celsius == 26.5
            assert device.power_watts == reported_power
            assert device.measured_power_watts == 30
            assert device.heating_time_seconds == 734032
            await device.set_temperature(27.0)
            state_frames = [frame for frame in transport.writes if frame[5] == 43]
            assert list(state_frames[-1][6:-1]) == [0, 27, 0, reported_power // 10]
        finally:
            await device.disconnect()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("method", "args", "initial_power", "expected_payloads"),
    [
        ("set_power", (400,), 200, [[0, 25, 0, 40], [0, 27, 0, 40]]),
        ("set_temperature", (26.5,), 600, [[0, 26, 50, 60], [0, 27, 0, 60]]),
    ],
)
def test_cold_heater_mutations_preserve_actual_configured_power(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    args: tuple[float | int, ...],
    initial_power: int,
    expected_payloads: list[list[int]],
) -> None:
    """Pre-write status cannot undo a new power setting or replace an existing power with defaults."""
    _fast_waits(monkeypatch)
    transport = ScriptedTransport(name="DYHET-test")
    configured_power = initial_power

    def report_state(_frame: bytes) -> list[bytes]:
        return [_heater_power_frame(configured_power, 734032)]

    def apply_state(frame: bytes) -> tuple[bytes, ...]:
        nonlocal configured_power
        configured_power = frame[9] * 10
        return ()

    transport.expect(90, 4, [1], respond=report_state)
    transport.expect(90, 43, respond=apply_state)

    async def run() -> None:
        device = _make_heater(transport)
        try:
            await getattr(device, method)(*args)
            assert device.power_watts == configured_power == expected_payloads[0][-1] * 10
            await device.set_temperature(27.0)
            assert device.power_watts == configured_power == expected_payloads[-1][-1] * 10
            state_frames = [frame for frame in transport.writes if frame[5] == 43]
            assert [list(frame[6:-1]) for frame in state_frames] == expected_payloads
        finally:
            await device.disconnect()

    asyncio.run(run())


def test_heater_validates_before_touching_the_device() -> None:
    """Invalid settings are rejected before the client opens a BLE connection."""
    transport = ScriptedTransport(name="DYHET-test")

    async def run() -> None:
        device = _make_heater(transport)
        if transport:
            with pytest.raises(ValueError, match="temperature"):
                await device.set_temperature(-5.0)
            with pytest.raises(ValueError, match="power"):
                await device.set_power(3000)
            with pytest.raises(ValueError, match="divisible by 10"):
                await device.set_power(805)
            with pytest.raises(ValueError, match="divisible by 10"):
                await device.set_state(20.0, 805)
            with pytest.raises(ValueError, match="temperature"):
                await device.set_protector_temperature(200.0)
            with pytest.raises(ValueError, match="start_hour"):
                await device.set_backlight_schedule(24, 0, 21, 30)
            with pytest.raises(ValueError, match="weekdays"):
                await device.set_backlight_schedule(8, 15, 21, 30, weekdays=128)
        assert transport.connections == 0
        assert transport.writes == []

    asyncio.run(run())


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
    state = RUNNER.invoke(cli.app, ["heater", "set-state", TEST_ADDRESS, "20", "805"])

    assert manual.exit_code != 0
    assert state.exit_code != 0
    assert "divisible by 10" in manual.output
    assert "divisible by 10" in state.output
    assert resolutions == 0


@pytest.mark.parametrize(
    ("arguments", "expected_frames"),
    [
        (["set-state", TEST_ADDRESS, "26.5", "800"], [(43, [1, 26, 50, 80])]),
        (["manual-set", TEST_ADDRESS, "26.5", "800"], [(5, [11, 255, 255]), (43, [0, 26, 50, 80])]),
        (["mode", TEST_ADDRESS, "auto"], [(5, [18, 255, 255])]),
        (["mode", TEST_ADDRESS, "manual"], [(5, [11, 255, 255])]),
        (["automatic-power", TEST_ADDRESS, "--enable"], [(5, [46, 255, 255])]),
        (["automatic-power", TEST_ADDRESS, "--disable"], [(5, [47, 255, 255])]),
    ],
)
def test_heater_cli_state_and_modes_write_production_transport(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    expected_frames: list[tuple[int, list[int]]],
) -> None:
    """CLI control writes the firmware's mode-preserving/manual flags and non-destructive modes."""
    _fast_waits(monkeypatch)
    transport = ScriptedTransport(name="DYHET-test")

    async def get_device_from_address(_address: str) -> ChihirosHeater:
        return _make_heater(transport)

    monkeypatch.setattr(cli, "get_device_from_address", get_device_from_address)
    result = RUNNER.invoke(cli.app, ["heater", *arguments])
    assert result.exit_code == 0, result.output
    assert _sent_frames(transport) == expected_frames
    assert not transport.is_connected


@pytest.mark.parametrize(
    ("options", "payload"),
    [
        ([], [8, 15, 21, 30, 127]),
        (["--weekdays", "monday", "--weekdays", "wednesday", "--weekdays", "sunday"], [8, 15, 21, 30, 81]),
    ],
)
def test_heater_cli_backlight_schedule_writes_production_transport(
    monkeypatch: pytest.MonkeyPatch, options: list[str], payload: list[int]
) -> None:
    """The distinct CLI schedule operation encodes clocks and named weekdays over BLE."""
    _fast_waits(monkeypatch)
    transports: list[ScriptedTransport] = []
    devices: list[ChihirosHeater] = []

    async def get_device_from_address(address: str) -> ChihirosHeater:
        assert address == TEST_ADDRESS
        transport = ScriptedTransport(name="DYHET-test")
        device = _make_heater(transport)
        transports.append(transport)
        devices.append(device)
        return device

    monkeypatch.setattr(cli, "get_device_from_address", get_device_from_address)
    result = RUNNER.invoke(cli.app, ["heater", "backlight-schedule", TEST_ADDRESS, "08:15", "21:30", *options])
    assert result.exit_code == 0, result.output
    assert _sent_frames(transports[0]) == [(56, payload)]
    assert devices[0].backlight is None
    assert devices[0].backlight_schedule == HeaterBacklightSchedule(*payload)
    assert not transports[0].is_connected


@pytest.mark.parametrize(("option", "level"), [("--enable", 200), ("--disable", 100)])
def test_heater_cli_backlight_always_modes_write_production_transport(
    monkeypatch: pytest.MonkeyPatch, option: str, level: int
) -> None:
    """The Boolean CLI command replaces scheduled mode with the corrected sentinel."""
    _fast_waits(monkeypatch)
    transports: list[ScriptedTransport] = []
    devices: list[ChihirosHeater] = []

    async def get_device_from_address(_address: str) -> ChihirosHeater:
        transport = ScriptedTransport(name="DYHET-test")
        device = _make_heater(transport)
        await device.set_backlight_schedule(8, 15, 21, 30, weekdays=81)
        transports.append(transport)
        devices.append(device)
        return device

    monkeypatch.setattr(cli, "get_device_from_address", get_device_from_address)
    result = RUNNER.invoke(cli.app, ["heater", "backlight", TEST_ADDRESS, option])
    assert result.exit_code == 0, result.output
    assert _sent_frames(transports[0]) == [(56, [8, 15, 21, 30, 81]), (56, [level] * 4 + [127])]
    assert devices[0].backlight is (option == "--enable")
    assert devices[0].backlight_schedule is None
    assert not transports[0].is_connected


@pytest.mark.parametrize(
    "arguments",
    [
        ["24:00", "21:30"],
        ["08:60", "21:30"],
        ["08:15", "-1:30"],
        ["08:15", "21:60"],
        ["08:15", "bad"],
        ["08:15", "21:30", "--weekdays", "noday"],
        ["08:15", "21:30", "--disable"],
    ],
)
def test_heater_cli_backlight_schedule_rejects_invalid_input_before_resolution(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    """Schedules cannot select always-off sentinels or invalid clocks and weekdays."""

    async def get_device_from_address(_address: str) -> ChihirosHeater:
        raise AssertionError("device resolution must not run")

    monkeypatch.setattr(cli, "get_device_from_address", get_device_from_address)
    result = RUNNER.invoke(cli.app, ["heater", "backlight-schedule", TEST_ADDRESS, *arguments])
    assert result.exit_code == 2, result.output


def test_heater_cli_status_displays_reported_measurements(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real readbacks expose measured and configured power without inventing target/firmware."""
    _fast_waits(monkeypatch)
    transport = ScriptedTransport(name="DYHET-test")
    transport.expect(
        90,
        4,
        [1],
        respond=[
            bytes.fromhex("5b060e017b25001e0bff00fa00"),
            bytes.fromhex("5b060e00010a0000cbffffff22050022"),
            bytes.fromhex("5b011000010b00000000000014000b335022"),
        ],
    )

    async def get_device_from_address(_address: str) -> ChihirosHeater:
        return _make_heater(transport)

    monkeypatch.setattr(cli, "get_device_from_address", get_device_from_address)
    result = RUNNER.invoke(cli.app, ["heater", "status", TEST_ADDRESS])
    assert result.exit_code == 0, result.output
    assert "25.0" in result.output
    assert "30" in result.output
    assert "200" in result.output
    assert "203" in result.output
    assert "734032" in result.output
    assert "Setting temperature" not in result.output
    assert "Firmware" not in result.output
    assert not transport.is_connected


def test_heater_cli_commands_reject_other_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    """Heater commands fail clearly when pointed at another device family."""

    async def get_device_from_address(address: str) -> ChihirosDevice:
        assert address == TEST_ADDRESS
        return create_device(FakeBLEDevice(name="DYMIXR-test"))  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "get_device_from_address", get_device_from_address)

    result = RUNNER.invoke(cli.app, ["heater", "manual-set", TEST_ADDRESS, "25", "200"])

    assert result.exit_code != 0
    assert "not a heater" in result.output

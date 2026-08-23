"""End-to-end client tests against a scripted BLE transport (no hardware).

The scripted transport replaces ``establish_connection`` so the real
``ChihirosDevice`` connect flow (characteristic resolution, notification
subscription, connection prelude), command writes, and notification parsing
run against scripted bytes instead of Bluetooth hardware.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import pytest
from bleak_retry_connector import BleakError

from chihiros_led_control import client as client_module
from chihiros_led_control import commands
from chihiros_led_control.models import WHITE_CHANNELS, WRGB_CHANNELS, DeviceModel
from chihiros_led_control.protocol import DosingTotalsNotification, RuntimeNotification
from chihiros_led_control.testing import ScriptedTransport
from chihiros_led_control.weekday_encoding import WeekdaySelect

RUNTIME_FRAME = bytes.fromhex("5b 1b 0a 00 01 0a 01 ff")


def _fast_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove notification sleeps so scripted sessions run quickly."""
    monkeypatch.setattr(client_module, "COMMAND_NOTIFICATION_WAIT", 0.0)
    monkeypatch.setattr(client_module, "STATUS_NOTIFICATION_WAIT", 0.0)
    monkeypatch.setattr(client_module, "BATCH_WRITE_DELAY", 0.0)


def test_scripted_query_status_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """A query status command connects, runs the prelude, and parses the reply."""
    transport = ScriptedTransport()
    transport.expect(90, 4, [1], respond=[RUNTIME_FRAME])
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = transport.make_device(DeviceModel("Test", (), WHITE_CHANNELS))
        with transport.patch_establish_connection():
            await device.query_status()

        assert device.last_runtime_notification == RuntimeNotification(27, 511, RUNTIME_FRAME)
        assert transport.connections == 1
        writes = transport.writes
        # The query frame is built before the connection prelude, so its message
        # id (0, 2) is issued first and the prelude ids follow in the write order.
        assert [frame[0] for frame in writes] == [90, 90, 90, 90]
        assert [frame[5] for frame in writes] == [4, 9, 9, 4]
        assert [frame[3:5] for frame in writes] == [bytes((0, 3)), bytes((0, 4)), bytes((0, 5)), bytes((0, 2))]
        assert writes[0] == commands.create_base_auth_command((0, 3))
        assert writes[3] == commands.create_query_status_command((0, 2))

    asyncio.run(run())


def test_scripted_fan_commands_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fan auto mode and manual speed write the expected frames."""
    transport = ScriptedTransport()
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = transport.make_device(DeviceModel("VIVID3", (), WRGB_CHANNELS, has_fan=True, min_fan_speed=25))
        with transport.patch_establish_connection():
            await device.set_fan_auto()
            assert device.fan_auto is True
            await device.set_fan_speed(50)
            assert device.fan_auto is False

        writes = transport.writes
        # Each command reconnects, so the connection prelude runs twice.
        assert transport.connections == 2
        auto_frames = [frame for frame in writes if frame[5] == 5 and frame[6] == 0x11]
        speed_frames = [frame for frame in writes if frame[5] == 15]
        assert len(auto_frames) == 1
        assert auto_frames[0][6:9] == bytes([0x11, 0xFF, 0xFF])  # autoFan frame
        assert len(speed_frames) == 1
        assert speed_frames[0][6] == 50  # manual speed frame

    asyncio.run(run())


def test_scripted_dosing_pump_dose_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    """A manual dose writes the pump auth pair plus the dose frame in order."""
    dosing_frame = bytes.fromhex("b6 10 10 00 01 3c 04 1f 00 00")
    transport = ScriptedTransport(name="DYDOSE-test")
    # The pump reports its counters in response to the connect auth frame.
    transport.expect(90, 4, [1], respond=[dosing_frame])
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = transport.make_pump()
        with transport.patch_establish_connection():
            await device.dose_ml(1, 2.0)

        assert device.last_dosing_totals_notification == DosingTotalsNotification((105500, 0), dosing_frame)
        writes = transport.writes
        assert len(writes) == 6  # prelude (3) + auth1, auth2, dose
        assert writes[3][0] == 165 and writes[3][5] == 4 and writes[3][6] == 4
        assert writes[4][0] == 165 and writes[4][5] == 4 and writes[4][6] == 5
        dose = writes[5]
        assert dose[0] == 165 and dose[5] == 27
        assert dose[6:11] == bytes([1, 0, 0, 0, 20])  # pump 1, 2.0 ml

    asyncio.run(run())


def test_scripted_retries_transient_write_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient BLE failure is retried on a fresh connection."""
    transport = ScriptedTransport()
    call_count = 0

    def flaky(frame: bytes) -> list[bytes]:
        del frame
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise BleakError("transient")
        return [RUNTIME_FRAME]

    transport.expect(90, 4, [1], respond=flaky)
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = transport.make_device(DeviceModel("Test", (), WHITE_CHANNELS))
        with transport.patch_establish_connection():
            await device.query_status()

        assert transport.connections == 2
        assert device.last_runtime_notification == RuntimeNotification(27, 511, RUNTIME_FRAME)
        # The failed attempt still recorded its auth write before raising.
        assert transport.writes[0][5] == 4
        assert len(transport.writes) >= 4

    asyncio.run(run())


def test_scripted_fan_speed_can_fail_permanently(monkeypatch: pytest.MonkeyPatch) -> None:
    """A permanently failing rule surfaces the BLE error after retries."""
    transport = ScriptedTransport()
    transport.expect(90, 15, [50], fail=True)
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = transport.make_device(DeviceModel("VIVID3", (), WRGB_CHANNELS, has_fan=True, min_fan_speed=25))
        with transport.patch_establish_connection():
            with pytest.raises(BleakError, match="scripted write failure"):
                await device.set_fan_speed(50)

        assert transport.connections == 3  # initial attempt plus two retries

    asyncio.run(run())


def test_scripted_turn_on_and_off_write_manual_switch_and_levels(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn on/off switch to manual mode and set every channel level."""
    transport = ScriptedTransport()
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = transport.make_device(DeviceModel("Test", (), WHITE_CHANNELS))
        with transport.patch_establish_connection():
            await device.turn_on()
            await device.turn_off()

        writes = transport.writes
        # Two transactions, each: prelude (3) + switch-to-manual + brightness.
        assert len(writes) == 10
        manual = [frame for frame in writes if frame[5] == 5 and frame[6] == 11]
        assert len(manual) == 2
        assert writes[4][5] == 7 and writes[4][6:8] == bytes([0, 100])
        assert writes[9][5] == 7 and writes[9][6:8] == bytes([0, 0])

    asyncio.run(run())


def test_scripted_remove_setting_writes_delete_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    """remove_setting writes the delete auto-setting frame with padded channels."""
    transport = ScriptedTransport()
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = transport.make_device(DeviceModel("Test", (), WHITE_CHANNELS))
        with transport.patch_establish_connection():
            await device.remove_setting(
                datetime(2024, 1, 1, 6, 0),
                datetime(2024, 1, 1, 18, 0),
                ramp_up_in_minutes=30,
                weekdays=[WeekdaySelect.monday, WeekdaySelect.friday],
            )

        writes = transport.writes
        assert len(writes) == 4  # prelude (3) + delete frame
        delete = writes[3]
        assert delete[0] == 165 and delete[5] == 25
        # sunrise 06:00, sunset 18:00, ramp 30, weekdays monday+friday = 64+4
        assert delete[6:12] == bytes([6, 0, 18, 0, 30, 68])
        # one white channel, remaining parameter slots padded with 255
        assert delete[12] == 255
        assert all(value == 255 for value in delete[13:-1])

    asyncio.run(run())


def test_scripted_reset_settings_writes_reset_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    """reset_settings writes the reset auto-settings frame."""
    transport = ScriptedTransport()
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = transport.make_device(DeviceModel("Test", (), WHITE_CHANNELS))
        with transport.patch_establish_connection():
            await device.reset_settings()

        writes = transport.writes
        assert len(writes) == 4  # prelude (3) + reset frame
        reset = writes[3]
        assert reset[0] == 90 and reset[5] == 5
        assert reset[6:9] == bytes([5, 255, 255])

    asyncio.run(run())


def test_scripted_disconnect_closes_connection_until_next_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit disconnect tears the client down; the next command reconnects."""
    transport = ScriptedTransport()
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = transport.make_device(DeviceModel("Test", (), WHITE_CHANNELS))
        with transport.patch_establish_connection():
            await device.query_status()
            assert transport.connections == 1
            await device.disconnect()
            assert device._client is None  # noqa: SLF001
            await device.query_status()
            assert transport.connections == 2

    asyncio.run(run())


def test_scripted_set_log_level_configures_device_logger() -> None:
    """set_log_level accepts level names and numeric levels."""

    async def run() -> None:
        transport = ScriptedTransport()
        device = transport.make_device(DeviceModel("Test", (), WHITE_CHANNELS))

        device.set_log_level("DEBUG")
        assert device._logger.level == logging.DEBUG  # noqa: SLF001
        device.set_log_level(logging.WARNING)
        assert device._logger.level == logging.WARNING  # noqa: SLF001
        device.set_log_level("NOT-A-LEVEL")
        assert device._logger.level == logging.INFO  # noqa: SLF001

    asyncio.run(run())

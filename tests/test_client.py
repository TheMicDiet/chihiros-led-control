"""Tests for the Chihiros BLE client."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from bleak_retry_connector import BleakError

from chihiros_led_control.client import ChihirosDevice, ChihirosDosingPump
from chihiros_led_control.exceptions import CharacteristicMissingError
from chihiros_led_control.models import RGB_CHANNELS, WHITE_CHANNELS, WRGB_CHANNELS, DeviceModel
from chihiros_led_control.protocol import (
    DosingDailyNotification,
    DosingTotalsNotification,
    FanStatusNotification,
    RuntimeNotification,
    ScheduleSnapshotNotification,
    Vivid3FanStatusNotification,
    calculate_checksum,
)


class FakeBLEDevice:
    """Small BLEDevice stand-in for client tests."""

    def __init__(self) -> None:
        """Create a fake BLE device."""
        self.name = "DYNA2-test"
        self.address = "AA:BB:CC:DD:EE:FF"


def framed(values: list[int]) -> bytearray:
    """Complete a notification frame with length and checksum."""
    frame = bytearray(values)
    frame[2] = len(frame) - 4
    frame.append(calculate_checksum(frame) ^ 0xFF)
    return frame


def test_enable_auto_mode_sends_time_before_switch() -> None:
    """Auto mode setup syncs time before enabling auto mode."""
    sent_commands: list[bytes] = []

    async def run() -> None:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test", (), WHITE_CHANNELS))  # type: ignore[arg-type]

        async def capture_command(command: list[bytes] | bytes | bytearray, retry: int | None = None) -> None:
            del retry
            sent_commands.append(bytes(command))

        device._send_command = capture_command  # type: ignore[method-assign]

        await device.enable_auto_mode()

    asyncio.run(run())

    assert [command[5] for command in sent_commands] == [9, 5]


def test_enable_auto_mode_uses_supplied_timestamp() -> None:
    """Auto mode time sync can use a caller-supplied local timestamp."""
    sent_commands: list[bytes] = []

    async def run() -> None:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test", (), WHITE_CHANNELS))  # type: ignore[arg-type]

        async def capture_command(command: list[bytes] | bytes | bytearray, retry: int | None = None) -> None:
            del retry
            sent_commands.append(bytes(command))

        device._send_command = capture_command  # type: ignore[method-assign]

        await device.enable_auto_mode(datetime(2026, 6, 16, 20, 30, 45))

    asyncio.run(run())

    assert sent_commands[0][5] == 9
    assert sent_commands[0][6:12] == bytes([26, 6, 2, 20, 30, 45])


def test_query_status_sends_runtime_status_query() -> None:
    """Status refresh sends the legacy runtime/status query."""
    sent_commands: list[bytes] = []
    notification_waits: list[float] = []

    async def run() -> None:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test", (), WHITE_CHANNELS))  # type: ignore[arg-type]

        async def capture_command(
            command: list[bytes] | bytes | bytearray,
            retry: int | None = None,
            notification_wait: float = 0,
        ) -> None:
            del retry
            sent_commands.append(bytes(command))
            notification_waits.append(notification_wait)

        device._send_command = capture_command  # type: ignore[method-assign]

        await device.query_status()

    asyncio.run(run())

    assert sent_commands[0][5:7] == bytes([4, 1])
    assert notification_waits == [1.0]


def test_dosing_pump_manual_dose_sends_auth_and_dose_batch() -> None:
    """Manual dosing sends dose auth frames before the one-shot dose command."""
    sent_batches: list[list[bytes]] = []
    retry_attempts: list[int | None] = []

    async def run() -> None:
        device = ChihirosDosingPump(FakeBLEDevice(), DeviceModel("Dosing Pump", (), {}))  # type: ignore[arg-type]

        async def capture_command(command: list[bytes] | bytes | bytearray, retry: int | None = None) -> None:
            assert isinstance(command, list)
            sent_batches.append([bytes(item) for item in command])
            retry_attempts.append(retry)

        device._send_command = capture_command  # type: ignore[method-assign]

        await device.dose_ml(1, 2.0)

    asyncio.run(run())

    assert [command[5:7] for command in sent_batches[0]] == [bytes([4, 4]), bytes([4, 5]), bytes([27, 1])]
    assert sent_batches[0][2][6:-1] == bytes([1, 0, 0, 0, 20])
    assert retry_attempts == [1]


def test_send_command_disconnects_after_command_batch() -> None:
    """Command batches do not keep the BLE connection alive."""
    events: list[str] = []
    sleeps: list[float] = []

    async def run() -> None:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test", (), WHITE_CHANNELS))  # type: ignore[arg-type]

        async def ensure_connected() -> None:
            events.append("connect")

        async def execute_command(commands: list[bytes]) -> None:
            events.append(f"send:{len(commands)}")

        async def execute_disconnect() -> None:
            events.append("disconnect")

        async def capture_sleep(delay: float) -> None:
            sleeps.append(delay)

        device._ensure_connected = ensure_connected  # type: ignore[method-assign]
        device._execute_command_locked = execute_command  # type: ignore[method-assign]
        device._execute_disconnect = execute_disconnect  # type: ignore[method-assign]
        original_sleep = asyncio.sleep
        asyncio.sleep = capture_sleep  # type: ignore[method-assign]

        try:
            await device._send_command([b"\x01", b"\x02"])  # noqa: SLF001
        finally:
            asyncio.sleep = original_sleep  # type: ignore[method-assign]

    asyncio.run(run())

    assert events == ["connect", "send:2", "disconnect"]
    assert sleeps == [0.5]


def test_concurrent_commands_serialize_complete_transactions() -> None:
    """Concurrent callers cannot disconnect another caller's transaction."""
    events: list[str] = []

    async def run() -> None:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test", (), WHITE_CHANNELS))  # type: ignore[arg-type]

        async def connect() -> None:
            events.append("connect")

        async def write(commands: list[bytes]) -> None:
            events.append(f"write:{commands[0].hex()}")
            await asyncio.sleep(0)

        async def disconnect() -> None:
            events.append("disconnect")

        device._ensure_connected = connect  # type: ignore[method-assign]
        device._execute_command_locked = write  # type: ignore[method-assign]
        device._execute_disconnect = disconnect  # type: ignore[method-assign]
        await asyncio.gather(
            device._send_command(b"\x01", retry=1, notification_wait=0),
            device._send_command(b"\x02", retry=1, notification_wait=0),
        )

    asyncio.run(run())
    assert events == ["connect", "write:01", "disconnect", "connect", "write:02", "disconnect"]


@pytest.mark.parametrize("failures", [1, 3])
def test_transient_write_retry_reconnects_and_exhausts(failures: int) -> None:
    """Every transient retry reconnects and exhausted retries preserve the BLE error."""
    connects = 0
    disconnects = 0
    writes = 0

    async def run() -> None:
        nonlocal connects, disconnects, writes
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test", (), WHITE_CHANNELS))  # type: ignore[arg-type]

        async def connect() -> None:
            nonlocal connects
            connects += 1

        async def write(_commands: list[bytes]) -> None:
            nonlocal writes
            writes += 1
            if writes <= failures:
                raise BleakError("temporary")

        async def disconnect() -> None:
            nonlocal disconnects
            disconnects += 1

        device._ensure_connected = connect  # type: ignore[method-assign]
        device._execute_command_locked = write  # type: ignore[method-assign]
        device._execute_disconnect = disconnect  # type: ignore[method-assign]
        if failures == 3:
            with pytest.raises(BleakError, match="temporary"):
                await device._send_command(b"x", retry=3, notification_wait=0)
        else:
            await device._send_command(b"x", retry=3, notification_wait=0)

    asyncio.run(run())
    expected = 3 if failures == 3 else 2
    assert (connects, disconnects, writes) == (expected, expected, expected)


def test_missing_characteristics_and_prelude_failure_clean_up_connection() -> None:
    """Connection setup failures disconnect the temporary client and clear state."""

    class FakeClient:
        is_connected = True
        services = SimpleNamespace(get_characteristic=lambda _uuid: None)

        async def get_services(self) -> object:
            return self.services

        async def disconnect(self) -> None:
            self.is_connected = False

    async def run() -> None:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test", (), WHITE_CHANNELS))  # type: ignore[arg-type]
        client = FakeClient()
        with patch("chihiros_led_control.client.establish_connection", return_value=client):
            with pytest.raises(CharacteristicMissingError):
                await device._ensure_connected()  # noqa: SLF001
        assert not client.is_connected
        assert device._client is None  # noqa: SLF001

    asyncio.run(run())


def test_connection_prelude_failure_cleans_up_connection() -> None:
    """A failed startup write stops notifications and disconnects the temporary client."""

    class FakeClient:
        is_connected = True
        services = SimpleNamespace(get_characteristic=lambda uuid: uuid)
        stopped = False

        async def start_notify(self, *_args: object) -> None:
            return None

        async def write_gatt_char(self, *_args: object) -> None:
            raise BleakError("prelude failed")

        async def stop_notify(self, _char: object) -> None:
            self.stopped = True

        async def disconnect(self) -> None:
            self.is_connected = False

    async def run() -> None:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test", (), WHITE_CHANNELS))  # type: ignore[arg-type]
        client = FakeClient()
        with patch("chihiros_led_control.client.establish_connection", return_value=client):
            with pytest.raises(BleakError, match="prelude failed"):
                await device._ensure_connected()  # noqa: SLF001
        assert client.stopped
        assert not client.is_connected
        assert device._client is None  # noqa: SLF001

    asyncio.run(run())


def test_unexpected_disconnect_aborts_command_batch() -> None:
    """A disconnect callback between writes aborts the remaining batch."""

    async def run() -> None:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test", (), WHITE_CHANNELS))  # type: ignore[arg-type]
        device._read_char = object()  # type: ignore[assignment]  # noqa: SLF001
        device._write_char = object()  # type: ignore[assignment]  # noqa: SLF001

        class FakeClient:
            calls = 0

            async def write_gatt_char(self, *_args: object) -> None:
                self.calls += 1
                device._disconnected(self)  # type: ignore[arg-type]  # noqa: SLF001

        client = FakeClient()
        device._client = client  # type: ignore[assignment]  # noqa: SLF001
        with pytest.raises(BleakError, match="unexpectedly disconnected"):
            await device._execute_command_locked([b"one", b"two"])  # noqa: SLF001
        assert client.calls == 1

    asyncio.run(run())


def test_disconnect_cleanup_tolerates_notification_and_disconnect_failures() -> None:
    """Cleanup clears client state even when both BLE cleanup calls fail."""

    async def run() -> None:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test", (), WHITE_CHANNELS))  # type: ignore[arg-type]

        class FakeClient:
            is_connected = True

            async def stop_notify(self, _char: object) -> None:
                raise BleakError("stop failed")

            async def disconnect(self) -> None:
                raise BleakError("disconnect failed")

        device._client = FakeClient()  # type: ignore[assignment]  # noqa: SLF001
        device._read_char = object()  # type: ignore[assignment]  # noqa: SLF001
        await device._execute_disconnect()  # noqa: SLF001
        assert device._client is None  # noqa: SLF001

    asyncio.run(run())


def test_notification_handler_stores_and_publishes_runtime_notification() -> None:
    """Parsed runtime notifications are stored and sent to subscribers."""
    received: list[RuntimeNotification] = []
    frame = bytearray.fromhex("5b170a00010a01ffffffffff13888c")

    async def run() -> ChihirosDevice:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test", (), WHITE_CHANNELS))  # type: ignore[arg-type]
        device.add_notification_callback(received.append)
        device._notification_handler(None, frame)  # type: ignore[arg-type]
        return device

    device = asyncio.run(run())
    assert device.last_runtime_notification == RuntimeNotification(
        firmware_version=23,
        runtime_minutes=511,
        raw=bytes(frame),
    )
    assert received == [device.last_runtime_notification]


def test_notification_handler_stores_and_publishes_schedule_snapshot() -> None:
    """Parsed schedule notifications are stored and sent to subscribers."""
    received: list[ScheduleSnapshotNotification] = []

    async def run() -> ChihirosDevice:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test", (), WHITE_CHANNELS))  # type: ignore[arg-type]
        device.add_notification_callback(received.append)
        device._notification_handler(
            None,  # type: ignore[arg-type]
            framed([0x5B, 0x17, 0, 0, 1, 0xFE, *([0] * 19), 8, 0, 50]),
        )
        return device

    device = asyncio.run(run())
    assert isinstance(device.last_schedule_snapshot_notification, ScheduleSnapshotNotification)
    assert received == [device.last_schedule_snapshot_notification]


def test_notification_handler_stores_and_publishes_fan_status() -> None:
    """Parsed fan status notifications are stored and sent to subscribers."""
    received: list[FanStatusNotification] = []
    frame = bytearray.fromhex("5b 1b 10 00 01 0b 02 58 19 00 01 00 00 00 00 00 48 22")

    async def run() -> ChihirosDevice:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test", (), WRGB_CHANNELS, has_fan=True))  # type: ignore[arg-type]
        device.add_notification_callback(received.append)
        device._notification_handler(None, frame)  # type: ignore[arg-type]
        return device

    device = asyncio.run(run())
    assert device.last_fan_status_notification == FanStatusNotification(
        firmware_version=27,
        fan_rpm=600,
        temperature_celsius=25,
        raw=bytes(frame),
    )
    assert received == [device.last_fan_status_notification]


def test_set_fan_speed_sends_captured_command_shape() -> None:
    """Fan speed commands use mode 0x0F with the speed percentage parameter."""
    sent_commands: list[bytes] = []

    async def run() -> None:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test", (), WRGB_CHANNELS, has_fan=True))  # type: ignore[arg-type]

        async def capture_command(command: list[bytes] | bytes | bytearray, retry: int | None = None) -> None:
            del retry
            sent_commands.append(bytes(command))

        device._send_command = capture_command  # type: ignore[method-assign]

        await device.set_fan_speed(100)

    asyncio.run(run())

    assert sent_commands[0][5:7] == bytes([15, 100])


def test_set_fan_speed_rejects_models_without_fan() -> None:
    """Fan control is limited to fan-equipped models."""

    async def run() -> None:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test", (), WHITE_CHANNELS))  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="fan"):
            await device.set_fan_speed(50)

    asyncio.run(run())


def test_set_brightness_sends_all_true_wrgb_channels() -> None:
    """Brightness commands can set red, green, blue, and white in one call."""
    sent_commands: list[list[bytes]] = []

    async def run() -> None:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test WRGB", (), WRGB_CHANNELS))  # type: ignore[arg-type]

        async def capture_command(command: list[bytes] | bytes | bytearray, retry: int | None = None) -> None:
            del retry
            assert isinstance(command, list)
            sent_commands.append([bytes(item) for item in command])

        device._send_command = capture_command  # type: ignore[method-assign]

        await device.set_brightness((10, 20, 30, 40))

    asyncio.run(run())

    assert [[command[6:8] for command in batch] for batch in sent_commands] == [
        [
            bytes([0, 10]),
            bytes([1, 20]),
            bytes([2, 30]),
            bytes([3, 40]),
        ]
    ]


def test_set_brightness_accepts_channel_mapping() -> None:
    """Brightness commands can target a named channel."""
    sent_commands: list[list[bytes]] = []

    async def run() -> None:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test WRGB", (), WRGB_CHANNELS))  # type: ignore[arg-type]

        async def capture_command(command: list[bytes] | bytes | bytearray, retry: int | None = None) -> None:
            del retry
            assert isinstance(command, list)
            sent_commands.append([bytes(item) for item in command])

        device._send_command = capture_command  # type: ignore[method-assign]

        await device.set_brightness({"white": 40})

    asyncio.run(run())

    assert [[command[6:8] for command in batch] for batch in sent_commands] == [[bytes([3, 40])]]


def test_notification_callback_failure_does_not_block_other_subscribers() -> None:
    """One failing notification subscriber does not prevent later subscribers."""
    received: list[RuntimeNotification] = []

    async def run() -> None:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test", (), WHITE_CHANNELS))  # type: ignore[arg-type]

        def fail(_notification: RuntimeNotification) -> None:
            raise RuntimeError("subscriber failed")

        device.add_notification_callback(fail)
        device.add_notification_callback(received.append)
        notification = RuntimeNotification(firmware_version=1, runtime_minutes=2, raw=b"test")
        device._notify_callbacks(notification)  # noqa: SLF001

    asyncio.run(run())

    assert len(received) == 1


def test_add_setting_sends_four_channel_brightness() -> None:
    """True WRGB auto schedules encode red, green, blue, and white levels."""
    sent_commands: list[bytes] = []

    async def run() -> None:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test WRGB", (), WRGB_CHANNELS))  # type: ignore[arg-type]

        async def capture_command(command: list[bytes] | bytes | bytearray, retry: int | None = None) -> None:
            del retry
            sent_commands.append(bytes(command))

        device._send_command = capture_command  # type: ignore[method-assign]

        await device.add_setting(
            sunrise=datetime(2026, 6, 14, 8, 0),
            sunset=datetime(2026, 6, 14, 18, 30),
            max_brightness=(10, 20, 30, 40),
        )

    asyncio.run(run())

    assert sent_commands[0][6:-1] == bytes([8, 0, 18, 30, 0, 127, 10, 20, 30, 40, 255, 255, 255, 255])


def test_add_setting_uses_white_channel_for_true_wrgb_models() -> None:
    """Single-channel auto schedules target the white slot on true WRGB models."""
    sent_commands: list[bytes] = []

    async def run() -> None:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test WRGB", (), WRGB_CHANNELS))  # type: ignore[arg-type]

        async def capture_command(command: list[bytes] | bytes | bytearray, retry: int | None = None) -> None:
            del retry
            sent_commands.append(bytes(command))

        device._send_command = capture_command  # type: ignore[method-assign]

        await device.add_setting(
            sunrise=datetime(2026, 6, 14, 8, 0),
            sunset=datetime(2026, 6, 14, 18, 30),
            max_brightness=40,
        )

    asyncio.run(run())

    assert sent_commands[0][6:-1] == bytes([8, 0, 18, 30, 0, 127, 255, 255, 255, 40, 255, 255, 255, 255])


def test_add_setting_uses_first_channel_when_model_has_no_white_channel() -> None:
    """Single-channel auto schedules keep targeting the first channel on RGB-only models."""
    sent_commands: list[bytes] = []

    async def run() -> None:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test RGB", (), RGB_CHANNELS))  # type: ignore[arg-type]

        async def capture_command(command: list[bytes] | bytes | bytearray, retry: int | None = None) -> None:
            del retry
            sent_commands.append(bytes(command))

        device._send_command = capture_command  # type: ignore[method-assign]

        await device.add_setting(
            sunrise=datetime(2026, 6, 14, 8, 0),
            sunset=datetime(2026, 6, 14, 18, 30),
            max_brightness=40,
        )

    asyncio.run(run())

    assert sent_commands[0][6:-1] == bytes([8, 0, 18, 30, 0, 127, 40, 255, 255, 255, 255, 255, 255, 255])


def test_set_manual_mode_sends_mode_switch_command() -> None:
    """Manual mode sends the vendor app's switchToManual frame without touching brightness."""
    sent_commands: list[bytes] = []

    async def run() -> None:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test", (), WHITE_CHANNELS))  # type: ignore[arg-type]

        async def capture_command(command: list[bytes] | bytes | bytearray, retry: int | None = None) -> None:
            del retry
            sent_commands.append(bytes(command))

        device._send_command = capture_command  # type: ignore[method-assign]

        await device.set_manual_mode()

    asyncio.run(run())

    assert sent_commands[0][5:8] == bytes([5, 11, 255])


def test_set_fan_speed_clamps_below_model_minimum() -> None:
    """VIVID3 fan speeds between 1 and 24 percent are clamped to the model minimum."""
    sent_commands: list[bytes] = []

    async def run() -> None:
        device = ChihirosDevice(
            FakeBLEDevice(),
            DeviceModel("VIVID3", (), WRGB_CHANNELS, has_fan=True, min_fan_speed=25),  # type: ignore[arg-type]
        )

        async def capture_command(command: list[bytes] | bytes | bytearray, retry: int | None = None) -> None:
            del retry
            sent_commands.append(bytes(command))

        device._send_command = capture_command  # type: ignore[method-assign]

        await device.set_fan_speed(10)
        await device.set_fan_speed(0)
        await device.set_fan_speed(30)

    asyncio.run(run())

    assert [command[6] for command in sent_commands] == [25, 0, 30]


def test_notification_handler_stores_and_publishes_dosing_totals() -> None:
    """Parsed dosing totals are stored and sent to subscribers."""
    received: list[DosingTotalsNotification] = []
    frame = bytearray([0xB6, 0x10, 0x10, 0x00, 0x01, 0x3C, 0x04, 0x1F, 0x00, 0x00])

    async def run() -> ChihirosDevice:
        device = ChihirosDosingPump(FakeBLEDevice(), DeviceModel("Dosing Pump", (), {}))  # type: ignore[arg-type]
        device.add_notification_callback(received.append)
        device._notification_handler(None, frame)  # type: ignore[arg-type]
        return device

    device = asyncio.run(run())
    assert device.last_dosing_totals_notification == DosingTotalsNotification(
        total_dosed_ul=(105500, 0),
        raw=bytes(frame),
    )
    assert received == [device.last_dosing_totals_notification]


def test_notification_handler_stores_and_publishes_dosing_daily() -> None:
    """Parsed dosing daily counters are stored and sent to subscribers."""
    received: list[DosingDailyNotification] = []
    frame = bytearray([0xB6, 0x10, 0x0E, 0x00, 0x01, 0x44, 0x00, 0x64, 0x01, 0x90])

    async def run() -> ChihirosDevice:
        device = ChihirosDosingPump(FakeBLEDevice(), DeviceModel("Dosing Pump", (), {}))  # type: ignore[arg-type]
        device.add_notification_callback(received.append)
        device._notification_handler(None, frame)  # type: ignore[arg-type]
        return device

    device = asyncio.run(run())
    assert device.last_dosing_daily_notification == DosingDailyNotification(
        dose_use_in_day_ul=(10000, 40000),
        raw=bytes(frame),
    )
    assert received == [device.last_dosing_daily_notification]


def test_notification_handler_stores_and_publishes_vivid3_fan_status() -> None:
    """VIVID3 fan readouts are stored and sent to subscribers."""
    received: list[Vivid3FanStatusNotification] = []
    frame = bytearray([0xB6, 0x00, 0x00, 0x00, 0x01, 0x16, 0x02, 0x58, 25])

    async def run() -> ChihirosDevice:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test", (), WRGB_CHANNELS, has_fan=True))  # type: ignore[arg-type]
        device.add_notification_callback(received.append)
        device._notification_handler(None, frame)  # type: ignore[arg-type]
        return device

    device = asyncio.run(run())
    assert device.last_vivid3_fan_status_notification == Vivid3FanStatusNotification(
        fan_rpm=600,
        temperature_celsius=25,
        raw=bytes(frame),
    )
    assert received == [device.last_vivid3_fan_status_notification]


def test_notification_handler_ignores_vivid3_fan_readout_on_non_fan_model() -> None:
    """0xB6/0x16 fan readout frames are ignored on models without a fan."""
    received: list[Vivid3FanStatusNotification] = []
    frame = bytearray([0xB6, 0x00, 0x00, 0x00, 0x01, 0x16, 0x02, 0x58, 25])

    async def run() -> ChihirosDevice:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test", (), RGB_CHANNELS))  # type: ignore[arg-type]
        device.add_notification_callback(received.append)
        device._notification_handler(None, frame)  # type: ignore[arg-type]
        return device

    device = asyncio.run(run())
    assert device.last_vivid3_fan_status_notification is None
    assert received == []


def test_set_fan_auto_sends_auto_mode_command() -> None:
    """Fan auto mode sends the vendor app's autoFan frame and tracks the mode."""
    sent_commands: list[bytes] = []

    async def run() -> None:
        device = ChihirosDevice(
            FakeBLEDevice(),
            DeviceModel("VIVID3", (), WRGB_CHANNELS, has_fan=True, min_fan_speed=25),  # type: ignore[arg-type]
        )
        assert device.fan_auto is False

        async def capture_command(command: list[bytes] | bytes | bytearray, retry: int | None = None) -> None:
            del retry
            sent_commands.append(bytes(command))

        device._send_command = capture_command  # type: ignore[method-assign]

        await device.set_fan_auto()

    asyncio.run(run())

    assert sent_commands[0][5:8] == bytes([5, 0x11, 0xFF])
    assert sent_commands[0][8] == 0xFF


def test_set_fan_speed_clears_fan_auto_mode() -> None:
    """A manual fan speed leaves temperature-controlled auto mode."""

    async def run() -> None:
        device = ChihirosDevice(
            FakeBLEDevice(),
            DeviceModel("VIVID3", (), WRGB_CHANNELS, has_fan=True, min_fan_speed=25),  # type: ignore[arg-type]
        )

        async def capture_command(command: list[bytes] | bytes | bytearray, retry: int | None = None) -> None:
            del retry
            del command

        device._send_command = capture_command  # type: ignore[method-assign]

        await device.set_fan_auto()
        assert device.fan_auto is True
        await device.set_fan_speed(50)
        assert device.fan_auto is False

    asyncio.run(run())


def test_set_fan_start_stop_temp_sends_command_and_stores_values() -> None:
    """Fan start/stop temperatures are sent and kept for the HA number entities."""
    sent_commands: list[bytes] = []

    async def run() -> None:
        device = ChihirosDevice(
            FakeBLEDevice(),
            DeviceModel("VIVID3", (), WRGB_CHANNELS, has_fan=True, min_fan_speed=25),  # type: ignore[arg-type]
        )
        assert (device.fan_start_temp, device.fan_stop_temp) == (38, 33)

        async def capture_command(command: list[bytes] | bytes | bytearray, retry: int | None = None) -> None:
            del retry
            sent_commands.append(bytes(command))

        device._send_command = capture_command  # type: ignore[method-assign]

        await device.set_fan_start_stop_temp(40, 35)

    asyncio.run(run())

    assert sent_commands[0][5:8] == bytes([0x2D, 40, 35])

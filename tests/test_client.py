"""Tests for the Chihiros BLE client."""

from __future__ import annotations

import asyncio
from datetime import datetime

from chihiros_led_control.client import ChihirosDevice
from chihiros_led_control.models import RGB_CHANNELS, WHITE_CHANNELS, WRGB_CHANNELS, DeviceModel
from chihiros_led_control.protocol import RuntimeNotification, ScheduleSnapshotNotification


class FakeBLEDevice:
    """Small BLEDevice stand-in for client tests."""

    def __init__(self) -> None:
        """Create a fake BLE device."""
        self.name = "DYNA2-test"
        self.address = "AA:BB:CC:DD:EE:FF"


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


def test_query_status_sends_runtime_status_query() -> None:
    """Status refresh sends the legacy runtime/status query."""
    sent_commands: list[bytes] = []

    async def run() -> None:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test", (), WHITE_CHANNELS))  # type: ignore[arg-type]

        async def capture_command(command: list[bytes] | bytes | bytearray, retry: int | None = None) -> None:
            del retry
            sent_commands.append(bytes(command))

        device._send_command = capture_command  # type: ignore[method-assign]

        await device.query_status()

    asyncio.run(run())

    assert sent_commands[0][5:7] == bytes([4, 1])


def test_notification_handler_stores_and_publishes_runtime_notification() -> None:
    """Parsed runtime notifications are stored and sent to subscribers."""
    received: list[RuntimeNotification] = []

    async def run() -> ChihirosDevice:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test", (), WHITE_CHANNELS))  # type: ignore[arg-type]
        device.add_notification_callback(received.append)
        device._notification_handler(None, bytearray.fromhex("5b170a00010a01ffffffffff13888c"))  # type: ignore[arg-type]
        return device

    device = asyncio.run(run())
    assert device.last_runtime_notification == RuntimeNotification(firmware_version=23, runtime_minutes=511)
    assert received == [RuntimeNotification(firmware_version=23, runtime_minutes=511)]


def test_notification_handler_stores_and_publishes_schedule_snapshot() -> None:
    """Parsed schedule notifications are stored and sent to subscribers."""
    received: list[ScheduleSnapshotNotification] = []

    async def run() -> ChihirosDevice:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test", (), WHITE_CHANNELS))  # type: ignore[arg-type]
        device.add_notification_callback(received.append)
        device._notification_handler(
            None,  # type: ignore[arg-type]
            bytearray([0x5B, 0x17, 0x08, 0x00, 0x01, 0xFE, 0x08, 0x00, 0x32]),
        )
        return device

    device = asyncio.run(run())
    assert isinstance(device.last_schedule_snapshot_notification, ScheduleSnapshotNotification)
    assert received == [device.last_schedule_snapshot_notification]


def test_set_brightness_sends_all_true_wrgb_channels() -> None:
    """Brightness commands can set red, green, blue, and white in one call."""
    sent_commands: list[bytes] = []

    async def run() -> None:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test WRGB", (), WRGB_CHANNELS))  # type: ignore[arg-type]

        async def capture_command(command: list[bytes] | bytes | bytearray, retry: int | None = None) -> None:
            del retry
            sent_commands.append(bytes(command))

        device._send_command = capture_command  # type: ignore[method-assign]

        await device.set_brightness((10, 20, 30, 40))

    asyncio.run(run())

    assert [command[6:8] for command in sent_commands] == [
        bytes([0, 10]),
        bytes([1, 20]),
        bytes([2, 30]),
        bytes([3, 40]),
    ]


def test_set_brightness_accepts_channel_mapping() -> None:
    """Brightness commands can target a named channel."""
    sent_commands: list[bytes] = []

    async def run() -> None:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test WRGB", (), WRGB_CHANNELS))  # type: ignore[arg-type]

        async def capture_command(command: list[bytes] | bytes | bytearray, retry: int | None = None) -> None:
            del retry
            sent_commands.append(bytes(command))

        device._send_command = capture_command  # type: ignore[method-assign]

        await device.set_brightness({"white": 40})

    asyncio.run(run())

    assert [command[6:8] for command in sent_commands] == [bytes([3, 40])]


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

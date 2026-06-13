"""Tests for Chihiros BLE protocol helpers."""

from __future__ import annotations

import datetime

from chihiros_led_control import commands
from chihiros_led_control.protocol import (
    RuntimeNotification,
    ScheduleSnapshotNotification,
    calculate_checksum,
    create_command_encoding,
    encode_timestamp,
    next_message_id,
    parse_notification,
)


def test_next_message_id_skips_reserved_lower_byte() -> None:
    """Message ids skip reserved lower byte 90."""
    assert next_message_id((0, 89)) == (0, 91)


def test_next_message_id_skips_reserved_higher_byte() -> None:
    """Message ids skip reserved higher byte 90."""
    assert next_message_id((89, 255)) == (91, 0)


def test_next_message_id_preserves_higher_byte() -> None:
    """Message ids increment the lower byte without resetting the higher byte."""
    assert next_message_id((1, 1)) == (1, 2)


def test_next_message_id_skips_reserved_lower_byte_with_higher_byte() -> None:
    """Message ids skip reserved lower byte without resetting the higher byte."""
    assert next_message_id((1, 89)) == (1, 91)


def test_next_message_id_wraps_after_maximum() -> None:
    """Message ids wrap after the maximum byte pair."""
    assert next_message_id((255, 255)) == (0, 1)


def test_calculate_checksum_xors_command_bytes() -> None:
    """Checksum is calculated by XORing bytes after the command id."""
    assert calculate_checksum(bytes([90, 1, 7, 0, 1, 7, 0, 100])) == 100


def test_command_encoding_sanitizes_reserved_parameter() -> None:
    """Command encoding avoids the reserved byte in parameters."""
    command = create_command_encoding(90, 7, (0, 1), [0, 90])

    assert command == bytearray([90, 1, 7, 0, 1, 7, 0, 89, 89])


def test_command_encoding_can_keep_reserved_parameter_for_newer_protocols() -> None:
    """Command encoding can keep reserved bytes when requested."""
    command = create_command_encoding(165, 27, (0, 1), [0, 90], avoid_reserved_byte=False)

    assert command == bytearray([165, 1, 7, 0, 1, 27, 0, 90, 70])


def test_command_encoding_normalizes_reserved_message_id() -> None:
    """Command encoding avoids reserved message IDs passed directly."""
    command = create_command_encoding(90, 7, (0, 90), [0, 100])

    assert command[3:5] == bytearray([0, 91])


def test_manual_setting_command_encoding() -> None:
    """Manual brightness commands encode color and brightness."""
    assert commands.create_manual_setting_command((0, 1), 0, 100) == bytearray([90, 1, 7, 0, 1, 7, 0, 100, 100])


def test_encode_timestamp() -> None:
    """Timestamps are encoded as protocol parameters."""
    timestamp = datetime.datetime(2026, 6, 11, 9, 8, 7)

    assert encode_timestamp(timestamp) == [26, 6, 4, 9, 8, 7]


def test_parse_runtime_notification() -> None:
    """Runtime notifications expose firmware and runtime minutes."""
    notification = parse_notification(bytearray.fromhex("5b170a00010a01ffffffffff13888c"))

    assert notification == RuntimeNotification(firmware_version=23, runtime_minutes=511)


def test_parse_schedule_snapshot_notification() -> None:
    """Schedule snapshot notifications expose saved curve points."""
    notification = parse_notification(
        bytearray(
            [
                0x5B,
                0x17,
                0x13,
                0x00,
                0x01,
                0xFE,
                0x0D,
                0x0F,
                0x00,
                0x0D,
                0x2D,
                0x64,
                0x15,
                0x0F,
                0x64,
                0x15,
                0x2D,
                0x00,
                0x00,
            ]
        )
    )

    assert notification == ScheduleSnapshotNotification(
        firmware_version=23,
        curve_points=((13, 15, 0), (13, 45, 100), (21, 15, 100), (21, 45, 0)),
    )

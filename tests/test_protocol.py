"""Tests for Chihiros BLE protocol helpers."""

from __future__ import annotations

import datetime

from chihiros_led_control import commands
from chihiros_led_control.protocol import (
    calculate_checksum,
    create_command_encoding,
    encode_timestamp,
    next_message_id,
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


def test_manual_setting_command_encoding() -> None:
    """Manual brightness commands encode color and brightness."""
    assert commands.create_manual_setting_command((0, 1), 0, 100) == bytearray(
        [90, 1, 7, 0, 1, 7, 0, 100, 100]
    )


def test_encode_timestamp() -> None:
    """Timestamps are encoded as protocol parameters."""
    timestamp = datetime.datetime(2026, 6, 11, 9, 8, 7)

    assert encode_timestamp(timestamp) == [26, 6, 4, 9, 8, 7]

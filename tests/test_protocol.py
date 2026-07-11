"""Tests for Chihiros BLE protocol helpers."""

from __future__ import annotations

import datetime

from chihiros_led_control import commands
from chihiros_led_control.models import RGB_CHANNELS, WHITE_CHANNELS, WRGB_CHANNELS
from chihiros_led_control.protocol import (
    RuntimeNotification,
    SchedulePoint,
    ScheduleSnapshotNotification,
    calculate_checksum,
    create_command_encoding,
    encode_timestamp,
    next_message_id,
    parse_notification,
)

SCHEDULE_SNAPSHOT_PREFIX = [
    0x5B,
    0x17,
    0x30,
    0x00,
    0x01,
    0xFE,
    0x01,
    0x12,
    0x0B,
    0x0D,
    0x0F,
    0x00,
    0x00,
    0x00,
    0x00,
    0x11,
    0x08,
    0x11,
    0x0C,
    0x11,
    0x13,
    0x00,
    0x01,
    0x12,
    0x0B,
]


def framed(values: list[int]) -> bytearray:
    """Add the declared length and checksum used by inbound frames."""
    frame = bytearray(values)
    frame[2] = len(frame) - 4
    frame.append(calculate_checksum(frame) ^ 0xFF)
    return frame


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


def test_set_brightness_command_encoding() -> None:
    """Brightness commands encode color and brightness."""
    assert commands.create_set_brightness_command((0, 1), 0, 100) == bytearray([90, 1, 7, 0, 1, 7, 0, 100, 100])


def test_base_auth_command_encoding() -> None:
    """Base auth commands use the LED status/auth frame."""
    assert commands.create_base_auth_command((0, 1)) == bytearray([90, 1, 6, 0, 1, 4, 1, 3])


def test_dosing_auth_command_encoding() -> None:
    """Dosing auth commands use DEVICE auth data 4 and 5."""
    assert commands.create_dose_auth_1_command((0, 4)) == bytearray([165, 1, 6, 0, 4, 4, 4, 3])
    assert commands.create_dose_auth_2_command((0, 5)) == bytearray([165, 1, 6, 0, 5, 4, 5, 3])


def test_manual_dose_command_encoding_small_volume() -> None:
    """Manual dose encoding is compatible with single-byte examples for small volumes."""
    assert commands.create_manual_dose_command((0, 6), 0, 2.0) == bytearray([165, 1, 10, 0, 6, 27, 0, 0, 0, 0, 20, 2])


def test_manual_dose_command_encoding_large_volume() -> None:
    """Manual dose encoding supports 25.6 mL buckets for larger volumes."""
    assert commands.split_dose_volume_ml(29.0) == (1, 34)
    command = commands.create_manual_dose_command((0, 6), 2, 29.0)
    assert command[6:-1] == bytearray([2, 0, 0, 1, 34])


def test_manual_dose_command_preserves_reserved_volume_bytes() -> None:
    """Dosing frames preserve 0x5A in either volume byte."""
    for volume_ml, expected in ((9.0, (0, 90)), (25.6, (1, 0)), (34.6, (1, 90))):
        command = commands.create_manual_dose_command((0, 6), 0, volume_ml)
        assert tuple(command[-3:-1]) == expected


def test_query_status_command_encoding() -> None:
    """Status query commands request runtime/status notifications."""
    assert commands.create_query_status_command((0, 1)) == commands.create_base_auth_command((0, 1))


def test_auto_setting_command_accepts_four_channel_brightness() -> None:
    """Auto schedule commands can encode true WRGB brightness values."""
    command = commands.create_add_auto_setting_command(
        (0, 1),
        datetime.time(8, 0),
        datetime.time(18, 30),
        (10, 20, 30, 40),
        15,
        127,
    )

    assert command[6:-1] == bytearray([8, 0, 18, 30, 15, 127, 10, 20, 30, 40, 255, 255, 255, 255])


def test_delete_auto_setting_command_matches_captured_frame() -> None:
    """Schedule deletion fills every brightness slot and produces the captured checksum."""
    command = commands.create_delete_auto_setting_command(
        (0, 0x17),
        datetime.time(2, 30),
        datetime.time(5, 10),
        1,
        127,
        brightness_channels=4,
    )

    assert command == bytearray.fromhex("A5 01 13 00 17 19 02 1E 05 0A 01 7F FF FF FF FF FF FF FF FF 71")


def test_encode_timestamp() -> None:
    """Timestamps are encoded as protocol parameters."""
    timestamp = datetime.datetime(2026, 6, 11, 9, 8, 7)

    assert encode_timestamp(timestamp) == [26, 6, 4, 9, 8, 7]


def test_parse_runtime_notification() -> None:
    """Runtime notifications expose firmware and runtime minutes."""
    frame = bytearray.fromhex("5b170a00010a01ffffffffff13888c")
    notification = parse_notification(frame)

    assert notification == RuntimeNotification(firmware_version=23, runtime_minutes=511, raw=bytes(frame))


def test_parse_notification_rejects_bad_length_and_checksum() -> None:
    """Inbound frames must match their declared length and checksum."""
    valid = bytearray.fromhex("5b170a00010a01ffffffffff13888c")
    bad_length = bytearray(valid)
    bad_length[2] -= 1
    bad_checksum = bytearray(valid)
    bad_checksum[-1] ^= 1
    assert parse_notification(bad_length) is None
    assert parse_notification(bad_checksum) is None
    assert parse_notification(bytes([0x5B, 0, 2, 0, 0, 0, 0])) is None


def test_parse_schedule_snapshot_notification_requires_channel_context() -> None:
    """Schedule snapshot notifications need model channel context."""
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

    assert notification is None


def test_parse_schedule_snapshot_notification_for_single_channel_model() -> None:
    """Single-channel schedule snapshots use the model channel name."""
    notification = parse_notification(
        framed([*SCHEDULE_SNAPSHOT_PREFIX, 0x08, 0x00, 0x32]),
        WHITE_CHANNELS,
    )

    assert notification == ScheduleSnapshotNotification(
        firmware_version=23,
        points=(SchedulePoint(hour=8, minute=0, levels={"white": 50}),),
    )


def test_parse_schedule_snapshot_notification_for_rgb_model() -> None:
    """RGB schedule snapshots apply each schedule level to all named channels."""
    notification = parse_notification(
        framed(
            [
                *SCHEDULE_SNAPSHOT_PREFIX,
                0x08,
                0x00,
                0x0A,
                0x12,
                0x1E,
                0x3C,
            ]
        ),
        RGB_CHANNELS,
    )

    assert notification == ScheduleSnapshotNotification(
        firmware_version=23,
        points=(
            SchedulePoint(hour=8, minute=0, levels={"red": 10, "green": 10, "blue": 10}),
            SchedulePoint(hour=18, minute=30, levels={"red": 60, "green": 60, "blue": 60}),
        ),
    )


def test_parse_schedule_snapshot_notification_for_true_wrgb_model() -> None:
    """True WRGB schedule snapshots apply each schedule level to all named channels."""
    notification = parse_notification(
        framed(
            [
                *SCHEDULE_SNAPSHOT_PREFIX,
                0x08,
                0x00,
                0x0A,
                0x12,
                0x1E,
                0x50,
            ]
        ),
        WRGB_CHANNELS,
    )

    assert notification == ScheduleSnapshotNotification(
        firmware_version=23,
        points=(
            SchedulePoint(hour=8, minute=0, levels={"red": 10, "green": 10, "blue": 10, "white": 10}),
            SchedulePoint(hour=18, minute=30, levels={"red": 80, "green": 80, "blue": 80, "white": 80}),
        ),
    )


def test_parse_captured_schedule_snapshot_notification_for_true_wrgb_model() -> None:
    """A captured WRGB response decodes its three-byte time and level records."""
    frame = framed(
        list(
            bytes.fromhex(
                "5B 15 30 00 01 FE 01 0C 0B 02 04 04 16 09 00 01 3B 02 04 02 16 00 01 0C 0B "
                "09 00 00 09 01 05 10 3B 05 11 00 00 11 01 41 15 3B 41 16 00 00 00 00 00 00"
            )
        )
    )

    notification = parse_notification(frame, WRGB_CHANNELS)

    assert notification == ScheduleSnapshotNotification(
        firmware_version=21,
        points=(
            SchedulePoint(hour=9, minute=0, levels={"red": 0, "green": 0, "blue": 0, "white": 0}),
            SchedulePoint(hour=9, minute=1, levels={"red": 5, "green": 5, "blue": 5, "white": 5}),
            SchedulePoint(hour=16, minute=59, levels={"red": 5, "green": 5, "blue": 5, "white": 5}),
            SchedulePoint(hour=17, minute=0, levels={"red": 0, "green": 0, "blue": 0, "white": 0}),
            SchedulePoint(hour=17, minute=1, levels={"red": 65, "green": 65, "blue": 65, "white": 65}),
            SchedulePoint(hour=21, minute=59, levels={"red": 65, "green": 65, "blue": 65, "white": 65}),
            SchedulePoint(hour=22, minute=0, levels={"red": 0, "green": 0, "blue": 0, "white": 0}),
        ),
    )


def test_parse_schedule_snapshot_notification_skips_metadata_prefix() -> None:
    """Schedule snapshots skip status metadata before hour/minute/level data points."""
    notification = parse_notification(
        framed(
            [
                *SCHEDULE_SNAPSHOT_PREFIX,
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
            ]
        ),
        WHITE_CHANNELS,
    )

    assert notification == ScheduleSnapshotNotification(
        firmware_version=23,
        points=(
            SchedulePoint(hour=13, minute=15, levels={"white": 0}),
            SchedulePoint(hour=13, minute=45, levels={"white": 100}),
            SchedulePoint(hour=21, minute=15, levels={"white": 100}),
            SchedulePoint(hour=21, minute=45, levels={"white": 0}),
        ),
    )

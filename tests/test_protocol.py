"""Tests for Chihiros BLE protocol helpers."""

from __future__ import annotations

import datetime

from chihiros_led_control import commands
from chihiros_led_control.models import RGB_CHANNELS, WHITE_CHANNELS, WRGB_CHANNELS
from chihiros_led_control.protocol import (
    DosingDailyNotification,
    DosingTotalsNotification,
    FanStatusNotification,
    RuntimeNotification,
    SchedulePoint,
    ScheduleSnapshotNotification,
    Vivid3FanStatusNotification,
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
    """Message ids skip the reserved lower byte 90 (0x5A)."""
    assert next_message_id((0, 89)) == (0, 91)


def test_next_message_id_skips_reserved_higher_byte() -> None:
    """Message ids skip the reserved higher byte 90 (0x5A)."""
    assert next_message_id((89, 255)) == (91, 0)


def test_next_message_id_does_not_skip_notification_header_byte() -> None:
    """0x5B is NOT skipped as a sequence byte (the 2.8.59 app skips only 0x5A)."""
    assert next_message_id((0, 90)) == (0, 91)


def test_next_message_id_preserves_higher_byte() -> None:
    """Message ids increment the lower byte without resetting the higher byte."""
    assert next_message_id((1, 1)) == (1, 2)


def test_next_message_id_skips_reserved_lower_byte_with_higher_byte() -> None:
    """Message ids skip the reserved lower byte without resetting the higher byte."""
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
    """Command encoding avoids the reserved message ID 0x5A passed directly."""
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


def test_set_fan_speed_command_matches_captured_frames() -> None:
    """Fan speed commands match frames captured from a WRGB VIVID III."""
    assert commands.create_set_fan_speed_command((0, 0xC7), 100) == bytearray.fromhex("5A 01 06 00 C7 0F 64 AB")
    assert commands.create_set_fan_speed_command((0, 0xC5), 53) == bytearray.fromhex("5A 01 06 00 C5 0F 35 F8")
    assert commands.create_set_fan_speed_command((0, 0xCB), 0) == bytearray.fromhex("5A 01 06 00 CB 0F 00 C3")


def test_set_fan_speed_command_validates_range() -> None:
    """Fan speed commands reject out-of-range percentages."""
    for speed in (-1, 101):
        try:
            commands.create_set_fan_speed_command((0, 1), speed)
        except ValueError:
            continue
        raise AssertionError(f"Expected ValueError for fan speed {speed}")


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


def test_auto_point_command_sea_led_family_uses_hour_minute_encoding() -> None:
    """SeaLed-family auto points encode [channel, hour, minute, level] (app's setSeaLedAutoCode)."""
    command = commands.create_auto_point_command((0, 1), 2, 8 * 60 + 30, 80, sea_led_family=True)

    # 0x5A, 6, [2, 8, 30, 80] with checksum over bytes 1..n-2
    assert command[0] == 0x5A
    assert command[2] == 4 + 5
    assert command[5] == 6
    assert command[6:-1] == bytearray([2, 8, 30, 80])
    assert command[-1] == calculate_checksum(command[:-1])


def test_auto_point_command_bleled_family_uses_30_minute_slots() -> None:
    """BleLed-family auto points encode [channel, 30-minute-slot, level] (app's setAutoCode)."""
    command = commands.create_auto_point_command((0, 1), 3, 8 * 60 + 30, 60, sea_led_family=False)

    # 8:30 is slot 17; 3-byte payload [3, 17, 60]
    assert command[6:-1] == bytearray([3, 17, 60])

    # 8:45 rounds up to slot 18 (remainder 15 > 14, matching the app's rule)
    command = commands.create_auto_point_command((0, 1), 3, 8 * 60 + 45, 60, sea_led_family=False)
    assert command[6:-1] == bytearray([3, 18, 60])

    # 8:44 stays in slot 17 (remainder 14 is not above the rounding threshold)
    command = commands.create_auto_point_command((0, 1), 3, 8 * 60 + 44, 60, sea_led_family=False)
    assert command[6:-1] == bytearray([3, 17, 60])


def test_auto_point_command_slot_boundaries() -> None:
    """BleLed slot boundaries: midnight is slot 0, 23:59 wraps to slot 48, 48 h is slot 96."""
    assert commands.create_auto_point_command((0, 1), 0, 0, 0, sea_led_family=False)[6:-1] == bytearray([0, 0, 0])
    assert commands.create_auto_point_command((0, 1), 0, 1439, 0, sea_led_family=False)[6:-1] == bytearray([0, 48, 0])
    assert commands.create_auto_point_command((0, 1), 0, 2880, 0, sea_led_family=False)[6:-1] == bytearray([0, 96, 0])


def test_auto_point_command_sea_led_family_hour_boundary() -> None:
    """SeaLed auto points wrap minutes into hour/minute fields (48 h → hour 48)."""
    command = commands.create_auto_point_command((0, 1), 1, 2880, 90, sea_led_family=True)

    assert command[6:-1] == bytearray([1, 48, 0, 0x5A])


def test_auto_point_command_sends_reserved_byte_verbatim() -> None:
    """Level 90 (0x5A) is sent verbatim: the 2.8.59 app's formatData does not escape payload bytes."""
    command = commands.create_auto_point_command((0, 1), 1, 10 * 60, 90, sea_led_family=True)

    assert command[6:-1] == bytearray([1, 10, 0, 0x5A])


def test_auto_point_command_validates_range() -> None:
    """Auto point commands reject out-of-range channels, minutes, and levels."""
    for kwargs in (
        {"channel": 8, "minutes": 60, "level": 50},
        {"channel": 0, "minutes": -1, "level": 50},
        {"channel": 0, "minutes": 2881, "level": 50},
        {"channel": 0, "minutes": 60, "level": 101},
    ):
        try:
            commands.create_auto_point_command((0, 1), sea_led_family=False, **kwargs)
        except ValueError:
            continue
        raise AssertionError(f"Expected ValueError for {kwargs}")


def test_encode_timestamp() -> None:
    """Timestamps are encoded as protocol parameters."""
    timestamp = datetime.datetime(2026, 6, 11, 9, 8, 7)

    assert encode_timestamp(timestamp) == [26, 6, 4, 9, 8, 7]


def test_parse_runtime_notification() -> None:
    """Runtime notifications expose firmware and runtime minutes."""
    frame = bytearray.fromhex("5b170a00010a01ffffffffff13888c")
    notification = parse_notification(frame)

    assert notification == RuntimeNotification(firmware_version=23, runtime_minutes=511, raw=bytes(frame))


def test_parse_runtime_notification_accepts_legacy_framing() -> None:
    """Runtime notifications tolerate unreliable legacy length and checksum bytes."""
    valid = bytearray.fromhex("5b170a00010a01ffffffffff13888c")
    bad_length = bytearray(valid)
    bad_length[2] -= 1
    bad_checksum = bytearray(valid)
    bad_checksum[-1] ^= 1
    assert parse_notification(bad_length) == RuntimeNotification(firmware_version=23, runtime_minutes=511)
    assert parse_notification(bad_checksum) == RuntimeNotification(firmware_version=23, runtime_minutes=511)


def test_parse_notification_rejects_short_or_unknown_frames() -> None:
    """Inbound frames still require the Chihiros header and mode fields."""
    assert parse_notification(bytes([0x5B, 0, 2, 0, 0, 0, 0])) is None
    assert parse_notification(bytes([0x00, 0, 3, 0, 0, 0, 0, 0])) is None


def test_parse_fan_status_notification() -> None:
    """Fan status notifications expose firmware, fan RPM, and temperature."""
    frame = bytearray.fromhex("5b 1b 10 00 01 0b 02 58 19 00 01 00 00 00 00 00 48 22")
    notification = parse_notification(frame)

    assert notification == FanStatusNotification(
        firmware_version=27, fan_rpm=600, temperature_celsius=25, raw=bytes(frame)
    )


def test_parse_fan_status_notification_tolerates_trailing_counter() -> None:
    """Fan status notifications parse regardless of the trailing uptime counter byte."""
    running = bytearray.fromhex("5b 1b 10 00 01 0b 07 bc 19 00 01 00 00 00 00 00 57 22")
    idle = bytearray.fromhex("5b 1b 10 00 01 0b 00 1e 18 00 01 00 00 00 00 00 00 22")

    assert parse_notification(running) == FanStatusNotification(
        firmware_version=27, fan_rpm=1980, temperature_celsius=25
    )
    assert parse_notification(idle) == FanStatusNotification(firmware_version=27, fan_rpm=30, temperature_celsius=24)


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


def test_parse_dosing_totals_notification() -> None:
    """Dosing lifetime totals use 0xB6 header, mode 0x3C, 16-bit x100 uL counters."""
    frame = bytearray([0xB6, 0x10, 0x10, 0x00, 0x01, 0x3C, 0x04, 0x1F, 0x00, 0x00, 0x05, 0xDC, 0x00, 0x00])
    notification = parse_notification(frame)

    assert notification == DosingTotalsNotification(total_dosed_ul=(105500, 0, 150000, 0), raw=bytes(frame))


def test_parse_dosing_daily_notification() -> None:
    """Dosing dosed-today totals use 0xB6 header, mode 0x44, 16-bit x100 uL counters."""
    frame = bytearray([0xB6, 0x10, 0x0E, 0x00, 0x01, 0x44, 0x00, 0x64, 0x01, 0x90])
    notification = parse_notification(frame)

    assert notification == DosingDailyNotification(dose_use_in_day_ul=(10000, 40000), raw=bytes(frame))


def test_parse_dosing_notification_requires_minimum_length() -> None:
    """Dosing notifications without a full channel counter are ignored."""
    assert parse_notification(bytearray([0xB6, 0, 0, 0, 0, 0x3C, 0])) is None
    assert parse_notification(bytearray([0xB6, 0, 0, 0, 0, 0x44, 0])) is None


def test_parse_vivid3_fan_status_notification() -> None:
    """VIVID3 fan readouts use 0xB6 header, mode 0x16, with RPM and temperature."""
    frame = bytearray([0xB6, 0x00, 0x00, 0x00, 0x01, 0x16, 0x02, 0x58, 25])
    notification = parse_notification(frame)

    assert notification == Vivid3FanStatusNotification(fan_rpm=600, temperature_celsius=25, raw=bytes(frame))


def test_parse_unknown_b6_modes_are_ignored() -> None:
    """Unknown 0xB6 frames (heater, standalone fan) are not misparsed."""
    assert parse_notification(bytearray([0xB6, 0, 0, 0, 0, 0x4A, 1, 2, 3, 4, 5, 6])) is None


def test_switch_to_manual_mode_command_encoding() -> None:
    """Manual mode switch uses the vendor app's [11, 255, 255] payload."""
    assert commands.create_switch_to_manual_mode_command((0, 1)) == bytearray([90, 1, 8, 0, 1, 5, 11, 255, 255, 6])


def test_manual_dose_command_accepts_eight_channels() -> None:
    """Dosing pumps expose up to eight channels."""
    command = commands.create_manual_dose_command((0, 6), 7, 2.0)
    assert command[6] == 7
    for pump_idx in (-1, 8):
        try:
            commands.create_manual_dose_command((0, 6), pump_idx, 2.0)
        except ValueError:
            continue
        raise AssertionError(f"Expected ValueError for pump index {pump_idx}")


def test_fan_auto_mode_command_encoding() -> None:
    """Fan auto mode uses the vendor app's autoFan frame (0x5A, 5, [0x11, 0xFF, 0xFF])."""
    assert commands.create_fan_auto_mode_command((0, 1)) == bytearray.fromhex("5a 01 08 00 01 05 11 ff ff 1c")


def test_vivid3_fan_start_stop_temp_command_encoding() -> None:
    """VIVID3 fan start/stop temps use (0xA5, 45, [start, stop])."""
    assert commands.create_vivid3_fan_start_stop_temp_command((0, 2), 38, 33) == bytearray.fromhex(
        "a5 01 07 00 02 2d 26 21 2e"
    )


def test_vivid3_fan_start_stop_temp_preserves_reserved_byte() -> None:
    """Temperature payload bytes are sent verbatim even when they equal 0x5A."""
    command = commands.create_vivid3_fan_start_stop_temp_command((0, 4), 90, 30)
    assert command[6:8] == bytearray([90, 30])


def test_vivid3_fan_start_stop_temp_validates_range() -> None:
    """Fan start/stop temperatures must fit a single byte."""
    for start, stop in ((-1, 33), (38, 256), (300, 33)):
        try:
            commands.create_vivid3_fan_start_stop_temp_command((0, 1), start, stop)
        except ValueError:
            continue
        raise AssertionError(f"Expected ValueError for temperatures {start}/{stop}")


def test_vivid3_temp_protect_command_encoding() -> None:
    """VIVID3 temperature protection uses (0x5A, 5, [0x31|0x30, 0xFF, 0xFF])."""
    assert commands.create_vivid3_temp_protect_command((0, 1), True) == bytearray.fromhex(
        "5a 01 08 00 01 05 31 ff ff 3c"
    )
    assert commands.create_vivid3_temp_protect_command((0, 1), False) == bytearray.fromhex(
        "5a 01 08 00 01 05 30 ff ff 3d"
    )


def test_vivid3_bluetooth_led_command_encoding() -> None:
    """VIVID3 indicator LED uses (0x5A, 5, [0x32|0x31, 0xFF, 0xFF])."""
    assert commands.create_vivid3_bluetooth_led_command((0, 1), True) == bytearray.fromhex(
        "5a 01 08 00 01 05 32 ff ff 3f"
    )
    assert commands.create_vivid3_bluetooth_led_command((0, 1), False) == bytearray.fromhex(
        "5a 01 08 00 01 05 31 ff ff 3c"
    )

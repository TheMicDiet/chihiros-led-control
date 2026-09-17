"""Protocol tests for dosing-pump and magnetic-stirrer commands.

Expected bytes follow the reverse engineering of My Chihiros 2.8.59
(``chihiros_xapk/DOSING_CONTROL.md``): all pump/stirrer frames use header
``0xA5`` with payload bytes sent verbatim (no reserved-byte escaping), and
volumes ride as 0.1 mL buckets ``(hi << 8 | lo)``.
"""

from __future__ import annotations

import pytest

from chihiros_led_control import commands
from chihiros_led_control.commands import DosingMode, DosingWorkPoint
from chihiros_led_control.models import DOSING_PUMP, MAG_STIRRER
from chihiros_led_control.protocol import DosingDailyNotification, DosingTotalsNotification

MSG_ID = (0, 6)


def _payload(frame: bytes | bytearray) -> list[int]:
    """Return the command payload of an encoded frame (mode byte excluded)."""
    return list(frame[6:-1])


def test_encode_dose_volume_ml_matches_app_volume_change() -> None:
    """Volumes encode as 0.1 mL buckets; the app example 105.5 mL -> (4, 31)."""
    assert commands.encode_dose_volume_ml(105.5) == (4, 31)
    assert commands.encode_dose_volume_ml(0.0) == (0, 0)
    assert commands.encode_dose_volume_ml(2.0) == (0, 20)
    assert commands.encode_dose_volume_ml(6553.5) == (255, 255)
    # Sub-0.1 mL remainders floor like the app's truncating ~/100 on µL.
    assert commands.encode_dose_volume_ml(105.55) == (4, 31)
    assert commands.encode_dose_volume_ml(0.7) == (0, 7)
    with pytest.raises(ValueError, match="between 0 and"):
        commands.encode_dose_volume_ml(6553.6)
    with pytest.raises(ValueError, match="between 0 and"):
        commands.encode_dose_volume_ml(-0.1)


def test_stirrer_runtime_dosage_round_trip() -> None:
    """Stir minutes convert to dosage at 0.6 mL/min and back."""
    assert commands.stirrer_dosage_for_minutes(10) == pytest.approx(6.0)
    assert commands.stirrer_minutes_for_dosage(6.0) == pytest.approx(10.0)


def test_create_dosing_set_command_payload() -> None:
    """DosingSet carries [channel, frequency, 1, first?0:1, vol_hi, vol_lo]."""
    frame = commands.create_dosing_set_command(MSG_ID, 0, 10.0, 127, is_first_setting=True)
    assert frame[0] == 165 and frame[5] == 27
    assert _payload(frame) == [0, 127, 1, 0, 0, 100]
    not_first = commands.create_dosing_set_command(MSG_ID, 3, 0.0, 1, is_first_setting=False)
    assert _payload(not_first) == [3, 1, 1, 1, 0, 0]
    null_volume = commands.create_dosing_set_command(MSG_ID, 0, None, 127, is_first_setting=True)
    assert _payload(null_volume)[-2:] == [255, 255]
    with pytest.raises(ValueError, match="Channel"):
        commands.create_dosing_set_command(MSG_ID, 8, 1.0, 127, is_first_setting=True)
    with pytest.raises(ValueError, match="Frequency"):
        commands.create_dosing_set_command(MSG_ID, 0, 1.0, 256, is_first_setting=True)


def test_create_manual_dose_and_set_share_volume_encoding() -> None:
    """Manual dose and dosingSet encode volumes identically (105.5 mL -> 4, 31)."""
    manual = commands.create_manual_dose_command(MSG_ID, 0, 105.5)
    assert _payload(manual) == [0, 0, 0, 4, 31]


def test_single_mode_schedule_frame_per_point() -> None:
    """Single/auto modes emit one 0xA5/21 frame per point with the mode byte."""
    points = [DosingWorkPoint(8, 30, volume_ml=2.5), DosingWorkPoint(20, 0, volume_ml=105.5)]
    for mode in (DosingMode.SINGLE, DosingMode.AUTO):
        frames = commands.create_dosing_schedule_command(MSG_ID, 1, mode, points)
        assert len(frames) == 2
        for frame, point in zip(frames, points, strict=True):
            assert frame[0] == 165 and frame[5] == 21
            assert _payload(frame) == [
                1,
                int(mode),
                point.start_hour,
                point.start_minute,
                *commands.encode_dose_volume_ml(point.volume_ml),
            ]


def test_timer_mode_schedule_batches_records() -> None:
    """Timer mode batches [channel, 3] + records, flushing when payload exceeds 50.

    App parity: records are appended first, so a frame can carry up to 54
    payload bytes (header + 13 timer records) before the accumulator resets.
    """
    points = [DosingWorkPoint(hour, 0, volume_ml=5.0) for hour in range(13)]
    frames = commands.create_dosing_schedule_command(MSG_ID, 0, DosingMode.TIMER, points)
    assert len(frames) == 1  # 2 header bytes + 13 * 4 = 54 payload bytes
    frame = frames[0]
    assert frame[5] == 21
    assert len(frame) - 7 == 54
    assert list(frame[6:12]) == [0, 3, 0, 0, 0, 50]
    assert list(frame[6:-1])[2::4] == [point.start_hour for point in points]
    # 25 points overflow once mid-batch: a 54-byte frame then a 50-byte frame.
    more = commands.create_dosing_schedule_command(
        MSG_ID, 0, DosingMode.TIMER, [DosingWorkPoint(h % 24, 0, volume_ml=5.0) for h in range(25)]
    )
    assert [len(f) - 7 for f in more] == [54, 50]
    assert list(more[1][6:8]) == [0, 3]


def test_free_mode_schedule_carries_windows_not_volumes() -> None:
    """Free mode batches [channel] + [sh, sm, eh, em, number] with no volume."""
    points = [
        DosingWorkPoint(8, 0, duration_minutes=120, number=3),
        DosingWorkPoint(23, 0, duration_minutes=180, number=1),
    ]
    frames = commands.create_dosing_schedule_command(MSG_ID, 1, DosingMode.FREE, points)
    assert len(frames) == 1
    assert frames[0][5] == 23
    # The app does not wrap the end hour past midnight: 23:00 + 180 min -> 26:00.
    assert _payload(frames[0]) == [1, 8, 0, 10, 0, 3, 23, 0, 26, 0, 1]


def test_schedule_validation() -> None:
    """Schedule builders reject empty lists, bad times, and bad free windows."""
    with pytest.raises(ValueError, match="At least one work point"):
        commands.create_dosing_schedule_command(MSG_ID, 0, DosingMode.TIMER, [])
    with pytest.raises(ValueError, match="Channel"):
        commands.create_dosing_schedule_command(MSG_ID, 9, DosingMode.TIMER, [DosingWorkPoint(0, 0, 1.0)])
    with pytest.raises(ValueError, match="wall-clock"):
        commands.create_dosing_schedule_command(MSG_ID, 0, DosingMode.SINGLE, [DosingWorkPoint(24, 0, 1.0)])
    with pytest.raises(ValueError, match="duration"):
        commands.create_dosing_schedule_command(MSG_ID, 0, DosingMode.FREE, [DosingWorkPoint(0, 0, duration_minutes=0)])


def test_reset_commands_use_channel_offset_payloads() -> None:
    """ResetDosingChannel uses ch+25 and resetTotalDosing ch+21, both 255-filled."""
    channel = commands.create_reset_dosing_channel_command(MSG_ID, 2)
    totals = commands.create_reset_total_dosing_command(MSG_ID, 2)
    assert channel[5] == 5 and _payload(channel) == [27, 255, 255]
    assert totals[5] == 5 and _payload(totals) == [23, 255, 255]


def test_calibrate_command_encodes_time_and_volume() -> None:
    """DosingCalibrate sends [ch, time?255, vol_int, vol_frac] with 2.5 mL -> (2, 50)."""
    timed = commands.create_dosing_calibrate_command(MSG_ID, 0, seconds=10)
    assert _payload(timed) == [0, 10, 255, 255]
    measured = commands.create_dosing_calibrate_command(MSG_ID, 0, volume_ml=2.5)
    assert _payload(measured) == [0, 255, 2, 50]
    both = commands.create_dosing_calibrate_command(MSG_ID, 0, seconds=10, volume_ml=8.0)
    assert _payload(both) == [0, 10, 8, 0]
    # The fraction byte is rounded half-up like the app (LibcRound @ 0xa69398)
    # and may be 100, which the device reads as the next whole mL (2.999 mL).
    assert _payload(commands.create_dosing_calibrate_command(MSG_ID, 0, volume_ml=2.999)) == [0, 255, 2, 100]
    assert _payload(commands.create_dosing_calibrate_command(MSG_ID, 0, volume_ml=0.999)) == [0, 255, 0, 100]
    # Boundary checks: 2.994 mL stays at 99; 2.985 mL rounds to 99 (not 98, no banker's rounding).
    assert _payload(commands.create_dosing_calibrate_command(MSG_ID, 0, volume_ml=2.994)) == [0, 255, 2, 99]
    assert _payload(commands.create_dosing_calibrate_command(MSG_ID, 0, volume_ml=2.985)) == [0, 255, 2, 99]
    with pytest.raises(ValueError, match="seconds"):
        commands.create_dosing_calibrate_command(MSG_ID, 0, seconds=256)
    # 255 is the wire sentinel for "omitted", so it cannot be an explicit run time.
    with pytest.raises(ValueError, match="0 and 254"):
        commands.create_dosing_calibrate_command(MSG_ID, 0, seconds=255)
    with pytest.raises(ValueError, match="volume"):
        commands.create_dosing_calibrate_command(MSG_ID, 0, volume_ml=256.0)


def test_dose_delay_and_active_compensation_commands() -> None:
    """Dose delay is a single boolean byte; active/compensation has inverted order."""
    delay_on = commands.create_set_dosing_delay_command(MSG_ID, True)
    delay_off = commands.create_set_dosing_delay_command(MSG_ID, False)
    assert delay_on[5] == 31 and _payload(delay_on) == [1]
    assert _payload(delay_off) == [0]
    active = commands.create_dosing_active_compensation_command(MSG_ID, 4, active=True, compensate=False)
    assert active[5] == 32 and _payload(active) == [4, 0, 1]
    inactive = commands.create_dosing_active_compensation_command(MSG_ID, 4, active=False, compensate=True)
    assert _payload(inactive) == [4, 1, 0]


def test_dosing_channel_color_command() -> None:
    """The new-generation pump color frame is (0xA5, 59, [channel, color])."""
    frame = commands.create_dosing_channel_color_command(MSG_ID, 2, 3)
    assert frame[5] == 59 and _payload(frame) == [2, 3]


def test_general_temp_run_command_layout() -> None:
    """GeneralTempSet is [min][sec][8 channel bytes]; duration comes first."""
    # Binary-verified against My Chihiros 2.8.59: generalTempSet builds the
    # [min, sec] list first, then appends the channel bytes (addAll at
    # 0x920030). The old [channels][min][sec] order shifted every channel up
    # by two device slots (GitHub issue #115: switches 1-2 hit the duration
    # bytes, switches 3-6 drove physical channels 1-4).
    unlimited = commands.create_general_temp_run_command(MSG_ID, {2: True})
    assert unlimited[5] == 20
    assert _payload(unlimited) == [255, 255, 255, 255, 1, 255, 255, 255, 255, 255]
    stopped = commands.create_general_temp_run_command(MSG_ID, {0: False})
    assert _payload(stopped)[2] == 0
    timed = commands.create_general_temp_run_command(MSG_ID, {7: True}, seconds=90)
    assert _payload(timed)[:2] == [1, 30]
    with pytest.raises(ValueError, match="Duration"):
        commands.create_general_temp_run_command(MSG_ID, {0: True}, seconds=15360)
    with pytest.raises(ValueError, match="Channel"):
        commands.create_general_temp_run_command(MSG_ID, {8: True})


def test_stirrer_pre_second_command_layout() -> None:
    """StirrerPreSecond is (0xA5, 42, [channel, sec_hi, sec_lo, speed])."""
    frame = commands.create_stirrer_pre_second_command(MSG_ID, 0, 90, 40)
    assert frame[5] == 42
    assert _payload(frame) == [0, 0, 90, 40]
    long_run = commands.create_stirrer_pre_second_command(MSG_ID, 3, 999, 100)
    assert _payload(long_run) == [3, 3, 231, 100]
    with pytest.raises(ValueError, match="999"):
        commands.create_stirrer_pre_second_command(MSG_ID, 0, 1000, 40)
    with pytest.raises(ValueError, match="speed"):
        commands.create_stirrer_pre_second_command(MSG_ID, 0, 90, 101)


def test_parse_captured_firmware_reply_frames() -> None:
    """0x5B uplink replies (fw 07.25.18 quirk) parse with identical scaling.

    Values from the DOSING_CONTROL.md §7.3 capture: today 60.0/205.2/33.9/19.5
    mL on channels 0..3. Wire bytes are 0.1 mL units, ×100 for µL, at the same
    positions as the 0xB6 0x3C/0x44 frames; the trailing checksum byte sits
    outside the channel region.
    """
    from chihiros_led_control.protocol import parse_notification

    today_frame = bytes.fromhex("5b 01 0a 00 01 22 02 58 08 04 01 53 00 c3 7f")
    parsed = parse_notification(today_frame)
    assert parsed == DosingDailyNotification((60000, 205200, 33900, 19500), today_frame)

    # Lifetime "after" row of the same capture: 69.0/1010.8/217.0/184.0 mL.
    lifetime_frame = bytes.fromhex("5b 01 0a 00 02 1e 02 b2 27 7c 08 7a 07 30 84")
    parsed = parse_notification(lifetime_frame)
    assert parsed == DosingTotalsNotification((69000, 1010800, 217000, 184000), lifetime_frame)


def test_dosing_frames_are_not_reserved_byte_escaped() -> None:
    """Dosing payloads pass bytes verbatim: a 90-byte payload value stays 0x5A."""
    frame = commands.create_stirrer_pre_second_command(MSG_ID, 0, 90, commands.STIRRER_SPEED_DEFAULT)
    assert frame[8] == 0x5A  # 90 seconds, unescaped
    # The checksum stays valid.
    from chihiros_led_control.protocol import calculate_checksum

    assert calculate_checksum(frame[:-1]) == frame[-1]


def test_stirrer_model_registry() -> None:
    """The stirrer model is registered under its DYMIXR advertisement prefix."""
    assert MAG_STIRRER.advertised_codes == ("DYMIXR",)
    assert DOSING_PUMP.advertised_codes == ("DYDOSE", "DYDOSED", "DYTDOS", "DYNDOS")

"""Tests for the Chihiros BLE client."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime

import pytest
from bleak_retry_connector import BleakError

from chihiros_led_control.devices import ChihirosDevice, ChihirosDosingPump, ChihirosMagStirrer
from chihiros_led_control.models import (
    RGB_CHANNELS,
    WHITE_CHANNELS,
    WRGB_CHANNELS,
    DeviceModel,
    LedFeature,
    LedProtocol,
    LedSpec,
)
from chihiros_led_control.protocol.frame import calculate_checksum
from chihiros_led_control.protocol.notifications import (
    DosingDailyNotification,
    DosingTotalsNotification,
    FanStatusNotification,
    RuntimeNotification,
    ScheduleSnapshotNotification,
)
from chihiros_led_control.registry import DOSING_PUMP, MAG_STIRRER
from chihiros_led_control.testing import ScriptedTransport


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
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test", (), LedSpec(WHITE_CHANNELS)))  # type: ignore[arg-type]

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
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test", (), LedSpec(WHITE_CHANNELS)))  # type: ignore[arg-type]

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
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test", (), LedSpec(WHITE_CHANNELS)))  # type: ignore[arg-type]

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


def test_dosing_pump_status_queries_counters_in_app_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dosing refresh batches lifetime/daily queries and waits for their replies."""
    from chihiros_led_control.devices import dosing

    sent_commands: list[list[bytes]] = []
    notification_waits: list[float] = []
    monkeypatch.setattr(dosing, "STATUS_NOTIFICATION_WAIT", 2.5)

    async def run() -> None:
        device = ChihirosDosingPump(FakeBLEDevice(), DOSING_PUMP)  # type: ignore[arg-type]

        async def capture_command(
            command: list[bytes] | bytes | bytearray,
            retry: int | None = None,
            notification_wait: float = 0,
        ) -> None:
            del retry
            assert isinstance(command, list)
            sent_commands.append([bytes(item) for item in command])
            notification_waits.append(notification_wait)

        device._send_command = capture_command  # type: ignore[method-assign]
        await device.query_status()

    asyncio.run(run())

    assert [[command[5:7] for command in batch] for batch in sent_commands] == [[bytes([4, 4]), bytes([4, 5])]]
    assert notification_waits == [2.5]


def test_mag_stirrer_status_refresh_is_fire_and_forget() -> None:
    """Stirrers do not request the generic runtime snapshot."""
    sent_commands: list[bytes] = []

    async def run() -> None:
        device = ChihirosMagStirrer(FakeBLEDevice(), MAG_STIRRER)  # type: ignore[arg-type]

        async def capture_command(
            command: list[bytes] | bytes | bytearray,
            retry: int | None = None,
            notification_wait: float = 0,
        ) -> None:
            del retry, notification_wait
            sent_commands.extend(command if isinstance(command, list) else [bytes(command)])

        device._send_command = capture_command  # type: ignore[method-assign]
        await device.query_status()

    asyncio.run(run())

    assert sent_commands == []


def test_dosing_pump_manual_dose_sends_auth_and_dose_batch() -> None:
    """Manual dosing sends dose auth frames before the one-shot dose command."""
    sent_batches: list[list[bytes]] = []
    retry_attempts: list[int | None] = []

    async def run() -> None:
        device = ChihirosDosingPump(FakeBLEDevice(), DOSING_PUMP)  # type: ignore[arg-type]

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


def test_dosing_pump_calibration_retry_policy() -> None:
    """A timed calibration run is never replayed; recording a volume is idempotent."""
    retry_attempts: list[int | None] = []

    async def run() -> None:
        device = ChihirosDosingPump(FakeBLEDevice(), DOSING_PUMP)  # type: ignore[arg-type]

        async def capture_command(command: list[bytes] | bytes | bytearray, retry: int | None = None) -> None:
            del command
            retry_attempts.append(retry)

        device._send_command = capture_command  # type: ignore[method-assign]

        await device.calibrate_channel(0, seconds=5)
        await device.calibrate_channel(0, volume_ml=4.05)

    asyncio.run(run())

    assert retry_attempts == [1, 3]


def _fast_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove notification and batch pacing delays from scripted sessions."""
    from chihiros_led_control import testing as testing_module
    from chihiros_led_control.devices import base as device_base
    from chihiros_led_control.devices import dosing, led

    monkeypatch.setattr(device_base, "COMMAND_NOTIFICATION_WAIT", 0.0)
    monkeypatch.setattr(device_base, "STATUS_NOTIFICATION_WAIT", 0.0)
    monkeypatch.setattr(dosing, "STATUS_NOTIFICATION_WAIT", 0.0)
    monkeypatch.setattr(led, "STATUS_NOTIFICATION_WAIT", 0.0)
    monkeypatch.setattr(testing_module, "BATCH_WRITE_DELAY", 0.0)


def _respond_once(frame: bytes) -> Callable[[bytes], list[bytes]]:
    """Respond only to the first matching frame (the connection prelude)."""
    delivered = False

    def respond(_written: bytes) -> list[bytes]:
        nonlocal delivered
        if delivered:
            return []
        delivered = True
        return [frame]

    return respond


def test_scripted_connection_reuse_runs_prelude_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two public commands reuse one physical connection and one prelude."""
    transport = ScriptedTransport()
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = transport.make_device(DeviceModel("Test", (), LedSpec(WHITE_CHANNELS)))
        await device.set_manual_mode()
        await device.set_manual_mode()

    asyncio.run(run())

    assert transport.connections == 1
    assert [frame[5] for frame in transport.writes] == [4, 9, 9, 5, 5]


def test_scripted_idle_disconnect_closes_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """An idle connection is closed by the transport before the next command."""
    transport = ScriptedTransport()
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = transport.make_device(DeviceModel("Test", (), LedSpec(WHITE_CHANNELS)))
        loop = asyncio.get_running_loop()
        original_call_later = loop.call_later

        def run_timer_now(delay: float, callback: object, *args: object) -> asyncio.TimerHandle:
            del delay
            return original_call_later(0, callback, *args)  # type: ignore[arg-type]

        monkeypatch.setattr(loop, "call_later", run_timer_now)
        await device.set_manual_mode()
        assert transport.is_connected
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not transport.is_connected

    asyncio.run(run())


def test_scripted_concurrent_commands_are_serialized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent public calls remain complete, ordered transport transactions."""
    transport = ScriptedTransport()
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = transport.make_device(DeviceModel("Test", (), LedSpec(WHITE_CHANNELS)))
        await asyncio.gather(device.set_manual_mode(), device.set_manual_mode())

    asyncio.run(run())

    assert transport.connections == 1
    assert [frame[5] for frame in transport.writes] == [4, 9, 9, 5, 5]


def test_scripted_transient_failure_reconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient write failure retries through a fresh physical connection."""
    transport = ScriptedTransport()
    calls = 0

    def flaky(_frame: bytes) -> list[bytes]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise BleakError("temporary")
        if calls == 2:
            return [bytes.fromhex("5b 1b 0a 00 01 0a 01 ff")]
        return []

    transport.expect(90, 4, [1], respond=flaky)
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = transport.make_device(DeviceModel("Test", (), LedSpec(WHITE_CHANNELS)))
        await device.query_status()

    asyncio.run(run())

    assert transport.connections == 2
    assert calls == 3


def test_scripted_missing_notify_characteristic_is_fire_and_forget(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A device without a notify endpoint still accepts public commands."""
    transport = ScriptedTransport(notify_characteristics=False)
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = transport.make_device(DeviceModel("Test", (), LedSpec(WHITE_CHANNELS)))
        with caplog.at_level(logging.WARNING):
            await device.query_status()
        assert device.last_runtime_notification is None

    asyncio.run(run())

    assert transport.connections == 1
    assert "No notify characteristic" in caplog.text


def test_scripted_characteristic_pairing_delivers_notifications(monkeypatch: pytest.MonkeyPatch) -> None:
    """The family driver receives replies through the paired notify endpoint."""
    transport = ScriptedTransport()
    runtime_frame = bytes.fromhex("5b 1b 0a 00 01 0a 01 ff")
    transport.expect(90, 4, [1], respond=_respond_once(runtime_frame))
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = transport.make_device(DeviceModel("Test", (), LedSpec(WHITE_CHANNELS)))
        await device.query_status()
        assert device.last_runtime_notification == RuntimeNotification(27, 511, runtime_frame)

    asyncio.run(run())

    assert transport.connections == 1
    assert transport.writes[0][5] == 4


def test_scripted_prelude_failure_disconnects_temporary_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed connection prelude does not leave a scripted session open."""
    transport = ScriptedTransport()
    transport.expect(90, 4, [1], fail=True)
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = transport.make_device(DeviceModel("Test", (), LedSpec(WHITE_CHANNELS)))
        with pytest.raises(BleakError, match="scripted write failure"):
            await device.query_status()

    asyncio.run(run())

    assert transport.connections == 3
    assert not transport.is_connected


def test_scripted_batch_writes_keep_vendor_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A public multi-frame command preserves the transport batch delay."""
    from chihiros_led_control import testing as testing_module
    from chihiros_led_control.devices import base as device_base

    transport = ScriptedTransport()
    monkeypatch.setattr(device_base, "COMMAND_NOTIFICATION_WAIT", 0.0)
    monkeypatch.setattr(testing_module, "BATCH_WRITE_DELAY", 0.5)
    sleeps: list[float] = []

    async def capture_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", capture_sleep)

    async def run() -> None:
        device = transport.make_device(DeviceModel("Test", (), LedSpec(WHITE_CHANNELS)))
        await device.set_brightness({"white": 40})

    asyncio.run(run())

    assert sleeps == [0.5, 0.5, 0.5]
    assert [frame[5] for frame in transport.writes] == [4, 9, 9, 5, 7]


def test_scripted_runtime_notification_is_stored_and_published(monkeypatch: pytest.MonkeyPatch) -> None:
    """A runtime notification delivered by transport reaches subscribers."""
    received: list[RuntimeNotification] = []
    frame = bytes.fromhex("5b170a00010a01ffffffffff13888c")
    transport = ScriptedTransport()
    transport.expect(90, 4, [1], respond=_respond_once(frame))
    _fast_waits(monkeypatch)

    async def run() -> ChihirosDevice:
        device = transport.make_device(DeviceModel("Test", (), LedSpec(WHITE_CHANNELS)))
        device.add_notification_callback(received.append)
        await device.query_status()
        return device

    device = asyncio.run(run())
    assert device.last_runtime_notification == RuntimeNotification(23, 511, frame)
    assert received == [device.last_runtime_notification]


def test_scripted_schedule_notification_is_stored_and_published(monkeypatch: pytest.MonkeyPatch) -> None:
    """A schedule snapshot delivered by transport reaches subscribers."""
    received: list[ScheduleSnapshotNotification] = []
    frame = bytes(framed([0x5B, 0x17, 0, 0, 1, 0xFE, *([0] * 19), 8, 0, 50]))
    transport = ScriptedTransport()
    transport.expect(90, 4, [1], respond=_respond_once(frame))
    _fast_waits(monkeypatch)

    async def run() -> ChihirosDevice:
        device = transport.make_device(DeviceModel("Test", (), LedSpec(WHITE_CHANNELS)))
        device.add_notification_callback(received.append)
        await device.query_status()
        return device

    device = asyncio.run(run())
    assert isinstance(device.last_schedule_snapshot_notification, ScheduleSnapshotNotification)
    assert received == [device.last_schedule_snapshot_notification]


def test_scripted_fan_notification_is_stored_and_published(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fan status delivered by transport reaches subscribers."""
    received: list[FanStatusNotification] = []
    frame = bytes.fromhex("5b 1b 10 00 01 0b 02 58 19 00 01 00 00 00 00 00 48 22")
    transport = ScriptedTransport()
    transport.expect(90, 4, [1], respond=_respond_once(frame))
    _fast_waits(monkeypatch)

    async def run() -> ChihirosDevice:
        model = DeviceModel(
            "Test",
            (),
            LedSpec(WRGB_CHANNELS, features=frozenset({LedFeature.FAN}), min_fan_speed=25),
        )
        device = transport.make_device(model)
        device.add_notification_callback(received.append)
        await device.query_status()
        return device

    device = asyncio.run(run())
    assert device.last_fan_status_notification == FanStatusNotification(27, 600, 25, frame)
    assert received == [device.last_fan_status_notification]


def test_set_fan_speed_rejects_models_without_fan() -> None:
    """Fan control is limited to fan-equipped models."""

    async def run() -> None:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test", (), LedSpec(WHITE_CHANNELS)))  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="fan"):
            await device.set_fan_speed(50)

    asyncio.run(run())


def test_set_brightness_sends_all_true_wrgb_channels() -> None:
    """Brightness commands can set red, green, blue, and white in one call."""
    sent_commands: list[list[bytes]] = []

    async def run() -> None:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test WRGB", (), LedSpec(WRGB_CHANNELS)))  # type: ignore[arg-type]

        async def capture_command(command: list[bytes] | bytes | bytearray, retry: int | None = None) -> None:
            del retry
            assert isinstance(command, list)
            sent_commands.append([bytes(item) for item in command])

        device._send_command = capture_command  # type: ignore[method-assign]

        await device.set_brightness((10, 20, 30, 40))

    asyncio.run(run())

    assert [[command[5] for command in batch] for batch in sent_commands] == [[5, 7, 7, 7, 7]]
    assert [[command[6:8] for command in batch[1:]] for batch in sent_commands] == [
        [
            bytes([0, 10]),
            bytes([1, 20]),
            bytes([2, 30]),
            bytes([3, 40]),
        ]
    ]


def test_notification_callback_failure_does_not_block_other_subscribers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One failing notification subscriber does not prevent later subscribers."""
    received: list[RuntimeNotification] = []
    frame = bytes.fromhex("5b 1b 0a 00 01 0a 01 ff")
    transport = ScriptedTransport()
    transport.expect(90, 4, [1], respond=_respond_once(frame))
    _fast_waits(monkeypatch)

    async def run() -> None:
        device = transport.make_device(DeviceModel("Test", (), LedSpec(WHITE_CHANNELS)))

        def fail(_notification: RuntimeNotification) -> None:
            raise RuntimeError("subscriber failed")

        device.add_notification_callback(fail)
        device.add_notification_callback(received.append)
        await device.query_status()

    asyncio.run(run())

    assert len(received) == 1


def test_add_setting_sends_four_channel_brightness() -> None:
    """True WRGB auto schedules encode red, green, blue, and white levels."""
    sent_commands: list[bytes] = []

    async def run() -> None:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test WRGB", (), LedSpec(WRGB_CHANNELS)))  # type: ignore[arg-type]

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
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test WRGB", (), LedSpec(WRGB_CHANNELS)))  # type: ignore[arg-type]

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
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test RGB", (), LedSpec(RGB_CHANNELS)))  # type: ignore[arg-type]

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


def test_set_auto_point_sends_family_specific_encoding() -> None:
    """Auto curve points use the app's per-family 0x5A/0x06 encoding."""
    bleled_sent: list[bytes] = []
    sealed_sent: list[bytes] = []

    async def run() -> None:
        bleled = ChihirosDevice(
            FakeBLEDevice(),
            DeviceModel("Commander 4", ("DYLED",), LedSpec(WRGB_CHANNELS)),  # type: ignore[arg-type]
        )
        sealed = ChihirosDevice(
            FakeBLEDevice(),
            DeviceModel("Commander 4", ("DYNLED",), LedSpec(WRGB_CHANNELS, protocol=LedProtocol.SEA_LED)),  # type: ignore[arg-type]
        )

        async def capture_bleled(command: list[bytes] | bytes | bytearray, retry: int | None = None) -> None:
            del retry
            bleled_sent.append(bytes(command))

        async def capture_sealed(command: list[bytes] | bytes | bytearray, retry: int | None = None) -> None:
            del retry
            sealed_sent.append(bytes(command))

        bleled._send_command = capture_bleled  # type: ignore[method-assign]
        sealed._send_command = capture_sealed  # type: ignore[method-assign]

        await bleled.set_auto_point(2, 8 * 60 + 30, 80)
        await sealed.set_auto_point(3, 8 * 60 + 30, 60)

    asyncio.run(run())

    # BleLed DYLED → [channel, 30-minute-slot, level] (mode at index 5, checksum last)
    assert bleled_sent[0][5:9] == bytes([6, 2, 17, 80])  # 8:30 → slot 17
    assert bleled_sent[0][9] == calculate_checksum(bleled_sent[0][:-1])
    # SeaLed DYNLED → [channel, hour, minute, level]
    assert sealed_sent[0][5:10] == bytes([6, 3, 8, 30, 60])
    assert sealed_sent[0][10] == calculate_checksum(sealed_sent[0][:-1])


def test_set_auto_point_rejects_out_of_range_channel() -> None:
    """Auto curve points validate the channel against the model layout."""

    async def run() -> None:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Commander X", ("DYONE",), LedSpec(WHITE_CHANNELS)))  # type: ignore[arg-type]

        with pytest.raises(ValueError, match="Channel"):
            await device.set_auto_point(1, 60, 50)

    asyncio.run(run())


def test_set_auto_curve_sends_all_points_in_one_transaction() -> None:
    """Auto curves batch every point frame into a single paced BLE transaction."""
    sent_batches: list[list[bytes]] = []

    async def run() -> None:
        device = ChihirosDevice(
            FakeBLEDevice(),
            DeviceModel("Commander 4", ("DYNLED",), LedSpec(WRGB_CHANNELS, protocol=LedProtocol.SEA_LED)),  # type: ignore[arg-type]
        )

        async def capture_command(command: list[bytes] | bytes | bytearray, retry: int | None = None) -> None:
            del retry
            sent_batches.append(list(command) if isinstance(command, list) else [bytes(command)])

        device._send_command = capture_command  # type: ignore[method-assign]

        await device.set_auto_curve([(0, 480, 100), (1, 480, 60), (2, 720, 0)])

    asyncio.run(run())

    assert len(sent_batches) == 1
    assert len(sent_batches[0]) == 3
    # SeaLed encoding (DYNLED): [channel, hour, minute, level]
    assert sent_batches[0][0][5:10] == bytes([6, 0, 8, 0, 100])  # 480 min = 08:00
    assert sent_batches[0][1][5:10] == bytes([6, 1, 8, 0, 60])
    assert sent_batches[0][2][5:10] == bytes([6, 2, 12, 0, 0])  # 720 min = 12:00


def test_set_auto_curve_rejects_empty_or_out_of_range_points() -> None:
    """Auto curves reject empty input and out-of-range channels before writing."""

    async def run() -> None:
        device = ChihirosDevice(
            FakeBLEDevice(),
            DeviceModel("Commander 4", ("DYNLED",), LedSpec(WRGB_CHANNELS, protocol=LedProtocol.SEA_LED)),  # type: ignore[arg-type]
        )
        dosing = ChihirosDevice(FakeBLEDevice(), DOSING_PUMP)  # type: ignore[arg-type]

        with pytest.raises(ValueError, match="at least one point"):
            await device.set_auto_curve([])
        with pytest.raises(ValueError, match="Channel"):
            await device.set_auto_curve([(4, 60, 50)])
        with pytest.raises(ValueError, match="Minutes"):
            await device.set_auto_curve([(0, 2881, 50)])
        with pytest.raises(ValueError, match="does not support auto curve"):
            await dosing.set_auto_curve([(0, 60, 50)])

    asyncio.run(run())


def test_set_manual_mode_sends_mode_switch_command() -> None:
    """Manual mode sends the vendor app's switchToManual frame without touching brightness."""
    sent_commands: list[bytes] = []

    async def run() -> None:
        device = ChihirosDevice(FakeBLEDevice(), DeviceModel("Test", (), LedSpec(WHITE_CHANNELS)))  # type: ignore[arg-type]

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
            DeviceModel("VIVID3", (), LedSpec(WRGB_CHANNELS, features=frozenset({LedFeature.FAN}), min_fan_speed=25)),  # type: ignore[arg-type]
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


def test_scripted_dosing_totals_notification_is_stored_and_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dosing totals notification delivered by transport reaches subscribers."""
    received: list[DosingTotalsNotification] = []
    frame = bytes([0x5B, 0x10, 0x10, 0x00, 0x01, 0x1E, 0x04, 0x1F, 0x00, 0x00])
    transport = ScriptedTransport(name="DYDOSE-test")
    transport.expect(90, 4, [1], respond=_respond_once(frame))
    _fast_waits(monkeypatch)

    async def run() -> ChihirosDosingPump:
        device = transport.make_pump()
        device.add_notification_callback(received.append)
        await device.query_status()
        return device

    device = asyncio.run(run())
    assert device.last_dosing_totals_notification == DosingTotalsNotification((105500, 0), frame)
    assert received == [device.last_dosing_totals_notification]


def test_scripted_dosing_daily_notification_is_stored_and_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dosing daily notification delivered by transport reaches subscribers."""
    received: list[DosingDailyNotification] = []
    frame = bytes([0x5B, 0x10, 0x0E, 0x00, 0x01, 0x22, 0x00, 0x64, 0x01, 0x90])
    transport = ScriptedTransport(name="DYDOSE-test")
    transport.expect(90, 4, [1], respond=_respond_once(frame))
    _fast_waits(monkeypatch)

    async def run() -> ChihirosDosingPump:
        device = transport.make_pump()
        device.add_notification_callback(received.append)
        await device.query_status()
        return device

    device = asyncio.run(run())
    assert device.last_dosing_daily_notification == DosingDailyNotification((10000, 40000), frame)
    assert received == [device.last_dosing_daily_notification]


def test_scripted_fan_notification_is_ignored_on_non_fan_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fan readout delivered to a model without fan support is ignored."""
    received: list[FanStatusNotification] = []
    frame = bytes([0x5B, 0x1B, 0x10, 0x00, 0x01, 0x0B, 0x02, 0x58, 25])
    transport = ScriptedTransport()
    transport.expect(90, 4, [1], respond=_respond_once(frame))
    _fast_waits(monkeypatch)

    async def run() -> ChihirosDevice:
        device = transport.make_device(DeviceModel("Test", (), LedSpec(RGB_CHANNELS)))
        device.add_notification_callback(received.append)
        await device.query_status()
        return device

    device = asyncio.run(run())
    assert device.last_fan_status_notification is None
    assert received == []


def test_set_fan_auto_sends_auto_mode_command() -> None:
    """Fan auto mode sends the vendor app's autoFan frame and tracks the mode."""
    sent_commands: list[bytes] = []

    async def run() -> None:
        device = ChihirosDevice(
            FakeBLEDevice(),
            DeviceModel("VIVID3", (), LedSpec(WRGB_CHANNELS, features=frozenset({LedFeature.FAN}), min_fan_speed=25)),  # type: ignore[arg-type]
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
            DeviceModel("VIVID3", (), LedSpec(WRGB_CHANNELS, features=frozenset({LedFeature.FAN}), min_fan_speed=25)),  # type: ignore[arg-type]
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
            DeviceModel("VIVID3", (), LedSpec(WRGB_CHANNELS, features=frozenset({LedFeature.FAN}), min_fan_speed=25)),  # type: ignore[arg-type]
        )
        assert (device.fan_start_temp, device.fan_stop_temp) == (38, 33)

        async def capture_command(command: list[bytes] | bytes | bytearray, retry: int | None = None) -> None:
            del retry
            sent_commands.append(bytes(command))

        device._send_command = capture_command  # type: ignore[method-assign]

        await device.set_fan_start_stop_temp(40, 35)

    asyncio.run(run())

    assert sent_commands[0][5:8] == bytes([0x2D, 40, 35])


def test_set_temp_protect_sends_command_and_tracks_state() -> None:
    """Temperature protection sends the vvd3tempProtect frame and tracks state."""
    sent_commands: list[bytes] = []

    async def run() -> None:
        device = ChihirosDevice(
            FakeBLEDevice(),
            DeviceModel(
                "VIVID3",
                (),
                LedSpec(
                    WRGB_CHANNELS,
                    features=frozenset({LedFeature.FAN, LedFeature.TEMPERATURE_PROTECTION, LedFeature.INDICATOR_LED}),
                    min_fan_speed=25,
                ),
            ),  # type: ignore[arg-type]
        )
        assert device.temp_protect is False

        async def capture_command(command: list[bytes] | bytes | bytearray, retry: int | None = None) -> None:
            del retry
            sent_commands.append(bytes(command))

        device._send_command = capture_command  # type: ignore[method-assign]

        await device.set_temp_protect(True)
        assert device.temp_protect is True
        await device.set_temp_protect(False)
        assert device.temp_protect is False

    asyncio.run(run())

    assert sent_commands[0][5:8] == bytes([5, 0x31, 0xFF])
    assert sent_commands[1][5:8] == bytes([5, 0x30, 0xFF])


def test_set_bluetooth_led_sends_command_and_tracks_state() -> None:
    """Indicator LED sends the vvd3BluetoothLed frame and tracks state."""
    sent_commands: list[bytes] = []

    async def run() -> None:
        device = ChihirosDevice(
            FakeBLEDevice(),
            DeviceModel(
                "VIVID3",
                (),
                LedSpec(
                    WRGB_CHANNELS,
                    features=frozenset({LedFeature.FAN, LedFeature.TEMPERATURE_PROTECTION, LedFeature.INDICATOR_LED}),
                    min_fan_speed=25,
                ),
            ),  # type: ignore[arg-type]
        )
        assert device.bluetooth_led is False

        async def capture_command(command: list[bytes] | bytes | bytearray, retry: int | None = None) -> None:
            del retry
            sent_commands.append(bytes(command))

        device._send_command = capture_command  # type: ignore[method-assign]

        await device.set_bluetooth_led(True)
        assert device.bluetooth_led is True

    asyncio.run(run())

    assert sent_commands[0][5:8] == bytes([5, 0x32, 0xFF])


def test_vivid3_switches_reject_non_vivid3_models() -> None:
    """Non-VIVID3 models reject the VIVID3-only switches."""

    async def run() -> None:
        device = ChihirosDevice(
            FakeBLEDevice(),
            DeviceModel("Fake RGB", (), LedSpec(RGB_CHANNELS)),  # type: ignore[arg-type]
        )
        for call in (device.set_temp_protect, device.set_bluetooth_led):
            try:
                await call(True)
            except ValueError:
                continue
            raise AssertionError(f"Expected ValueError from {call.__name__}")

    asyncio.run(run())

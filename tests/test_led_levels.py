"""Model-unit LED ranges and scaling through the production BLE transport."""

from __future__ import annotations

import asyncio

import pytest

from chihiros_led_control import testing as testing_module
from chihiros_led_control.devices import ChihirosDevice
from chihiros_led_control.devices import base as device_base
from chihiros_led_control.models import WHITE_CHANNELS, DeviceModel, LedLevelScale, LedSpec
from chihiros_led_control.protocol.led import parse_notification
from chihiros_led_control.protocol.notifications import SchedulePoint, ScheduleSnapshotNotification
from chihiros_led_control.registry import detect_model
from chihiros_led_control.testing import ScriptedBLEDevice, ScriptedTransport


def _fast_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(device_base, "COMMAND_NOTIFICATION_WAIT", 0.0)
    monkeypatch.setattr(device_base, "STATUS_NOTIFICATION_WAIT", 0.0)
    monkeypatch.setattr(testing_module, "BATCH_WRITE_DELAY", 0.0)


def _snapshot(*levels: int) -> bytes:
    prefix = bytes([0x5B, 27, 0, 0, 1, 0xFE]) + bytes(19)
    return prefix + b"".join(bytes([8 + index, 0, level]) for index, level in enumerate(levels))


def test_verified_vivid_raw_levels_manual_points_and_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """DYRGBV forwards extended levels; notifications retain valid model units."""
    _fast_waits(monkeypatch)
    transport = ScriptedTransport(name="DYRGBV-test")
    snapshot = _snapshot(115, 130, 200, 201)
    transport.expect(90, 4, [1], respond=[snapshot])

    async def run() -> None:
        device = ChihirosDevice(
            ScriptedBLEDevice(transport.name, transport.address), detect_model(transport.name), transport=transport
        )
        await device.set_brightness([115, 130, 200])
        await device.set_auto_point(2, 510, 200)
        await device.set_brightness({"blue": 0})
        await device.set_brightness({"blue": 1})
        await device.turn_on()
        assert [frame[6:8] for frame in transport.writes if frame[5] == 7] == [
            bytes([0, 115]),
            bytes([1, 130]),
            bytes([2, 200]),
            bytes([2, 0]),
            bytes([2, 1]),
            bytes([0, 115]),
            bytes([1, 130]),
            bytes([2, 200]),
        ]
        await device.set_auto_curve([(0, 480, 115), (1, 510, 130), (2, 540, 200)])
        assert [frame[6:9] for frame in transport.writes if frame[5] == 6] == [
            bytes([2, 17, 200]),
            bytes([0, 16, 115]),
            bytes([1, 17, 130]),
            bytes([2, 18, 200]),
        ]
        assert device.last_schedule_snapshot_notification == ScheduleSnapshotNotification(
            27,
            (
                SchedulePoint(8, 0, {"red": 115, "green": 115, "blue": 115}),
                SchedulePoint(9, 0, {"green": 130, "blue": 130}),
                SchedulePoint(10, 0, {"blue": 200}),
            ),
            snapshot,
        )
        writes_before = len(transport.writes)
        for channel, maximum in enumerate((115, 130, 200)):
            with pytest.raises(ValueError, match=f"between 0 and {maximum}"):
                await device.set_brightness({channel: maximum + 1})
            with pytest.raises(ValueError, match=f"between 0 and {maximum}"):
                await device.set_auto_point(channel, 510, maximum + 1)
        assert len(transport.writes) == writes_before

    asyncio.run(run())


@pytest.mark.parametrize("scale", [LedLevelScale.NEW_A_SERIES, LedLevelScale.MAGNETIC])
def test_explicit_scaled_model_manual_points_and_snapshot(
    monkeypatch: pytest.MonkeyPatch, scale: LedLevelScale
) -> None:
    """Only explicitly selected scaled setters normalize raw values, including 0/1."""
    _fast_waits(monkeypatch)
    spec = LedSpec(WHITE_CHANNELS, channel_maxima={0: 125}, level_scale=scale)
    model = DeviceModel("Explicit model, not a prefix assignment", (), spec)
    transport = ScriptedTransport()
    snapshot = _snapshot(0, 1, 72, 100, 101)
    transport.expect(90, 4, [1], respond=[snapshot])

    async def run() -> None:
        device = ChihirosDevice(ScriptedBLEDevice(transport.name, transport.address), model, transport=transport)
        for requested in (0, 1, 2, 90, 125):
            await device.set_brightness(requested)
            await device.set_auto_point(0, 480, requested)
        expected = [0, 1, 1, 72, 100]
        assert [frame[7] for frame in transport.writes if frame[5] == 7] == expected
        assert [frame[8] for frame in transport.writes if frame[5] == 6] == expected
        assert device.last_schedule_snapshot_notification == ScheduleSnapshotNotification(
            27,
            tuple(SchedulePoint(8 + index, 0, {"white": level}) for index, level in enumerate((0, 1, 90, 125))),
            snapshot,
        )
        with pytest.raises(ValueError, match="between 0 and 125"):
            await device.set_auto_point(0, 480, 126)

    asyncio.run(run())


def test_default_models_do_not_gain_unverified_extended_ranges() -> None:
    """Other prefixes keep max100; decoding must use actual metadata rather than a global allowance."""
    for name in ("DYNVVD-test", "DYNV-test", "DYDD-test", "DYCOM-test", "DYA-test"):
        spec = detect_model(name).spec
        assert isinstance(spec, LedSpec)
        assert all(spec.max_level(channel) == 100 for channel in spec.channels.values())
        with pytest.raises(ValueError, match="between 0 and 100"):
            spec.to_wire_level(next(iter(spec.channels.values())), 101)
        assert parse_notification(_snapshot(101), spec).points == ()


@pytest.mark.parametrize("channel,maximum", [(0, 115), (1, 130), (2, 200)])
def test_percentage_conversion_is_channel_specific(channel: int, maximum: int) -> None:
    """Percentages use each verified channel maximum, preserving zero."""
    spec = detect_model("DYRGBV-test").spec
    assert spec.percent_to_level(channel, 0) == 0
    assert spec.percent_to_level(channel, 100) == maximum
    assert spec.percent_to_level(channel, 50) == (maximum + 1) // 2

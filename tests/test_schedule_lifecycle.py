"""Behavioral scene/custom-controller and Lite replay regressions."""

from __future__ import annotations

import asyncio
from datetime import datetime, time

import pytest
from bleak_retry_connector import BleakError

from chihiros_led_control import testing as testing_module
from chihiros_led_control.devices import base as device_base
from chihiros_led_control.devices.led import ChihirosDevice, LedSchedule
from chihiros_led_control.models import (
    WHITE_CHANNELS,
    DeviceModel,
    LedControllerMode,
    LedSpec,
    supports_weekdays,
)
from chihiros_led_control.protocol import led as commands
from chihiros_led_control.protocol.notifications import RuntimeNotification
from chihiros_led_control.testing import ScriptedBLEDevice, ScriptedTransport
from chihiros_led_control.weekday_encoding import WeekdaySelect


@pytest.fixture
def fast_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the production transport path without notification/pacing sleeps."""
    monkeypatch.setattr(device_base, "COMMAND_NOTIFICATION_WAIT", 0.0)
    monkeypatch.setattr(device_base, "STATUS_NOTIFICATION_WAIT", 0.0)
    monkeypatch.setattr(testing_module, "BATCH_WRITE_DELAY", 0.0)


def _device(name: str = "DYLITE-test", *, spec: LedSpec | None = None) -> tuple[ChihirosDevice, ScriptedTransport]:
    transport = ScriptedTransport(name=name)
    device = ChihirosDevice(
        ScriptedBLEDevice(name, transport.address),
        DeviceModel("Test", (), spec or LedSpec(WHITE_CHANNELS)),
        transport=transport,
    )
    return device, transport


def _scene() -> LedSchedule:
    return LedSchedule(datetime(2026, 10, 16, 8), datetime(2026, 10, 16), 90)


def _operations(frames: list[bytes]) -> list[tuple[int, int, bytes]]:
    return [(frame[0], frame[5], frame[6:-1]) for frame in frames if frame[5] not in (4, 9)]


@pytest.mark.parametrize(("end", "encoded"), [(time(0, 0), b"\x17\x3b"), (time(18, 0), b"\x12\x00")])
def test_scene_end_midnight_only(end: time, encoded: bytes) -> None:
    """Only total end minutes zero becomes 23:59; inactive slots remain FF."""
    active = commands.create_add_auto_setting_command((0, 1), time(8), end, [90], 0, 127)
    inactive = commands.create_delete_auto_setting_command((0, 1), time(8), end, 0, 127)
    assert active[8:10] == encoded
    assert len(active[6:-1]) == 14
    assert active[12] == 90
    assert inactive[8:10] == encoded
    assert inactive[12:-1] == bytes([255] * 8)


@pytest.mark.usefixtures("fast_transport")
@pytest.mark.parametrize(
    "prefix",
    "DYZSD DYNA2N DYNA2 DYNC2N DYCXD DYECO MDTM DYDD DYCOM DYNCM DYFISHDL DYLITE DYZLPD DYZPRO".split(),
)
def test_scene_initialization_and_unchanged_lite_reconnect(prefix: str) -> None:
    """Scene replacement and every Lite auto reconnect replay reset/mode/entries."""

    async def run() -> None:
        device, transport = _device(f"{prefix}-test")
        await device.replace_settings([_scene()])
        assert device.controller_mode is LedControllerMode.SCENE
        expected = [
            (90, 5, bytes([0x28, 255, 255])),
            (90, 5, bytes([18, 255, 255])),
            (165, 25, bytes([8, 0, 23, 59, 0, 127, 90] + [255] * 7)),
        ]
        assert _operations(transport.writes) == expected
        for _ in range(2):
            await device.disconnect()
            before = len(transport.writes)
            await device.query_status()
            assert _operations(transport.writes[before:]) == expected
        assert transport.connections == 3
        await device.set_manual_mode()
        before = len(transport.writes)
        await device.enable_auto_mode()
        assert _operations(transport.writes[before:]) == expected

    asyncio.run(run())


@pytest.mark.usefixtures("fast_transport")
@pytest.mark.parametrize("firmware", [19, 20, 27])
def test_scene_firmware_gate_does_not_convert_custom_curves(firmware: int) -> None:
    """Old firmware rejects scenes; all firmware retains valid custom points."""

    async def run() -> None:
        device, transport = _device()
        device.last_runtime_notification = RuntimeNotification(firmware, 0, b"")
        if firmware < 20:
            with pytest.raises(ValueError, match="firmware 20"):
                await device.replace_settings([_scene()])
            assert not transport.writes
        else:
            await device.replace_settings([_scene()])
        before = len(transport.writes)
        await device.set_auto_curve([(0, 480, 90), (0, 1080, 0)])
        assert device.controller_mode is LedControllerMode.CUSTOM_CURVE
        assert _operations(transport.writes[before:]) == [
            (90, 5, bytes([5, 255, 255])),
            (90, 5, bytes([3, 255, 255])),
            (90, 6, bytes([0, 16, 90])),
            (90, 6, bytes([0, 36, 0])),
        ]
        await device.disconnect()
        before = len(transport.writes)
        await device.query_status()
        assert _operations(transport.writes[before:]) == [
            (90, 5, bytes([5, 255, 255])),
            (90, 5, bytes([3, 255, 255])),
            (90, 6, bytes([0, 16, 90])),
            (90, 6, bytes([0, 36, 0])),
        ]

    asyncio.run(run())


@pytest.mark.usefixtures("fast_transport")
def test_explicit_scene_capability_gate() -> None:
    """An explicit no-scene capability wins even on recent firmware."""

    async def run() -> None:
        device, transport = _device(spec=LedSpec(WHITE_CHANNELS, scene_min_firmware=None))
        device.last_runtime_notification = RuntimeNotification(27, 0, b"")
        with pytest.raises(ValueError, match="does not support scenes"):
            await device.replace_settings([_scene()])
        assert not transport.writes
        await device.set_auto_curve([(0, 480, 50)])
        assert transport.writes[-1][5] == 6

    asyncio.run(run())


async def _leave_auto_mode(device: ChihirosDevice, authority: str) -> None:
    """Exercise either manual-mode operation that must prevent reconnect replay."""
    if authority == "manual":
        await device.set_manual_mode()
    elif authority == "brightness":
        await device.set_brightness(50)


@pytest.mark.usefixtures("fast_transport")
@pytest.mark.parametrize("authority", ["unknown", "incremental", "manual", "brightness", "non_lite"])
def test_reconnect_never_resets_without_authoritative_lite_auto_schedule(authority: str) -> None:
    """Unknown device state, incremental edits and manual mode never trigger replay."""

    async def run() -> None:
        device, transport = _device("DYRGB-test" if authority == "non_lite" else "DYLITE-test")
        if authority in ("manual", "brightness", "non_lite"):
            await device.replace_settings([_scene()])
            await _leave_auto_mode(device, authority)
        elif authority == "incremental":
            await device.add_setting(_scene().sunrise, _scene().sunset)
            await device.enable_auto_mode()
        else:
            await device.enable_auto_mode()
        await device.disconnect()
        before = len(transport.writes)
        await device.query_status()
        assert not _operations(transport.writes[before:])

    asyncio.run(run())


@pytest.mark.parametrize(
    "prefix",
    "DYZSD DYNA2N DYNA2 DYNC2N DYCXD DYECO MDTM DYDD DYCOM DYNCM DYFISHDL DYLITE DYZLPD DYZPRO".split(),
)
def test_no_weekday_selector_classification(prefix: str) -> None:
    """Verified prefix capability does not require speculative product mappings."""
    assert not supports_weekdays(f"{prefix}-test")
    assert supports_weekdays("DYRGBV-test")
    assert supports_weekdays("unclassified-test")


@pytest.mark.usefixtures("fast_transport")
def test_no_weekday_selector_rejects_partial_week_before_writing() -> None:
    """The library does not silently advertise or encode unsupported weekday edits."""

    async def run() -> None:
        device, transport = _device()
        setting = LedSchedule(_scene().sunrise, _scene().sunset, weekdays=[WeekdaySelect.monday])
        with pytest.raises(ValueError, match="weekday selection"):
            await device.replace_settings([setting])
        assert not transport.writes

    asyncio.run(run())


@pytest.mark.usefixtures("fast_transport")
def test_failed_replacement_revokes_stale_replay_authority() -> None:
    """Retries and later reconnects cannot replay an obsolete library schedule."""

    async def run() -> None:
        device, transport = _device()
        await device.replace_settings([_scene()])
        transport.expect(165, 25, fail=True)
        with pytest.raises(BleakError):
            await device.replace_settings([_scene()])
        before = len(transport.writes)
        await device.query_status()
        assert not _operations(transport.writes[before:])

    asyncio.run(run())


@pytest.mark.usefixtures("fast_transport")
@pytest.mark.parametrize(("name", "weekdays"), [("DYSSD-test", True), ("DYZSD-test", False)])
def test_mixed_aliases_use_advertised_capability(name: str, weekdays: bool) -> None:
    """One classified alias does not assign Lite behavior to every model alias."""

    async def run() -> None:
        device, transport = _device(name)
        device.model = DeviceModel("Mixed aliases", ("DYSSD", "DYZSD"), LedSpec(WHITE_CHANNELS))
        assert device.supports_weekdays is weekdays
        await device.replace_settings([_scene()])
        await device.disconnect()
        before = len(transport.writes)
        await device.query_status()
        assert bool(_operations(transport.writes[before:])) is not weekdays

    asyncio.run(run())


@pytest.mark.usefixtures("fast_transport")
def test_explicit_custom_controller_enable_and_reset_preserve_point_path() -> None:
    """A fresh client can select and clear the custom controller explicitly."""

    async def run() -> None:
        device, transport = _device("DYRGB-test")
        await device.enable_auto_mode(controller_mode=LedControllerMode.CUSTOM_CURVE)
        await device.reset_settings(controller_mode=LedControllerMode.CUSTOM_CURVE)
        assert _operations(transport.writes) == [
            (90, 5, bytes([3, 255, 255])),
            (90, 5, bytes([5, 255, 255])),
        ]

    asyncio.run(run())

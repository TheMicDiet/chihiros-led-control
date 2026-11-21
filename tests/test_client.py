"""Device contracts not covered by the generic BLE session journeys."""

from __future__ import annotations

from datetime import datetime

import pytest
from bleak_retry_connector import BleakError

from chihiros_led_control.devices import ChihirosDevice, ChihirosDosingPump
from chihiros_led_control.protocol.notifications import (
    DosingDailyNotification,
    FanStatusNotification,
    RuntimeNotification,
)
from chihiros_led_control.registry import detect_model
from chihiros_led_control.testing import ScriptedBLEDevice, ScriptedTransport

pytestmark = pytest.mark.component


def _light(name: str) -> tuple[ChihirosDevice, ScriptedTransport]:
    ble = ScriptedTransport(name=name, realtime=False)
    return ChihirosDevice(ScriptedBLEDevice(name, ble.address), detect_model(name), transport=ble), ble


@pytest.mark.parametrize(
    ("name", "brightness", "levels"),
    [
        ("DYWPRO60-test", (10, 20, 30, 40), "0a 14 1e 28"),
        ("DYWPRO60-test", 40, "ff ff ff 28"),
        ("DYNW60-test", 40, "28 ff ff ff"),
    ],
    ids=["rgbw-tuple", "scalar-maps-to-white", "scalar-maps-to-first-channel"],
)
async def test_scene_levels_follow_model_channel_layout(
    name: str, brightness: int | tuple[int, ...], levels: str
) -> None:
    """Scalar and multichannel schedules retain the layout of the advertised light."""
    light, ble = _light(name)
    await light.add_setting(datetime(2026, 6, 14, 8), datetime(2026, 6, 14, 18, 30), brightness)
    await light.disconnect()
    assert ble.writes[-1][5:-1].hex(" ") == f"19 08 00 12 1e 00 7f {levels} ff ff ff ff"


async def test_sea_led_curve_uses_wall_clock_and_validates_before_writes() -> None:
    """Points use hours and minutes on SeaLed, and invalid requests cannot reset a curve."""
    light, ble = _light("DYNLED-test")
    for points, message in (([], "at least one point"), ([(4, 60, 50)], "Channel"), ([(0, 2881, 50)], "Minutes")):
        with pytest.raises(ValueError, match=message):
            await light.set_auto_curve(points)
    assert not ble.writes

    await light.set_auto_curve([(0, 480, 100), (1, 480, 60), (2, 720, 0)])
    await light.disconnect()
    assert [frame[5:-1].hex(" ") for frame in ble.writes[2:]] == [
        "05 05 ff ff",
        "05 03 ff ff",
        "06 00 08 00 64",
        "06 01 08 00 3c",
        "06 02 0c 00 00",
    ]


async def test_fan_thresholds_commit_only_after_success() -> None:
    """A failed replacement leaves the last applied hysteresis on the device API."""
    light, ble = _light("DYVVD3-test")
    assert (light.fan_start_temp, light.fan_stop_temp) == (38, 33)
    await light.set_fan_start_stop_temp(40, 35)
    assert ble.writes[-1][5:-1].hex(" ") == "2d 28 23"
    ble.expect(ble.writes[-1][0], 45, fail=True)
    with pytest.raises(BleakError, match="scripted write failure"):
        await light.set_fan_start_stop_temp(41, 36)
    assert (light.fan_start_temp, light.fan_stop_temp) == (40, 35)


@pytest.mark.parametrize("name", ["DYVVD3-test", "DYNA2-test"], ids=["fan", "no-fan"])
async def test_fan_notification_requires_fan_support(name: str) -> None:
    """The same BLE notification is published only for a compatible model."""
    light, ble = _light(name)
    frame = bytes.fromhex("5b 1b 10 00 01 0b 02 58 19 00 01 00 00 00 00 00 48 22")
    seen = []
    ble.expect(90, 4, [1], respond=lambda _frame: [] if seen else [frame])
    light.add_notification_callback(seen.append)
    await light.query_status()
    await light.disconnect()
    expected = FanStatusNotification(27, 600, 25, frame) if name.startswith("DYVVD3") else None
    assert light.last_fan_status_notification == expected
    assert seen == ([expected] if expected else [])


async def test_notification_subscribers_are_isolated() -> None:
    """A failing consumer cannot hide a notification from a different consumer."""
    light, ble = _light("DYNA2-test")
    frame = bytes.fromhex("5b 1b 0a 00 01 0a 01 ff")
    seen = []

    def fail(_notification: RuntimeNotification) -> None:
        raise RuntimeError("subscriber failed")

    light.add_notification_callback(fail)
    light.add_notification_callback(seen.append)
    ble.expect(90, 4, [1], respond=lambda _frame: [] if seen else [frame])
    await light.query_status()
    await light.disconnect()
    assert seen == [RuntimeNotification(27, 511, frame)]


async def test_pump_publishes_daily_report() -> None:
    """The daily report is both published and exposed on the public client."""
    name = "DYDOSE-test"
    ble = ScriptedTransport(name=name, realtime=False)
    pump = ChihirosDosingPump(ScriptedBLEDevice(name, ble.address), detect_model(name), transport=ble)
    frame = bytes.fromhex("5b 10 0e 00 01 22 00 64 01 90")
    ble.expect(165, 4, [5], respond=lambda _frame: [frame])
    seen = []
    pump.add_notification_callback(seen.append)
    await pump.query_status()
    await pump.disconnect()
    expected = DosingDailyNotification((10000, 40000), frame)
    assert pump.last_dosing_daily_notification == expected
    assert seen and all(notification == expected for notification in seen)


async def test_legacy_transport_rejects_authoritative_edit_before_writing() -> None:
    """Public custom transports with an old send signature still handle ordinary commands."""

    class LegacyTransport(ScriptedTransport):
        """Simulated transport using the original extension signature."""

        async def send(self, frames, *, attempts: int, notification_wait: float) -> None:
            """Forward a batch without supporting the authoritative write boundary."""
            await super().send(frames, attempts=attempts, notification_wait=notification_wait)

    ble = LegacyTransport(realtime=False)
    light = ChihirosDevice(ScriptedBLEDevice(ble.name, ble.address), transport=ble)
    await light.query_status()
    with pytest.raises(TypeError, match=r"Custom transports must implement send\(\.\.\., boundary="):
        await light.set_auto_curve([(0, 480, 0)])
    await light.disconnect()
    assert [frame[5] for frame in ble.writes] == [4, 9, 4]

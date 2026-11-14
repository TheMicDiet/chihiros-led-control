"""Schedule replacement failures through HA helpers, real drivers and scripted BLE."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from bleak_retry_connector import BleakError

pytest.importorskip("homeassistant", reason="Home Assistant test group is not installed")

from homeassistant.exceptions import HomeAssistantError

from chihiros_led_control import testing as testing_module
from chihiros_led_control.testing import ScriptedBLEDevice, ScriptedTransport
from custom_components.chihiros.schedule_services import _async_write_auto_curve, async_replace_schedule
from custom_components.chihiros.vendor.chihiros_led_control.devices import base as device_base
from custom_components.chihiros.vendor.chihiros_led_control.devices.led import ChihirosDevice
from custom_components.chihiros.vendor.chihiros_led_control.models import DeviceModel, LedSpec

pytestmark = [pytest.mark.component, pytest.mark.homeassistant]


@dataclass
class _Schedule:
    """Model only the reset/add effects accepted at the injected GATT boundary."""

    kind: str
    entries: list[bytes] = field(default_factory=list)
    resets: int = 0

    def is_entry(self, frame: bytes) -> bool:
        """Identify the selected schedule's entry command."""
        return (frame[0], frame[5]) == ((165, 25) if self.kind == "scene" else (90, 6))

    def is_reset(self, frame: bytes) -> bool:
        """Identify the selected schedule's reset command."""
        return (frame[0], frame[5], frame[6]) == (90, 5, 40 if self.kind == "scene" else 5)

    def accept(self, frame: bytes) -> None:
        """Apply an acknowledged reset or entry to the simulated device state."""
        if self.is_reset(frame):
            self.entries.clear()
            self.resets += 1
        elif self.is_entry(frame):
            self.entries.append(frame[6:-1])


@pytest.fixture(autouse=True)
def fast_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep real transport lifecycle without notification or pacing delays."""
    monkeypatch.setattr(device_base, "COMMAND_NOTIFICATION_WAIT", 0.0)
    monkeypatch.setattr(testing_module, "BATCH_WRITE_DELAY", 0.0)


def _device() -> tuple[ChihirosDevice, ScriptedTransport]:
    """Build a real vendored client backed by the production transport lifecycle."""
    transport = ScriptedTransport(name="DYRGB-test")
    client = ChihirosDevice(
        ScriptedBLEDevice(transport.name, transport.address),
        DeviceModel("Test", (), LedSpec({"white": 0})),
        transport=transport,
    )
    return client, transport


async def _replace(client: ChihirosDevice, kind: str) -> None:
    """Replace two periods through the actual Home Assistant service helpers."""
    data = SimpleNamespace(device=client)
    if kind == "curve":
        await _async_write_auto_curve(data, [(0, 480, 50), (0, 1080, 0)])
        return
    await async_replace_schedule(
        data,
        [
            {"start": "08:00", "end": "18:00", "brightness": 50, "ramp_up_minutes": 0, "weekdays": ["monday"]},
            {"start": "09:00", "end": "19:00", "brightness": 75, "ramp_up_minutes": 0, "weekdays": ["tuesday"]},
        ],
    )


@pytest.mark.parametrize("kind", ["scene", "curve"])
@pytest.mark.parametrize("failure", ["cancel", "error"])
async def test_partial_schedule_is_cleared_after_failure(
    monkeypatch: pytest.MonkeyPatch, kind: str, failure: str
) -> None:
    """A rejected or cancelled second entry must not leave the first installed."""
    client, transport = _device()
    state = _Schedule(kind)
    partial, release = asyncio.Event(), asyncio.Event()
    original = transport._write_frame

    async def reject_second(connection: object, characteristic: object, frame: bytes) -> None:
        if state.is_entry(frame) and state.entries:
            partial.set()
            await release.wait()
            raise BleakError("second period rejected")
        await original(connection, characteristic, frame)
        state.accept(frame)

    monkeypatch.setattr(transport, "_write_frame", reject_second)
    pending = asyncio.create_task(_replace(client, kind))
    try:
        await asyncio.wait_for(partial.wait(), timeout=5)
        if failure == "cancel":
            pending.cancel()
        else:
            release.set()
        expected = asyncio.CancelledError if failure == "cancel" else HomeAssistantError
        with pytest.raises(expected):
            await pending
        assert state.entries == []
    finally:
        release.set()
        await client.disconnect()


async def test_cancelled_cleanup_cannot_erase_a_newer_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    """Schedule ownership spans cancellation cleanup before the next writer runs."""
    client, transport = _device()
    state = _Schedule("curve")
    partial, cleanup, requested = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_cleanup = asyncio.Event()
    original = transport._write_frame

    async def gate_writes(connection: object, characteristic: object, frame: bytes) -> None:
        if state.is_entry(frame) and state.entries:
            partial.set()
            await asyncio.Event().wait()
        if state.is_reset(frame) and state.resets == 1:
            cleanup.set()
            await release_cleanup.wait()
        await original(connection, characteristic, frame)
        state.accept(frame)

    async def replace_later() -> None:
        requested.set()
        await client.set_auto_curve([(0, 720, 70)])

    monkeypatch.setattr(transport, "_write_frame", gate_writes)
    pending = asyncio.create_task(_replace(client, "curve"))
    try:
        await asyncio.wait_for(partial.wait(), timeout=5)
        pending.cancel()
        await asyncio.wait_for(cleanup.wait(), timeout=5)
        newer = asyncio.create_task(replace_later())
        await asyncio.wait_for(requested.wait(), timeout=5)
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await newer
        assert state.entries == [bytes([0, 24, 70])]
    finally:
        release_cleanup.set()
        await client.disconnect()

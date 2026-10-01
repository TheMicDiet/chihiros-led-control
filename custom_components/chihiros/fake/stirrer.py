"""Development fake for magnetic stirrers."""

# These fake methods mirror runtime protocols; narrow surfaces intentionally omit docs.
# ruff: noqa: D102, D107

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from ..vendor.chihiros_led_control.protocol.dosing import DosingMode, DosingWorkPoint
from ..vendor.chihiros_led_control.protocol.stirrer import STIRRER_SPEED_DEFAULT
from .base import FakeBaseDevice


class FakeStirrerDevice(FakeBaseDevice):
    """In-memory stirrer client with only stirrer-family operations."""

    def __init__(self, device_info) -> None:
        super().__init__(device_info)
        self.stir_running: dict[int, bool] = {}
        self.stir_speeds: dict[int, int] = {}
        self.stir_pre_seconds: dict[int, int] = {}
        self.stir_schedules: list[tuple[int, tuple[tuple[int, int, float], ...], int, bool]] = []
        self.dosing_programming_calls: list[dict[str, object]] = []
        self.broadcast_frames: list[bytes] = []

    async def query_status(self) -> None:
        await asyncio.sleep(0)

    async def stir(self, channel: int, on: bool, *, seconds: int | None = None) -> None:
        await asyncio.sleep(0)
        del seconds
        self.stir_running[channel] = on

    async def set_pre_second(
        self,
        channel: int,
        seconds: int,
        speed: int = STIRRER_SPEED_DEFAULT,
        *,
        restart: bool = False,
    ) -> None:
        await asyncio.sleep(0)
        del restart
        self.stir_speeds[channel] = speed
        self.stir_pre_seconds[channel] = seconds

    async def program_channel(
        self,
        channel: int,
        *,
        active: bool,
        compensate: bool = False,
        dose_per_day_ml: float | None = None,
        frequency: int = 127,
        is_first_setting: bool = True,
        mode: DosingMode | None = None,
        points: Sequence[DosingWorkPoint] = (),
    ) -> None:
        await asyncio.sleep(0)
        self.dosing_programming_calls.append(
            {
                "kind": "program",
                "channel": channel,
                "active": active,
                "compensate": compensate,
                "ml": dose_per_day_ml,
                "frequency": frequency,
                "first_setting": is_first_setting,
                "mode": getattr(mode, "name", None),
                "points": tuple(points),
            }
        )

    async def set_stir_schedule(
        self,
        channel: int,
        points: Sequence[object],
        *,
        frequency: int = 127,
        active: bool = True,
        is_first_setting: bool = True,
    ) -> None:
        await asyncio.sleep(0)
        del is_first_setting
        records = tuple((point.start_hour, point.start_minute, point.volume_ml) for point in points)
        self.stir_schedules.append((channel, records, frequency, active))

    async def send_frame(self, frame: bytes | bytearray) -> None:
        await asyncio.sleep(0)
        self.broadcast_frames.append(bytes(frame))

    async def set_dose_delay(self, enabled: bool) -> None:
        await asyncio.sleep(0)
        self.dosing_programming_calls.append({"kind": "delay", "enabled": enabled})


__all__ = ["FakeStirrerDevice"]

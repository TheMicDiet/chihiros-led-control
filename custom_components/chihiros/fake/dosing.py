"""Development fake for dosing pumps."""

# These fake methods mirror runtime protocols; narrow surfaces intentionally omit docs.
# ruff: noqa: D102, D107

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from ..dosing import normalize_pump_count
from ..vendor.chihiros_led_control.protocol.notifications import DosingDailyNotification, DosingTotalsNotification
from .base import FakeBaseDevice


class FakeDosingDevice(FakeBaseDevice):
    """In-memory dosing client with only pump-family operations."""

    def __init__(self, device_info, pump_count: int = 4) -> None:
        super().__init__(device_info)
        self.pump_count = normalize_pump_count(pump_count)
        self._dosed_ml = [0.0] * self.pump_count
        self.last_dosing_totals_notification: DosingTotalsNotification | None = None
        self.last_dosing_daily_notification: DosingDailyNotification | None = None
        self.dosing_programming_calls: list[dict[str, object]] = []
        self.calibration_calls: list[dict[str, object]] = []

    async def query_status(self) -> None:
        await asyncio.sleep(0)
        self.last_dosing_totals_notification = self._dosing_totals_notification()
        self.last_dosing_daily_notification = self._dosing_daily_notification()
        self._notify_callbacks(self.last_dosing_totals_notification)
        self._notify_callbacks(self.last_dosing_daily_notification)

    async def dose_ml(self, pump_idx: int, volume_ml: float) -> bytes:
        await asyncio.sleep(0)
        self._dosed_ml[pump_idx] = round(self._dosed_ml[pump_idx] + volume_ml, 1)
        await self.query_status()
        return b""

    async def reset_channel(self, channel: int) -> bytes:
        await asyncio.sleep(0)
        self.dosing_programming_calls.append({"kind": "reset", "channel": channel})
        return b""

    async def calibrate_channel(
        self, channel: int, *, seconds: int | None = None, volume_ml: float | None = None
    ) -> bytes:
        await asyncio.sleep(0)
        self.calibration_calls.append({"channel": channel, "seconds": seconds, "volume_ml": volume_ml})
        return b""

    async def set_channel_active(self, channel: int, *, active: bool = True, compensate: bool = False) -> None:
        await asyncio.sleep(0)
        self.dosing_programming_calls.append(
            {"kind": "active", "channel": channel, "active": active, "compensate": compensate}
        )

    async def apply_dosing_settings(
        self,
        channel: int,
        dose_per_day_ml: float | None,
        frequency: int = 127,
        *,
        is_first_setting: bool = True,
    ) -> None:
        await asyncio.sleep(0)
        self.dosing_programming_calls.append(
            {
                "kind": "daily",
                "channel": channel,
                "ml": dose_per_day_ml,
                "frequency": frequency,
                "first_setting": is_first_setting,
            }
        )

    async def set_schedule(self, channel: int, mode: object, points: Sequence[object]) -> None:
        await asyncio.sleep(0)
        self.dosing_programming_calls.append(
            {
                "kind": "schedule",
                "channel": channel,
                "mode": getattr(mode, "name", str(mode)),
                "points": tuple(points),
            }
        )

    async def set_dose_delay(self, enabled: bool) -> None:
        await asyncio.sleep(0)
        self.dosing_programming_calls.append({"kind": "delay", "enabled": enabled})

    async def program_channel(
        self,
        channel: int,
        *,
        active: bool,
        compensate: bool = False,
        dose_per_day_ml: float | None = None,
        frequency: int = 127,
        is_first_setting: bool = True,
        mode: object = None,
        points: Sequence[object] = (),
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

    def _dosing_totals_notification(self) -> DosingTotalsNotification:
        return DosingTotalsNotification(tuple(round(volume * 1000) for volume in self._dosed_ml), raw=b"")

    def _dosing_daily_notification(self) -> DosingDailyNotification:
        return DosingDailyNotification(tuple(round(volume * 1000) for volume in self._dosed_ml), raw=b"")


__all__ = ["FakeDosingDevice"]

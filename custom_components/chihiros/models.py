"""The chihiros integration models."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .coordinator import ChihirosDataUpdateCoordinator
from .dosing import DosingCalibrationTracker, DosingDailyTotals, DosingProgrammingTracker
from .runtime import ChihirosClient


@dataclass
class StirrerChannelState:
    """Locally tracked state of one magnetic-stirrer channel.

    The stirrer UI is fire-and-forget (no notifications are parsed from the
    device, DOSING_CONTROL.md §6.5), so all stirrer entity state is
    optimistic and restored across Home Assistant restarts. Defaults match
    the app's model (speed 40, pre_second 0).

    ``lock`` serializes device writes for the channel: speed and pre-run
    share one ``(0xA5, 42)`` frame, so concurrent writes must not interleave
    their uncommitted values.
    """

    speed: int = 40
    pre_seconds: int = 0
    running: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class ChihirosData:
    """Data for the chihiros integration."""

    title: str
    device: ChihirosClient
    coordinator: ChihirosDataUpdateCoordinator
    dosing_totals: DosingDailyTotals | None = None
    dosing_volumes: list[float] = field(default_factory=list)
    stirrer_states: list[StirrerChannelState] = field(default_factory=list)
    dosing_programming: DosingProgrammingTracker | None = None
    dosing_calibration: DosingCalibrationTracker | None = None

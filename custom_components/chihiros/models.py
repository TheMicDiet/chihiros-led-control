"""Typed data records used by the Chihiros integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .coordinator import ChihirosDataUpdateCoordinator
from .dosing import DosingCalibrationTracker, DosingDailyTotals, DosingProgrammingTracker
from .runtime import BaseChihirosClient, DosingChihirosClient, StirrerChihirosClient
from .vendor.chihiros_led_control.protocol.stirrer import STIRRER_SPEED_DEFAULT


@dataclass
class StirrerChannelState:
    """Locally tracked state of one magnetic-stirrer channel."""

    speed: int = STIRRER_SPEED_DEFAULT
    pre_seconds: int = 0
    running: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class ChihirosData:
    """Common data held for every configured device."""

    title: str
    device: BaseChihirosClient
    coordinator: ChihirosDataUpdateCoordinator


@dataclass
class DosingChihirosData(ChihirosData):
    """Data held for a dosing-pump entry."""

    device: DosingChihirosClient
    dosing_totals: DosingDailyTotals
    dosing_volumes: list[float]
    dosing_programming: DosingProgrammingTracker
    dosing_calibration: DosingCalibrationTracker


@dataclass
class StirrerChihirosData(ChihirosData):
    """Data held for a magnetic-stirrer entry."""

    device: StirrerChihirosClient
    stirrer_states: list[StirrerChannelState]
    dosing_programming: DosingProgrammingTracker

"""The chihiros integration models."""

from __future__ import annotations

from dataclasses import dataclass, field

from .coordinator import ChihirosDataUpdateCoordinator
from .dosing import DosingDailyTotals
from .runtime import ChihirosClient


@dataclass
class ChihirosData:
    """Data for the chihiros integration."""

    title: str
    device: ChihirosClient
    coordinator: ChihirosDataUpdateCoordinator
    dosing_totals: DosingDailyTotals | None = None
    dosing_volumes: list[float] = field(default_factory=list)

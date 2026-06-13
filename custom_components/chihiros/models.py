"""The chihiros integration models."""

from __future__ import annotations

from dataclasses import dataclass

from .coordinator import ChihirosDataUpdateCoordinator
from .vendor.chihiros_led_control import ChihirosDevice


@dataclass
class ChihirosData:
    """Data for the chihiros integration."""

    title: str
    device: ChihirosDevice
    coordinator: ChihirosDataUpdateCoordinator

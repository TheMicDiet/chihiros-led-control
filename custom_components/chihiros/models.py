"""The chihiros integration models."""

from __future__ import annotations

from dataclasses import dataclass

from .coordinator import ChihirosDataUpdateCoordinator
from .runtime import ChihirosClient


@dataclass
class ChihirosData:
    """Data for the chihiros integration."""

    title: str
    device: ChihirosClient
    coordinator: ChihirosDataUpdateCoordinator

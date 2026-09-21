"""BLE transport boundary shared by family device drivers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

FrameSender = Callable[[Sequence[bytes]], Awaitable[None]]


class ChihirosTransport(Protocol):
    """Transport contract consumed by device drivers."""

    async def send(
        self,
        frames: Sequence[bytes],
        *,
        attempts: int,
        notification_wait: float,
    ) -> None:
        """Send an already encoded frame batch."""

    async def disconnect(self) -> None:
        """Disconnect the current transport session."""

    def update_device(self, ble_device: BLEDevice, advertisement_data: AdvertisementData | None) -> None:
        """Update the transport's current BLE identity."""


class BleTransport:
    """Small production transport adapter around a device-owned sender.

    The sender is injected by the runtime device so this class contains no
    device-family or command knowledge. It remains useful for integrations
    that own connection lifecycle separately from the library client.
    """

    def __init__(self, sender: FrameSender) -> None:
        self._sender = sender
        self.ble_device: BLEDevice | None = None
        self.advertisement_data: AdvertisementData | None = None

    async def send(self, frames: Sequence[bytes], *, attempts: int, notification_wait: float) -> None:
        """Forward encoded frames to the owned BLE transaction sender."""
        del attempts, notification_wait
        await self._sender(frames)

    async def disconnect(self) -> None:
        """No-op when connection lifecycle is owned by the sender."""

    def update_device(self, ble_device: BLEDevice, advertisement_data: AdvertisementData | None) -> None:
        """Record the latest BLE identity."""
        self.ble_device = ble_device
        self.advertisement_data = advertisement_data

__all__ = ["BleTransport", "ChihirosTransport"]

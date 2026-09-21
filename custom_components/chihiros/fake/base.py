"""Shared state for family-specific development fakes."""

# These fake methods mirror runtime protocols; narrow surfaces intentionally omit docs.
# ruff: noqa: D102, D107

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from ..vendor.chihiros_led_control.models import DeviceKind, DeviceModel
from ..vendor.chihiros_led_control.protocol.notifications import ParsedNotification

NotificationCallback = Callable[[ParsedNotification], None]


class FakeBaseDevice:
    """Common identity, callbacks, notification dispatch, and lifecycle."""

    def __init__(self, device_info: Any) -> None:
        self._device_info = device_info
        self.model: DeviceModel = device_info.model
        self._callbacks: set[NotificationCallback] = set()

    @property
    def address(self) -> str:
        return self._device_info.address

    @property
    def name(self) -> str:
        return self._device_info.name

    @property
    def model_name(self) -> str:
        return self.model.name

    @property
    def device_kind(self) -> DeviceKind:
        return self.model.device_kind

    def add_notification_callback(self, callback: NotificationCallback) -> Callable[[], None]:
        self._callbacks.add(callback)

        def remove_callback() -> None:
            self._callbacks.discard(callback)

        return remove_callback

    async def disconnect(self) -> None:
        await asyncio.sleep(0)

    def _notify_callbacks(self, notification: ParsedNotification) -> None:
        for callback in tuple(self._callbacks):
            callback(notification)

    @staticmethod
    def _yield() -> None:
        """Keep fake writes visibly asynchronous without adding latency."""
        return None

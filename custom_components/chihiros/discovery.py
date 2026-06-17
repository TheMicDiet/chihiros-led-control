"""Discovery option helpers for the Chihiros config flow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.const import CONF_ADDRESS

from .fake import FakeChihirosDeviceInfo

if TYPE_CHECKING:
    from .runtime import ChihirosClient


@dataclass(frozen=True)
class ChihirosDiscovery:
    """A selectable real or fake Chihiros discovery candidate."""

    address: str
    name: str
    bluetooth_info: BluetoothServiceInfoBleak | None = None
    fake_info: FakeChihirosDeviceInfo | None = None

    @classmethod
    def from_bluetooth(cls, discovery_info: BluetoothServiceInfoBleak) -> ChihirosDiscovery:
        """Create a selectable candidate from a Bluetooth discovery."""
        return cls(
            address=discovery_info.address,
            name=discovery_info.name,
            bluetooth_info=discovery_info,
        )

    @classmethod
    def from_fake(cls, fake_info: FakeChihirosDeviceInfo) -> ChihirosDiscovery:
        """Create a selectable candidate from fake device metadata."""
        return cls(
            address=fake_info.address,
            name=fake_info.name,
            fake_info=fake_info,
        )

    @property
    def is_fake(self) -> bool:
        """Return whether this candidate is a development fake."""
        return self.fake_info is not None

    def entry_data(self) -> dict[str, str]:
        """Return config entry data for a direct entry."""
        return {CONF_ADDRESS: self.address}

    def display_name(self) -> str:
        """Return the option label shown in the config flow."""
        return f"{self.name} ({self.address})"


def discovery_title(device: ChihirosClient, discovery: ChihirosDiscovery) -> str:
    """Return the config entry title for a real discovery."""
    return device.name or discovery.name

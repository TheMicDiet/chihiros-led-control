"""Magnetic-stirrer support: capability check and Home Assistant entities.

The stirrer (``DYMIXR``, app device type ``MagStirrer``) speaks the dosing
pump protocol in timer mode (``chihiros_xapk/DOSING_CONTROL.md`` §6). The
device sends no notifications that the vendor app parses (§6.5 — its UI is
fire-and-forget), so every stirrer entity is optimistic and restored across
Home Assistant restarts, mirroring the app's own behaviour of showing the
locally persisted model state.
"""

from __future__ import annotations

import logging
from typing import cast

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .coordinator import ChihirosDataUpdateCoordinator
from .entity import chihiros_device_info, chihiros_entity_name, chihiros_unique_id
from .models import ChihirosData, StirrerChannelState
from .runtime import StirrerChihirosClient
from .vendor.chihiros_led_control.models import MAG_STIRRER

_LOGGER = logging.getLogger(__name__)

# The stirrer model exposes up to 8 channels (DOSING_CONTROL.md §2/§6.1,
# field_f3 default 8). The config flow lets owners configure a smaller count.
STIRRER_CHANNEL_MAX = 8
# Backwards-compatible alias: the default/full channel count.
STIRRER_CHANNEL_COUNT = STIRRER_CHANNEL_MAX


def is_stirrer_capable(device: object) -> bool:
    """Return whether a runtime client or model is a magnetic stirrer."""
    return getattr(device, "model_name", getattr(device, "name", None)) == MAG_STIRRER.name


def stirrer_client(device: object) -> StirrerChihirosClient:
    """Return the device as a stirrer client, raising if it is not one."""
    if not is_stirrer_capable(device):
        raise HomeAssistantError(f"{getattr(device, 'name', device)} is not a magnetic stirrer")
    return cast(StirrerChihirosClient, device)


def set_stirrer_pre_run_entities_enabled(
    hass: HomeAssistant, address: str, channel_count: int, *, enabled: bool
) -> None:
    """Enable or disable a stirrer's pre-run numbers.

    Pre-stir time only matters in slave mode, so the entities start disabled
    (``entity_registry_enabled_default = False``). Linking a master enables
    them and explicitly unlinking restores the disabled default. Plain reloads
    intentionally leave the registry untouched so a user who enabled the
    numbers manually is not overridden on every restart.
    """
    registry = er.async_get(hass)
    desired_disabled_by = None if enabled else er.RegistryEntryDisabler.INTEGRATION
    for channel in range(1, channel_count + 1):
        unique_id = chihiros_unique_id(address, f"stir_channel_{channel}_pre_run")
        entity_id = registry.async_get_entity_id("number", DOMAIN, unique_id)
        if entity_id is None:
            continue
        entity_entry = registry.async_get(entity_id)
        if entity_entry is None or entity_entry.disabled_by == desired_disabled_by:
            continue
        registry.async_update_entity(entity_id, disabled_by=desired_disabled_by)


def _channel_states(chihiros_data: ChihirosData) -> list[StirrerChannelState]:
    """Return the per-channel state list for a configured stirrer."""
    if not chihiros_data.stirrer_states:
        raise HomeAssistantError(f"{chihiros_data.device.name} has no stirrer channels configured")
    return chihiros_data.stirrer_states


class ChihirosStirSwitch(
    PassiveBluetoothCoordinatorEntity[ChihirosDataUpdateCoordinator],
    SwitchEntity,
    RestoreEntity,
):
    """Switch to manually start/stop one stir channel (app's ``tempRun``).

    State is optimistic: the device confirms nothing back, and the vendor app
    likewise only shows its persisted model (§6.5). The restored value is not
    re-sent on Home Assistant restart — the device keeps running its own
    schedule regardless of this switch. Availability follows the coordinator's
    bluetooth reachability so automations can react to the device going away.
    """

    _attr_should_poll = False

    def __init__(self, device: object, chihiros_data: ChihirosData, channel: int) -> None:
        """Initialize the stir switch for one channel."""
        super().__init__(chihiros_data.coordinator)
        self._device = device
        self._coordinator = chihiros_data.coordinator
        self._state = _channel_states(chihiros_data)[channel]
        self._channel = channel
        channel_number = channel + 1
        self._attr_name = chihiros_entity_name(device, f"Stir channel {channel_number}")
        self._attr_unique_id = chihiros_unique_id(device.address, f"stir_channel_{channel_number}")
        self._attr_device_info = chihiros_device_info(device, device.address)

    @property
    def available(self) -> bool:
        """Return whether the device is reachable (or faked)."""
        if self._coordinator.always_available:
            return True
        return super().available

    async def async_added_to_hass(self) -> None:
        """Restore the last known optimistic state without re-sending it."""
        await super().async_added_to_hass()
        if last_state := await self.async_get_last_state():
            self._state.running = last_state.state == "on"

    @property
    def is_on(self) -> bool:
        """Return whether the channel is currently stirring."""
        return self._state.running

    async def async_turn_on(self, **kwargs: object) -> None:
        """Start stirring the channel (unlimited duration)."""
        await self._apply(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        """Stop stirring the channel."""
        await self._apply(False)

    async def _apply(self, on: bool) -> None:
        """Send the manual start/stop frame and update the optimistic state."""
        client = stirrer_client(self._device)
        try:
            await client.stir(self._channel, on)
        except Exception as ex:
            raise HomeAssistantError(f"Failed to {'start' if on else 'stop'} {self._attr_name}") from ex
        self._state.running = on
        self.async_write_ha_state()


class ChihirosStirNumberBase(
    PassiveBluetoothCoordinatorEntity[ChihirosDataUpdateCoordinator],
    NumberEntity,
    RestoreEntity,
):
    """Base for the per-channel stir speed and pre-run numbers.

    Both values ride on one ``(0xA5, 42)`` frame (``stirrerPreSecond``), so
    every write sends the shared current state for both. State is optimistic
    and restored across restarts without re-sending (§6.5).
    """

    _attr_should_poll = False
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_step = 1

    _unique_id_suffix = ""

    def __init__(self, device: object, chihiros_data: ChihirosData, channel: int) -> None:
        """Initialize the number for one stir channel."""
        super().__init__(chihiros_data.coordinator)
        self._device = device
        self._coordinator = chihiros_data.coordinator
        self._state = _channel_states(chihiros_data)[channel]
        self._channel = channel
        channel_number = channel + 1
        self._attr_name = chihiros_entity_name(device, f"Stir channel {channel_number} {self._label}")
        self._attr_unique_id = chihiros_unique_id(
            device.address, f"stir_channel_{channel_number}_{self._unique_id_suffix}"
        )
        self._attr_device_info = chihiros_device_info(device, device.address)

    @property
    def _label(self) -> str:
        """Return the human-readable label for this number."""
        raise NotImplementedError

    def _read_state(self) -> int:
        """Return this number's value from the shared channel state."""
        raise NotImplementedError

    def _write_state(self, value: int) -> None:
        """Store this number's value into the shared channel state."""
        raise NotImplementedError

    async def async_added_to_hass(self) -> None:
        """Restore the last configured value into the shared state (no write)."""
        await super().async_added_to_hass()
        if last_state := await self.async_get_last_state():
            try:
                value = int(float(last_state.state))
            except ValueError:
                return
            if self.native_min_value <= value <= self.native_max_value:
                self._write_state(value)

    @property
    def available(self) -> bool:
        """Return whether the device is reachable (or faked)."""
        if self._coordinator.always_available:
            return True
        return super().available

    @property
    def native_value(self) -> float | None:
        """Return the current optimistic value."""
        return float(self._read_state())

    async def async_set_native_value(self, value: float) -> None:
        """Write the value to the device and update the shared state."""
        # Speed and pre-run share one (0xA5, 42) frame: serialize concurrent
        # writes so the second frame always carries both committed values.
        async with self._state.lock:
            clamped = int(min(max(value, self.native_min_value), self.native_max_value))
            previous = self._read_state()
            self._write_state(clamped)
            client = stirrer_client(self._device)
            try:
                if self._state.running:
                    await client.set_pre_second(
                        self._channel,
                        self._state.pre_seconds,
                        self._state.speed,
                        restart=True,
                    )
                else:
                    await client.set_pre_second(self._channel, self._state.pre_seconds, self._state.speed)
            except Exception as ex:
                self._write_state(previous)
                raise HomeAssistantError(f"Failed to set {self._attr_name}") from ex
        self.async_write_ha_state()


class ChihirosStirSpeedNumber(ChihirosStirNumberBase):
    """Number entity for one channel's stir speed (app default 40)."""

    _attr_native_max_value = 100
    _attr_native_unit_of_measurement = "%"
    _unique_id_suffix = "speed"
    _label = "speed"

    def _read_state(self) -> int:
        """Return the channel's stir speed."""
        return self._state.speed

    def _write_state(self, value: int) -> None:
        """Store the channel's stir speed."""
        self._state.speed = value


class ChihirosStirPreRunNumber(ChihirosStirNumberBase):
    """Number entity for one channel's pre-stir time (0-999 s, app bound).

    The pre-stir time only takes effect while the stirrer runs as a slave of
    a linked dosing pump (stir before each dose), so the entity is disabled
    by default to keep the per-channel entity list focused on manual use.
    """

    _attr_native_max_value = 999
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_entity_registry_enabled_default = False
    _unique_id_suffix = "pre_run"
    _label = "pre-run"

    def _read_state(self) -> int:
        """Return the channel's pre-stir seconds."""
        return self._state.pre_seconds

    def _write_state(self, value: int) -> None:
        """Store the channel's pre-stir seconds."""
        self._state.pre_seconds = value

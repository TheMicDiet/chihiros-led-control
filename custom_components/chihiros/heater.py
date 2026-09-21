"""Heater support: capability check and Home Assistant entities.

The heater (``DYHET``/``DYH1T``) is a plain-BLE accessory that speaks the
standard ``0x5A`` framing with its own mode bytes, and pushes two ``0x5B``
notification frames (temperatures, runtime/alarms). It only reports its
temperatures, runtime, firmware and alarms, so the power, auto-heating state,
display unit and protection temperature are tracked optimistically and
restored across Home Assistant restarts — the same way the vendor app
persists them (``chihiros_xapk/HEATER_CONTROL.md``).

The vendor app has two separate "auto" controls and so does this module: the
mode switch (``switchToManual``/``switchToScene``) picks between the manual
setpoints and the stored auto schedule, while the auto-heating switch
(``setHeaterAuto``) only arms the heating element while the device runs in
auto mode. Auto mode heats towards its own defaults (``initAutoDefault``), so
the auto temperature and power numbers are separate from the manual ones.
"""

from __future__ import annotations

from typing import Any, cast

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.components.button import ButtonEntity
from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import (
    ATTR_HEATER_ALARM_BITS,
    ATTR_HEATER_ALARMS,
    ATTR_HEATER_SETTING_TEMPERATURE_CELSIUS,
    HEATER_MODE_AUTO,
    HEATER_MODE_MANUAL,
    HEATER_MODES,
    ChihirosDataUpdateCoordinator,
)
from .entity import chihiros_device_info, chihiros_entity_name, chihiros_unique_id
from .runtime import ChihirosClient, HeaterChihirosClient, is_device_kind
from .vendor.chihiros_led_control.commands import (
    HEATER_MAX_POWER_WATTS,
    HEATER_MAX_TEMPERATURE_C,
)
from .vendor.chihiros_led_control.models import DeviceKind

# The wire carries power as watts ÷ 10, so 10 W is the finest settable step.
HEATER_POWER_STEP_WATTS = 10
# Temperatures ride as [whole, tenths]; the app's picker moves in 0.5 °C steps.
HEATER_TEMPERATURE_STEP_C = 0.5


def is_heater_capable(device: object) -> bool:
    """Return whether a runtime client or model is a Chihiros heater."""
    return is_device_kind(device, DeviceKind.HEATER)


def heater_client(device: object) -> HeaterChihirosClient:
    """Return the device as a heater client, raising if it is not one."""
    if not is_heater_capable(device):
        raise HomeAssistantError(f"{getattr(device, 'name', device)} is not a heater")
    return cast(HeaterChihirosClient, device)


class ChihirosHeaterEntity(PassiveBluetoothCoordinatorEntity[ChihirosDataUpdateCoordinator]):
    """Shared availability and naming for heater entities."""

    _attr_should_poll = False

    def __init__(
        self,
        coordinator: ChihirosDataUpdateCoordinator,
        device: ChihirosClient,
        unique_id_suffix: str,
        name_suffix: str,
    ) -> None:
        """Initialize the heater entity."""
        super().__init__(coordinator)
        self._device = device
        self._client = heater_client(device)
        self._attr_name = chihiros_entity_name(device, name_suffix)
        self._attr_unique_id = chihiros_unique_id(coordinator.address, unique_id_suffix)
        self._attr_device_info = chihiros_device_info(device, coordinator.address)

    @property
    def available(self) -> bool:
        """Return whether the device is reachable (or faked)."""
        if self.coordinator.always_available:
            return True
        return super().available


class ChihirosHeaterNumber(ChihirosHeaterEntity, NumberEntity, RestoreEntity):
    """Base for the heater's setpoint numbers.

    Numbers the device never reports (power, protection temperature,
    calibration) show the last value written through Home Assistant, or the
    value restored from the previous run. The target temperature prefers what
    the device reports back, which outranks a value written through Home
    Assistant as soon as it arrives.
    """

    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = HEATER_MAX_TEMPERATURE_C
    _attr_native_step = HEATER_TEMPERATURE_STEP_C

    def __init__(
        self,
        coordinator: ChihirosDataUpdateCoordinator,
        device: ChihirosClient,
        unique_id_suffix: str,
        name_suffix: str,
    ) -> None:
        """Initialize the heater number."""
        super().__init__(coordinator, device, unique_id_suffix, name_suffix)
        self._restored_value: float | None = None
        self._pending_value: float | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the last value written through Home Assistant, without re-sending it."""
        await super().async_added_to_hass()
        if last_state := await self.async_get_last_state():
            try:
                value = float(last_state.state)
            except ValueError:
                return
            if self.native_min_value <= value <= self.native_max_value:
                try:
                    self._restore_client_value(value)
                except ValueError:
                    return
                self._restored_value = value

    @property
    def native_value(self) -> float | None:
        """Return the value written through Home Assistant or the device state."""
        if self._pending_value is not None:
            return self._pending_value
        return self._fallback_value()

    def _fallback_value(self) -> float | None:
        """Return the value to show when Home Assistant has not written one."""
        return self._restored_value

    def _restore_client_value(self, value: float) -> None:
        """Restore a paired client value without writing to the device."""

    def _handle_coordinator_update(self) -> None:
        """Clear a pending value when this coordinator update supersedes it."""
        if self._clear_pending_on_coordinator_update():
            self._pending_value = None
        super()._handle_coordinator_update()

    def _clear_pending_on_coordinator_update(self) -> bool:
        """Return whether a coordinator update supersedes the pending value."""
        return True

    async def async_set_native_value(self, value: float) -> None:
        """Write the value to the device."""
        previous_pending_value = self._pending_value
        self._pending_value = value
        try:
            await self._async_write_value(value)
        except Exception as ex:
            if self._pending_value == value:
                self._pending_value = previous_pending_value
            raise HomeAssistantError(f"Failed to set {self._attr_name}") from ex
        # Remember what was written: the device never reports the power,
        # protection temperature or calibration, so this is their last known
        # value, and it keeps a restored value from overriding a fresh write.
        # Do not re-assert the pending value here: an authoritative notification
        # may already have cleared it while the BLE write was in progress.
        self._restored_value = value
        self.async_write_ha_state()

    async def _async_write_value(self, value: float) -> None:
        """Send one value to the device."""
        raise NotImplementedError


class ChihirosHeaterTemperatureNumber(ChihirosHeaterNumber):
    """Target temperature the heater holds in manual mode."""

    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = NumberDeviceClass.TEMPERATURE

    def __init__(self, coordinator: ChihirosDataUpdateCoordinator, device: ChihirosClient) -> None:
        """Initialize the target temperature number."""
        super().__init__(coordinator, device, "heater_temperature", "Temperature")
        self._last_temperature_update_id = coordinator.heater_temperature_update_id

    def _fallback_value(self) -> float | None:
        """Return the setting temperature the device reported, if any."""
        return self.coordinator.data.get(ATTR_HEATER_SETTING_TEMPERATURE_CELSIUS)

    def _restore_client_value(self, value: float) -> None:
        """Restore the manual target temperature for future paired writes."""
        self._client.restore_setting_temperature(value)

    def _clear_pending_on_coordinator_update(self) -> bool:
        """Clear only when a temperature notification reports authoritative state."""
        update_id = self.coordinator.heater_temperature_update_id
        if update_id == self._last_temperature_update_id:
            return False
        self._last_temperature_update_id = update_id
        return True

    async def _async_write_value(self, value: float) -> None:
        """Set the target temperature (the client switches to manual mode)."""
        await self._client.set_temperature(value)
        self.coordinator.async_set_heater_mode(HEATER_MODE_MANUAL)


class ChihirosHeaterPowerNumber(ChihirosHeaterNumber):
    """Manual power of the heating element."""

    _attr_native_min_value = 0
    _attr_native_max_value = HEATER_MAX_POWER_WATTS
    _attr_native_step = HEATER_POWER_STEP_WATTS
    _attr_native_unit_of_measurement = "W"

    def __init__(self, coordinator: ChihirosDataUpdateCoordinator, device: ChihirosClient) -> None:
        """Initialize the power number."""
        super().__init__(coordinator, device, "heater_power", "Power")

    def _fallback_value(self) -> float | None:
        """Return the restored power, or the client's tracked power."""
        if self._restored_value is not None:
            return self._restored_value
        return float(self._client.power_watts)

    def _restore_client_value(self, value: float) -> None:
        """Restore manual power for future paired writes."""
        self._client.restore_manual_power(int(value))

    async def _async_write_value(self, value: float) -> None:
        """Set the manual power (the client switches to manual mode)."""
        await self._client.set_power(int(value))
        self.coordinator.async_set_heater_mode(HEATER_MODE_MANUAL)


class ChihirosHeaterAutoDefaultNumber(ChihirosHeaterNumber):
    """Base for the setpoints the heater's auto schedules heat towards.

    Auto mode ignores the manual temperature and power and uses this pair
    instead, so the two are configured separately. The device never reports
    them, and both always travel in one frame, which is why each number shows
    the client's tracked pair and resends its sibling when written.
    """

    _attr_entity_category = EntityCategory.CONFIG


class ChihirosHeaterAutoTemperatureNumber(ChihirosHeaterAutoDefaultNumber):
    """Temperature auto mode heats towards."""

    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = NumberDeviceClass.TEMPERATURE

    def __init__(self, coordinator: ChihirosDataUpdateCoordinator, device: ChihirosClient) -> None:
        """Initialize the auto default temperature number."""
        super().__init__(coordinator, device, "heater_auto_temperature", "Auto temperature")

    def _fallback_value(self) -> float | None:
        """Return the client's tracked auto temperature."""
        return self._client.auto_default_temperature_celsius

    def _restore_client_value(self, value: float) -> None:
        """Restore auto temperature for future paired writes."""
        self._client.restore_auto_default_temperature(value)

    async def _async_write_value(self, value: float) -> None:
        """Set the auto-mode default temperature."""
        await self._client.set_auto_default_temperature(value)


class ChihirosHeaterAutoPowerNumber(ChihirosHeaterAutoDefaultNumber):
    """Power auto mode heats with."""

    _attr_native_max_value = HEATER_MAX_POWER_WATTS
    _attr_native_step = HEATER_POWER_STEP_WATTS
    _attr_native_unit_of_measurement = "W"

    def __init__(self, coordinator: ChihirosDataUpdateCoordinator, device: ChihirosClient) -> None:
        """Initialize the auto default power number."""
        super().__init__(coordinator, device, "heater_auto_power", "Auto power")

    def _fallback_value(self) -> float | None:
        """Return the client's tracked auto power."""
        return float(self._client.auto_default_power_watts)

    def _restore_client_value(self, value: float) -> None:
        """Restore auto power for future paired writes."""
        self._client.restore_auto_default_power(int(value))

    async def _async_write_value(self, value: float) -> None:
        """Set the auto-mode default power."""
        await self._client.set_auto_default_power(int(value))


class ChihirosHeaterProtectorNumber(ChihirosHeaterNumber):
    """Overheat protection temperature."""

    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: ChihirosDataUpdateCoordinator, device: ChihirosClient) -> None:
        """Initialize the protection temperature number."""
        super().__init__(coordinator, device, "heater_protector_temperature", "Protection temperature")

    def _fallback_value(self) -> float | None:
        """Return the restored protection temperature, or the client's tracked value."""
        if self._restored_value is not None:
            return self._restored_value
        return self._client.protector_temperature_celsius

    async def _async_write_value(self, value: float) -> None:
        """Set the overheat protection temperature."""
        await self._client.set_protector_temperature(value)


class ChihirosHeaterCalibrationNumber(ChihirosHeaterNumber):
    """Reference temperature used to calibrate the heater's sensor.

    Writing a value tells the device that its sensor should currently read
    exactly that temperature; the value is therefore a record of the last
    calibration, not a device state that is read back.
    """

    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:thermometer-check"

    def __init__(self, coordinator: ChihirosDataUpdateCoordinator, device: ChihirosClient) -> None:
        """Initialize the calibration number."""
        super().__init__(coordinator, device, "heater_calibration_temperature", "Calibration temperature")

    async def _async_write_value(self, value: float) -> None:
        """Calibrate the sensor against the measured reference temperature."""
        await self._client.calibrate(value)


class ChihirosHeaterOptimisticSwitch(ChihirosHeaterEntity, SwitchEntity, RestoreEntity):
    """Base for the heater's write-only switches.

    The device does not report the auto-heating or backlight state, so both are
    optimistic and restored across restarts without being re-sent.
    """

    _state_property = ""
    _unique_id_suffix = ""
    _name_suffix = ""

    def __init__(self, coordinator: ChihirosDataUpdateCoordinator, device: ChihirosClient) -> None:
        """Initialize the heater switch."""
        super().__init__(coordinator, device, self._unique_id_suffix, self._name_suffix)
        self._restored_state: bool | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the last known state."""
        await super().async_added_to_hass()
        if last_state := await self.async_get_last_state():
            self._restored_state = last_state.state == "on"

    @property
    def is_on(self) -> bool:
        """Return the tracked switch state."""
        if self._restored_state is not None:
            return self._restored_state
        return bool(getattr(self._client, self._state_property))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the setting on the device."""
        await self._async_apply(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the setting on the device."""
        await self._async_apply(False)

    async def _async_apply(self, enabled: bool) -> None:
        """Send the switch state and update the optimistic state."""
        try:
            await self._async_write(enabled)
        except Exception as ex:
            raise HomeAssistantError(f"Failed to set {self._attr_name}") from ex
        self._restored_state = enabled
        self.async_write_ha_state()

    async def _async_write(self, enabled: bool) -> None:
        """Send one switch state to the device."""
        raise NotImplementedError


class ChihirosHeaterAutoHeatingSwitch(ChihirosHeaterOptimisticSwitch):
    """Switch the heater's automatic heating on or off."""

    _state_property = "auto_heating"
    _unique_id_suffix = "heater_auto_heating"
    _name_suffix = "Auto heating"

    async def _async_write(self, enabled: bool) -> None:
        """Enable or disable automatic heating."""
        await self._client.set_auto_heating(enabled)


class ChihirosHeaterBacklightSwitch(ChihirosHeaterOptimisticSwitch):
    """Switch the heater's display backlight on or off."""

    _state_property = "backlight"
    _unique_id_suffix = "heater_backlight"
    _name_suffix = "Backlight"
    _attr_entity_category = EntityCategory.CONFIG

    async def _async_write(self, enabled: bool) -> None:
        """Turn the display backlight on or off."""
        await self._client.set_backlight(enabled)


class ChihirosHeaterModeSelect(ChihirosHeaterEntity, SelectEntity, RestoreEntity):
    """Mode the heater runs in: its manual setpoints or the stored schedule.

    The mode frames carry no setpoint — switching modes never changes the
    manual or auto temperature/power, which the four numbers own. The device
    never reports its mode, so the selection is optimistic, restored across
    restarts, and put back to manual whenever a manual setpoint is written
    (the client sends ``switchToManual`` before every manual state frame).
    """

    _attr_options = list(HEATER_MODES)

    def __init__(self, coordinator: ChihirosDataUpdateCoordinator, device: ChihirosClient) -> None:
        """Initialize the mode select."""
        super().__init__(coordinator, device, "heater_mode", "Mode")

    async def async_added_to_hass(self) -> None:
        """Restore the last mode, which the device never reports back."""
        await super().async_added_to_hass()
        if last_state := await self.async_get_last_state():
            if last_state.state in HEATER_MODES:
                self.coordinator.async_set_heater_mode(last_state.state)

    @property
    def current_option(self) -> str:
        """Return the mode the integration last drove or restored."""
        return self.coordinator.heater_mode

    async def async_select_option(self, option: str) -> None:
        """Switch the heater to manual mode or apply its stored auto schedule."""
        try:
            if option == HEATER_MODE_AUTO:
                await self._client.apply_scene()
            else:
                await self._client.set_manual_mode()
        except Exception as ex:
            raise HomeAssistantError(f"Failed to set {self._attr_name}") from ex
        self.coordinator.async_set_heater_mode(option)


class ChihirosHeaterTemperatureUnitSelect(ChihirosHeaterEntity, SelectEntity, RestoreEntity):
    """Unit the heater's own display shows.

    Home Assistant converts temperatures to the unit system configured for the
    instance, so this setting only changes what the device itself displays.
    """

    _attr_options = [UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT]
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: ChihirosDataUpdateCoordinator, device: ChihirosClient) -> None:
        """Initialize the temperature unit select."""
        super().__init__(coordinator, device, "heater_temperature_unit", "Temperature unit")
        self._restored_option: str | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the last known display unit."""
        await super().async_added_to_hass()
        if last_state := await self.async_get_last_state():
            if last_state.state in self._attr_options:
                self._restored_option = last_state.state

    @property
    def current_option(self) -> str:
        """Return the unit the device displays."""
        if self._restored_option is not None:
            return self._restored_option
        if self._client.is_celsius:
            return UnitOfTemperature.CELSIUS
        return UnitOfTemperature.FAHRENHEIT

    async def async_select_option(self, option: str) -> None:
        """Set the display unit."""
        try:
            await self._client.set_temperature_unit(celsius=option == UnitOfTemperature.CELSIUS)
        except Exception as ex:
            raise HomeAssistantError(f"Failed to set {self._attr_name}") from ex
        self._restored_option = option
        self.async_write_ha_state()


class ChihirosHeaterResetWorkTimeButton(ChihirosHeaterEntity, ButtonEntity):
    """Zero the heater's runtime counter after the heating tube is cleaned."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: ChihirosDataUpdateCoordinator, device: ChihirosClient) -> None:
        """Initialize the runtime reset button."""
        super().__init__(coordinator, device, "heater_reset_work_time", "Reset runtime")

    async def async_press(self) -> None:
        """Reset the runtime counter."""
        try:
            await self._client.reset_work_time()
        except Exception as ex:
            raise HomeAssistantError(f"Failed to reset {self._attr_name}") from ex


class ChihirosHeaterAlarmSensor(ChihirosHeaterEntity, SensorEntity):
    """Active alarms reported by the heater's status frame.

    The state lists the active alarm names; the raw bitfield and its labels
    are exposed as attributes so automations can react to individual bits.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:alert-outline"

    def __init__(self, coordinator: ChihirosDataUpdateCoordinator, device: ChihirosClient) -> None:
        """Initialize the alarm sensor."""
        super().__init__(coordinator, device, "heater_alarms", "Alarms")

    @property
    def native_value(self) -> str | None:
        """Return the active alarm names, or ``ok`` when the device reported none."""
        alarms = self.coordinator.data.get(ATTR_HEATER_ALARMS)
        if alarms is None:
            return None
        return ", ".join(alarms) if alarms else "ok"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the raw alarm bitfield and the bit labels."""
        alarm_bits = self.coordinator.data.get(ATTR_HEATER_ALARM_BITS)
        if alarm_bits is None:
            return None
        return {"alarm_bits": alarm_bits, "alarms": list(self.coordinator.data.get(ATTR_HEATER_ALARMS, ()))}

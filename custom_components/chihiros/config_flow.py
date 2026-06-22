"""Config flow for chihiros integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS, CONF_NAME

from .const import DOMAIN
from .discovery import ChihirosDiscovery, discovery_title
from .dosing import CONF_PUMP_COUNT, PUMP_COUNT, PUMP_COUNT_OPTIONS, is_dosing_capable, normalize_pump_count
from .fake import iter_enabled_fake_devices
from .vendor.chihiros_led_control import (
    ChihirosDevice,
    create_device,
    needs_device_type,
)

_LOGGER = logging.getLogger(__name__)


class ChihirosConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for chihiros."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_device: ChihirosDevice | None = None
        self._discovered_devices: dict[str, ChihirosDiscovery] = {}
        self._entry_title: str | None = None
        self._entry_address: str | None = None

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        device = create_device(discovery_info.device)
        self._discovery_info = discovery_info
        self._discovered_device = device
        _LOGGER.debug("async_step_bluetooth - discovered device %s", discovery_info.name)
        if needs_device_type(discovery_info.name):
            return await self.async_step_fallback_config()

        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm discovery."""
        assert self._discovered_device is not None
        device = self._discovered_device
        assert self._discovery_info is not None
        discovery_info = self._discovery_info
        title = device.name or discovery_info.name
        if user_input is not None:
            self._entry_title = title
            self._entry_address = discovery_info.address
            if is_dosing_capable(device):
                return await self.async_step_dosing_config()
            return self.async_create_entry(title=title, data={CONF_ADDRESS: discovery_info.address})

        self._set_confirm_only()
        placeholders = {"name": title}
        self.context["title_placeholders"] = placeholders
        return self.async_show_form(step_id="bluetooth_confirm", description_placeholders=placeholders)

    async def async_step_dosing_config(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask user how many channels a dosing pump has."""
        assert self._entry_title is not None
        assert self._entry_address is not None

        if user_input is not None:
            return self.async_create_entry(
                title=self._entry_title,
                data={
                    CONF_ADDRESS: self._entry_address,
                    CONF_PUMP_COUNT: normalize_pump_count(user_input[CONF_PUMP_COUNT]),
                },
            )

        data_schema = vol.Schema(
            {vol.Required(CONF_PUMP_COUNT, default=PUMP_COUNT): vol.All(vol.Coerce(int), vol.In(PUMP_COUNT_OPTIONS))}
        )
        return self.async_show_form(step_id="dosing_config", data_schema=data_schema, errors={})

    async def async_step_fallback_config(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask user for device details when fallback device is detected."""
        assert self._discovered_device is not None
        assert self._discovery_info is not None
        discovery_info = self._discovery_info

        errors: dict[str, str] = {}
        if user_input is not None:
            # Create config entry including the address, chosen name and device type
            title = user_input.get(CONF_NAME) or self._discovered_device.name or discovery_info.name
            data = {
                CONF_ADDRESS: discovery_info.address,
                CONF_NAME: user_input.get(CONF_NAME, title),
                "device_type": user_input["device_type"],
            }
            return self.async_create_entry(title=title, data=data)

        # Default name to discovered name
        default_name = self._discovered_device.name or discovery_info.name
        data_schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=default_name): str,
                vol.Required("device_type", default="white"): vol.In(["white", "rgb", "wrgb"]),
            }
        )
        return self.async_show_form(step_id="fallback_config", data_schema=data_schema, errors=errors)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the user step to pick discovered device."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            discovery = self._discovered_devices[address]
            if discovery.is_fake:
                await self.async_set_unique_id(discovery.address, raise_on_progress=False)
                self._abort_if_unique_id_configured()
                self._entry_title = discovery.name
                self._entry_address = discovery.address
                if discovery.fake_info and is_dosing_capable(discovery.fake_info.model):
                    return await self.async_step_dosing_config()
                return self.async_create_entry(title=discovery.name, data=discovery.entry_data())

            discovery_info = discovery.bluetooth_info
            assert discovery_info is not None
            await self.async_set_unique_id(discovery_info.address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            device = create_device(discovery_info.device)

            self._discovery_info = discovery_info
            self._discovered_device = device
            if needs_device_type(discovery_info.name):
                return await self.async_step_fallback_config()

            title = discovery_title(device, discovery)
            self._entry_title = title
            self._entry_address = discovery_info.address
            if is_dosing_capable(device):
                return await self.async_step_dosing_config()
            return self.async_create_entry(title=title, data={CONF_ADDRESS: discovery_info.address})

        if discovery := self._discovery_info:
            self._discovered_devices[discovery.address] = ChihirosDiscovery.from_bluetooth(discovery)
        else:
            current_addresses = self._async_current_ids()
            for discovery in async_discovered_service_info(self.hass):
                if (
                    discovery is not None
                    and discovery.address not in current_addresses
                    and discovery.address not in self._discovered_devices
                ):
                    self._discovered_devices[discovery.address] = ChihirosDiscovery.from_bluetooth(discovery)

        current_addresses = self._async_current_ids()
        for fake_device in iter_enabled_fake_devices(current_addresses):
            fake_discovery = ChihirosDiscovery.from_fake(fake_device)
            self._discovered_devices.setdefault(fake_discovery.address, fake_discovery)

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        data_schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS): vol.In(
                    {device.address: device.display_name() for device in self._discovered_devices.values()}
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=data_schema, errors=errors)

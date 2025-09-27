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

from .chihiros_led_control.device import BaseDevice, get_model_class_from_name
from .chihiros_led_control.device.commander1 import Commander1
from .chihiros_led_control.device.commander4 import Commander4
from .chihiros_led_control.device.fallback import Fallback
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

ADDITIONAL_DISCOVERY_TIMEOUT = 60


class ChihirosConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for chihiros."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_device: BaseDevice | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        model_class = get_model_class_from_name(discovery_info.name)
        device = model_class(discovery_info.device)
        self._discovery_info = discovery_info
        self._discovered_device = device
        _LOGGER.debug(
            "async_step_bluetooth - discovered device %s", discovery_info.name
        )
        # If we don't know the exact device model (fallback), ask for extra info
        model_class = get_model_class_from_name(discovery_info.name)
        if model_class in (Fallback, Commander1, Commander4):
            # Fallback detected - move to fallback config step
            self._discovery_info = discovery_info
            self._discovered_device = model_class(discovery_info.device)
            return await self.async_step_fallback_config()

        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm discovery."""
        assert self._discovered_device is not None
        device = self._discovered_device
        assert self._discovery_info is not None
        discovery_info = self._discovery_info
        title = device.name or discovery_info.name
        if user_input is not None:
            return self.async_create_entry(title=title, data={CONF_ADDRESS: discovery_info.address})  # type: ignore

        self._set_confirm_only()
        placeholders = {"name": title}
        self.context["title_placeholders"] = placeholders
        return self.async_show_form(  # type: ignore
            step_id="bluetooth_confirm", description_placeholders=placeholders
        )

    async def async_step_fallback_config(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask user for device details when fallback device is detected."""
        assert self._discovered_device is not None
        assert self._discovery_info is not None
        discovery_info = self._discovery_info

        errors: dict[str, str] = {}
        if user_input is not None:
            # Create config entry including the address, chosen name and device type
            title = (
                user_input.get(CONF_NAME)
                or self._discovered_device.name
                or discovery_info.name
            )
            data = {
                CONF_ADDRESS: discovery_info.address,
                CONF_NAME: user_input.get(CONF_NAME, title),
                "device_type": user_input["device_type"],
            }
            return self.async_create_entry(title=title, data=data)  # type: ignore

        # Default name to discovered name
        default_name = self._discovered_device.name or discovery_info.name
        data_schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=default_name): str,
                vol.Required("device_type", default="white"): vol.In(
                    ["white", "rgb", "wrgb"]
                ),
            }
        )
        return self.async_show_form(
            step_id="fallback_config", data_schema=data_schema, errors=errors
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the user step to pick discovered device."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            discovery_info = self._discovered_devices[address]
            await self.async_set_unique_id(
                discovery_info.address, raise_on_progress=False
            )
            self._abort_if_unique_id_configured()
            model_class = get_model_class_from_name(discovery_info.name)
            device = model_class(discovery_info.device)

            self._discovery_info = discovery_info
            self._discovered_device = device
            model_class = get_model_class_from_name(discovery_info.name)
            # If fallback detected, ask for device details
            if model_class in (Fallback, Commander1, Commander4):
                return await self.async_step_fallback_config()

            title = device.name or discovery_info.name
            return self.async_create_entry(  # type: ignore
                title=title, data={CONF_ADDRESS: discovery_info.address}
            )

        if discovery := self._discovery_info:
            self._discovered_devices[discovery.address] = discovery
        else:
            current_addresses = self._async_current_ids()
            for discovery in async_discovered_service_info(self.hass):
                if (
                    discovery is not None
                    and discovery.address not in current_addresses
                    and discovery.address not in self._discovered_devices
                ):
                    self._discovered_devices[discovery.address] = discovery

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")  # type: ignore

        data_schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS): vol.In(
                    {
                        service_info.address: (
                            f"{service_info.name} ({service_info.address})"
                        )
                        for service_info in self._discovered_devices.values()
                    }
                ),
            }
        )
        return self.async_show_form(  # type: ignore
            step_id="user", data_schema=data_schema, errors=errors
        )

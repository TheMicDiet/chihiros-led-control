"""Config flow for chihiros integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import callback

from .const import CONF_MASTER_ADDRESS, DOMAIN
from .discovery import ChihirosDiscovery, discovery_title
from .dosing import (
    CONF_PUMP_COUNT,
    CONF_STIRRER_CHANNEL_COUNT,
    PUMP_COUNT,
    PUMP_COUNT_OPTIONS,
    STIRRER_CHANNEL_COUNT_OPTIONS,
    STIRRER_CHANNEL_MAX,
    is_dosing_capable,
    normalize_pump_count,
    normalize_stirrer_channel_count,
)
from .fake import iter_enabled_fake_devices
from .master_slave_services import async_mirror_pump_to_stirrer
from .models import DosingChihirosData, StirrerChihirosData
from .stirrer import is_stirrer_capable, set_stirrer_pre_run_entities_enabled
from .vendor.chihiros_led_control import (
    ChihirosDevice,
    create_device,
    needs_device_type,
)
from .vendor.chihiros_led_control.factory import is_known_unsupported_device

_LOGGER = logging.getLogger(__name__)

# Sentinel select value for "no master pump linked" (addresses never look like this).
UNLINKED_MASTER = "none"


class ChihirosConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for chihiros."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ChihirosOptionsFlow:
        """Return the options flow that changes the exposed channel count."""
        return ChihirosOptionsFlow()

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
        if is_known_unsupported_device(discovery_info.name):
            return self.async_abort(reason="not_supported")
        device = create_device(discovery_info.device)
        self._discovery_info = discovery_info
        self._discovered_device = device
        # The dosing/stirrer steps below create the entry from these fields, so
        # they must be populated on the discovery shortcut (not just after the
        # confirm step).
        self._entry_title = device.name or discovery_info.name
        self._entry_address = discovery_info.address
        _LOGGER.debug("async_step_bluetooth - discovered device %s", discovery_info.name)
        if needs_device_type(discovery_info.name):
            return await self.async_step_fallback_config()
        if is_dosing_capable(device):
            return await self.async_step_dosing_config()
        if is_stirrer_capable(device):
            return await self.async_step_stirrer_config()

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
            if is_stirrer_capable(device):
                return await self.async_step_stirrer_config()
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

    async def async_step_stirrer_config(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask user how many stir channels the magnetic stirrer should expose."""
        assert self._entry_title is not None
        assert self._entry_address is not None

        if user_input is not None:
            return self.async_create_entry(
                title=self._entry_title,
                data={
                    CONF_ADDRESS: self._entry_address,
                    CONF_STIRRER_CHANNEL_COUNT: normalize_stirrer_channel_count(user_input[CONF_STIRRER_CHANNEL_COUNT]),
                },
            )

        data_schema = vol.Schema(
            {
                vol.Required(CONF_STIRRER_CHANNEL_COUNT, default=STIRRER_CHANNEL_MAX): vol.All(
                    vol.Coerce(int), vol.In(STIRRER_CHANNEL_COUNT_OPTIONS)
                )
            }
        )
        return self.async_show_form(step_id="stirrer_config", data_schema=data_schema, errors={})

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

    async def _async_handle_fake_submission(self, discovery: ChihirosDiscovery) -> ConfigFlowResult:
        """Register a discovered fake device and continue its flow."""
        await self.async_set_unique_id(discovery.address, raise_on_progress=False)
        self._abort_if_unique_id_configured()
        self._entry_title = discovery.name
        self._entry_address = discovery.address
        if discovery.fake_info and is_dosing_capable(discovery.fake_info.model):
            return await self.async_step_dosing_config()
        if discovery.fake_info and is_stirrer_capable(discovery.fake_info.model):
            return await self.async_step_stirrer_config()
        return self.async_create_entry(title=discovery.name, data=discovery.entry_data())

    async def _async_handle_bluetooth_submission(self, discovery: ChihirosDiscovery) -> ConfigFlowResult:
        """Register a discovered Bluetooth device and continue its flow."""
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
        if is_stirrer_capable(device):
            return await self.async_step_stirrer_config()
        return self.async_create_entry(title=title, data={CONF_ADDRESS: discovery_info.address})

    async def _async_handle_user_submission(self, user_input: dict[str, Any]) -> ConfigFlowResult:
        """Continue the flow for the device the user picked."""
        address = user_input[CONF_ADDRESS]
        discovery = self._discovered_devices[address]
        if discovery.is_fake:
            return await self._async_handle_fake_submission(discovery)
        return await self._async_handle_bluetooth_submission(discovery)

    def _is_new_discovery(self, discovery: BluetoothServiceInfoBleak | None, current_addresses: set[str]) -> bool:
        """Return whether a Bluetooth service info should be offered in the picker."""
        return (
            discovery is not None
            and discovery.address not in current_addresses
            and discovery.address not in self._discovered_devices
            and not is_known_unsupported_device(discovery.name)
        )

    def _async_gather_bluetooth_discoveries(self) -> None:
        """Populate the picker from the active discovery info or the scanner cache."""
        if discovery := self._discovery_info:
            self._discovered_devices[discovery.address] = ChihirosDiscovery.from_bluetooth(discovery)
            return
        current_addresses = self._async_current_ids()
        for discovery in async_discovered_service_info(self.hass):
            if self._is_new_discovery(discovery, current_addresses):
                self._discovered_devices[discovery.address] = ChihirosDiscovery.from_bluetooth(discovery)

    def _async_gather_fake_discoveries(self) -> None:
        """Add enabled fake devices to the picker without overwriting entries."""
        current_addresses = self._async_current_ids()
        for fake_device in iter_enabled_fake_devices(current_addresses):
            fake_discovery = ChihirosDiscovery.from_fake(fake_device)
            self._discovered_devices.setdefault(fake_discovery.address, fake_discovery)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the user step to pick discovered device."""
        if user_input is not None:
            return await self._async_handle_user_submission(user_input)

        self._async_gather_bluetooth_discoveries()
        self._async_gather_fake_discoveries()

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        errors: dict[str, str] = {}
        data_schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS): vol.In(
                    {device.address: device.display_name() for device in self._discovered_devices.values()}
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=data_schema, errors=errors)


class ChihirosOptionsFlow(OptionsFlowWithReload):
    """Change channel counts and, for a stirrer, the linked master pump.

    The master link is stored on the stirrer's config entry data (the same
    ``master_address`` the ``chihiros.set_stirrer_master`` service writes), so
    both surfaces share one source of truth. Selecting a master here replays
    the pump's recorded programming immediately, best-effort.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the options for the configured device type."""
        data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if data is None:
            return self.async_abort(reason="not_loaded")
        if isinstance(data, DosingChihirosData):
            return self._channel_count_step(CONF_PUMP_COUNT, PUMP_COUNT, PUMP_COUNT_OPTIONS, user_input)
        if isinstance(data, StirrerChihirosData):
            return await self._async_stirrer_step(data, user_input)
        return self.async_abort(reason="no_options")

    def _channel_count_step(
        self,
        key: str,
        default: int,
        options: tuple[int, ...],
        user_input: dict[str, Any] | None,
    ) -> ConfigFlowResult:
        """Handle the dosing-pump channel-count form.

        The select carries string values (coerced back to int on save): the
        frontend reliably preselects the configured value only for string
        options, so keep this consistent with the stirrer/master selects.
        """
        if user_input is not None:
            return self.async_create_entry(title="", data={key: int(user_input[key])})
        current = self.config_entry.options.get(key, self.config_entry.data.get(key, default))
        data_schema = vol.Schema(
            {
                vol.Required(key, default=str(current)): vol.All(
                    vol.Coerce(str), vol.In({str(option): str(option) for option in options})
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=data_schema)

    async def _async_stirrer_step(
        self, data: StirrerChihirosData, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Handle the stirrer channel-count + master-pump form."""
        if user_input is not None:
            return await self._async_apply_stirrer_options(user_input, data)
        return self._async_stirrer_form()

    async def _async_apply_stirrer_options(
        self, user_input: dict[str, Any], data: StirrerChihirosData
    ) -> ConfigFlowResult:
        """Persist the selected master link and mirror the pump programming.

        An omitted ``master_address`` field leaves the persisted link untouched
        instead of unlinking it (the select normally submits its default, but a
        partial/automation-driven submission must not drop the link).
        """
        if CONF_MASTER_ADDRESS in user_input:
            selected = user_input[CONF_MASTER_ADDRESS]
            master = None if selected in (None, "", UNLINKED_MASTER) else selected
            self._update_master_link(master)
            selected_channel_count = int(user_input[CONF_STIRRER_CHANNEL_COUNT])
            set_stirrer_pre_run_entities_enabled(
                self.hass, data.device.address, selected_channel_count, enabled=master is not None
            )
            if master is not None:
                await self._async_mirror_new_master(
                    master,
                    data,
                    channel_count=selected_channel_count,
                )
        return self.async_create_entry(
            title="", data={CONF_STIRRER_CHANNEL_COUNT: int(user_input[CONF_STIRRER_CHANNEL_COUNT])}
        )

    def _async_stirrer_form(self) -> ConfigFlowResult:
        """Render the stirrer options form (channel count + master pump)."""
        entry = self.config_entry
        current_count = entry.options.get(
            CONF_STIRRER_CHANNEL_COUNT, entry.data.get(CONF_STIRRER_CHANNEL_COUNT, STIRRER_CHANNEL_MAX)
        )
        current_master = str(entry.data.get(CONF_MASTER_ADDRESS, "")) or UNLINKED_MASTER
        data_schema = vol.Schema(
            {
                vol.Required(CONF_STIRRER_CHANNEL_COUNT, default=str(current_count)): vol.All(
                    vol.Coerce(str), vol.In({str(option): str(option) for option in STIRRER_CHANNEL_COUNT_OPTIONS})
                ),
                vol.Optional(CONF_MASTER_ADDRESS, default=current_master): vol.In(
                    self._master_select_options(current_master)
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=data_schema)

    def _master_select_options(self, current_master: str) -> dict[str, str]:
        """Return the selectable master pumps as ``{address: label}`` plus unlink."""
        options = {UNLINKED_MASTER: "None (unlinked)"}
        for entry_id, candidate in self.hass.data.get(DOMAIN, {}).items():
            if entry_id == self.config_entry.entry_id or not isinstance(candidate, DosingChihirosData):
                continue
            options[candidate.device.address] = f"{candidate.title} ({candidate.device.address})"
        if current_master != UNLINKED_MASTER and current_master not in options:
            options[current_master] = f"{current_master} (not loaded)"
        return options

    def _update_master_link(self, master: str | None) -> None:
        """Write or clear the persisted master address on the stirrer entry."""
        entry = self.config_entry
        if master is None:
            new_data = {key: value for key, value in entry.data.items() if key != CONF_MASTER_ADDRESS}
        else:
            new_data = {**entry.data, CONF_MASTER_ADDRESS: master}
        self.hass.config_entries.async_update_entry(entry, data=new_data)

    async def _async_mirror_new_master(
        self,
        master_address: str,
        stirrer_data: StirrerChihirosData,
        *,
        channel_count: int | None = None,
    ) -> None:
        """Replay the pump's recorded programming onto the stirrer (best effort)."""
        master_data = self._find_master(master_address)
        if master_data is None:
            return
        try:
            await async_mirror_pump_to_stirrer(master_data, stirrer_data, channel_count=channel_count)
        except Exception as ex:  # noqa: BLE001 — linking must succeed even if replay does not
            _LOGGER.warning(
                "Linked %s to %s, but replaying the pump programming failed: %s",
                stirrer_data.device.name,
                master_data.device.name,
                ex,
            )

    def _find_master(self, master_address: str) -> DosingChihirosData | None:
        """Return the loaded device data for a master address, if any."""
        target = master_address.upper()
        for candidate in self.hass.data.get(DOMAIN, {}).values():
            if isinstance(candidate, DosingChihirosData) and candidate.device.address.upper() == target:
                return candidate
        return None

"""Home Assistant dosing service."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .models import DosingChihirosData
from .service_utils import DEVICE_SELECTOR_SCHEMA, resolve_service_device
from .vendor.chihiros_led_control.protocol.dosing import MANUAL_DOSE_VOLUME_MAX_ML, MANUAL_DOSE_VOLUME_MIN_ML

_LOGGER = logging.getLogger(__name__)

SERVICE_DOSE_ML = "dose_ml"
ATTR_ML = "ml"
ATTR_PUMP = "pump"

DOSE_ML_SCHEMA = vol.Schema(
    {
        **DEVICE_SELECTOR_SCHEMA,
        vol.Required(ATTR_PUMP): vol.All(vol.Coerce(int), vol.Range(min=1, max=8)),
        vol.Required(ATTR_ML): vol.All(
            vol.Coerce(float), vol.Range(min=MANUAL_DOSE_VOLUME_MIN_ML, max=MANUAL_DOSE_VOLUME_MAX_ML)
        ),
    }
)


def async_register_dosing_service(hass: HomeAssistant) -> None:
    """Register the manual dosing service for configured pumps."""
    if hass.services.has_service(DOMAIN, SERVICE_DOSE_ML):
        return

    async def async_dose_ml(call: ServiceCall) -> None:
        data = resolve_service_device(hass, call.data)
        if not isinstance(data, DosingChihirosData):
            raise HomeAssistantError(f"{data.device.name} is not a dosing pump")
        pump_idx = int(call.data[ATTR_PUMP]) - 1
        if pump_idx >= data.dosing_totals.pump_count:
            raise HomeAssistantError(f"{data.device.name} has {data.dosing_totals.pump_count} pumps")
        await async_trigger_dose_ml(hass, data, pump_idx, float(call.data[ATTR_ML]))

    hass.services.async_register(DOMAIN, SERVICE_DOSE_ML, async_dose_ml, schema=DOSE_ML_SCHEMA)


def async_remove_dosing_service(hass: HomeAssistant) -> None:
    """Remove the dosing service if registered."""
    if hass.services.has_service(DOMAIN, SERVICE_DOSE_ML):
        hass.services.async_remove(DOMAIN, SERVICE_DOSE_ML)


async def async_trigger_dose_ml(
    hass: HomeAssistant,
    chihiros_data: DosingChihirosData,
    pump_idx: int,
    volume_ml: float,
) -> None:
    """Trigger a manual dose, then update local totals and broadcast to slaves."""
    manual_dose_frame = await chihiros_data.device.dose_ml(pump_idx, volume_ml)
    try:
        await chihiros_data.dosing_totals.async_add_dose(pump_idx, volume_ml)
    except Exception as ex:  # noqa: BLE001 — the physical dose already succeeded
        _LOGGER.error(
            "Dose of %.1f mL on %s succeeded, but recording its local totals failed: %s",
            volume_ml,
            chihiros_data.device.name,
            ex,
        )

    from .master_slave_services import async_broadcast_frame_to_linked_stirrers

    try:
        await async_broadcast_frame_to_linked_stirrers(
            hass, chihiros_data.device.address, manual_dose_frame, "manual dose"
        )
    except Exception as ex:  # noqa: BLE001 — the physical dose already succeeded
        _LOGGER.warning(
            "Dose of %.1f mL on %s succeeded, but broadcasting to linked stirrers failed: %s",
            volume_ml,
            chihiros_data.device.name,
            ex,
        )

"""Home Assistant dosing service."""

from __future__ import annotations

from typing import cast

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .models import ChihirosData
from .runtime import DosingChihirosClient
from .service_utils import DEVICE_SELECTOR_SCHEMA, resolve_service_device

SERVICE_DOSE_ML = "dose_ml"
ATTR_ML = "ml"
ATTR_PUMP = "pump"

DOSE_ML_SCHEMA = vol.Schema(
    {
        **DEVICE_SELECTOR_SCHEMA,
        vol.Required(ATTR_PUMP): vol.All(vol.Coerce(int), vol.Range(min=1, max=4)),
        vol.Required(ATTR_ML): vol.All(vol.Coerce(float), vol.Range(min=0.2, max=999.9)),
    }
)


def async_register_dosing_service(hass: HomeAssistant) -> None:
    """Register the manual dosing service for configured pumps."""
    if hass.services.has_service(DOMAIN, SERVICE_DOSE_ML):
        return

    async def async_dose_ml(call: ServiceCall) -> None:
        data = resolve_service_device(hass, call.data)
        if not data.dosing_totals:
            raise HomeAssistantError(f"{data.device.name} is not a dosing pump")
        pump_idx = int(call.data[ATTR_PUMP]) - 1
        if pump_idx >= data.dosing_totals.pump_count:
            raise HomeAssistantError(f"{data.device.name} has {data.dosing_totals.pump_count} pumps")
        await async_trigger_dose_ml(data, pump_idx, float(call.data[ATTR_ML]))

    hass.services.async_register(DOMAIN, SERVICE_DOSE_ML, async_dose_ml, schema=DOSE_ML_SCHEMA)


def async_remove_dosing_service(hass: HomeAssistant) -> None:
    """Remove the dosing service if registered."""
    if hass.services.has_service(DOMAIN, SERVICE_DOSE_ML):
        hass.services.async_remove(DOMAIN, SERVICE_DOSE_ML)


async def async_trigger_dose_ml(chihiros_data: ChihirosData, pump_idx: int, volume_ml: float) -> None:
    """Trigger a manual dose and update local totals."""
    if not chihiros_data.dosing_totals:
        raise HomeAssistantError(f"{chihiros_data.device.name} is not a dosing pump")
    dosing_device = cast(DosingChihirosClient, chihiros_data.device)
    await dosing_device.dose_ml(pump_idx, volume_ml)
    await chihiros_data.dosing_totals.async_add_dose(pump_idx, volume_ml)

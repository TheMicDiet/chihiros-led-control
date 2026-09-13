"""Repairs platform: expose the dosing pump calibration wizard as a repair.

Home Assistant never opens a config-flow dialog that was started outside the
frontend, so a "Calibrate pump" button press alone would look like a no-op.
Instead the button raises a fixable repairs issue (see
``calibration_flow.async_start_calibration_issue``); the frontend surfaces it
as a clickable notification and opening it runs the wizard dialog directly —
the same core pattern used e.g. by unifiprotect.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant

from .calibration_flow import DosingCalibrationFlowMixin


class ChihirosCalibrationRepairFlow(RepairsFlow, DosingCalibrationFlowMixin):
    """Repairs flow that runs the dosing pump calibration wizard."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> Any:
        """Enter the wizard (repairs flows always start at ``init``).

        The flow manager passes the issue's ``data`` dict here as
        ``user_input``; the wizard ignores it (the entry id was already read
        in ``async_create_fix_flow``) and always shows the pump picker.
        """
        return await self.async_step_calibrate_pump(None)


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the calibration wizard flow for one repair issue."""
    del issue_id  # the issue id is only bookkeeping; the wizard reads its data
    flow = ChihirosCalibrationRepairFlow()
    if data and data.get("entry_id"):
        flow._calibration_entry_id = str(data["entry_id"])
    return flow

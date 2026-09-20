"""Dosing pump calibration wizard exposed as a Home Assistant repairs flow.

Replays the vendor app's per-channel calibration wizard
(``DosingCalibrateWidget``, verified against the 2.8.59 decompile and the
smi-tagged constants in its call sites):

1. timed calibration run — fixed 5 s (``calibration(time: 5)``, the app's
   "will take up to 5 seconds"),
2. enter the measured volume (``calibration(volume: X)``),
3. a fixed 4 mL test dose (``tempDosing(4000)`` — the app's "Dose 4ml"
   button; smi raw 8000 decodes to 4000 µL),
4. "Was this accurate? (between 3.95-4.05ml)" — Yes finishes, No restarts
   the wizard.

While a stirrer slave is linked every frame is broadcast to it verbatim,
matching the app's device-null ``DataSendEvent`` routing (verified in
``DosingPumpInfo.calibration`` @ 0xa6922c). The app's optional channel-rename
step is local-only (no wire frame) and has no HA equivalent.
"""

from __future__ import annotations

import logging
from typing import Any, cast

import voluptuous as vol
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .models import ChihirosData
from .runtime import DosingChihirosClient

_LOGGER = logging.getLogger(__name__)

SOURCE_CALIBRATE_PUMP = "calibrate_pump"  # kept for context tagging; the wizard now runs as a repairs flow
STEP_CALIBRATE_PUMP = "calibrate_pump"
STEP_CALIBRATE_RUN = "calibrate_run"
STEP_CALIBRATE_MEASURE = "calibrate_measure"
STEP_CALIBRATE_TEST = "calibrate_test"
STEP_CALIBRATE_ACCURACY = "calibrate_accuracy"
STEP_CALIBRATE_DOSE_UNCLEAR = "calibrate_dose_unclear"

ATTR_PUMP = "pump"
ATTR_ACCURATE = "accurate"
ATTR_VOLUME_ML = "volume_ml"
ATTR_DOSE_CHOICE = "dose_choice"
DOSE_RETRY = "retry"
DOSE_CONTINUE = "continue"

ACCURACY_YES = "yes"
ACCURACY_NO = "no"

CALIBRATION_RUN_SECONDS = 5
TEST_DOSE_ML = 4.0
MIN_MEASURED_VOLUME = 0.05
MAX_MEASURED_VOLUME = 255.99


def _find_entry_id(hass: HomeAssistant, chihiros_data: ChihirosData) -> str | None:
    """Return the config entry id a device data object is stored under."""
    for entry_id, candidate in hass.data.get(DOMAIN, {}).items():
        if candidate is chihiros_data:
            return entry_id
    return None


def calibration_issue_id(entry_id: str) -> str:
    """Return the repairs issue id for one entry's calibration wizard."""
    return f"{DOMAIN}_calibrate_{entry_id}"


def async_start_calibration_issue(hass: HomeAssistant, chihiros_data: ChihirosData) -> None:
    """Raise the repairs issue that opens the calibration wizard for one pump.

    Home Assistant never opens a config-flow dialog that was started outside
    the frontend, so a button press alone would look like a no-op. Instead the
    wizard is exposed as a *fixable repairs issue*: pressing the button raises
    the issue, the frontend surfaces it as a clickable notification, and
    opening it runs the wizard dialog directly (the core pattern used e.g. by
    unifiprotect). Re-raising an existing issue is a no-op update, so repeated
    button presses are harmless.

    Raises ``HomeAssistantError`` when the target is not a dosing pump or is
    not loaded.
    """
    if not chihiros_data.dosing_totals:
        raise HomeAssistantError(f"{chihiros_data.device.name} is not a dosing pump")
    entry_id = _find_entry_id(hass, chihiros_data)
    if entry_id is None:
        raise HomeAssistantError(f"{chihiros_data.device.name} is not loaded")
    ir.async_create_issue(
        hass,
        DOMAIN,
        calibration_issue_id(entry_id),
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="calibrate_pump",
        translation_placeholders={"name": chihiros_data.device.name},
        data={"entry_id": entry_id},
    )


def _pump_schema(pump_count: int) -> vol.Schema:
    """Return the pump-picker schema."""
    options = {str(number): f"Pump {number}" for number in range(1, pump_count + 1)}
    return vol.Schema({vol.Required(ATTR_PUMP): vol.In(options)})


def _measure_schema() -> vol.Schema:
    """Return the measured-volume schema (the app asks for the nearest 0.05 mL)."""
    return vol.Schema(
        {
            vol.Required(ATTR_VOLUME_ML): vol.All(
                vol.Coerce(float), vol.Range(min=MIN_MEASURED_VOLUME, max=MAX_MEASURED_VOLUME)
            )
        }
    )


def _accuracy_schema() -> vol.Schema:
    """Return the was-this-accurate schema (app: Yes!Continue / No!Re-calibrate)."""
    return vol.Schema(
        {
            vol.Required(ATTR_ACCURATE): vol.In(
                {ACCURACY_YES: "Yes — the 4 ml test dose was accurate", ACCURACY_NO: "No — re-calibrate"}
            )
        }
    )


def _dose_unclear_schema() -> vol.Schema:
    """Return the retry-or-continue schema for an ambiguous dose frame."""
    return vol.Schema(
        {
            vol.Required(ATTR_DOSE_CHOICE): vol.In(
                {DOSE_RETRY: "Retry the dose", DOSE_CONTINUE: "Continue without dosing"}
            )
        }
    )


def _dosing_device(chihiros_data: ChihirosData) -> DosingChihirosClient:
    """Return the runtime client cast to the dosing-pump surface."""
    return cast("DosingChihirosClient", chihiros_data.device)


async def _async_broadcast_calibration_frame(
    hass: HomeAssistant, chihiros_data: ChihirosData, frame: bytes, action: str
) -> None:
    """Broadcast one calibration frame to linked stirrers, best effort only.

    The pump write already happened (and the timed run cannot be safely
    replayed), so a failed mirror must not fail the wizard step and invite a
    retry that would dose again.
    """
    from .master_slave_services import async_broadcast_frame_to_linked_stirrers

    try:
        await async_broadcast_frame_to_linked_stirrers(hass, chihiros_data.device.address, frame, action)
    except HomeAssistantError as ex:
        _LOGGER.warning(
            "Calibration %s on %s succeeded, but broadcasting to linked stirrers failed: %s",
            action,
            chihiros_data.device.name,
            ex,
        )


async def _async_send_calibration_run(hass: HomeAssistant, chihiros_data: ChihirosData, pump_idx: int) -> None:
    """Start the fixed 5 s timed calibration run (app's ``calibration(time: 5)``)."""
    frame = await _dosing_device(chihiros_data).calibrate_channel(pump_idx, seconds=CALIBRATION_RUN_SECONDS)
    await _async_broadcast_calibration_frame(hass, chihiros_data, frame, "calibration test dose")


async def _async_submit_measured_volume(
    hass: HomeAssistant, chihiros_data: ChihirosData, pump_idx: int, volume_ml: float
) -> None:
    """Record the measured volume on the device and in the local tracker (app step 2)."""
    frame = await _dosing_device(chihiros_data).calibrate_channel(pump_idx, volume_ml=volume_ml)
    await _async_broadcast_calibration_frame(hass, chihiros_data, frame, "calibration")
    if chihiros_data.dosing_calibration:
        await chihiros_data.dosing_calibration.async_record(
            pump_idx, seconds=CALIBRATION_RUN_SECONDS, volume_ml=volume_ml
        )


async def _async_run_test_dose(hass: HomeAssistant, chihiros_data: ChihirosData, pump_idx: int) -> None:
    """Run the fixed 4 mL test dose (app's ``tempDosing(4000)`` "Dose 4ml" button).

    Uses the manual-dose path so the volume is added to the locally tracked
    daily totals (the app's ``addExtraDosing``) and broadcast to linked
    stirrers, exactly like the app's manual dose.
    """
    from . import async_trigger_dose_ml

    await async_trigger_dose_ml(hass, chihiros_data, pump_idx, TEST_DOSE_ML)


class DosingCalibrationFlowMixin:
    """Config flow mixin implementing the dosing pump calibration wizard.

    Mixed into :class:`ChihirosConfigFlow`; relies on the config-flow base for
    ``hass``, ``context``, and the ``async_show_form``/``async_abort`` helpers.
    """

    _calibration_entry_id: str
    _calibration_data: ChihirosData
    _calibration_pump: int
    # Step to jump to when the user chooses "continue" after an ambiguous
    # dose frame (measure after a failed run, accuracy after a failed test dose).
    _calibration_after_unclear: str

    def _async_dismiss_issue(self) -> None:
        """Delete the repairs issue backing this wizard (no-op outside repairs).

        The repairs flow manager only auto-deletes the issue on
        ``create_entry``; the wizard always finishes with a descriptive abort,
        so the issue is removed explicitly here.
        """
        issue_id = getattr(self, "issue_id", None)
        if issue_id is not None:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    async def async_step_calibrate_pump(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick the pump channel to calibrate."""
        entry_id = self.context.get("entry_id") or getattr(self, "_calibration_entry_id", None)
        data = self.hass.data.get(DOMAIN, {}).get(entry_id) if entry_id else None
        if data is None or data.dosing_totals is None:
            self._async_dismiss_issue()
            return self.async_abort(reason="not_dosing_pump")
        self._calibration_entry_id = entry_id
        self._calibration_data = data
        if user_input is not None:
            self._calibration_pump = int(user_input[ATTR_PUMP]) - 1
            return await self.async_step_calibrate_run()
        return self.async_show_form(
            step_id=STEP_CALIBRATE_PUMP, data_schema=_pump_schema(data.dosing_totals.pump_count)
        )

    async def async_step_calibrate_run(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Run the fixed 5 s timed calibration dose (app wizard step 1)."""
        if user_input is not None:
            try:
                await _async_send_calibration_run(self.hass, self._calibration_data, self._calibration_pump)
            except Exception as ex:  # noqa: BLE001 — wizard steps must not crash on failure
                _LOGGER.warning(
                    "Calibration run for pump %d on %s failed: %s",
                    self._calibration_pump + 1,
                    self._calibration_data.device.name,
                    ex,
                )
                # The pump write failed ambiguously: the frame may already
                # have been dispensed. Never silently re-offer the dose —
                # let the user decide between retrying (a fresh dose) and
                # continuing to the measured-volume step.
                self._calibration_after_unclear = STEP_CALIBRATE_MEASURE
                return await self.async_step_calibrate_dose_unclear(
                    description_placeholders={"dose": "timed calibration run"}
                )
            else:
                return await self.async_step_calibrate_measure()
        return self.async_show_form(step_id=STEP_CALIBRATE_RUN, data_schema=vol.Schema({}))

    async def async_step_calibrate_measure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Enter the measured volume (app wizard step 2)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            volume_ml = float(user_input[ATTR_VOLUME_ML])
            try:
                await _async_submit_measured_volume(
                    self.hass, self._calibration_data, self._calibration_pump, volume_ml
                )
            except Exception as ex:  # noqa: BLE001 — wizard steps must re-show the form on failure
                _LOGGER.warning(
                    "Recording the measured volume for pump %d on %s failed: %s",
                    self._calibration_pump + 1,
                    self._calibration_data.device.name,
                    ex,
                )
                errors["base"] = "cannot_connect"
            else:
                return await self.async_step_calibrate_test()
        return self.async_show_form(step_id=STEP_CALIBRATE_MEASURE, data_schema=_measure_schema(), errors=errors)

    async def async_step_calibrate_test(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Dose a known 4 mL volume (app wizard step 3, "Dose 4ml" button)."""
        if user_input is not None:
            try:
                await _async_run_test_dose(self.hass, self._calibration_data, self._calibration_pump)
            except Exception as ex:  # noqa: BLE001 — wizard steps must not crash on failure
                _LOGGER.warning(
                    "Test dose for pump %d on %s failed: %s",
                    self._calibration_pump + 1,
                    self._calibration_data.device.name,
                    ex,
                )
                # Same ambiguity as the timed run: the 4 ml frame may have
                # been dispensed even though the write failed.
                self._calibration_after_unclear = STEP_CALIBRATE_ACCURACY
                return await self.async_step_calibrate_dose_unclear(description_placeholders={"dose": "4 ml test dose"})
            else:
                return await self.async_step_calibrate_accuracy()
        return self.async_show_form(step_id=STEP_CALIBRATE_TEST, data_schema=vol.Schema({}))

    async def async_step_calibrate_dose_unclear(
        self, user_input: dict[str, Any] | None = None, description_placeholders: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Ask whether to re-send an ambiguous dose frame or move on.

        A failed pump write for the timed run or the 4 ml test dose is
        ambiguous: the frame may already have been dispensed. Re-offering the
        dose form directly would invite a second dose on resubmit, so the
        wizard asks explicitly (Retry sends a fresh dose frame; Continue
        skips to the next step without dosing again).
        """
        if user_input is None:
            return self.async_show_form(
                step_id=STEP_CALIBRATE_DOSE_UNCLEAR,
                data_schema=_dose_unclear_schema(),
                description_placeholders=description_placeholders or {},
            )
        if user_input[ATTR_DOSE_CHOICE] == DOSE_RETRY:
            if self._calibration_after_unclear == STEP_CALIBRATE_MEASURE:
                return await self.async_step_calibrate_run()
            return await self.async_step_calibrate_test()
        if self._calibration_after_unclear == STEP_CALIBRATE_MEASURE:
            return await self.async_step_calibrate_measure()
        return await self.async_step_calibrate_accuracy()

    async def async_step_calibrate_accuracy(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask whether the 4 mL test dose was accurate (app wizard step 4)."""
        if user_input is None:
            return self.async_show_form(step_id=STEP_CALIBRATE_ACCURACY, data_schema=_accuracy_schema())
        if user_input.get(ATTR_ACCURATE) == ACCURACY_YES:
            self._async_dismiss_issue()
            return self.async_abort(
                reason="calibration_complete",
                description_placeholders={"pump": str(self._calibration_pump + 1)},
            )
        # "No! Re-calibrate" restarts the wizard from the timed run.
        return await self.async_step_calibrate_run()

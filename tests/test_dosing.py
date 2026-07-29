"""Unit tests for dosing daily-total storage and reset behavior."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

pytest.importorskip("homeassistant", reason="Home Assistant test group is not installed")

from homeassistant.util import dt as dt_util

from custom_components.chihiros.dosing import (
    PUMP_COUNT,
    SIGNAL_DOSING_TOTALS_UPDATED,
    DosingDailyTotals,
    _coerce_total,
    normalize_pump_count,
)

pytestmark = [
    pytest.mark.integration,
]


def test_normalize_pump_count_defaults_invalid_values() -> None:
    """Unsupported or non-numeric values fall back to the default pump count."""
    assert normalize_pump_count("2") == 2
    assert normalize_pump_count("4") == 4
    assert normalize_pump_count("3") == PUMP_COUNT
    assert normalize_pump_count(None) == PUMP_COUNT
    assert normalize_pump_count("not a number") == PUMP_COUNT


def test_coerce_total_tolerates_garbage() -> None:
    """_coerce_total rounds valid numbers and rejects non-numeric input to 0.0."""
    assert _coerce_total(2.56) == 2.6
    assert _coerce_total("3.4") == 3.4
    assert _coerce_total(None) == 0.0
    assert _coerce_total("nope") == 0.0


@pytest.mark.asyncio
async def test_async_load_restores_today_totals(hass: Any) -> None:
    """A stored dict for today restores per-pump totals using _coerce_total."""
    totals = DosingDailyTotals(hass, "FA:CE:C0:FF:00:01", pump_count=2)

    async def _load() -> dict[str, Any]:
        return {
            "date": dt_util.now().date().isoformat(),
            "totals_ml": [1.5, "2.7", "bad"],
        }

    totals._store.async_load = _load  # type: ignore[assignment]

    await totals.async_load()

    assert totals.total_ml(0) == 1.5
    assert totals.total_ml(1) == 2.7
    # The stored list has a third entry; pump_count=2 so only two are read,
    # and out-of-range indices return the coerced stored value (or 0.0).
    with pytest.raises(ValueError, match="Pump index must be between 0 and 1"):
        totals.total_ml(2)


@pytest.mark.asyncio
async def test_async_load_stale_date_resets_totals(hass: Any) -> None:
    """A stored dict with a stale date triggers an async_reset of the totals."""
    saved: list[dict[str, Any]] = []
    totals = DosingDailyTotals(hass, "FA:CE:C0:FF:00:02", pump_count=2)

    async def _load() -> dict[str, Any]:
        return {"date": "1999-01-01", "totals_ml": [9.9, 8.8]}

    totals._store.async_load = _load  # type: ignore[assignment]

    async def _save(data: dict[str, Any]) -> None:
        saved.append(data)

    totals._store.async_save = _save  # type: ignore[assignment]

    await totals.async_load()

    assert totals.total_ml(0) == 0.0
    assert totals.total_ml(1) == 0.0
    # async_reset persists the zeroed totals for today.
    assert saved and saved[0]["date"] == dt_util.now().date().isoformat()
    assert saved[0]["totals_ml"] == [0.0, 0.0]


@pytest.mark.asyncio
async def test_async_add_dose_accumulates_and_persists(hass: Any) -> None:
    """async_add_dose accumulates onto today's running total and saves it."""
    saved: list[dict[str, Any]] = []
    totals = DosingDailyTotals(hass, "FA:CE:C0:FF:00:03", pump_count=2)

    async def _save(data: dict[str, Any]) -> None:
        saved.append(data)

    totals._store.async_save = _save  # type: ignore[assignment]
    await totals.async_load()

    await totals.async_add_dose(0, 2.5)
    await totals.async_add_dose(0, 1.1)

    assert totals.total_ml(0) == 3.6
    assert totals.total_ml(1) == 0.0
    assert any(entry["totals_ml"][0] == 3.6 for entry in saved)
    with pytest.raises(ValueError, match="Pump index must be between 0 and 1"):
        await totals.async_add_dose(5, 1.0)


@pytest.mark.asyncio
async def test_address_signal_and_async_close_cancel_schedule(hass: Any) -> None:
    """The dispatcher signal is address-scoped and close cancels the reset timer."""
    address = "FA:CE:C0:FF:00:04"
    totals = DosingDailyTotals(hass, address, pump_count=2)
    await totals.async_load()

    assert totals.address_signal == f"{SIGNAL_DOSING_TOTALS_UPDATED}_{address.lower()}"
    assert totals._unsub_midnight_reset is not None

    totals.async_close()
    assert totals._unsub_midnight_reset is None
    # A second close is a no-op (no error).
    totals.async_close()


@pytest.mark.asyncio
async def test_midnight_reset_resets_totals_and_reschedules(hass: Any) -> None:
    """_async_midnight_reset zeros totals and schedules the next reset."""
    totals = DosingDailyTotals(hass, "FA:CE:C0:FF:00:05", pump_count=2)
    await totals.async_load()
    await totals.async_add_dose(0, 5.0)
    assert totals.total_ml(0) == 5.0

    first_reset_handle = totals._unsub_midnight_reset

    await totals._async_midnight_reset(dt_util.utcnow() + timedelta(days=1))

    assert totals.total_ml(0) == 0.0
    # The reset re-schedules a fresh midnight callback.
    assert totals._unsub_midnight_reset is not None
    assert totals._unsub_midnight_reset is not first_reset_handle
    totals.async_close()

"""Component tests for dosing storage, dispatcher and reset behavior."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("homeassistant", reason="Home Assistant test group is not installed")

from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util

from custom_components.chihiros import dosing as dosing_module
from custom_components.chihiros.dosing import (
    SIGNAL_DOSING_CALIBRATION_UPDATED,
    SIGNAL_DOSING_TOTALS_UPDATED,
    DosingCalibrationTracker,
    DosingDailyTotals,
)

pytestmark = [
    pytest.mark.component,
    pytest.mark.homeassistant,
]


@pytest.mark.asyncio
async def test_async_load_restores_today_totals(hass: Any) -> None:
    """A stored dict for today restores per-pump totals using _coerce_total."""
    totals = DosingDailyTotals(hass, "FA:CE:C0:FF:00:01", pump_count=2)

    async def _load() -> dict[str, Any]:
        return {
            "date": dt_util.now().date().isoformat(),
            "totals_ml": [1.5, "2.7", "bad"],
            "lifetime_ml": [10.0, "20.5"],
            "lifetime_cycles": [3, "4"],
        }

    totals._store.async_load = _load  # type: ignore[assignment]

    await totals.async_load()

    assert totals.total_ml(0) == 1.5
    assert totals.total_ml(1) == 2.7
    assert totals.lifetime_ml(0) == 10.0
    assert totals.lifetime_ml(1) == round(20.5, 1)
    assert totals.lifetime_cycles(0) == 3
    assert totals.lifetime_cycles(1) == 4
    # The stored list has a third entry; pump_count=2 so only two are read,
    # and out-of-range indices return the coerced stored value (or 0.0).
    with pytest.raises(ValueError, match="Pump index must be between 0 and 1"):
        totals.total_ml(2)
    with pytest.raises(ValueError, match="Pump index must be between 0 and 1"):
        totals.lifetime_ml(2)
    with pytest.raises(ValueError, match="Pump index must be between 0 and 1"):
        totals.lifetime_cycles(2)


@pytest.mark.asyncio
async def test_async_load_stale_date_resets_totals(hass: Any) -> None:
    """A stored dict with a stale date triggers an async_reset of the totals."""
    saved: list[dict[str, Any]] = []
    totals = DosingDailyTotals(hass, "FA:CE:C0:FF:00:02", pump_count=2)

    async def _load() -> dict[str, Any]:
        return {
            "date": "1999-01-01",
            "totals_ml": [9.9, 8.8],
            "lifetime_ml": [12.3, 4.5],
            "lifetime_cycles": [7, 1],
        }

    totals._store.async_load = _load  # type: ignore[assignment]

    async def _save(data: dict[str, Any]) -> None:
        saved.append(data)

    totals._store.async_save = _save  # type: ignore[assignment]

    await totals.async_load()

    assert totals.total_ml(0) == 0.0
    assert totals.total_ml(1) == 0.0
    # Lifetime counters are preserved across the daily reset.
    assert totals.lifetime_ml(0) == round(12.3, 1)
    assert totals.lifetime_ml(1) == round(4.5, 1)
    assert totals.lifetime_cycles(0) == 7
    assert totals.lifetime_cycles(1) == 1
    # async_reset persists the zeroed totals for today while keeping lifetime data.
    assert saved and saved[0]["date"] == dt_util.now().date().isoformat()
    assert saved[0]["totals_ml"] == [0.0, 0.0]
    assert saved[0]["lifetime_ml"] == [round(12.3, 1), round(4.5, 1)]
    assert saved[0]["lifetime_cycles"] == [7, 1]


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
    assert totals.lifetime_ml(0) == 3.6
    assert totals.lifetime_ml(1) == 0.0
    assert totals.lifetime_cycles(0) == 2
    assert totals.lifetime_cycles(1) == 0
    assert any(entry["totals_ml"][0] == 3.6 for entry in saved)
    assert saved[-1]["lifetime_ml"] == [3.6, 0.0]
    assert saved[-1]["lifetime_cycles"] == [2, 0]
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
async def test_calibration_tracker_records_and_notifies(hass: Any) -> None:
    """async_record stores the result, persists it, and fires the address signal."""
    saved: list[dict[str, Any]] = []
    tracker = DosingCalibrationTracker(hass, "FA:CE:C0:FF:00:07")
    await tracker.async_load()

    async def _save(data: dict[str, Any]) -> None:
        saved.append(data)

    tracker._store.async_save = _save  # type: ignore[assignment]

    received: list[str] = []
    remove = async_dispatcher_connect(hass, tracker.address_signal, lambda: received.append("update"))
    await tracker.async_record(0, seconds=30, volume_ml=7.5)
    await hass.async_block_till_done()
    remove()

    record = tracker.record(0)
    assert record is not None
    assert record["seconds"] == 30
    assert record["volume_ml"] == 7.5
    assert tracker.calibrated_at(0) is not None
    assert received == ["update"]
    assert saved and saved[-1]["channels"]["0"]["volume_ml"] == 7.5


@pytest.mark.asyncio
async def test_calibration_tracker_loads_stored_records(hass: Any) -> None:
    """Stored records are restored; invalid timestamps degrade to None."""
    tracker = DosingCalibrationTracker(hass, "FA:CE:C0:FF:00:08")

    async def _load() -> dict[str, Any]:
        return {
            "channels": {
                "1": {"calibrated": dt_util.now().isoformat(), "seconds": 10, "volume_ml": 2.5},
                "2": {"calibrated": "not-a-date", "seconds": 5, "volume_ml": 1.0},
            }
        }

    tracker._store.async_load = _load  # type: ignore[assignment]
    await tracker.async_load()

    record = tracker.record(1)
    assert record is not None
    assert record["volume_ml"] == 2.5
    assert tracker.calibrated_at(1) is not None
    assert tracker.calibrated_at(2) is None
    assert tracker.address_signal == f"{SIGNAL_DOSING_CALIBRATION_UPDATED}_fa:ce:c0:ff:00:08"


@pytest.fixture
def midnight_clock(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Provide a local clock and observable one-shot midnight callbacks."""
    clock = SimpleNamespace(now=dt_util.now(), timers=[])

    def track(_hass: Any, callback: Callable[..., Any], when: datetime) -> Callable[[], None]:
        """Capture the callback instead of scheduling wall-clock delivery."""
        timer = SimpleNamespace(callback=callback, when=when, active=True)
        clock.timers.append(timer)
        return lambda: setattr(timer, "active", False)

    monkeypatch.setattr(
        dosing_module,
        "dt_util",
        SimpleNamespace(now=lambda: clock.now, start_of_local_day=dt_util.start_of_local_day),
    )
    monkeypatch.setattr(dosing_module, "async_track_point_in_time", track)
    return clock


@pytest.mark.asyncio
async def test_delayed_midnight_callback_keeps_new_day_dose(hass: Any, midnight_clock: SimpleNamespace) -> None:
    """A late callback cannot erase a dose counted after lazy date rollover."""
    totals = DosingDailyTotals(hass, "FA:CE:C0:FF:00:09")
    await totals.async_load()
    await totals.async_add_dose(0, 5.0)
    pending = midnight_clock.timers[-1]
    midnight_clock.now = pending.when + timedelta(seconds=1)
    await totals.async_add_dose(0, 2.5)
    try:
        await pending.callback(midnight_clock.now)
        assert totals.total_ml(0) == 2.5
        assert totals.lifetime_ml(0) == 7.5
        assert totals.lifetime_cycles(0) == 2
    finally:
        totals.async_close()


@pytest.mark.asyncio
async def test_midnight_storage_failure_keeps_next_reset_scheduled(
    hass: Any, midnight_clock: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed midnight save still schedules and executes the next rollover."""
    totals = DosingDailyTotals(hass, "FA:CE:C0:FF:00:10")
    await totals.async_load()
    await totals.async_add_dose(0, 5.0)
    assert totals.total_ml(0) == 5.0
    assert totals.lifetime_ml(0) == 5.0
    assert totals.lifetime_cycles(0) == 1
    pending = midnight_clock.timers[-1]
    midnight_clock.now = pending.when + timedelta(seconds=1)
    original_save = totals._store.async_save

    async def fail_save(_data: dict[str, Any]) -> None:
        """Fail persistence after the in-memory rollover."""
        raise OSError("storage unavailable")

    monkeypatch.setattr(totals._store, "async_save", fail_save)
    try:
        with pytest.raises(OSError, match="storage unavailable"):
            await pending.callback(midnight_clock.now)
        future = midnight_clock.timers[-1]
        assert future.active and future.when > midnight_clock.now
        monkeypatch.setattr(totals._store, "async_save", original_save)
        await totals.async_add_dose(0, 3.0)
        midnight_clock.now = future.when + timedelta(seconds=1)
        await future.callback(midnight_clock.now)
        next_reset = midnight_clock.timers[-1]
        assert next_reset is not future
        assert next_reset.active and next_reset.when > midnight_clock.now
        assert totals.total_ml(0) == 0.0
        assert totals.lifetime_ml(0) == 8.0
        assert totals.lifetime_cycles(0) == 2
    finally:
        totals.async_close()


@pytest.mark.asyncio
async def test_close_during_midnight_save_does_not_reschedule(
    hass: Any, midnight_clock: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closing during a cancelled save cannot resurrect the midnight timer."""
    totals = DosingDailyTotals(hass, "FA:CE:C0:FF:00:11")
    await totals.async_load()
    entered = asyncio.Event()

    async def blocked_save(_data: dict[str, Any]) -> None:
        """Suspend persistence until cancellation."""
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(totals._store, "async_save", blocked_save)
    pending = midnight_clock.timers[-1]
    task = asyncio.create_task(pending.callback(midnight_clock.now))
    await entered.wait()
    totals.async_close()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not any(timer.active for timer in midnight_clock.timers)

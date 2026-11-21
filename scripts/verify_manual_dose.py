#!/usr/bin/env python3
"""Explore bounded BLE write outcomes for pump runs and raw slave broadcasts."""

from __future__ import annotations

import asyncio
import itertools
import logging

from bleak_retry_connector import BleakError

from chihiros_led_control.devices.dosing import ChihirosDosingPump
from chihiros_led_control.devices.stirrer import ChihirosMagStirrer
from chihiros_led_control.protocol import dosing as commands
from chihiros_led_control.testing import ScriptedBLEDevice, ScriptedTransport

# Two complete attempts of each operation, including its whole write batch.
MANUAL_WRITE_BOUND = 6
CALIBRATION_WRITE_BOUND = 2
RAW_BROADCAST_WRITE_BOUND = 2
OUTCOMES = ("ok", "before", "after")


class BoundExceeded(Exception):
    """The execution requested a write beyond the explored bound."""


class WriteOutcomes:
    """Count accepted run frames, without assuming a physical response or deduplication."""

    def __init__(self, outcomes: tuple[str, ...], mode: int) -> None:
        """Initialize the next write outcomes and accepted-operation count."""
        self.outcomes = iter(outcomes)
        self.mode = mode
        self.trace: list[tuple[int, str]] = []
        self.accepted_operations = 0

    def respond(self, frame: bytes) -> list[bytes]:
        """Apply one simulated BLE write and possibly report an ambiguous error."""
        try:
            outcome = next(self.outcomes)
        except StopIteration as exc:
            raise BoundExceeded from exc
        mode = frame[5]
        self.trace.append((mode, outcome))
        if outcome != "before" and frame[0] == 0xA5 and mode == self.mode:
            self.accepted_operations += 1
        if outcome != "ok":
            raise BleakError(f"simulated write failure {outcome} acceptance")
        return []


async def run_sequence(outcomes: tuple[str, ...], *, calibration: bool, raw: bool = False) -> WriteOutcomes:
    """Execute one outcome sequence against a real pump or raw slave sender."""
    mode = 22 if calibration else 27
    operation = "Timed calibration" if calibration else "Manual dose"
    if raw:
        operation = f"Raw {operation.lower()} broadcast"
    model = WriteOutcomes(outcomes, mode)
    transport = ScriptedTransport(name="DYMIXR-verification" if raw else "DYDOSE-verification", realtime=False)
    transport.expect(165, mode, respond=model.respond)
    if not (calibration or raw):
        transport.expect(165, 4, respond=model.respond)
    try:
        await send_operation(transport, calibration=calibration, raw=raw)
    except BleakError:
        pass
    except BoundExceeded as exc:
        raise RuntimeError(f"{operation}: write bound exceeded; trace: {model.trace}") from exc
    finally:
        await transport.disconnect()
    check_sequence(model, outcomes, operation, 1 if calibration or raw else 3)
    return model


async def send_operation(transport: ScriptedTransport, *, calibration: bool, raw: bool) -> None:
    """Submit the real run operation or its exact raw frame to a slave."""
    identity = ScriptedBLEDevice(transport.name, transport.address)
    if raw:
        frame = (
            commands.create_dosing_calibrate_command((0, 1), 0, seconds=10)
            if calibration
            else commands.create_manual_dose_command((0, 1), 0, 1.0)
        )
        await ChihirosMagStirrer(identity, transport=transport).send_frame(frame)
        return
    pump = ChihirosDosingPump(identity, transport=transport)
    if calibration:
        await pump.calibrate_channel(0, seconds=10)
    else:
        await pump.dose_ml(0, 1.0)


def check_sequence(client: WriteOutcomes, outcomes: tuple[str, ...], operation: str, success_writes: int) -> None:
    """Reject duplicated operations and a missing all-success operation."""
    if client.accepted_operations > 1:
        raise AssertionError(f"{operation} accepted more than once; trace: {client.trace}")
    if outcomes[:success_writes] == ("ok",) * success_writes and client.accepted_operations != 1:
        raise AssertionError(f"Successful {operation.lower()} was not accepted; trace: {client.trace}")


async def explore_operation(*, calibration: bool, bound: int, mode: int, label: str, raw: bool = False) -> None:
    """Explore one operation and require an ambiguous write to be reached."""
    explored = 0
    ambiguous = 0
    for outcomes in itertools.product(OUTCOMES, repeat=bound):
        client = await run_sequence(outcomes, calibration=calibration, raw=raw)
        explored += 1
        if (mode, "after") in client.trace:
            ambiguous += 1
    if not ambiguous:
        raise AssertionError(f"The exploration never reached an ambiguous {label} write")
    print(f"PASS: {label}: {explored} outcome sequences; {ambiguous} reach an ambiguous write")


async def explore() -> None:
    """Reject duplicated accepted run frames within every bounded outcome sequence."""
    for calibration, raw, bound, mode, label in (
        (False, False, MANUAL_WRITE_BOUND, 27, "manual dose"),
        (True, False, CALIBRATION_WRITE_BOUND, 22, "timed calibration"),
        (False, True, RAW_BROADCAST_WRITE_BOUND, 27, "raw manual-dose broadcast"),
        (True, True, RAW_BROADCAST_WRITE_BOUND, 22, "raw timed-calibration broadcast"),
    ):
        await explore_operation(calibration=calibration, bound=bound, mode=mode, label=label, raw=raw)


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    asyncio.run(explore())

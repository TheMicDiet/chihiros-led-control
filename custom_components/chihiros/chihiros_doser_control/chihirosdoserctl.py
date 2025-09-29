"""Expose the doser Typer app for mounting under chihirosctl.

This file re-exports the Typer `app` defined in
`custom_components.chihiros.chihiros_doser_control/device/doser_device.py`
and *extends the same app instance* with a few extra helper commands.
"""

from __future__ import annotations

from typing import List

import typer
from typing_extensions import Annotated

# Import the existing doser CLI app and (optionally) the device class
from .device.doser_device import app as app
from .device.doser_device import DoserDevice  # used by the extra helpers below


# ────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────

def _parse_params_tokens(tokens: List[str]) -> List[int]:
    """
    Accept decimal or hex tokens:
      - decimal: 10, 255
      - hex: 0x0A, 0Ah
    """
    out: List[int] = []
    for t in tokens:
        s = t.strip().lower()
        if s.startswith("0x"):
            v = int(s, 16)
        elif s.endswith("h") and all(c in "0123456789abcdef" for c in s[:-1]):
            v = int(s[:-1], 16)
        else:
            v = int(s, 10)
        if not (0 <= v <= 255):
            raise typer.BadParameter(f"Parameter byte out of range 0..255: {t}")
        out.append(v)
    return out


def _parse_hex_blob(blob: str) -> bytes:
    s = "".join(blob.strip().split())
    if len(s) % 2 != 0:
        raise typer.BadParameter("Hex length must be even.")
    try:
        return bytes.fromhex(s)
    except ValueError as e:
        raise typer.BadParameter("Invalid hex characters in payload.") from e


# ────────────────────────────────────────────────────────────────
# BYTES ENCODE — pretty-print + decode helpers
# (Extends the same `app` imported from doser_device)
# ────────────────────────────────────────────────────────────────

@app.command(name="bytes-encode")
def bytes_encode(
    params: Annotated[str, typer.Argument(help="Hex string with/without spaces")],
    table: Annotated[bool, typer.Option("--table/--no-table")] = True,
) -> None:
    """
    Pretty-print an A5/5B frame and (if 8 params) decode 4 channels of daily totals
    using the 25.6 bucket + 0.1 mL scheme.
    """
    value_bytes = _parse_hex_blob(params)

    if not table:
        norm = " ".join(f"{b:02x}" for b in value_bytes)
        typer.echo(f"{norm}   (len={len(value_bytes)})")
        return

    # Safe extraction with placeholders
    cmd_id      = value_bytes[0] if len(value_bytes) >= 1 else "????"
    proto_ver   = value_bytes[1] if len(value_bytes) >= 2 else "????"
    length_fld  = value_bytes[2] if len(value_bytes) >= 3 else None
    msg_hi      = value_bytes[3] if len(value_bytes) >= 4 else "????"
    msg_lo      = value_bytes[4] if len(value_bytes) >= 5 else "????"
    mode        = value_bytes[5] if len(value_bytes) >= 6 else "????"

    # Determine param length per protocol family
    total_after_header = max(0, len(value_bytes) - 7)
    if isinstance(length_fld, int):
        if cmd_id in (0x5B, 91):         # LED-style
            param_len = max(0, length_fld - 2)
        else:                             # A5-style (doser)
            param_len = max(0, length_fld - 5)
    else:
        param_len = total_after_header

    if param_len != total_after_header:
        param_len = total_after_header

    params_start = 6
    params_end   = min(len(value_bytes) - 1, params_start + param_len)
    params_list  = [int(b) for b in value_bytes[params_start:params_end]]
    checksum     = value_bytes[-1] if len(value_bytes) >= 1 else "????"

    try:
        # PrettyTable is optional; fall back to plain output if missing
        from prettytable import PrettyTable, SINGLE_BORDER  # type: ignore
        table_obj = PrettyTable()
        table_obj.set_style(SINGLE_BORDER)
        table_obj.title = "Encode Message"
        table_obj.field_names = [
            "Command Print", "Command ID", "Version", "Command Length",
            "Message ID High", "Message ID Low", "Mode", "Parameters", "Checksum",
        ]
        table_obj.add_row([
            str([int(b) for b in value_bytes]),
            str(cmd_id),
            str(proto_ver),
            str(length_fld if length_fld is not None else "????"),
            str(msg_hi),
            str(msg_lo),
            str(mode),
            str(params_list),
            str(checksum),
        ])
        print(table_obj)  # rich.print
    except Exception:
        # Fallback (no prettytable)
        typer.echo("Encode Message")
        typer.echo(f"  Command ID       : {cmd_id}")
        typer.echo(f"  Version          : {proto_ver}")
        typer.echo(f"  Command Length   : {length_fld if length_fld is not None else '????'}")
        typer.echo(f"  Message ID High  : {msg_hi}")
        typer.echo(f"  Message ID Low   : {msg_lo}")
        typer.echo(f"  Mode             : {mode}")
        typer.echo(f"  Parameters       : {params_list}")
        typer.echo(f"  Checksum         : {checksum}")

    # Optional: decode daily totals for 4 channels using the 25.6 scheme
    if len(params_list) == 8:
        def decode_ml_25_6(hi: int, lo: int) -> float:
            # hi*25.6 + lo*0.1
            return round(hi * 25.6 + lo / 10.0, 1)

        mls = [decode_ml_25_6(params_list[i], params_list[i + 1]) for i in range(0, 8, 2)]
        typer.echo(
            f"Decoded daily totals (ml): ch0={mls[0]:.2f}, ch1={mls[1]:.2f}, "
            f"ch2={mls[2]:.2f}, ch3={mls[3]:.2f}"
        )


# ────────────────────────────────────────────────────────────────
# Simple READ helpers (call into the DoserDevice class)
# ────────────────────────────────────────────────────────────────

@app.command(name="read-dosing-auto")
def read_dosing_auto(
    device_address: Annotated[str, typer.Argument(help="BLE MAC, e.g. AA:BB:CC:DD:EE:FF")],
    ch_id: Annotated[int | None, typer.Option(help="Channel 0..3; omit for all")] = None,
    timeout_s: Annotated[float, typer.Option(help="Timeout seconds", min=0.1)] = 2.0,
) -> None:
    async def run():
        dd = DoserDevice(device_address)
        try:
            await dd.read_dosing_pump_auto_settings(ch_id=ch_id, timeout_s=timeout_s)
        finally:
            await dd.disconnect()
    import asyncio as _asyncio
    _asyncio.run(run())


@app.command(name="read-dosing-container")
def read_dosing_container(
    device_address: Annotated[str, typer.Argument(help="BLE MAC, e.g. AA:BB:CC:DD:EE:FF")],
    ch_id: Annotated[int | None, typer.Option(help="Channel 0..3; omit for all")] = None,
    timeout_s: Annotated[float, typer.Option(help="Timeout seconds", min=0.1)] = 2.0,
) -> None:
    async def run():
        dd = DoserDevice(device_address)
        try:
            await dd.read_dosing_container_status(ch_id=ch_id, timeout_s=timeout_s)
        finally:
            await dd.disconnect()
    import asyncio as _asyncio
    _asyncio.run(run())

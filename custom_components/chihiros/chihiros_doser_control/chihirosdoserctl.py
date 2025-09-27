from __future__ import annotations

import asyncio
import inspect
from datetime import datetime
from typing import Any, List, Tuple

import typer
from rich import print
from rich.table import Table
from typing_extensions import Annotated
from prettytable import PrettyTable, SINGLE_BORDER

# chihiros-led-control plumbing
from ..chihiros_led_control import chihirosctl
from ..chihiros_led_control.weekday_encoding import WeekdaySelect

# Doser protocol (A5/1B)
from . import protocol as dosingpump

app = typer.Typer()

# ────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────

def _parse_params_tokens(tokens: List[str]) -> List[int]:
    """
    Accepts decimal or hex tokens:
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
            raise typer.BadParameter(f"Parameter-Byte außerhalb 0..255: {t}")
        out.append(v)
    return out


def _parse_hex_blob(blob: str) -> bytes:
    s = "".join(blob.strip().split())
    if len(s) % 2 != 0:
        raise typer.BadParameter("Hex-Länge muss gerade sein.")
    try:
        return bytes.fromhex(s)
    except ValueError as e:
        raise typer.BadParameter("Ungültige Hex-Zeichen im Payload.") from e


# ────────────────────────────────────────────────────────────────
# RAW dosing (frames) — build & send
# ────────────────────────────────────────────────────────────────

@app.command()
def raw_dosing_pump(
    device_address: str,
    cmd_id: Annotated[int, typer.Option(help="Command ID, z.B. 165 (0xA5)")] = 165,
    mode:   Annotated[int, typer.Option(min=0, help="Mode/Subtyp, z.B. 21 oder 27")] = 21,
    params: Annotated[List[str], typer.Argument(metavar="PARAMS...", show_default=False)] = [],
    repeats: Annotated[int, typer.Option("--repeats", "-r", help="Wiederholungen")] = 3,
) -> None:
    """
    Beispiel:
      chihirosdosesctl raw-dosing-pump XX:XX:XX:XX:XX:XX --cmd-id 165 --mode 27 0 0 0 0 10
    """
    p: List[int] = _parse_params_tokens(params)

    def _send(dev, *, cmd_id: int, mode: int, p: List[int], repeats: int):
        # Build a frame with the protocol encoder (uses its internal msg-id)
        frame = dosingpump._encode(cmd_id, mode, p)  # type: ignore[attr-defined]
        return dev._send_command(frame, repeats)

    chihirosctl._run_device_func(
        device_address,
        cmd_id=cmd_id,
        mode=mode,
        p=p,
        repeats=repeats,
        func=_send,
    )


# ────────────────────────────────────────────────────────────────
# High-level CLIs
# ────────────────────────────────────────────────────────────────

@app.command()
def add_setting_dosing_pump(
    device_address: str,
    performance_time: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
    ch_id: Annotated[int, typer.Option(max=3, min=0)] = 0,
    weekdays: Annotated[list[WeekdaySelect], typer.Option()] = [WeekdaySelect.everyday],
    # 0.1 ml units (1 == 0.1 ml). Keep as int if your device method expects tenths.
    ch_ml: Annotated[int, typer.Option(min=0, max=9999)] = 0,
) -> None:
    chihirosctl._run_device_func(
        device_address,
        performance_time=performance_time,
        ch_id=ch_id,
        weekdays=weekdays,
        ch_ml=ch_ml,
        func_name="add_setting_dosing_pump",
    )


@app.command()
def enable_auto_mode_dosing_pump(
    device_address: str,
    ch_id: Annotated[int, typer.Option(max=3, min=0)] = 0,
) -> None:
    """Enable auto mode for a dosing channel."""
    chihirosctl._run_device_func(
        device_address,
        ch_id=ch_id,
        func_name="enable_auto_mode_dosing_pump",
    )


@app.command()
def set_dosing_pump_manuell_ml(
    device_address: str,
    ch_id: Annotated[int, typer.Option(max=3, min=0)] = 0,
    # Accept float; device code / protocol handles 25.6 + 0.1 encoding.
    ch_ml: Annotated[float, typer.Option(min=0.2, max=999.9)] = None,
) -> None:
    chihirosctl._run_device_func(
        device_address,
        ch_id=ch_id,
        ch_ml=ch_ml,
        func_name="set_dosing_pump_manuell_ml",
    )


# ────────────────────────────────────────────────────────────────
# BYTES ENCODE — pretty-print + decode helpers
# ────────────────────────────────────────────────────────────────

@app.command(name="bytes-encode")
def bytes_encode(
    params: Annotated[str, typer.Argument(help="Hex-String mit/ohne Leerzeichen")],
    show_table: Annotated[bool, typer.Option("--table/--no-table")] = True,
) -> None:
    """
    Pretty-prints an A5/5B frame and (if 8 params) decodes 4 channels of daily totals
    using the 25.6 bucket + 0.1 mL scheme.
    """
    value_bytes = _parse_hex_blob(params)

    if not show_table:
        norm = " ".join(f"{b:02x}" for b in value_bytes)
        typer.echo(f"{norm}   (len={len(value_bytes)})")
        return

    cmd_id = value_bytes[0] if len(value_bytes) >= 1 else "????"
    proto_ver = value_bytes[1] if len(value_bytes) >= 2 else "????"
    length_field = value_bytes[2] if len(value_bytes) >= 3 else None
    msg_hi = value_bytes[3] if len(value_bytes) >= 4 else "????"
    msg_lo = value_bytes[4] if len(value_bytes) >= 5 else "????"
    mode = value_bytes[5] if len(value_bytes) >= 6 else "????"

    # Determine param length per protocol family
    param_len_total = max(0, len(value_bytes) - 7)
    if isinstance(length_field, int):
        if cmd_id in (0x5B, 91):      # LED-style
            param_len = max(0, length_field - 2)
        else:                          # A5-style (doser)
            param_len = max(0, length_field - 5)
    else:
        param_len = param_len_total

    # If heuristic and explicit disagree, trust actual bytes
    if param_len != param_len_total:
        param_len = param_len_total

    params_start = 6
    params_end = min(len(value_bytes) - 1, params_start + param_len)
    params_list = [int(b) for b in value_bytes[params_start:params_end]]
    checksum = value_bytes[-1] if len(value_bytes) >= 1 else "????"

    table = PrettyTable()
    table.set_style(SINGLE_BORDER)
    table.title = "Encode Message"
    table.field_names = [
        "Command Print", "Command ID", "Version", "Command Length",
        "Message ID High", "Message ID Low", "Mode", "Parameters", "Checksum",
    ]
    table.add_row([
        str([int(b) for b in value_bytes]),
        str(cmd_id),
        str(proto_ver),
        str(length_field if length_field is not None else "????"),
        str(msg_hi),
        str(msg_lo),
        str(mode),
        str(params_list),
        str(checksum),
    ])
    print(table)

    # Optional: decode daily totals for 4 channels using the 25.6 scheme
    if len(params_list) == 8:
        def decode_ml_25_6(hi: int, lo: int) -> float:
            # hi*25.6 + lo*0.1
            return round(hi * 25.6 + lo / 10.0, 1)

        mls = [decode_ml_25_6(params_list[i], params_list[i + 1]) for i in range(0, 8, 2)]
        typer.echo(
            f"Decoded daily totals (ml): ch0={mls[0]:.2f}, ch1={mls[1]:.2f}, ch2={mls[2]:.2f}, ch3={mls[3]:.2f}"
        )


# ────────────────────────────────────────────────────────────────
# READ CLIs
# ────────────────────────────────────────────────────────────────

@app.command()
def read_dosing_auto(
    device_address: str,
    ch_id: Annotated[int | None, typer.Option()] = None,
    timeout_s: Annotated[float, typer.Option()] = 2.0,
) -> None:
    chihirosctl._run_device_func(
        device_address,
        ch_id=ch_id,
        timeout_s=timeout_s,
        func_name="read_dosing_pump_auto_settings",
    )


@app.command()
def read_dosing_container(
    device_address: str,
    ch_id: Annotated[int | None, typer.Option()] = None,
    timeout_s: Annotated[float, typer.Option()] = 2.0,
) -> None:
    chihirosctl._run_device_func(
        device_address,
        ch_id=ch_id,
        timeout_s=timeout_s,
        func_name="read_dosing_container_status",
    )

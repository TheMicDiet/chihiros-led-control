from __future__ import annotations
from typing import List, Tuple

# Nordic UART (same pair as your LEDs)
UART_SERVICE = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
UART_RX      = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"  # WRITE
UART_TX      = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"  # NOTIFY

CMD_MANUAL_DOSE  = 0xA5
MODE_MANUAL_DOSE = 0x1B

_last_msg_id: Tuple[int, int] = (0, 0)

def _next_msg_id() -> Tuple[int, int]:
    hi, lo = _last_msg_id
    if lo == 255:
        if hi == 255:
            new = (0, 1)
        elif hi == 89:        # never decimal 90
            new = (hi + 2, 0)
        else:
            new = (hi + 1, 0)
    else:
        if lo == 89:          # never decimal 90
            new = (0, lo + 2)
        else:
            new = (0, lo + 1)
    globals()["_last_msg_id"] = new
    return new

def _xor_checksum(buf: bytes) -> int:
    c = buf[1]
    for b in buf[2:]:
        c ^= b
    return c

def _encode(cmd_id: int, mode: int, params: List[int]) -> bytes:
    # sanitize params: never 90 (0x5A) per device rule
    ps = [(p if p != 90 else 89) & 0xFF for p in params]
    hi, lo = _next_msg_id()
    body = bytes([cmd_id, 0x01, len(ps) + 5, hi, lo, mode, *ps])
    chk = _xor_checksum(body)
    if chk == 90:
        _next_msg_id()
        hi2, lo2 = _last_msg_id
        body = bytes([cmd_id, 0x01, len(ps) + 5, hi2, lo2, mode, *ps])
        chk = _xor_checksum(body)
    return body + bytes([chk])

async def dose_ml(client, channel_1based: int, ml: float) -> None:
    """
    Immediate one-shot dose on selected channel.
    channel_1based: 1..4
    ml: 0.2 .. 999.9 (0.1 mL resolution)
    """
    ch = max(1, min(int(channel_1based), 4)) - 1  # protocol uses 0-based
    tenths = int(round(max(0.2, min(ml, 999.9)) * 10))
    ml_hi, ml_lo = (tenths >> 8) & 0xFF, tenths & 0xFF

    # Frame proven by your captures:
    # [A5,01,0A, msg_hi, msg_lo, 1B, ch, 00, 00, ml_hi, ml_lo, checksum]
    pkt = _encode(CMD_MANUAL_DOSE, MODE_MANUAL_DOSE, [ch, 0x00, 0x00, ml_hi, ml_lo])
    await client.write_gatt_char(UART_RX, pkt, response=True)

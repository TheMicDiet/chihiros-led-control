from __future__ import annotations
from typing import List, Tuple

# Nordic UART
UART_SERVICE = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
UART_RX      = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"  # write
UART_TX      = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"  # notify

# Dosing command (matches captures): CMD=0xA5 (165), MODE=0x1B (27)
CMD_MANUAL_DOSE  = 0xA5
MODE_MANUAL_DOSE = 0x1B

_last_msg_id: Tuple[int, int] = (0, 0)

def _next_msg_id() -> Tuple[int, int]:
    hi, lo = _last_msg_id
    if lo == 255:
        if hi == 255:
            new = (0, 1)
        elif hi == 89:  # never 90
            new = (hi + 2, 0)
        else:
            new = (hi + 1, 0)
    else:
        if lo == 89:    # never 90
            new = (0, lo + 2)
        else:
            new = (0, lo + 1)
    globals()['_last_msg_id'] = new
    return new

def _xor_checksum(buf: bytes) -> int:
    c = buf[1]
    for b in buf[2:]:
        c ^= b
    return c

def _encode(cmd: int, mode: int, params: List[int]) -> bytes:
    # Avoid 0x5A in payload bytes
    ps = [(p if p != 0x5A else 0x59) & 0xFF for p in params]
    hi, lo = _next_msg_id()
    body = bytes([cmd, 0x01, len(ps) + 5, hi, lo, mode, *ps])
    chk = _xor_checksum(body)
    if chk == 0x5A:  # adjust id if checksum hits 0x5A
        _next_msg_id()
        hi, lo = _last_msg_id
        body = bytes([cmd, 0x01, len(ps) + 5, hi, lo, mode, *ps])
        chk = _xor_checksum(body)
    return body + bytes([chk])

# ---------- NEW: 25.6-bucket helpers ----------

def _split_ml_25_6(total_ml: float) -> tuple[int, int]:
    """
    Encode ml as (hi, lo) with 25.6-mL buckets (+0.1-mL remainder).
      hi = floor(ml / 25.6)
      lo = round((ml - hi*25.6) * 10)   # 0..255 (0.1 mL)
    Normalize exact multiples so 25.6 -> (1,0) not (0,256).
    """
    if total_ml < 0 or total_ml > 999.9:
        raise ValueError("ml must be within 0..999.9")
    q = round(total_ml, 1)          # device resolution is 0.1 mL
    hi = int(q // 25.6)
    rem = round(q - hi * 25.6, 1)
    lo = int(round(rem * 10))
    if lo == 256:
        hi += 1
        lo = 0
    if not (0 <= lo <= 255):
        raise ValueError("remainder out of range")
    return hi, lo

# ---------- PUBLIC API ----------

async def dose_ml(client, channel_1based: int, ml: float) -> None:
    """
    Immediate, one-shot dose on the selected channel.

    Protocol encoding (confirmed by sniffing):
      params = [channel(0..3), 0x00, 0x00, ml_hi, ml_lo]
      where:
        ml_hi = floor(ml / 25.6)
        ml_lo = round((ml - ml_hi*25.6) * 10)    # 0.1 mL remainder

    Examples:
      11.3  -> (0,113)
      25.6  -> (1,0)
      51.2  -> (2,0)
      29.0  -> (1,34)
    """
    ch = max(1, min(int(channel_1based), 4)) - 1  # 0-based on wire
    ml = round(max(0.2, min(float(ml), 999.9)), 1)

    ml_hi, ml_lo = _split_ml_25_6(ml)
    pkt = _encode(CMD_MANUAL_DOSE, MODE_MANUAL_DOSE, [ch, 0x00, 0x00, ml_hi, ml_lo])
    await client.write_gatt_char(UART_RX, pkt, response=True)

from __future__ import annotations
from typing import List, Tuple, Union, Optional
from decimal import Decimal, ROUND_HALF_UP, ROUND_FLOOR

# Nordic UART
UART_SERVICE = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
UART_RX      = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"  # write
UART_TX      = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"  # notify

# Dosing command (matches captures): CMD=0xA5 (165), MODE=0x1B (27)
CMD_MANUAL_DOSE  = 0xA5
MODE_MANUAL_DOSE = 0x1B

# LED-style (0x5B) command used for totals-query frames (modes 0x22, 0x1E seen)
CMD_LED_QUERY = 0x5B  # 91

_last_msg_id: Tuple[int, int] = (0, 0)

def _next_msg_id() -> Tuple[int, int]:
    """
    Increment msg id, skipping 0x5A in either byte, and only bump 'hi' on wrap.
    """
    hi, lo = _last_msg_id
    lo = (lo + 1) & 0xFF
    if lo == 0x5A:                # never 0x5A
        lo = (lo + 1) & 0xFF
    if lo == 0:                   # wrapped → bump hi
        hi = (hi + 1) & 0xFF
        if hi == 0x5A:            # never 0x5A
            hi = (hi + 1) & 0xFF
    globals()['_last_msg_id'] = (hi, lo)
    return hi, lo

def _xor_checksum(buf: bytes) -> int:
    c = buf[1]
    for b in buf[2:]:
        c ^= b
    return c & 0xFF

def _sanitize_params(params: List[int]) -> List[int]:
    """
    Some firmwares avoid 0x5A in payload bytes; map to 0x59.
    (Consistent with the rest of the project’s dosing helpers.)
    """
    out: List[int] = []
    for p in params:
        b = p & 0xFF
        out.append(0x59 if b == 0x5A else b)
    return out

def _encode(cmd: int, mode: int, params: List[int]) -> bytes:
    """
    A5-style frame:
      [cmd, 0x01, len(params)+5, msg_hi, msg_lo, mode, *params, checksum]
    If checksum equals 0x5A, try a few different msg ids; do NOT mutate params.
    """
    ps = _sanitize_params(params)
    body = b""
    chk = 0
    for _ in range(8):
        hi, lo = _next_msg_id()
        body = bytes([cmd, 0x01, len(ps) + 5, hi, lo, mode, *ps])
        chk = _xor_checksum(body)
        if chk != 0x5A:
            break
    return body + bytes([chk])

# 0x5B LED-style encoder (length = len(params) + 2)
def encode_5b(mode: int, params: List[int]) -> bytes:
    """
    0x5B (LED-style) frame used by the doser for 'totals' queries:
      [0x5B, 0x01, len(params)+2, msg_hi, msg_lo, mode, *params, checksum]
    Same checksum/byte-avoid rules as A5.
    """
    ps = _sanitize_params(params)
    body = b""
    chk = 0
    for _ in range(8):
        hi, lo = _next_msg_id()
        body = bytes([CMD_LED_QUERY, 0x01, len(ps) + 2, hi, lo, mode, *ps])
        chk = _xor_checksum(body)
        if chk != 0x5A:
            break
    return body + bytes([chk])

# ---------- 25.6-bucket helpers ----------

def _split_ml_25_6(total_ml: Union[float, int, str]) -> tuple[int, int]:
    """
    Encode ml as (hi, lo) with 25.6-mL buckets (+0.1-mL remainder).
      hi = floor(ml / 25.6)
      lo = round((ml - hi*25.6) * 10)   # 0..255 (0.1 mL)
    Normalize exact multiples so 25.6 -> (1,0) not (0,256).
    Accepts "51,3" or "51.3"; clamps to 0.2..999.9 with 0.1 resolution.
    """
    if isinstance(total_ml, str):
        s = total_ml.replace(",", ".")
    else:
        s = str(total_ml)

    q = Decimal(s).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)

    if q < Decimal("0.2") or q > Decimal("999.9"):
        raise ValueError("ml must be within 0.2..999.9")

    hi = int((q / Decimal("25.6")).to_integral_value(rounding=ROUND_FLOOR))
    rem = (q - Decimal(hi) * Decimal("25.6")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    lo  = int((rem * 10).to_integral_value(rounding=ROUND_HALF_UP))

    if lo == 256:  # normalize exact multiples (e.g., 25.6 → (1,0)) — defensive
        hi += 1
        lo  = 0
    return hi & 0xFF, lo & 0xFF

# ---------- PUBLIC API ----------

async def dose_ml(client, channel_1based: int, ml: Union[float, int, str]) -> None:
    """
    Immediate, one-shot dose on the selected channel.

    Protocol encoding (confirmed by sniffing):
      params = [channel(0..3), 0x00, 0x00, ml_hi, ml_lo]
      where:
        ml_hi = floor(ml / 25.6)
        ml_lo = round((ml - ml_hi*25.6) * 10)  # 0.1 mL remainder

    Examples:
      11.3  -> (0,113)
      25.6  -> (1,0)
      51.2  -> (2,0)
      29.0  -> (1,34)
    """
    ch = max(1, min(int(channel_1based), 4)) - 1  # 0-based on wire
    ml_hi, ml_lo = _split_ml_25_6(ml)
    pkt = _encode(CMD_MANUAL_DOSE, MODE_MANUAL_DOSE, [ch, 0x00, 0x00, ml_hi, ml_lo])
    await client.write_gatt_char(UART_RX, pkt, response=True)

# ---------- Convenience helpers for sensors/services ----------

def build_totals_probes() -> list[bytes]:
    """
    Return a small set of frames different firmwares respond to for 'daily totals'.
    Order matters; we try LED-style (0x5B) first, then A5 fallbacks.
    """
    frames: list[bytes] = []
    try:
        # 0x5B (LED-style) in two observed modes
        frames.append(encode_5b(0x22, []))
        frames.append(encode_5b(0x1E, []))
    except Exception:
        pass
    try:
        # A5-style fallbacks (some firmwares echo totals after these)
        frames.append(_encode(CMD_MANUAL_DOSE, 0x22, []))
        frames.append(_encode(CMD_MANUAL_DOSE, 0x1E, []))
    except Exception:
        pass
    # de-dup while preserving order
    seen, uniq = set(), []
    for f in frames:
        b = bytes(f)
        if b not in seen:
            seen.add(b)
            uniq.append(f)
    return uniq

def build_totals_query() -> bytes:
    """
    Backwards-compatible helper: prefer a 0x5B/0x22 totals request;
    keep A5/0x22 as a fallback if needed.
    """
    try:
        return encode_5b(0x22, [])
    except Exception:
        return _encode(CMD_MANUAL_DOSE, 0x22, [])

def parse_totals_frame(payload: bytes | bytearray) -> Optional[list[float]]:
    """
    Tolerant decoder:
    If 'payload' looks like a totals frame (cmd 0x5B with exactly 8 params),
    return [ml0, ml1, ml2, ml3] using the 25.6 + 0.1 scheme; else None.
    (Observed modes include 0x22 and 0x1E; we do not require a specific mode.)
    """
    if not isinstance(payload, (bytes, bytearray)) or len(payload) < 15:
        return None
    cmd = payload[0]
    params = list(payload[6:-1])  # everything after 'mode' up to checksum
    if cmd == CMD_LED_QUERY and len(params) == 8:
        pairs = list(zip(params[0::2], params[1::2]))
        return [round(h * 25.6 + l / 10.0, 1) for h, l in pairs]
    return None

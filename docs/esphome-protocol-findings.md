# ESPHome Chihiros Protocol Findings

Extracted from [`BartdeJonge/chihiros-esphome`](https://github.com/BartdeJonge/chihiros-esphome), inspected from a local clone at `/tmp/chihiros-esphome`.

These notes are external corroboration, not captures from this repository. Treat any behavior not already validated by our own captures as provisional until tested against real hardware.

## Transport

The ESPHome project uses the same Nordic UART-style BLE transport as this repository.

| Role | UUID |
| --- | --- |
| Service | `6e400001-b5a3-f393-e0a9-e50e24dcca9e` |
| Write/RX | `6e400002-b5a3-f393-e0a9-e50e24dcca9e` |
| Notify/TX | `6e400003-b5a3-f393-e0a9-e50e24dcca9e` |

## Frame Format

ESPHome frame shape:

```text
[header] [0x01] [len] [0x00] [seq] [cmd] [data...] [xor]
```

Checksum is XOR over all bytes after the header and before the checksum:

```text
xor = 0x01 ^ len ^ 0x00 ^ seq ^ cmd ^ data...
```

Headers:

| Header | Meaning in ESPHome project |
| ---: | --- |
| `0x5a` | BASE: auth, RTC, CO2, fan mode/speed, WRGB brightness |
| `0xa5` | DEVICE: stirrer, fan thresholds, Doctor Mate, WRGB schedule |

Sequence behavior:

- ESPHome uses a one-byte sequence in the low message-id byte: `[0x00, seq]`.
- Sequence starts at `1`.
- Sequence skips `0x5a`.

This is compatible with this repository's two-byte message-id handling.

## Shared Commands

| Command | Params | Meaning |
| --- | --- | --- |
| `0x5a / 0x04` | `[0x01]` | base auth/query style command |
| `0xa5 / 0x04` | `[0x01]` | device auth, used by Doctor Mate |
| `0x5a / 0x09` | `[yy, month, weekday, hour, minute, second]` | RTC sync |
| `0x5a / 0x05` | `[mode, 0xff, 0xff]` | mode/reset/init command |

RTC uses a weekday-like third date byte, matching this repository's current newer-style behavior.

## Discovery Prefixes

ESPHome advertises/logs these prefixes:

| Prefix | Device type |
| --- | --- |
| `DYNT90` | WRGB2 light |
| `DYPCO2` | CO2 controller |
| `DYMIX` | Magnetic stirrer |
| `DYNFAN` | Cooling fan |
| `DYNDOC` | Doctor Mate |
| `DYDOSE` | Dosing pump |

## Doctor Mate

Likely prefix:

```text
DYNDOC
```

Connect/config sequence:

```text
1. a5 / 04 [01]                         auth_device
2. 5a / 09 [yy, month, weekday, h, m, s] RTC sync
3. a5 / 01 [00, ec_value]               TDS/EC target
4. a5 / 01 [00, volume_value]           tank volume
```

Important: TDS and volume use the same command shape. ESPHome notes that the device distinguishes them only by send order:

```text
first  a5/01 [00, value] = TDS/EC target
second a5/01 [00, value] = tank volume
```

Encodings:

```text
ec_value = round(tds_ppm / 0.4)
volume_value = liters * 2
```

Preset values from ESPHome:

| Profile | TDS ppm | EC value |
| --- | ---: | ---: |
| Plant | `80` | `200` / `0xc8` |
| Fish | `93` | `233` / `0xe9` |
| Shrimp | `66` | `166` / `0xa6` |

Example from ESPHome docs:

```text
auth_device    a5 01 06 00 01 04 01 03
rtc            5a 01 0b 00 02 09 ...
settings TDS   a5 01 07 00 03 01 00 64 60   # 100 µS/cm, first settings write
settings VOL   a5 01 07 00 04 01 00 c8 cb   # 100 L * 2, second settings write
```

Notification guess from ESPHome:

```text
x[5] = EC measurement, probably µS/cm
```

This needs validation before exposing a sensor.

## WRGB II

ESPHome prefix:

```text
DYNT90
```

Manual brightness:

```text
5a / 07 [channel, brightness]
```

Channels:

```text
0 = red
1 = green
2 = blue
```

Auto schedule:

```text
a5 / 19 [
  on_h, on_m,
  off_h, off_m,
  ramp_min,
  weekdays,
  red, green, blue,
  ff, ff, ff, ff, ff
]
```

Auto sequence in ESPHome:

```text
1. 5a / 04 [01]       auth
2. 5a / 09 RTC
3. 5a / 09 RTC again
4. 5a / 05 [07, ff, ff] reset/evaluate schema
5. a5 / 19 schedule
6. 5a / 05 [12, ff, ff] switch to auto
7. 5a / 09 RTC again    trigger schedule evaluation
```

Notes:

- `ramp_min == 90` / `0x5a` is avoided by changing to `89`.
- Weekday bitmask matches this repository: Monday `64`, Tuesday `32`, Wednesday `16`, Thursday `8`, Friday `4`, Saturday `2`, Sunday `1`, all `127`.

## CO2 Controller

Likely prefix:

```text
DYPCO2
```

Sequence:

```text
1. 5a / 04 [01]                         auth
2. 5a / 09 RTC
3. 5a / 09 RTC again
4. 5a / 05 [07, ff, ff]                 reset schema
5. 5a / 16 [start_h, start_m, value]    start slot
6. 5a / 16 [end_h, end_m, value]        end slot
```

Slot values:

| Value | Meaning |
| ---: | --- |
| `0x64` | CO2 on |
| `0x00` | CO2 off |
| `0x6f` | empty/disabled slot |

Example:

```text
5a / 16 [08, 00, 64]   # 08:00 on
5a / 16 [22, 00, 00]   # 22:00 off
```

## Cooling Fan

Likely prefix:

```text
DYNFAN
```

Sequence:

```text
1. 5a / 04 [01]                         auth
2. 5a / 09 RTC
3. 5a / 09 RTC again
4. a5 / 04 [06]                         fan auth ext 1
5. a5 / 04 [08]                         fan auth ext 2
6. 5a / 05 [silent_mode, ff, ff]         mode init
7. a5 / 21 [start_temp, max_temp, ff]    temperature thresholds
8. 5a / 07 [ff, speed]                  fan speed, 0 = auto
```

Silent mode values:

| Value | Meaning |
| ---: | --- |
| `0x22` | silent on |
| `0x23` | silent off |

Notification parser from ESPHome:

```text
if x[4] == 0x01:
  x[5]        = fan speed percent
  x[6:7] / 256 = room temperature °C
  x[11] / 10 = water temperature °C
  x[12]      = humidity percent
```

## Magnetic Stirrer

Likely prefix:

```text
DYMIX
```

Four channels, `0..3`.

Full config sequence:

```text
1. 5a / 04 [01]                         auth
2. 5a / 09 RTC
3. for each channel:
   a5 / 20 [channel, 00, 01]             enable channel
   a5 / 1b [channel, weekdays, 01, 00, 00, 00]
   a5 / 2a [channel, 00, lead_time_sec, speed_0_20]
   a5 / 15 [channel, 03, hour, minute, 00, duration_sec]
4. a5 / 1f [00]                         apply/save
5. a5 / 14 [ff, ff, k0, k1, k2, k3, ff, ff, ff, ff] restore on/off state
```

Direct channel toggle uses `a5 / 14`. The channel state bytes begin at payload index `2`:

```text
a5 / 14 [ff, ff, ch0, ch1, ch2, ch3, ff, ff, ff, ff]
```

Use `0xff` to leave a channel unchanged.

Important ESPHome correction:

```text
a5 / 1b is weekdays, not speed.
a5 / 2a is speed/lead-time.
```

Timer forms:

```text
a5 / 15 [channel, 03, hour, minute, 00, duration_sec]  # daily clock schedule
a5 / 15 [channel, 00, duration_sec, interval_sec, ...] # interval-style mode, less used
```

## Dosing Pump

Likely prefix:

```text
DYDOSE
```

ESPHome sequence:

```text
1. 5a / 04 [01]                         auth
2. 5a / 09 RTC
3. 5a / 09 RTC again
4. a5 / 04 [04]                         dosing auth 1
5. a5 / 04 [05]                         dosing auth 2
```

Manual dose:

```text
a5 / 1b [pump_idx, 00, 00, 00, vol_01ml]
```

Schedule per pump:

```text
a5 / 20 [pump_idx, 00, active]
a5 / 1b [pump_idx, weekdays, hour & 1, minute, 00, vol_01ml]
a5 / 15 [pump_idx, 00, hour >> 1, 00, 00, 00]
```

Encoding:

```text
vol_01ml = ml * 10
```

Caveat: ESPHome uses a single-byte volume, which limits values to `25.5 ml`. This repository's protocol notes and captures support a broader big-endian tenths-of-ml amount form for dosing devices. Treat ESPHome's dosing mapping as a limited/simple firmware variant or as incomplete relative to our captures.

## Implementation Takeaways

Most immediately useful for this repository:

1. Doctor Mate support is now much clearer:
   - prefix `DYNDOC`
   - `a5/04 [01]`
   - RTC sync
   - `a5/01 [00, ec]`
   - `a5/01 [00, volume*2]`
2. Fan support may be feasible because the command sequence and notification parser are explicit.
3. CO2 support looks straightforward as a two-slot schedule over `5a/16`.
4. Stirrer support is detailed but would require a new device/entity model.
5. Dosing support differs from this repository's existing captures; do not replace the current dosing notes with the ESPHome variant without hardware validation.

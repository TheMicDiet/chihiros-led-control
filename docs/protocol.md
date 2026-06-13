# Chihiros Bluetooth Protocol

These notes describe the application-level BLE protocol used by supported
Chihiros LED devices. They are based on the working implementation in this
repository plus reverse-engineering notes from the old Chihiros Magic app, the
newer Flutter app, and BLE captures.

## BLE Transport

Most supported devices use a Nordic UART-style BLE service:

| Purpose | UUID |
| --- | --- |
| Service | `6e400001-b5a3-f393-e0a9-e50e24dcca9e` |
| Write/RX characteristic | `6e400002-b5a3-f393-e0a9-e50e24dcca9e` |
| Notify/TX characteristic | `6e400003-b5a3-f393-e0a9-e50e24dcca9e` |
| CCCD | `00002902-0000-1000-8000-00805f9b34fb` |

Notifications are enabled by writing `01 00` to the TX characteristic CCCD.
Application commands are written to the RX characteristic. Responses and status
updates arrive as notifications from the TX characteristic.

Some legacy app paths also reference these characteristics:

| Purpose | UUID |
| --- | --- |
| Legacy write/notify characteristic | `0000ffe1-0000-1000-8000-00805f9b34fb` |
| Legacy AT characteristic | `0000ffab-0000-1000-8000-00805f9b34fb` |

## Frame Format

Commands are byte arrays with this structure:

| Offset | Name | Description |
| ---: | --- | --- |
| `0` | Command ID / family | Common values are `0x5a`, `0xa5`, and `0x5f` |
| `1` | TX marker | `0x01` for transmitted commands |
| `2` | Command length | Number of parameter bytes plus `5` |
| `3` | Message ID high | High byte of the incrementing message ID |
| `4` | Message ID low | Low byte of the incrementing message ID |
| `5` | Mode / sub-command | Command-specific mode byte |
| `6..n-2` | Parameters | Command-specific payload |
| `n-1` | Checksum | XOR/BCC checksum |

Total frame length is `parameter_count + 7`.

Example manual brightness frame before checksum:

```text
5a 01 07 msg_hi msg_lo 07 color brightness
```

## Message IDs And Reserved Bytes

The two message ID bytes are maintained by the app and incremented for each
command.

Older LED protocol paths avoid the reserved byte `0x5a` in message ID bytes,
parameters, and checksums:

- Message ID high and low bytes skip `0x5a`.
- Parameter bytes equal to `0x5a` are sent as `0x59`.
- If the calculated checksum would be `0x5a`, the message ID is incremented and
  the frame is rebuilt.

This repository implements those rules in `src/chihiros_led_control/protocol.py`.

## Checksum

The frame checksum is a simple XOR/BCC. It excludes byte `0` and includes byte
`1` through the last payload byte:

```text
checksum = byte[1] ^ byte[2] ^ ... ^ byte[n-2]
```

Python equivalent:

```python
def checksum(frame_without_checksum: bytes) -> int:
    value = frame_without_checksum[1]
    for item in frame_without_checksum[2:]:
        value ^= item
    return value & 0xff
```

## Manual Brightness

Set a single channel to a specific brightness:

- Command ID: `0x5a` / `90`
- Mode: `0x07` / `7`
- Parameters: `[color, brightness]`

Parameter details:

- `color`: channel ID
- `brightness`: `0` to `100`

Known channel IDs:

| Channel | Meaning |
| ---: | --- |
| `0` | Red, or white on non-RGB models |
| `1` | Green |
| `2` | Blue |
| `3` | White on WRGB, WRGB Pro, and Universal WRGB models |

For RGB and WRGB devices, each channel is sent as a separate command.

Captured example for channel `0` at `100%`:

```text
5a 01 07 00 20 07 00 64 45
```

## Auto Mode

Auto mode can be enabled with:

- Command ID: `0x5a` / `90`
- Mode: `0x05` / `5`
- Parameters: `[18, 255, 255]`

Auto mode and its settings can be reset with:

- Command ID: `0x5a` / `90`
- Mode: `0x05` / `5`
- Parameters: `[5, 255, 255]`

Other observed `0x5a / 0x05` first parameters:

| First parameter | Observed meaning |
| ---: | --- |
| `4` | Stop/exit demo in the old app |
| `5` | Reset auto settings |
| `6` | Temporary/new-firmware demo in the old app |
| `11` / `0x0b` | First-connect/manual setup command, exact meaning unknown |
| `18` | Enable auto mode |

## Auto Schedule Settings

Create or update an automatic schedule setting:

- Command ID: `0xa5` / `165`
- Mode: `0x19` / `25`
- Parameters:
  `[sunrise hour, sunrise minute, sunset hour, sunset minute, ramp up minutes, weekdays, red brightness, green brightness, blue brightness, 255, 255, 255, 255, 255]`

For non-RGB models, put the desired white brightness in the red brightness field
and set the other two brightness fields to `255`:

```text
[white_brightness, 255, 255]
```

To delete or deactivate a setting, send the same schedule fields with all three
brightness fields set to `255`:

```text
[255, 255, 255]
```

Only one setting can be configured per day, so settings cannot conflict. There
is a maximum of 7 settings.

## Weekday Bitmask

Weekdays are encoded as a 7-bit mask:

| Day | Value |
| --- | ---: |
| Monday | `64` |
| Tuesday | `32` |
| Wednesday | `16` |
| Thursday | `8` |
| Friday | `4` |
| Saturday | `2` |
| Sunday | `1` |
| Everyday | `127` |

For example, Monday, Wednesday, and Sunday encode as `64 + 16 + 1 = 81`.

## Set Time

The current time is required for auto mode and can be set with:

- Command ID: `0x5a` / `90`
- Mode: `0x09` / `9`
- Parameters: `[year - 2000, month, third date field, hour, minute, second]`

The third date field is firmware/app-generation dependent:

- This repository sends ISO weekday, `1` to `7` for Monday to Sunday.
- The old Chihiros Magic 2.6.0e app used day of month.

Captured newer-style examples use the weekday-like form.

## Runtime And Status Responses

Old LED notifications can start with `0x5b`. For these notifications, byte `1`
is a firmware/protocol version byte rather than the TX marker. One captured LED
reported firmware/protocol version `0x17` / `23`.

Runtime/status query:

- Command ID: `0x5a` / `90`
- Mode: `0x04` / `4`
- Parameters: `[1]`

Captured query:

```text
5a 01 06 00 04 04 01 06
```

Captured runtime response:

```text
5b 17 0a 00 01 0a 01 ff ff ff ff 13 88 8c
```

For this response type, bytes `[6..7]` contain a big-endian runtime value in
minutes. The example above reports `0x01ff = 511` minutes. The checksum validates
with the same XOR rule over bytes `[1..n-2]`.

The old app also handled related legacy `0xb5` frames where runtime is a
32-bit seconds value at bytes `[6..9]`.

## Auto Schedule Snapshot Responses

The same startup/status flow can produce a longer old LED `0x5b` notification
with mode byte `0xfe`. A captured example contained saved auto curve points as
hour/minute/level triples:

```text
0d 0f 00  -> 13:15 level 0
0d 2d 64  -> 13:45 level 100
15 0f 64  -> 21:15 level 100
15 2d 00  -> 21:45 level 0
```

Unlike the short runtime response, the captured `0x5b / 0xfe` snapshot did not
validate when the final byte was treated as the simple XOR checksum. Treat this
snapshot as a status payload whose checksum/trailer is not yet confirmed.

## Other Confirmed LED Commands

| Command ID | Mode | Parameters | Meaning |
| ---: | ---: | --- | --- |
| `0x5a` / `90` | `0x04` / `4` | `[1]` | Query LED runtime/status |
| `0x5a` / `90` | `0x06` / `6` | `[color, time_index, level]` | Old 48-point auto curve update; `time_index` is `0..47` in 30-minute steps |
| `0x5a` / `90` | `0x07` / `7` | `[color, brightness]` | Manual brightness |
| `0x5a` / `90` | `0x09` / `9` | `[year - 2000, month, date_field, hour, minute, second]` | Set device time |
| `0xa5` / `165` | `0x19` / `25` | 14 bytes | Add, update, or delete auto schedule |

## Observed Command Families

The newer Flutter app uses the same frame builder for several command families.
Some modes are confirmed by implementation or captures; others are only observed
in decompiled app paths and still need semantic validation.

| Command ID | Observed modes |
| ---: | --- |
| `0x5a` / `90` | `0x04`, `0x05`, `0x06`, `0x07`, `0x09`, `0x0f`, `0x16`, `0x2b`, `0x2f`, `0x30` |
| `0x5f` / `95` | `0x01`, `0x02` |
| `0xa5` / `165` | `0x01`, `0x02`, `0x04`, `0x05`, `0x14`, `0x15`, `0x19`, `0x1b`, `0x1f`, `0x20`, `0x2d`, `0x37`, `0x38`, `0x3d`, `0x41`, `0x42`, `0xae` |

## Doctor Commands

The old Chihiros Magic app identifies these Doctor commands:

| Command ID | Mode | Parameters | Meaning |
| ---: | ---: | --- | --- |
| `0xa5` / `165` | `0x01` / `1` | `[time_hi, time_lo]` | Doctor operation time/duration as a big-endian 16-bit value |
| `0xa5` / `165` | `0x02` / `2` | `[1]` | Doctor power on |
| `0xa5` / `165` | `0x02` / `2` | `[2]` | Doctor power off |
| `0xa5` / `165` | `0x02` / `2` | `[3]` | Query Doctor runtime/status |

## Dosing Pump Commands

Dosing pump findings are not used by the current LED implementation, but they
confirm that Chihiros dosing devices use the same frame format, checksum, and
Nordic UART-style transport. Observed device name prefixes include `DYDOSE` and
`DYNDOS`.

Amounts are encoded as a big-endian integer in tenths of a milliliter:

```python
units = round(ml * 10)
ml_hi = units >> 8
ml_lo = units & 0xff
```

Known dosing pump commands:

| Command ID | Mode | Parameters | Meaning |
| ---: | ---: | --- | --- |
| `0xa5` / `165` | `0x15` / `21` | `[channel, unknown_or_timer_type, hour, minute, 0, 0]` | Timer/time command |
| `0xa5` / `165` | `0x1b` / `27` | `[channel, 0, subtype/status, ml_hi, ml_lo]` | Manual or amount command variant |
| `0xa5` / `165` | `0x1b` / `27` | `[channel, weekdays, recurrence_flag, unknown/status, ml_hi, ml_lo]` | Scheduled/recurring amount command variant |
| `0xa5` / `165` | `0x20` / `32` | `[channel, catch_up_missed, active]` | Auto/channel enable |

Captured OEM automatic schedule save order:

```text
0x5a / 0x04 / [1]
0x5a / 0x09 / [year - 2000, month, weekday, hour, minute, second]
0x5a / 0x09 / [year - 2000, month, weekday, hour, minute, second]
0xa5 / 0x04 / [4]
0xa5 / 0x04 / [5]
0xa5 / 0x20 / [channel, catch_up_missed, active]
0xa5 / 0x1b / [channel, weekdays, recurrence_flag, unknown/status, ml_hi, ml_lo]
0xa5 / 0x15 / [channel, unknown_or_timer_type, hour, minute, 0, 0]
```

Captured example for channel `0`, every day, `60.0 ml`, time `22:35`:

```text
a5 01 08 00 57 20 00 00 01 7f
a5 01 0b 00 58 1b 00 7f 01 01 02 58 6c
a5 01 0b 00 59 15 00 00 16 23 00 00 73
```

## Decompiler Notes

The Flutter app's central frame builder appears as
`sub_8c25ac(parameterLength, commandId, mode, parameterBytes)` in decompiled
pseudocode, with a checksum helper around native address `0x8c2950`.

Many integer arguments in the decompiled Dart AOT pseudocode are tagged small
integers. A displayed first argument of `4` often means an actual parameter
length of `2`, `6` means `3`, and `0xa` means `5`. Command ID and mode values
listed in these docs are actual byte values.

The old app also contains an unused `0xaa ...` frame builder. No callers were
found, so it should not be treated as the active BLE protocol.

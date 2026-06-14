# Chihiros App BLE Protocol Notes

Reverse-engineering notes from the decompiled Flutter APK under `decompiled-app/pseudocode/` and strings/disassembly from `libapp.so`.

## BLE transport

The app uses UART-style BLE characteristics.

Known UUIDs found in the binary:

| Purpose | UUID |
|---|---|
| Write/RX | `6e400002-b5a3-f393-e0a9-e50e24dcca9e` |
| Notify/TX | `6e400003-b5a3-f393-e0a9-e50e24dcca9e` |
| Legacy/write+notify characteristic | `0000ffe1-0000-1000-8000-00805f9b34fb` |
| Legacy/AT characteristic | `0000ffab-0000-1000-8000-00805f9b34fb` |
| CCCD | `00002902-0000-1000-8000-00805f9b34fb` |

Related binary strings include `BleController`, `write_without_response`, `service_uuids`, `characteristicUuid`, `setNotifyListener`, and `BmWriteCharacteristicRequest`.

## Frame builder

The central application-level frame builder is:

- `decompiled-app/pseudocode/02931_sub_8c25ac.dartpseudo`
- native address around `0x8c25ac`
- checksum helper around `0x8c2950`

It is effectively called as:

```dart
sub_8c25ac(parameterLength, commandId, mode, parameterBytes)
```

Important decompiler caveat: many integer arguments in the pseudocode are Dart tagged small integers. A displayed first argument of `4` often means an actual parameter length of `2`, `6` means `3`, `0xa` means `5`, etc. The command ID and mode values shown below are actual byte values.

Examples found, with actual parameter lengths in comments:

```dart
sub_8c25ac(4,   0xa5, 0x01, payload) // 2 params
sub_8c25ac(2,   0xa5, 0x04, payload) // 1 param
sub_8c25ac(6,   0x5a, 0x05, payload) // 3 params
sub_8c25ac(0xa, 0x5f, 0x01, payload) // 5 params
```

## Packet format

The generated frame has `payload_len + 7` total bytes:

```text
[0]      command ID / family byte   // commonly 0x5a, 0xa5, 0x5f
[1]      0x01                       // TX marker/version; in old LED RX `0x5b` notifications this byte is firmware/protocol version
[2]      payload_len + 5             // also described as params + 5
[3]      message ID high byte
[4]      message ID low byte
[5]      mode / sub-command byte
[6..n-2] payload/parameter bytes
[n-1]    checksum
```

Note: the Dart AOT disassembly stores small integers as tagged SMIs (`value << 1`), which initially made byte `[1]` look like `0x02`. The existing Python analysis in `../chihiros-led-control` and the Dart tagging convention confirm the actual on-air value is `0x01`.

The two message ID bytes are maintained by the app and incremented for each command. Existing working code avoids `0x5a` in the message ID bytes and in the checksum.

## Checksum

Despite the app containing `package:chihiros_magic_new/util/crc.dart`, the frame checksum helper at `0x8c2950` performs a simple XOR/BCC.

```text
checksum = byte[1] ^ byte[2] ^ ... ^ byte[n-2]
```

It excludes byte `0` and includes byte `1` through the last payload byte. Existing working code also avoids producing a checksum byte of `0x5a` by incrementing the message ID and rebuilding the frame.

Python approximation:

```python
def chihiros_frame(family, cmd, payload, seq_a=0, seq_b=0):
    payload = bytes(payload)
    frame = bytearray()
    frame.append(family & 0xff)
    frame.append(0x01)
    frame.append(len(payload) + 5)
    frame.append(seq_a & 0xff)
    frame.append(seq_b & 0xff)
    frame.append(cmd & 0xff)
    frame.extend(payload)

    checksum = 0
    for b in frame[1:]:
        checksum ^= b
    frame.append(checksum)
    return bytes(frame)
```

Working implementation detail from `../chihiros-led-control/custom_components/chihiros/chihiros_led_control/commands.py`:

```python
def next_message_id(current_msg_id=(0, 0)):
    high, low = current_msg_id
    if low == 255:
        if high == 255:
            return (0, 1)
        if high == 89:       # avoid high byte becoming 0x5a
            return (high + 2, low)
        return (high + 1, 0)
    if low == 89:            # avoid low byte becoming 0x5a
        return (0, low + 2)
    return (0, low + 1)
```

The same implementation sanitizes parameters equal to decimal `90` (`0x5a`) to `89` for the older LED protocol.

## Command families

### Family `0xa5`

Observed command bytes:

```text
0x01, 0x02, 0x04, 0x05, 0x14, 0x15, 0x1b, 0x1f,
0x20, 0x2d, 0x37, 0x38, 0x3d, 0x41, 0x42, 0xae
```

Likely used by newer/SeaLed-style devices and more complex schedules/scenes.

Example references:

- `03674_sub_9b95cc.dartpseudo` -> `sub_8c25ac(4, 0xa5, 0x01, ...)`
- `03705_sub_9c1c54.dartpseudo` -> `sub_8c25ac(2, 0xa5, 0x04, ...)`
- `03708_sub_9c2478.dartpseudo` -> `sub_8c25ac(6, 0xa5, 0x05, ...)`
- `03613_sub_9a2800.dartpseudo` -> `sub_8c25ac(0xc, 0xa5, 0x3d, ...)`
- `03783_sub_9e2c54.dartpseudo` -> `sub_8c25ac(0xa, 0xa5, 0x42, ...)`

### Family `0x5a`

Observed command bytes:

```text
0x04, 0x05, 0x06, 0x07, 0x0f, 0x16, 0x2b, 0x2f, 0x30
```

Likely used by older BLE LED/controller protocol variants.

Example references:

- `03644_sub_9b0530.dartpseudo` -> `sub_8c25ac(2, 0x5a, 0x04, ...)`
- `03564_sub_997d34.dartpseudo` -> `sub_8c25ac(6, 0x5a, 0x05, ...)`
- `06541_sub_e54bf0.dartpseudo` -> `sub_8c25ac(6, 0x5a, 0x06, ...)`
- `04205_sub_a5e2c8.dartpseudo` -> `sub_8c25ac(4, 0x5a, 0x07, ...)`

### Family `0x5f`

Observed command bytes:

```text
0x01, 0x02
```

Likely used by newer Vivid/NewBleLed style control paths.

Example references:

- `03585_sub_99ba50.dartpseudo` -> `sub_8c25ac(0xa, 0x5f, 0x01, ...)`
- `03673_sub_9b947c.dartpseudo` -> `sub_8c25ac(0xa, 0x5f, 0x02, ...)`

## Filled gaps from `../chihiros-led-control`

Yes: the sibling project is very useful. It confirms that the old LED protocol already uses the same frame builder, lets us correct Dart tagged-integer artifacts in the pseudocode, and gives semantic names for several commands.

The sibling project contains a working Home Assistant integration/CLI and confirms the older LED command meanings.

Reference files:

- `../chihiros-led-control/README.md`
- `../chihiros-led-control/custom_components/chihiros/chihiros_led_control/commands.py`
- `../chihiros-led-control/custom_components/chihiros/chihiros_led_control/device/*.py`

### Confirmed old LED commands

In that project, byte `[0]` is called `Command ID` and byte `[5]` is called `Mode`.

#### Manual brightness

```text
Command ID: 0x5a / 90
Mode:       0x07 / 7
Payload:    [color, brightness]
```

`color` mapping:

```text
0 = red, or white on non-RGB models
1 = green
2 = blue
3 = white on WRGB/WRGB-Pro/Universal WRGB models
```

For RGB/WRGB devices, each channel is sent as a separate command.

Example frame before checksum:

```text
5a 01 07 msg_hi msg_lo 07 color brightness
```

#### Switch to auto mode

```text
Command ID: 0x5a / 90
Mode:       0x05 / 5
Payload:    [18, 255, 255]
```

#### Reset auto settings

```text
Command ID: 0x5a / 90
Mode:       0x05 / 5
Payload:    [5, 255, 255]
```

#### Set device time

```text
Command ID: 0x5a / 90
Mode:       0x09 / 9
Payload, newer/sibling implementation: `[year - 2000, month, weekday, hour, minute, second]`
Payload, old Chihiros Magic 2.6.0e app: `[year - 2000, month, day_of_month, hour, minute, second]`
```

The old hybrid app uses `SimpleDateFormat("yyyy,MM,dd,HH,mm,ss")`, so its third byte is day-of-month. Some newer notes/captures appear to use a weekday-like third byte; treat this field as firmware/app-generation dependent.

#### Add/update auto schedule setting

```text
Command ID: 0xa5 / 165
Mode:       0x19 / 25
Payload:
[
  sunrise_hour,
  sunrise_minute,
  sunset_hour,
  sunset_minute,
  ramp_up_minutes,
  weekdays,
  red_brightness,
  green_brightness,
  blue_brightness,
  255, 255, 255, 255, 255
]
```

For non-RGB models, use:

```text
[white_brightness, 255, 255]
```

for the three brightness fields.

To delete/deactivate a setting, send the same schedule fields but brightness:

```text
[255, 255, 255]
```

Weekday bitmask from `weekday_encoding.py`:

```text
Monday    = 64
Tuesday   = 32
Wednesday = 16
Thursday  = 8
Friday    = 4
Saturday  = 2
Sunday    = 1
Everyday  = 127
```

The project notes a maximum of 7 settings and no conflicting settings for the same day.

### Old Chihiros Magic 2.6.0e hybrid app validation

The web assets in `../../Downloads/assets/` only call a JavaScript bridge (`window.ble.*`). Decompiling the companion APK (`../../Downloads/Chihiros+Magic_2.6.0e_APKPure.apk`) shows the bridge implementation in `com.godlee.game.bleled.*`.

Key validation points:

- The old app uses the same application frame format as above.
- Legacy UUID constants are:
  - write characteristic: `0000ffe1-0000-1000-8000-00805f9b34fb`
  - notification characteristic: `0000ffe1-0000-1000-8000-00805f9b34fb`
  - AT characteristic: `0000ffab-0000-1000-8000-00805f9b34fb`
  - CCCD: `00002902-0000-1000-8000-00805f9b34fb`
- `DataMaker.formatData(commandId, mode, params)` builds `[commandId, 0x01, len(params)+5, msg_hi, msg_lo, mode, ...params, checksum]`.
- `DataMaker.addVerifyByte()` XORs bytes `[1..n-2]` and, if the checksum would be `0x5a`, increments message-id low byte and recomputes.
- `DataMaker.formatData()` mutates any parameter byte equal to `0x5a` to `0x59` before inserting it into the frame.
- Message IDs skip `0x5a` in both low and high bytes.

Old-app LED/Doctor commands now identified:

| Command ID | Mode | Params | Meaning in old app |
|---:|---:|---|---|
| `0x5a` | `0x04` | `[1]` | Query LED runtime/status (`getRunedTimeLed`) |
| `0x5a` | `0x05` | `[4, 255, 255]` | Stop/exit demo (`stopDemo`) |
| `0x5a` | `0x05` | `[5, 255, 255]` | Reset auto settings (`resetAuto`) |
| `0x5a` | `0x05` | `[6, 255, 255]` | Temporary demo/new-firmware demo (`tempDemo`) |
| `0x5a` | `0x06` | `[color, time_index, level]` | Old 48-point auto curve update; `time_index` is 0..47 in 30-minute steps |
| `0x5a` | `0x07` | `[color, brightness]` | Manual brightness |
| `0x5a` | `0x09` | `[year-2000, month, day_of_month, hour, minute, second]` | Set/simulate device time in old app |
| `0xa5` | `0x01` | `[time_hi, time_lo]` | Doctor operation time/duration; big-endian 16-bit value from `doctor.html`'s `time` field |
| `0xa5` | `0x02` | `[1]` | Doctor power on |
| `0xa5` | `0x02` | `[2]` | Doctor power off |
| `0xa5` | `0x02` | `[3]` | Query Doctor runtime/status |

Note: the old app contains an unused `ControllerPoint.getCode()` builder for a different 12-byte `0xaa ...` frame shape; no callers were found, so it should not be treated as the active BLE protocol.

### Old LED first-connect/manual capture validation

Capture: `/mnt/hgfs/shared/btsnoop_hci.log`, recorded while connecting to an LED and then setting manual brightness to `100%`.

The capture confirms Nordic UART-style transport for this LED:

```text
Service:      6e400001-b5a3-f393-e0a9-e50e24dcca9e
Write/RX:     6e400002-b5a3-f393-e0a9-e50e24dcca9e  ATT value handle 0x0010
Notify/TX:    6e400003-b5a3-f393-e0a9-e50e24dcca9e  ATT value handle 0x0012
TX CCCD:      00002902-0000-1000-8000-00805f9b34fb  ATT handle 0x0013
```

Notifications are enabled by writing `01 00` to handle `0x0013`.

#### Firmware/protocol version in notifications

The old app's notification callback stores notification byte `[1]` into `mFirmVersion` for frames beginning with `0x5b` or `0xb5`. The capture and the app UI agree:

```text
RX notifications start with: 5b 17 ...
Firmware/protocol version:  0x17 = 23
```

So for old LED `0x5b` notifications, byte `[1]` is confirmed as the firmware/protocol version byte.

#### Startup status query and runtime response

The app sends the old LED status/runtime query:

```text
TX: 5a 01 06 00 04 04 01 06
```

Decoded:

```text
family:   0x5a
mode:     0x04
msg id:   0004
params:   [01]
checksum: OK
```

The device responds:

```text
RX: 5b 17 0a 00 01 0a 01 ff ff ff ff 13 88 8c
```

Decoded/observed:

```text
family:             0x5b
firmware/protocol:  0x17 = 23
mode byte [5]:      0x0a
runtime bytes:      byte[6..7] = 01 ff = 511 minutes
checksum:           XOR over bytes [1..n-2] validates to 0x8c
```

The old app's `uploadDeviceTime()` decodes this `0x5b` runtime response as a 16-bit minute count at bytes `[6]` and `[7]`, multiplied by `60` for seconds. For the captured frame:

```text
0x01ff minutes = 511 minutes = 30,660 seconds = 8h31m
```

For related legacy frames beginning with `0xb5`, the old app instead reads a 32-bit seconds value from bytes `[6..9]`.

#### Auto schedule/status snapshot response

The same startup query also produced a long status/config notification:

```text
RX: 5b 17 30 00 01 fe
    06 0c 27 00 00 00 00 00 00 0c 21 00 00 00 00 00 06 0c 27
    0d 0f 00 0d 2d 64 15 0f 64 15 2d 00
    00 00 00 00 00 00 00 00 00 00 00 00 00 00
```

Decoded/observed:

```text
family:             0x5b
firmware/protocol:  0x17 = 23
length byte:        0x30
mode byte [5]:      0xfe
```

Unlike the short `0x0a` runtime response, this `0xfe` snapshot did not validate with the simple TX XOR checksum when the final byte is treated as a checksum. Treat it as a snapshot payload without the normal TX checksum rule unless further captures show otherwise.

The user confirmed that the following triples match the configured auto schedule. They appear as `(hour, minute, level)`:

```text
0d 0f 00  -> 13:15 level 0
0d 2d 64  -> 13:45 level 100
15 0f 64  -> 21:15 level 100
15 2d 00  -> 21:45 level 0
```

So old LED `0x5b / mode 0xfe` contains the auto schedule/status snapshot, including saved auto curve points encoded as hour/minute/level triples.

#### First-connect time sync and mode setup

After status notifications, the app sends time sync twice:

```text
TX: 5a 01 0b 00 05 09 1a 06 06 0c 27 2e 19
TX: 5a 01 0b 00 06 09 1a 06 06 0c 27 2e 1a
```

Decoded:

```text
mode:   0x09
params: [0x1a, 0x06, 0x06, 0x0c, 0x27, 0x2e]
       = year 2026, month 6, third date field 6, 12:39:46
checksum: OK
```

The app then sends:

```text
TX: 5a 01 08 00 08 05 0b ff ff 0f
```

Decoded:

```text
mode:   0x05
params: [0x0b, 0xff, 0xff]
checksum: OK
```

This is another old LED mode/setup command in the `0x5a / 0x05` family. Exact semantic name is not yet confirmed; it occurs during first-connect/manual-mode setup.

#### Manual brightness 100%

Initial manual brightness command before the later `100%` write:

```text
TX: 5a 01 07 00 07 07 00 00 06
```

Decoded:

```text
mode:   0x07
params: [channel=0, brightness=0]
checksum: OK
```

This appears to be the app setting/syncing manual channel `0` to brightness `0` before the user changes it to `100%`.

After setting manual brightness to `100%`:

```text
TX: 5a 01 07 00 20 07 00 64 45
```

Decoded:

```text
mode:   0x07
params: [channel=0, brightness=0x64]
       = channel 0, brightness 100
checksum: OK
```

This capture directly confirms manual brightness as `0x5a / mode 0x07 / [channel, brightness]`.

### Extra commands now identifiable in the current decompile

Using the sibling project as an anchor, the observed `sub_8c25ac` calls in the current Flutter app can be interpreted as command ID + mode + parameter count. Confirmed rows are known from working code; unknown rows are additional commands present in the current app that need BLE captures or deeper UI call tracing for semantics.

| Command ID | Mode | Actual parameter count | Status / meaning |
|---:|---:|---:|---|
| `0x5a` / 90 | `0x05` / 5 | 3 | Confirmed: auto-mode/demo/reset/setup depending first parameter (`18` = auto in sibling project, `4` = stop demo old app, `5` = reset settings, `6` = temp demo old app, `0x0b` observed during first-connect/manual setup) |
| `0x5a` / 90 | `0x06` / 6 | 3 | Confirmed in old app: old 48-point auto curve update `[color, time_index, level]` |
| `0x5a` / 90 | `0x07` / 7 | 2 | Confirmed: manual brightness `[color, brightness]` |
| `0x5a` / 90 | `0x09` / 9 | 6 | Confirmed: set device time; old app uses day-of-month as third byte, newer notes may use weekday |
| `0xa5` / 165 | `0x01` / 1 | 2 | Confirmed in old app for Doctor time/duration `[time_hi, time_lo]`; additional newer-app uses may exist |
| `0xa5` / 165 | `0x02` / 2 | 1 | Confirmed in old app for Doctor power/runtime (`[1]` on, `[2]` off, `[3]` query) |
| `0xa5` / 165 | `0x19` / 25 | 14 | Confirmed by sibling project: add/update/delete auto schedule. Not clearly seen in current decompile call list. |
| `0x5a` / 90 | `0x04` / 4 | 1 | Confirmed in old app: query LED runtime/status `[1]` |
| `0x5a` / 90 | `0x0f` / 15 | 1 | Additional current-app command, unknown meaning |
| `0x5a` / 90 | `0x16` / 22 | 3 | Additional current-app command, unknown meaning |
| `0x5a` / 90 | `0x2b` / 43 | 4 | Additional current-app command, unknown meaning |
| `0x5a` / 90 | `0x2f` / 47 | 2 | Additional current-app command, unknown meaning |
| `0x5a` / 90 | `0x30` / 48 | 2 | Additional current-app command, unknown meaning |
| `0x5f` / 95 | `0x01` / 1 | 5 | Additional current-app/newer LED command, unknown meaning |
| `0x5f` / 95 | `0x02` / 2 | 5 | Additional current-app/newer LED command, unknown meaning |
| `0xa5` / 165 | `0x04` / 4 | 1 | Additional current-app command, unknown meaning |
| `0xa5` / 165 | `0x05` / 5 | 3 | Additional current-app command, unknown meaning |
| `0xa5` / 165 | `0x14` / 20 | dynamic | Additional current-app command, unknown meaning |
| `0xa5` / 165 | `0x15` / 21 | 6/dynamic | Dosing pump: timer/time `[channel, unknown_or_timer_type, hour, minute, 0, 0]`; capture confirms time fields but recurring schedules used second byte `0`; maps to current app `03692_sub_9bece4.dartpseudo` |
| `0xa5` / 165 | `0x1b` / 27 | 5/6/dynamic | Dosing pump: amount/recurrence command. 5-byte and 6-byte variants observed; amount is big-endian tenths-of-ml in final two bytes; maps to current app `03696_sub_9bfbc4.dartpseudo` / `03700_sub_9c1598.dartpseudo` |
| `0xa5` / 165 | `0x1f` / 31 | 1 | Additional current-app command, unknown meaning |
| `0xa5` / 165 | `0x20` / 32 | 3 | Dosing pump: auto/channel enable `[channel, catch_up_missed, active]` per issue #67; maps to current app `03701_sub_9c1694.dartpseudo` |
| `0xa5` / 165 | `0x2d` / 45 | 2 | Additional current-app command, unknown meaning |
| `0xa5` / 165 | `0x37` / 55 | 4 | Additional current-app command, unknown meaning |
| `0xa5` / 165 | `0x38` / 56 | 5/dynamic | Additional current-app command, unknown meaning |
| `0xa5` / 165 | `0x3d` / 61 | 6 | Additional current-app command, likely time/period/schedule-related based on code shape |
| `0xa5` / 165 | `0x41` / 65 | 4 | Additional current-app command, unknown meaning |
| `0xa5` / 165 | `0x42` / 66 | 5 | Additional current-app command, likely time/period/channel-related based on code shape |
| `0xa5` / 165 | `0xae` / 174 | dynamic | Additional current-app command, unknown meaning |

## Dosing pump mapping from GitHub issue #67 and BLE capture validation

Issue: <https://github.com/TheMicDiet/chihiros-led-control/issues/67>

Additional validation capture:

```text
../../Downloads/btsnoop_2025-10-11_16-01-34.jsonl
```

That capture contains real ATT writes to the Nordic UART RX/write characteristic and notifications from the TX/notify characteristic. It confirms the same frame format, checksum, command families, and most dosing-pump command mappings inferred from the Flutter app and issue #67.

### Dosing pump model/prefix

The fork/issue uses `DYDOSE...` device names for the 4-channel doser, matching binary strings found in the Flutter app:

```text
DYDOSE, DYNDOS, DosingPump, DosingPumpInfo, ChihirosDosingPump
```

### BLE handles observed in the capture

The capture validates the Nordic UART-style transport already described above:

```text
Service:          6e400001-b5a3-f393-e0a9-e50e24dcca9e
Write/RX char:    6e400002-b5a3-f393-e0a9-e50e24dcca9e  ATT value handle 0x0010
Notify/TX char:   6e400003-b5a3-f393-e0a9-e50e24dcca9e  ATT value handle 0x0012
TX CCCD:          00002902-0000-1000-8000-00805f9b34fb  ATT handle 0x0013
```

Notifications are enabled with:

```text
ATT Write Request to handle 0x0013: 01 00
```

Application commands are then written with ATT Write Request to handle `0x0010`; responses/status updates arrive as Handle Value Notifications from handle `0x0012`.

### Captured TX frame validation

Example command from the capture:

```text
a5 01 08 00 4d 20 00 00 01 65
```

Decoded:

```text
family/checksum-excluded byte: 0xa5
version/TX marker:             0x01
length:                        0x08 = 3 params + 5
message id:                    00 4d
mode:                          0x20
params:                        00 00 01
checksum:                      0x65
```

The checksum validates as XOR of bytes `[1..n-2]`. All captured TX application frames validate this way.

The capture also confirms that message IDs increment and avoid `0x5a`: one sequence goes `00 59` then `00 5b`, skipping `00 5a`.

### Amount encoding

Dosing amounts are encoded as a big-endian integer in tenths of a ml:

```python
units = round(ml * 10)
ml_hi = units >> 8
ml_lo = units & 0xff
```

This is equivalent to the earlier issue/fork formula:

```text
ml_hi = floor(ml / 25.6)
ml_lo = round((ml - ml_hi * 25.6) * 10)
```

Examples:

| ml | units | encoded `(ml_hi, ml_lo)` |
|---:|---:|---:|
| `10.5` | `105` | `(0x00, 0x69)` |
| `25.5` | `255` | `(0x00, 0xff)` |
| `25.6` | `256` | `(0x01, 0x00)` |
| `29.0` | `290` | `(0x01, 0x22)` |
| `36.0` | `360` | `(0x01, 0x68)` |
| `51.2` | `512` | `(0x02, 0x00)` |
| `60.0` | `600` | `(0x02, 0x58)` |
| `80.0` | `800` | `(0x03, 0x20)` |
| `999.9` | `9999` | `(0x27, 0x0f)` |

### Manual / amount command: `0xa5 / mode 0x1b`

Issue #67 reported a manual one-shot dose form:

```text
Command ID: 0xa5 / 165
Mode:       0x1b / 27
Params:     [channel, 0, 0, ml_hi, ml_lo]
```

This still maps to the Flutter app `0xa5 mode 0x1b` builders:

- `03696_sub_9bfbc4.dartpseudo`
- `03700_sub_9c1598.dartpseudo`

The BLE capture confirms `0xa5 / 0x1b`, but shows that there is more than one payload shape.

Captured 5-parameter variant:

```text
a5 01 0a 00 4f 1b 00 00 02 02 53 0c
```

Params:

```text
[channel=0, unknown0=0, subtype/status=2, ml_hi=0x02, ml_lo=0x53]
```

Amount:

```text
0x0253 = 595 tenths = 59.5 ml
```

So the 5-byte form is not always exactly `[channel, 0, 0, hi, lo]`; the third byte can be non-zero. Treat the non-amount bytes as command-specific until more manual-dose captures are available.

Captured 6-parameter scheduled/recurrence variant:

```text
a5 01 0b 00 58 1b 00 7f 01 01 02 58 6c
```

Params:

```text
[channel=0, weekdays=0x7f, recurrence_flag=1, unknown/status=1, ml_hi=0x02, ml_lo=0x58]
```

Amount:

```text
0x0258 = 600 tenths = 60.0 ml
```

The `weekdays` mask reuses the LED bitmask:

```text
Monday=64, Tuesday=32, Wednesday=16, Thursday=8, Friday=4, Saturday=2, Sunday=1, Everyday=127
```

Earlier notes called the fourth byte `completed_today`; the capture shows it as `1` for saved recurring entries, so its exact meaning should be treated as unknown/status rather than confirmed.

### Timer/time command: `0xa5 / mode 0x15`

Issue #67 described:

```text
Command ID: 0xa5 / 165
Mode:       0x15 / 21
Params:     [channel, timer_type, hour, minute, 0, 0]
```

The capture confirms the command and time fields, but suggests that the second byte is not simply `1 = 24-hour mode` for recurring entries. Captured recurring/everyday schedules used `0` in this byte while the recurrence flag was present in the `0xa5 / 0x1b` payload.

Examples:

```text
a5 01 0b 00 59 15 00 00 16 23 00 00 73
```

Params:

```text
[channel=0, unknown/type=0, hour=0x16, minute=0x23, 0, 0]
```

Meaning:

```text
channel 0, time 22:35
```

Another captured example:

```text
a5 01 0b 00 63 15 00 00 17 0f 00 00 64
```

Params:

```text
[channel=0, unknown/type=0, hour=23, minute=15, 0, 0]
```

So use:

```text
[channel, unknown_or_timer_type, hour, minute, 0, 0]
```

and treat the second byte as unresolved. In captured recurring schedules it was `0`.

### Auto/channel enable command: `0xa5 / mode 0x20`

Confirmed by issue #67, Flutter app call `03701_sub_9c1694.dartpseudo`, and the BLE capture.

```text
Command ID: 0xa5 / 165
Mode:       0x20 / 32
Params:     [channel, catch_up_missed, active]
```

Captured example:

```text
a5 01 08 00 57 20 00 00 01 7f
```

Params:

```text
[channel=0, catch_up_missed=0, active=1]
```

### Dosing pump automatic schedule save flow

Issue #67 observed this sequence, and `btsnoop_2025-10-11_16-01-34.jsonl` confirms the same prelude and core commands:

```text
0x5a / mode 0x04 / [1]
0x5a / mode 0x09 / [year-2000, month, weekday, hour, minute, second]
0x5a / mode 0x09 / [year-2000, month, weekday, hour, minute, second]
0xa5 / mode 0x04 / [4]
0xa5 / mode 0x04 / [5]
0xa5 / mode 0x20 / [channel, catch_up_missed, active]
0xa5 / mode 0x1b / [channel, weekdays, recurrence_flag, unknown/status, ml_hi, ml_lo]
0xa5 / mode 0x15 / [channel, unknown/type, hour, minute, 0, 0]
```

Important capture update: the OEM app sent the amount/recurrence command before the timer command:

```text
0xa5 / 0x20
0xa5 / 0x1b
0xa5 / 0x15
```

Earlier PR/issue notes sometimes used or proposed `0xa5 / 0x20`, then `0xa5 / 0x15`, then `0xa5 / 0x1b`. The captured OEM order should be preferred unless testing shows a device accepts both.

Captured full prelude/core example, channel `0`, every day, `60.0 ml`, time `22:35`:

```text
5a 01 06 00 51 04 01 53
5a 01 0b 00 52 09 19 0a 04 16 1b 05 4e
5a 01 0b 00 53 09 19 0a 04 16 1b 05 4f
a5 01 06 00 54 04 04 53
a5 01 06 00 55 04 05 53
a5 01 08 00 57 20 00 00 01 7f
a5 01 0b 00 58 1b 00 7f 01 01 02 58 6c
a5 01 0b 00 59 15 00 00 16 23 00 00 73
```

Decoded:

```text
5a/04 [1]                                      # start/order confirmation
5a/09 [year=2025, month=10, weekday=4, 22:27:05] # set device time, repeated
5a/09 [year=2025, month=10, weekday=4, 22:27:05]
a5/04 [4]                                      # confirmation
a5/04 [5]                                      # confirmation
a5/20 [0, 0, 1]                                # enable automation for channel 0
a5/1b [0, 127, 1, 1, 0x02, 0x58]               # every day, 60.0 ml
a5/15 [0, 0, 22, 35, 0, 0]                     # time 22:35
```

Another captured schedule set `80.0 ml` at `23:15`:

```text
a5 01 08 00 61 20 00 00 01 49
a5 01 0b 00 62 1b 00 7f 01 01 03 20 2f
a5 01 0b 00 63 15 00 00 17 0f 00 00 64
```

### Practical dosing pump control

#### Frame helper

```python
def checksum(frame_without_checksum: bytes) -> int:
    c = frame_without_checksum[1]
    for b in frame_without_checksum[2:]:
        c ^= b
    return c & 0xff


def make_tx(cmd_id: int, mode: int, msg_hi: int, msg_lo: int, params: list[int]) -> bytes:
    body = bytes([cmd_id, 0x01, len(params) + 5, msg_hi, msg_lo, mode, *params])
    return body + bytes([checksum(body)])


def amount_bytes(ml: float) -> tuple[int, int]:
    units = int(round(ml * 10))
    if not 2 <= units <= 9999:
        raise ValueError("dose must be 0.2..999.9 ml")
    return (units >> 8) & 0xff, units & 0xff
```

Avoid `0x5a` in message ID bytes and, for older firmwares, avoid checksum `0x5a` by incrementing the message ID and rebuilding.

#### Manual one-shot dose

The issue/fork manual command is still a reasonable starting point:

```python
def manual_dose_frame(msg_hi, msg_lo, channel_0_based, ml):
    hi, lo = amount_bytes(ml)
    return make_tx(0xa5, 0x1b, msg_hi, msg_lo, [channel_0_based, 0, 0, hi, lo])
```

Example: channel 0, 10.5 ml, message id `00 01`:

```text
a5 01 0a 00 01 1b 00 00 00 00 69 78
```

Example: channel 1, 10.5 ml, message id `00 01`:

```text
a5 01 0a 00 01 1b 01 00 00 00 69 79
```

However, the capture shows a 5-parameter `0xa5 / 0x1b` amount variant with third parameter `0x02`, so verify manual one-shot behavior against the target firmware.

#### Automatic schedule frame builders

Captured OEM-like schedule order:

```python
def enable_channel_frame(msg_hi, msg_lo, channel_0_based, catch_up=False, active=True):
    return make_tx(0xa5, 0x20, msg_hi, msg_lo, [
        channel_0_based,
        1 if catch_up else 0,
        1 if active else 0,
    ])


def schedule_amount_frame(msg_hi, msg_lo, channel_0_based, weekdays, ml, recurrence_flag=1, status=1):
    hi, lo = amount_bytes(ml)
    return make_tx(0xa5, 0x1b, msg_hi, msg_lo, [
        channel_0_based,
        weekdays,
        recurrence_flag,
        status,          # observed as 1 in the capture; exact meaning unknown
        hi,
        lo,
    ])


def schedule_time_frame(msg_hi, msg_lo, channel_0_based, hour, minute, unknown_type=0):
    return make_tx(0xa5, 0x15, msg_hi, msg_lo, [
        channel_0_based,
        unknown_type,    # observed as 0 in recurring schedule captures
        hour,
        minute,
        0,
        0,
    ])
```

Use the full prelude before these frames when saving through the same flow as the OEM app:

```text
1.  0x5a / mode 0x04 / [1]
2.  0x5a / mode 0x09 / [time]
3.  0x5a / mode 0x09 / [time]
4.  0xa5 / mode 0x04 / [4]
5.  0xa5 / mode 0x04 / [5]
6.  0xa5 / mode 0x20 / [channel, catch_up, active]
7.  0xa5 / mode 0x1b / [channel, weekdays, 1, 1, amount_hi, amount_lo]
8.  0xa5 / mode 0x15 / [channel, 0, hour, minute, 0, 0]
```

In real usage, increment the message ID for every frame and avoid `0x5a` message ID bytes.

### Dosing pump receive/notification frames

Issue #67 and the capture both show notification frames on the Nordic UART TX characteristic:

```text
UUID: 6e400003-b5a3-f393-e0a9-e50e24dcca9e
ATT value handle: 0x0012
```

Observed RX frame family:

```text
0x5b / 91
```

Captured notification modes include:

```text
0x0a, 0xfe, 0x1e, 0x22
```

Examples:

```text
5b 01 0a 00 01 1e 02 b2 27 7c 08 6a 07 12 f7
5b 01 0a 00 01 22 02 58 08 04 01 53 00 c3 7f
```

Mode `0x22` notifications contain amount-like byte pairs. In the captured recurring schedule examples, `02 58` corresponds to the scheduled `60.0 ml` and `03 20` corresponds to `80.0 ml`.

Earlier issue examples interpreted `0x22` as:

```text
5b 01 0a msg_hi msg_lo 22  h0 l0 h1 l1 h2 l2 h3 l3  checksum
```

with each pair decoded as `hi * 25.6 + lo / 10.0` ml / big-endian tenths-of-ml. This remains plausible for at least part of the payload, but the captured notifications also contain other changing/status fields. Treat notification decoding as provisional.

Observed/proposed notification modes:

| RX command | Mode | Payload | Meaning |
|---:|---:|---|---|
| `0x5b` | `0x22` | amount/status-like fields; includes amount pairs such as `02 58` = 60.0 ml | per-channel/current schedule or daily total status, exact layout unresolved |
| `0x5b` | `0x1e` | status-like fields, includes amount-ish pairs | alternate status/total window, exact distinction unknown |
| `0x5b` | `0x34` | optional `[hdr0, ch_count]` + 4 × big-endian u16 `ml*100` per PR #2 comments | proposed totals query/report mode; not seen in this capture |
| `0x5b` | `0x0a` | short status-like payload; old LED firmware 23 uses bytes `[6..7]` as runtime minutes | old LED runtime/status response; other devices still provisional |
| `0x5b` | `0xfe` | long status/config snapshot; old LED firmware 23 includes auto schedule triples `(hour, minute, level)` | old LED auto schedule/status snapshot; other devices still provisional |

RX length/checksum behavior appears firmware/mode-dependent or mode-dependent. Confirmed old LED firmware 23 behavior: short `0x5b / 0x0a` validates with the same XOR rule, while long `0x5b / 0xfe` does not validate if the final byte is treated as a checksum. Do not rely on TX checksum/length assumptions for all RX frames.

### Read daily totals

Try querying with:

```text
0x5b / mode 0x34 / no params   # PR #2 later preferred totals query; not validated in this capture
0x5b / mode 0x22 / no params   # earlier issue/fallback query
0x5b / mode 0x1e / no params   # earlier fallback query
```

Example query for `0x5b / 0x22`, message id `00 01`:

```text
5b 01 02 00 01 22 20
```

Example query for `0x5b / 0x34`, message id `00 01`:

```text
5b 01 02 00 01 34 36
```

Earlier expected notification shape:

```text
5b 01 0a msg_hi msg_lo 22  h0 l0 h1 l1 h2 l2 h3 l3  checksum
```

For that variant, decode each pair as `hi * 25.6 + lo / 10.0` ml.

Example:

```text
5b 01 0a 00 01 22 00 21 00 42 00 63 00 84 4b
```

Means:

```text
ch0 = 3.3 ml, ch1 = 6.6 ml, ch2 = 9.9 ml, ch3 = 13.2 ml
```

PR #2's later proposed `0x34` parser instead expects parameters like:

```text
00 04  00 30  01 20  13 50  16 30
^^^^^  ^^^^^  ^^^^^  ^^^^^  ^^^^^
hdr    ch1    ch2    ch3    ch4
```

with header `[0, 4]` and each channel decoded as big-endian `u16 / 100.0` ml. This still needs validation against a capture containing `0x34` traffic.

### GitHub PR #2 notes

PR: <https://github.com/MankiniChykan/chihiros-led-control/pull/2>

This PR is mostly a package/CLI restructuring branch, but it is useful because it codifies the dosing-pump sequence from issue #67.

Relevant files in contributor branch `Martin11180:ch4--support-ctl`:

```text
custom_components/chihiros/chihiros_doser_control/main/protocol.py
custom_components/chihiros/chihiros_doser_control/main/dosingpump.py
custom_components/chihiros/chihiros_doser_control/main/doser_device.py
custom_components/chihiros/chihiros_doser_control/main/chihirosdoserctl.py
custom_components/chihiros/chihiros_led_control/main/chihirosctl.py
```

Protocol points confirmed by PR #2 code and/or the capture:

- `UART_RX` / write UUID is `6E400002-B5A3-F393-E0A9-E50E24DCCA9E`.
- `UART_TX` / notify UUID is `6E400003-B5A3-F393-E0A9-E50E24DCCA9E`.
- Amount commands use `0xa5 / mode 0x1b`.
- Amount encoding is big-endian tenths of ml / the equivalent 25.6 ml bucket + 0.1 ml remainder scheme.
- Auto schedule uses the reconstructed prelude: `5a/04`, two `5a/09` time syncs, `a5/04 [4]`, `a5/04 [5]`, then `a5/20`, `a5/1b`, `a5/15` in the captured OEM order.
- Channel IDs are sent as `0..3` on the wire.

PR #2 also adds a `Doser` discovery stub with model codes like `DYDOSED`, `DYDOSE`, and `DOSER`, which supports the device-prefix mapping inferred from the app strings.

Caveats from PR #2:

- The PR was open and still under active debugging when inspected.
- Several later comments discuss CTL path/import problems and missing notification/ack handling.
- The branch's `sensor.py` still had a kill switch enabled, while later comments proposed replacing it.
- Some PR code has implementation issues unrelated to protocol semantics, so use it as protocol corroboration rather than production-ready code.
- PR/issue command ordering may differ from the OEM capture; prefer the captured OEM order unless testing shows otherwise.

#### PR #2 totals/readback update

The branch protocol originally queries totals with `0x5b` modes `0x22` and `0x1e`, and can fall back to `0xa5` modes `0x22`/`0x1e`.

Later PR comments clarify that "0x91 totals" means decimal `91`, i.e. command byte `0x5b`, not a new command family. Those comments prefer:

```text
Query:    0x5b / mode 0x34 / []      # preferred
Fallback: 0x5b / mode 0x22 / []      # alternate firmware
```

They also propose a stricter notification parser for:

```text
[0x5b, 0x01, len, msg_hi, msg_lo, mode, hdr0, ch_count, h1,l1, h2,l2, h3,l3, h4,l4, checksum]
```

where each `(hi, lo)` is big-endian `ml * 100`. This differs from the earlier issue #67 interpretation of `0x22` as 4 pairs in tenths of ml using the 25.6-bucket representation. Treat both as possible firmware/report variants until verified with actual captures from the same device/firmware.

### Verification conclusion

The issue #67 / PR #2 protocol is consistent with the reverse-engineered Flutter app and is now partially validated by `btsnoop_2025-10-11_16-01-34.jsonl`:

- Same Nordic UART BLE transport and handles.
- Same TX frame format/checksum.
- Same `0xa5` command family.
- Issue modes `21`, `27`, and `32` are all present in the Flutter app command-builder call list and in the capture.
- The capture confirms the multi-stage save prelude and `0xa5/0x20`, `0xa5/0x1b`, `0xa5/0x15` core schedule commands.
- The capture refines the schedule order to `0xa5/0x20`, `0xa5/0x1b`, `0xa5/0x15`.

Remaining uncertainty: exact meanings of some non-amount bytes in `0xa5/0x1b`, the second byte of `0xa5/0x15`, and most `0x5b` notification payload layouts.

## Payload observations

The payload is command-specific.

Examples:

### `0xa5 / 0x01`

Reference: `03674_sub_9b95cc.dartpseudo`; old-app reference: `LightsController.setDoctorTime`.

- Actual payload length: `2`
- Encodes a 16-bit value split high/low. In the old app this is the Doctor operation time/duration from `doctor.html`'s `time` field.

### `0xa5 / 0x04`

Reference: `03705_sub_9c1c54.dartpseudo`

- Payload length: `2`
- Payload includes `0x0a`.

### `0xa5 / 0x05`

References: `03708_sub_9c2478.dartpseudo`, `03690_sub_9be5b8.dartpseudo`

- Payload length: `6`
- Includes a user value plus repeated `0xff`-like marker/default values.

### Larger schedule/scene payloads

References:

- `03613_sub_9a2800.dartpseudo` -> 12-byte payload, command `0x3d`
- `03692_sub_9bece4.dartpseudo` -> variable payload, command `0x15`
- `03783_sub_9e2c54.dartpseudo` -> 10-byte payload, command `0x42`

These paths manipulate times, periods, channels, and levels. Static decompilation gives the structure but not a clean symbolic naming of each byte.

## Supported BLE devices

Embedded JSON device templates found in `libapp.so` strings:

| Device label | Category | Device type | Channels / notes |
|---|---|---|---|
| `DYA` | `A series` | `BleLed` / `SeaLed` | 1 channel, max `[100]` |
| `New C` | `New C` | `BleLed` / `SeaLed` | 1 channel, max `[100]` |
| `DYWRGB` | `WRGB2` | `BleLed` / `SeaLed` | 3 channels, max `[100,100,100]` |
| `DYRGBA+` | `RGB+APLUS` | `BleLed` / `SeaLed` | 3 channels, max `[100,100,100]` |
| `DYLED` | `RGB VIVID` | `BleLed` | 3 channels |
| `DYLED` | `X300` | `BleLed` | 2 channels |
| `DYLED` | `Commander X` | `BleLed` | 1 channel |
| `DYLED` | `Commander 4` | `BleLed` / `SeaLed` | 4 channels |
| `DYSEA` | `SEA_LED` | `SeaLed` | 4 channels |
| `RGB VIVID2` | `RGB VIVID2` | `NewBleLed` / `SeaLed` | 3 channels, max `[115,130,200]`, max power `120` |
| `Chihiros gateway` | `Dy gateway` | `Gateway` | BLE gateway |
| `Chihiros doctor` | `Doctor` | `BleDoctor` | doctor device |
| `Doctor mate` | `Doctor mate` | `BleDoctor` | max volume `125` |
| `Co2Generator` | `Co2Generator` | `Co2Generator` | CO2 device |

Advertisement/device-name prefixes also found:

```text
DYA, DYWRGB, DYNWRGB, DYARGB, DYNARGB, DYLED, DYSEA,
DYRGBV, DYVVD3, DYNVVD, DYNCO2, DYCO2, DYAPRCO2,
DYDOSE, DYNDOS, DYFAN, DYNFAN, DYHET, DYCHIL,
DYGATE, DYNGATE, DYMIXR, DYPWR, DYDOC, DYNDOC
```

These indicate support for LEDs, gateways, doctors, CO2 devices, dosing pumps, fans, heaters, chillers, magnetic stirrers, power/multi-outlet devices, and related newer devices.

### Confirmed working LED model codes from `chihiros-led-control`

That project maps the advertised device name prefix to a model class by stripping the last 12 characters from the BLE name (`device_name[:-12]`). Confirmed/tested model code mappings:

| Model | Prefixes | Channels/colors |
|---|---|---|
| A II | `DYNA2`, `DYNA2N` | white `0` |
| C II | `DYNC2N` | white `0` |
| C II RGB | `DYNCRGP`, `DYNCRGB` | red `0`, green `1`, blue `2` |
| WRGB II | `DYNWRGB`, `DYNW30`, `DYNW45`, `DYNW60`, `DYNW90`, `DYNW12P` | red `0`, green `1`, blue `2` |
| WRGB II Pro | `DYWPRO30`, `DYWPRO45`, `DYWPRO60`, `DYWPRO80`, `DYWPRO90`, `DYWPR120` | red `0`, green `1`, blue `2`, white `3` |
| WRGB II Slim | `DYSILN`, `DYSL30`, `DYSL45`, `DYSL60`, `DYSL90`, `DYSL120`, `DYSL12` | red `0`, green `1`, blue `2` |
| Universal WRGB | `DYU550`, `DYU600`, `DYU700`, `DYU800`, `DYU920`, `DYU1000`, `DYU1200`, `DYU1500` | red `0`, green `1`, blue `2`, white `3` |
| Commander 1 | `DYCOM` | white/red `0`, green `1`, blue `2` |
| Commander 4 | `DYLED` | white/red `0`, green `1`, blue `2` |
| Tiny Terrarium Egg | `DYDD` | red `0`, green `1` |
| Z Light TINY | `DYSSD`, `DYZSD` | white `0`, warm `1` |

## Control capabilities seen in method names

Important strings/methods:

```text
sendControllerCode
sendSettingCode
sendWeekSettingCode
setSeaLedAutoCode
setAutoCode
setManualCode
setTimeCode
setHeaterCode
setNewCo2ManualCode
setDCPumpScene
setManualPumpLevel
setManualPumpMode
setFanSpeed
setFanTemperature
sendAddSubDeviceCode
sendRemoveSubDeviceCode
sendResetCode
resetToBle
```

Supported controls appear to include:

- LED manual channel levels
- LED auto schedules
- LED scenes/new scenes
- device time/program settings
- heater temperature/protection/auto/calibration
- fan speed, eco mode, start/stop temperature, alerts
- DC pump manual level, manual mode, scenes, feed mode
- dosing pump manual dosing, calibration, dosing schedules
- CO2 manual and auto/scene control
- feeder feed modes/schedules
- gateway sub-device add/remove/reset
- WiFi/gateway setup and reset

## Remaining unknowns

The static decompilation identifies the frame structure, command families, command IDs, and supported devices. It does not fully name every payload field.

Best next step: capture BLE writes while changing one setting at a time in the app, e.g. one LED slider or one schedule entry. With the frame format above, mapping individual payload bytes should be straightforward.

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
5a 01 07 msg_hi msg_lo 07 channel brightness
```

## Message IDs And Reserved Bytes

The two message ID bytes are maintained by the app and incremented for each
command.

Older LED protocol paths avoid the reserved byte `0x5a` in message ID bytes,
parameters, and checksums:

- Message ID high and low bytes skip `0x5a` (the legacy command header). The
  2.8.59 app's sequence counters skip **only** `0x5a` (`0x59` → `0x5b`), so
  `0x5b` *can* appear as a sequence byte even though it is the notification
  header.
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

### Max Brightness Per Channel

The vendor app's offline device registry (`config/device_offline.dart`)
assigns a per-channel `max_level` to every LED device. Every LED model this
repository supports is registered as a plain `BleLed` with
`max_level: [100, ...]`, so the wire level `0..100` is both the UI percentage
and the channel maximum. The registry also defines the channel layout used by
`SUPPORTED_MODELS`:

| Device label(s) | Category | `device_type` | `max_level` | Channels |
| --- | --- | --- | --- | --- |
| `DYLED` | Commander 4 / 四路控制器 | `BleLed` | `[100, 100, 100, 100]` | 4 (red/green/blue/white) |
| `DYNLED` | Commander 4 / 四路控制器 | `SeaLed` | `[100, 100, 100, 100]` | 4 (red/green/blue/white) |
| `DYARGB`, `DYRGBA+`, `DYNARGB` | RGB+APLUS | `BleLed` | `[100, 100, 100]` | 3 (RGB) |
| `DYREE` | RGB VIVID | `BleLed` | `[100, 100, 100]` | 3 (RGB) |
| `DYONE` | Commander X / 一路控制器 | `BleLed` | `[100]` | 1 |
| `DYTWO` | X300 | `BleLed` | `[100, 100]` | 2 (white/warm) |
| `DYA` | A series / A系列 | `BleLed` | `[100]` | 1 |
| `DYWRGB` | WRGB2 | `BleLed` | `[100, 100, 100]` | 3 (RGB) |
| `DYNWRGB`, `DYNW90`, `DYNW30/45/60/12P` | WRGB2 | `SeaLed` | `[100, 100, 100]` | 3 (RGB) |
| `DYC` | New C / C系列 | `BleLed` | `[100]` | 1 |
| `DYNC2`, `DYNC2N` | New C / C II | `SeaLed` | `[100]` | 1 |
| `DYSEA` | SEA_LED / 海水灯 | `SeaLed` | `[100, 100, 100, 100]` | 4 (WRGB) |
| `DYRGBV` | RGB VIVID2 | `NewBleLed` | `[115, 130, 200]` | 3 (RGB) |
| `DYNVVD`, `DYNV` | RGB VIVID2 | `SeaLed` | `[115, 130, 200]` | 3 (RGB) |
| `DYWPRO30–90`, `DYWPR120` | WRGB II Pro | `SeaLed`¹ | `[100, 100, 100, 100]` | 4 (WRGB) |
| `DYSILN`, `DYSL30–120` | WRGB II Slim | `SeaLed`¹ | `[100, 100, 100]` | 3 (RGB) |
| `DYU550–1500` | Universal WRGB | `SeaLed`¹ | `[100, 100, 100, 100]` | 4 (WRGB) |
| `DYVVD3` | WRGB VIVID III | `SeaLed`² | `[100, 100, 100, 100]` | 4 (WRGB) + fan |

¹ device_type is server metadata (not in the 2.8.59 offline registry); SeaLed
is inferred from the vendor's new-gen convention and the numeric suffix being
light length (see `models.py`).

² binary-verified: the VIVID III's device_type is `"NewVivid3"` (factory key
`pp+0xfe08` → `NewVivid3` class, which has no `setAuto` override), and
`"NewVivid3"` is not in `{BleLed, NewBleLed}`, so `_judgeNewLed` sets
`field_147` true → SeaLed family.

Findings from the 2.8.59 app decompilation:

- The model factory maps `BleLed`, `NewBleLed`, and `SeaLed` to the base
  `ChihirosLed` class, whose `setManual(channel, level)` sends the level
  verbatim on the wire (`0x5a / 0x07`). Even `RGB VIVID2` with a `[115, 130,
  200]` max level therefore uses `0..100` wire values; `max_level` is metadata
  (used by the app for power/display estimation), not a wire scale.
- Only the `NewA2Led` device type maps to the scaling `NewASeriesLed` class,
  which converts `level` to `max(1, floor(level * 100 / max_level[channel]))`
  before sending. No repository-supported model uses this class.
- This matches the existing behaviour: brightness validation is `0..100` and
  schedule snapshot levels above `100` are treated as invalid.

## Auto Mode

Auto mode can be enabled with:

- Command ID: `0x5a` / `90`
- Mode: `0x05` / `5`
- Parameters: `[18, 255, 255]`

The same payload is the vendor app's `switchToScene()` frame: it activates the
stored auto schedule. Manual mode uses the vendor app's `switchToManual()`
frame `[11, 255, 255]` (see below). The vendor app's own `switchToAuto()`
frame uses `[3, 255, 255]`; this repository keeps the `[18, 255, 255]`
schedule-driven form used by LED devices.

Manual mode can be entered with:

- Command ID: `0x5a` / `90`
- Mode: `0x05` / `5`
- Parameters: `[11, 255, 255]`

Auto mode and its settings can be reset with:

- Command ID: `0x5a` / `90`
- Mode: `0x05` / `5`
- Parameters: `[5, 255, 255]`

Other observed `0x5a / 0x05` first parameters:

| First parameter | Observed meaning |
| ---: | --- |
| `3` | `switchToAuto` (the 2.8.59 app's own auto-mode switch) |
| `4` | Stop/exit demo in the old app |
| `5` | Reset auto settings |
| `6` | Temporary/new-firmware demo in the old app |
| `11` / `0x0b` | `switchToManual`; sent by the vendor app before manual slider control on the WRGB VIVID III |
| `17` | `autoFan` (the 2.8.59 app's fan-auto-mode switch, `[0x11, 0xFF, 0xFF]`) |
| `18` | `switchToScene` / enable auto schedule |
| `40` | `resetLedQuick`, reset scene data (used after scene delete) |
| `48` / `49` | VIVID III `vvd3tempProtect` off / on (`[0x30\|0x31, 0xFF, 0xFF]`) |
| `49` / `50` | VIVID III `vvd3BluetoothLed` off / on (`[0x31\|0x32, 0xFF, 0xFF]`) |
| `0x22` / `0x23` | `setFanEco` on / off (standalone fan echo mode) |
| `0x2c` / `0x2d` | `setTemType` °C / °F (heater) |
| `0x2e` / `0x2f` | `setHeaterAuto` on / off |
| `58` | `heaterResetWorkTime` |

## Auto Schedule Settings

Create or update an automatic schedule setting:

- Command ID: `0xa5` / `165`
- Mode: `0x19` / `25`
- Parameters:
  `[sunrise hour, sunrise minute, sunset hour, sunset minute, ramp up minutes, weekdays, brightness values by channel id, 255 padding...]`

The parameter payload is 14 bytes total. Brightness values are written in
protocol channel order. RGB models use red, green, and blue brightness fields:

```text
[red_brightness, green_brightness, blue_brightness]
```

True WRGB models use all four channel fields:

```text
[red_brightness, green_brightness, blue_brightness, white_brightness]
```

For non-RGB models, put the desired white brightness in the red brightness field
and set the other two brightness fields to `255`:

```text
[white_brightness, 255, 255]
```

To delete or deactivate a setting, send the same schedule metadata with every
remaining brightness/padding slot set to `255`. Since the parameter payload is
always 14 bytes, this means eight trailing `0xff` bytes:

```text
[sunrise hour, sunrise minute, sunset hour, sunset minute, ramp up minutes,
 weekdays, 255, 255, 255, 255, 255, 255, 255, 255]
```

Captured deletion of an everyday `02:30` to `05:10` schedule with a one-minute
ramp and message ID `0x0017`:

```text
a5 01 13 00 17 19 02 1e 05 0a 01 7f ff ff ff ff ff ff ff ff 71
```

Here, `0x13` is the command length, `0x19` is the schedule mode, `0x7f`
selects every weekday, and `0x71` is the valid XOR checksum.

Only one setting can be configured per day, so settings cannot conflict. There
is a maximum of 7 settings.

### Auto Curve Points (0x5a / 0x06)

The vendor app stores the auto curve as one frame per point per channel
(`ChihirosLed::setAuto`, verified in `chihiros_led.dart`). The time encoding
depends on the device family (`field_147 = device_type not in
{"BleLed", "NewBleLed"}`):

- **SeaLed family** (`DYNLED` Commander 4, `DYSEA`, `DYNVVD`/`DYNV`,
  `DYNARGB`, `DYNWRGB`/`DYNW90`, `DYNA2`, `DYNC2`): `[channel, hour, minute,
  level]`
- **BleLed family** (`DYLED` Commander 4, `DYCOM`, `DYONE`, `DYTWO`, `DYWRGB`,
  ...): `[channel, minutes // 30, level]`, rounded to the nearest 30-minute
  slot (a remainder above 14 advances to the next slot); the app's BleLed
  capacity allows up to 96 slots for 48-hour cross-day curves

Payload bytes are sent **verbatim**: the 2.8.59 app's `formatData` does not
escape `0x5A` payload bytes, so a level of 90 stays `0x5A` on the wire. The
`create_auto_point_command` helper in this repository follows the app here
(`avoid_reserved_byte=False`). A full curve is a burst of these frames, 30 ms
apart, sent in one BLE transaction; clear the stored curve first with
`0x5a / 0x05 [5, 255, 255]`. The Home Assistant `chihiros.set_auto_curve`
service writes curves in this format. (Note: the BleLed/SeaLed *model lists*
above are inferred from each model's registry `device_type`; the encoding
selection itself is binary-verified.)

Single-channel A2 devices also have a distinct point-based custom-curve
protocol using a four-parameter `0x5a / 0x06` command. It is not interchangeable
with the `0xa5 / 0x19` schedule described above. See
[A2 Custom Schedule Protocol](a2-custom-schedule-protocol.md) for the captured
setup sequence, point encoding, and remaining unknowns.

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
with mode byte `0xfe`. Saved auto curve points start at offset `25` and use
hour/minute/level triples regardless of the model's channel count. The level is
the schedule's common brightness and is exposed under every named RGB or WRGB
channel by this repository:

```text
0d 0f 00  -> 13:15 level 0
0d 2d 64  -> 13:45 level 100
15 0f 64  -> 21:15 level 100
15 2d 00  -> 21:45 level 0
```

An all-zero triple is an unused schedule slot and is omitted. For example, a
captured WRGB payload decodes as:

```text
09 00 00  -> 09:00 level 0
09 01 05  -> 09:01 level 5
10 3b 05  -> 16:59 level 5
11 00 00  -> 17:00 level 0
11 01 41  -> 17:01 level 65
15 3b 41  -> 21:59 level 65
16 00 00  -> 22:00 level 0
00 00 00  -> unused slot
```

Unlike the short runtime response, the captured `0x5b / 0xfe` snapshot did not
validate when the final byte was treated as the simple XOR checksum. Treat this
snapshot as a status payload whose checksum/trailer is not yet confirmed.

## Other Confirmed LED Commands

| Command ID | Mode | Parameters | Meaning |
| ---: | ---: | --- | --- |
| `0x5a` / `90` | `0x04` / `4` | `[1]` | Query LED runtime/status |
| `0x5a` / `90` | `0x06` / `6` | `[channel, hour, minute, level]` | Auto-curve point, **SeaLed family** (per the 2.8.59 app's registry `device_type`; encoder verified in the binary: `ChihirosLed::setAuto` @ `0xe7a680` calls `setSeaLedAutoCode` for SeaLed. Includes `DYNLED` Commander 4, `DYSEA`, `DYNVVD`/`DYNV`, `DYNARGB`, `DYNWRGB`/`DYNW90`, `DYNA2`, `DYNC2`. Captured on a real `DYNA2` — see `a2-custom-schedule-protocol.md`) |
| `0x5a` / `90` | `0x06` / `6` | `[channel, time_index, level]` | Auto-curve point, **BleLed/NewBleLed family** (per the 2.8.59 app's registry `device_type`; encoder verified in the binary: `setAutoCode` for BleLed. `time_index = minutes/30` with the app's rounding rule; up to 96 slots for 48-hour cross-day curves. Includes `DYLED` Commander 4, `DYCOM`, `DYONE`, `DYTWO`, `DYWRGB`, `DYRGBV`, ...) |
| `0x5a` / `90` | `0x07` / `7` | `[color, brightness]` | Manual brightness |
| `0x5a` / `90` | `0x09` / `9` | `[year - 2000, month, date_field, hour, minute, second]` | Set device time |
| `0x5a` / `90` | `0x0f` / `15` | `[speed_percent]` | Fan speed, `0` to `100`; confirmed on WRGB VIVID III |
| `0xa5` / `165` | `0x19` / `25` | 14 bytes | Add, update, or delete auto schedule |

## Fan Status Notifications

Fan-equipped devices such as the WRGB VIVID III (advertised name prefix
`DYVVD3`) push a periodic status notification roughly every three seconds while
connected. It uses the `0x5b` header with mode `0x0b`:

```text
5b 1b 10 00 01 0b 02 58 19 00 01 00 00 00 00 00 48 22
```

| Offset | Name | Observed meaning |
| ---: | --- | --- |
| `1` | Firmware/protocol version | `0x1b` / `27` on the captured VIVID III |
| `6..7` | Fan RPM | Big-endian measured fan speed; `0x0258 = 600` rpm at 25%, about `1980` rpm at 100% |
| `8` | Temperature | Whole degrees Celsius |
| `16` | Uptime counter | Increments between periodic notifications |

The trailing byte is not a valid XOR checksum for this mode, so parsers should
treat bytes after offset `8` as opaque. Fan speed is set with the
`0x5a / 0x0f` command listed above; measured RPM follows the set percentage.

Some VIVID III firmware revisions instead report the fan readout with the
newer `0xb6` header and mode `0x16` (the vendor app's `vvd3_fan_widget`):
`rpm = (data[6] << 8) | data[7]`, `temperature = data[8]`. This repository
parses both frame shapes into fan notifications.

Fan control on the VIVID III has a manual speed and a temperature-controlled
auto mode (both confirmed in `dataMaker.dart`):

| Action | Frame | Payload |
| --- | --- | --- |
| Manual speed | `0x5a / 0x0f` | `[speed_percent]` (clamped to a 25 % minimum) |
| Auto mode | `0x5a / 0x05` | `[0x11, 0xff, 0xff]` — `LedInfo::setFanAuto` → `autoFan` |
| Start/stop temps | `0xa5 / 0x2d` | `[start ?? 38, stop ?? 33]` — `vvd3FanStartStopTemp` |

In auto mode the fan starts at the start temperature and stops at the stop
temperature (defaults 38 / 33 °C, a 5 °C hysteresis). The vendor app tracks
`fan_mode` app-side (`"auto"` or the manual speed string); the integration
exposes Auto/Manual presets on the fan entity and start/stop temperature
numbers.

### VIVID III Switches

The 2.8.59 app's `Vivid3Info` (beyond the shared `LedInfo` set) adds two
boolean settings, both binary-verified in the encoder disassembly. Neither has
a readback notification, so state is tracked optimistically:

| Action | Frame | Payload |
| --- | --- | --- |
| Temperature protection | `0x5a / 0x05` | `[0x31\|0x30, 0xff, 0xff]` — `vvd3tempProtect`, byte 0 = 49 (on) / 48 (off) |
| Indicator LED | `0x5a / 0x05` | `[0x32\|0x31, 0xff, 0xff]` — `vvd3BluetoothLed`, byte 0 = 50 (on) / 49 (off) |

Example frames (msg id `00 01`):

```text
5a 01 08 00 01 05 31 ff ff 3c   temperature protection ON
5a 01 08 00 01 05 30 ff ff 3d   temperature protection OFF
5a 01 08 00 01 05 32 ff ff 3f   indicator LED ON
5a 01 08 00 01 05 31 ff ff 3c   indicator LED OFF
```

The integration exposes both as optimistic switch entities that restore their
last state across restarts (without re-sending it to the device).

The VIVID III also sends a constant `0x5b` notification with mode `0x36` after
each auth/status query. Its payload is static and its meaning is unknown; it
can be ignored.

Captured WRGB VIVID III channel order matches other true WRGB models: channel
`0` red, `1` green, `2` blue, `3` white.

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

Chihiros dosing pumps use the same Nordic UART-style transport, frame format,
and checksum. The current implementation recognizes devices whose BLE name
starts with `DYDOSE` and supports manual one-shot dosing.

Dosing pump interactions use the normal connection prelude (`90 / 4`, then
two `90 / 9` time sync commands), followed by two dosing auth commands:

| Command ID | Mode | Parameters | Meaning |
| ---: | ---: | --- | --- |
| `165` | `4` | `[4]` | Dosing auth step 1 |
| `165` | `4` | `[5]` | Dosing auth step 2 |

Manual dose command:

| Command ID | Mode | Parameters | Meaning |
| ---: | ---: | --- | --- |
| `165` | `27` | `[pump, 0, 0, ml_hi, ml_lo]` | Dose one pump channel immediately |

Parameter details:

- `pump`: zero-based pump index, `0` to `7` (the vendor app exposes eight channels).
- Dose volume is encoded in tenths of a milliliter as `ml_hi * 25.6 mL + ml_lo * 0.1 mL`.
- The Home Assistant integration currently accepts `0.2 mL` to `999.9 mL`.

Python equivalent:

```python
tenths_ml = round(ml * 10)
ml_hi, ml_lo = divmod(tenths_ml, 256)
```

Example for pump `0`, `2.0 mL`:

```text
165 1 10 0 6 27 0 0 0 0 20 2
```

### Dosing Pump Notifications

Dosing pumps report counters with the newer `0xb6` header (not the `0x5b` LED
header). Two frame types are parsed by this repository:

| Byte 5 | Meaning | Payload |
| ---: | --- | --- |
| `0x3c` / `60` | Lifetime totals | Per channel `i`: `(data[6+2i] << 8 | data[7+2i]) * 100` µL |
| `0x44` / `68` | Dosed today | Per channel `i`: `(data[6+2i] << 8 | data[7+2i]) * 100` µL |

Example lifetime frame (channels `0..1` = `105.5 mL`, `0 mL`):

```text
b6 10 10 00 01 3c 04 1f 00 00
```

The Home Assistant integration stores these device-reported counters in the
coordinator data and exposes them as attributes on the per-pump dosing sensors
(`device_total_ml`, `device_dosed_today_ml`), so doses made from the pump or
the vendor app are reflected even though the daily/lifetime sensors themselves
stay locally tracked for immediate feedback.

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

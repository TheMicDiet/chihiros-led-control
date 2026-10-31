# Chihiros Bluetooth Protocol

These notes describe the application-level BLE protocol used by supported
Chihiros LED devices. They are based on the working implementation in this
repository plus reverse-engineering notes from the old Chihiros Magic app, the
newer Flutter app, and BLE captures.

Corrections tracked in [issue #133](https://github.com/TheMicDiet/chihiros-led-control/issues/133)
are grounded in My Chihiros **2.8.59, versionCode 759**, Dart 3.5.3.
The inspected `libapp.so` SHA-256 is
`d9c3de6693760fc1f4900a7f92bca8a0a15181f6d2ab6161c697cc142780285e`.
ELF execution establishes command construction and app routing, not successful
operation on every physical device. Representative hardware/HCI comparison is
still required for the changed indicator/protection commands and legacy framing.

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

When FFE1 supports notifications, the app uses **that same characteristic** for
writing and notification subscription. Nordic UART instead pairs write `...0002`
with notify `...0003`; these layouts must not be mixed.
`8ec90003-f315-4f60-9fb8-838830daea50` belongs to the app's **DFU** map, not
ordinary control/status notifications.

The official app uses **write-with-response** followed by a **30 ms queue gap**
(`provider/bluetooth.dart @ 0x91349c–0x9134cc`). The library likewise awaits
acknowledged writes when the control characteristic advertises `write`, using
no-response writes only for legacy endpoints without that property. Its 30 ms
inter-frame gap starts after the awaited write completes, not when it is queued.
An ATT acknowledgement confirms the GATT write, not the applied heater state;
device state readback still arrives through notifications.
The app's ordinary BLE queue explicitly filters out frames whose header is
`0x5f` (`batchWrite` predicate at `0x9136e4–0x91373c`). A recovered `0x5f`
sync builder is not evidence that those frames should be transmitted over BLE.


### Connection Lifetime

The client reuses one configured GATT connection for multiple successful
transactions. The connection prelude (`0x5a / 0x04` followed by one
`0x5a / 0x09` time-sync frame), characteristic discovery, and notification
subscription run once per physical connection.

After each successful transaction, the client arms a 120-second idle
disconnect timer. A subsequent transaction cancels and refreshes that timer.
BLE failures, cancelled transactions, explicit `disconnect()` calls, and
Home Assistant config-entry unloads tear the connection down immediately.
The CLI also explicitly disconnects after each one-shot command.

## Codec module layout

The implementation is split by responsibility:

- `chihiros_led_control.protocol.frame` provides frame construction, message
  IDs, timestamps, and checksums.
- `chihiros_led_control.protocol.notifications` provides parsed notification
  data classes and passive notification dispatch.
- `chihiros_led_control.protocol.led` contains LED commands and LED-family
  notification parsing.
- `chihiros_led_control.protocol.dosing`, `.stirrer`, and `.heater` contain
  their family commands and notification codecs.

Family drivers in `chihiros_led_control.devices` select the appropriate codec
from the `DeviceModel.spec`; callers should import those modules directly
instead of relying on a broad compatibility surface.

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

Message ID high and low bytes skip **only** `0x5a`: `0x59` advances to `0x5b`,
and `0xffff` wraps to `0x0001`. This sequence-counter rule is independent of
payload and checksum handling.

Parameters are copied **verbatim**, including `0x5a` (brightness 90). The XOR
checksum is also preserved when it equals `0x5a`; it does not advance the ID.
There is no escaping option or alternate legacy sanitizing path.

Executed app output for channel 0, level 90, message ID `0001`:

```text
5a 01 07 00 01 07 00 5a 5a
```

Evidence: `dataMaker.dart::formatData @ 0x8e8e78`, copy loop
`0x8e9060–0x8e90ec`, checksum helper `_addVerifyByte @ 0x8e921c`.

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
- `brightness`: model-specific wire level (see below), not a universal percentage

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

Library LED inputs use **raw model channel levels**. `LedSpec` describes each
channel's maximum and its normalization; omitted maxima retain the existing
100-level model contract. The frame builder accepting a byte is not evidence
that any particular model supports that level.

Verified embedded metadata:

| Prefix | App device type | Channels | Raw maxima | Wire normalization |
| --- | --- | --- | --- | --- |
| `DYRGBV` | `NewBleLed` | RGB | `[115, 130, 200]` | unchanged |

The app passes these maxima to its slider, and base `ChihirosLed::setManual`
forwards the selected raw level unchanged. Thus this model can transmit values
above 100 in manual commands and custom-curve points. Do not extrapolate its
maxima to server-only models or similarly named prefixes.

`NewASeriesLed` and its `MagneticLed` subclass instead send
`floor(requested * 100 / max_level[channel])`, except requested **1 stays 1**
and **0 stays 0**. With maximum 125, requests `0, 1, 2, 90, 125` encode as
`0, 1, 1, 72, 100`. This normalization is explicit model metadata; it is not
selected by a guessed physical prefix or applied universally to levels above 100.
Schedule snapshots use the selected model's wire limits and normalization rather
than dropping every level above 100.

Home Assistant brightness (`0..255`) and schedule/curve service levels
(`0..100` percent) remain percentage-based and convert channel-by-channel to
raw model units. They are not silently reinterpreted as raw levels.
The library and CLI accept raw channel units.

Evidence: embedded `config/device_offline.dart @ 0x920834–0x920848`,
slider maxima path `0xca81d4–0xca852c`, base setter `0xe6455c`, scaled setter
`0xe64374`. No speculative DYDD/DYCOM channel-layout or max125 mappings are added.

## Auto Mode

The driver selects the automatic controller explicitly:

| Operation | Mode-switch payload (`0x5a / 0x05`) | Data frames |
| --- | --- | --- |
| Custom curve | `[3, 255, 255]` (`switchToAuto`) | `0x5a / 0x06` |
| Sunrise/sunset scene | `[18, 255, 255]` (`switchToScene`) | `0xa5 / 0x19` |

`enable_auto_mode` syncs time and enables the selected controller. Manual
brightness or `set_manual_mode` leaves auto operation without forgetting which
automatic controller was selected.

Manual mode can be entered with:

- Command ID: `0x5a` / `90`
- Mode: `0x05` / `5`
- Parameters: `[11, 255, 255]`

Reset depends on the selected automatic controller: custom curves use
`0x5a / 0x05 [5, 255, 255]`, scenes use `[40, 255, 255]` (`0x28`).
They are not interchangeable resets.

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
| `40` / `0x28` | `resetLedQuick`, reset scene data |
| `48` / `49` | VIVID III `vvd3tempProtect` on / off (`[0x30\|0x31, 0xff, 0xff]`) |
| `51` / `50` | VIVID III `vvd3BluetoothLed` on / off (`[0x33\|0x32, 0xff, 0xff]`) |
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

Only a scene ending at **total minutes zero** (`00:00`) is rewritten to
`23:59`; `12:00`, for example, remains unchanged.

`replace_settings(Sequence[LedSchedule])` syncs time, sends scene reset
`5a/05 [28, ff, ff]`, selects scene mode `5a/05 [12, ff, ff]`, then writes
`a5/19` entries (these payloads are hexadecimal). The entire sequence is paced
as one transaction. Incremental `add_setting`/`remove_setting` do not claim
ownership of previously unknown device schedules.

`LedSpec.scene_min_firmware` makes the firmware/capability gate explicit.
Known firmware below the configured minimum (normally 20), or an explicit
`None` capability, rejects scenes. Unknown firmware retains the established
scene API; a successful write is not capability confirmation.
Custom `5a/06` curves remain available independently of this scene gate.

After a successful full replacement, the driver retains immutable wire payloads
as its authoritative, in-memory replay schedule. Lite auto initialization
replays them after reset/mode selection even when unchanged; it does not trust
a matching schedule hash. Without an authoritative schedule it never resets
the device on reconnect. Failed/incremental mutations relinquish replay
authority rather than clearing and replaying stale data.

The HA replacement services select auto mode after a successful transaction.
Validation errors (unsupported weekdays, firmware, or levels) do not trigger
a cleanup reset of an existing device schedule.

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

Payload and checksum bytes are sent **verbatim**, including level 90 (`0x5a`).
A full curve is a burst of point frames, 30 ms apart. Custom points remain a
separate controller mode from `0xa5 / 0x19` sunrise/sunset scenes. Firmware
>=20 does **not** universally eliminate point commands; the app's `_setAuto`
retains an independent point path. Choose the operation and model capabilities
explicitly rather than converting all schedules to scenes.

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

The app suppresses weekday selection for these exact Lite-class prefixes:

```text
DYZSD DYNA2N DYNA2 DYNC2N DYCXD DYECO MDTM DYDD
DYCOM DYNCM DYFISHDL DYLITE DYZLPD DYZPRO
```

Such devices require an everyday schedule. This classification also controls
library-owned auto initialization/reconnect replay; it does not establish
channel layouts, maxima, or server device types for unknown prefixes.
Evidence: weekday guide `0xa97778–0xa97928`; Lite replay override
`led_info.dart @ 0xa9a5f4–0xa9a648`.

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

The generic app dispatcher recognizes both `0x5b` and `0xb5`, but several
model/widget parsers explicitly require `0x5b`. This does **not** establish a
shared layout for every `0xb5` message; the library does not blanket-alias headers.

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
| `10` | Fan present | `data[10] == 1` updates the app's `have_fan`, not temperature protection |
| `16` | Uptime counter | Increments between periodic notifications |

The trailing byte is not a valid XOR checksum for this mode. The library
preserves bytes beyond its decoded fan/temperature fields as raw data; byte 10
must not overwrite the optimistic temperature-protection setting.
Fan speed is set with `0x5a / 0x0f`; measured RPM follows the set percentage.

The vendor app's `vvd3_fan_widget` reads this same `0x5b`/`0x0b` frame — its
disassembly immediates `#0xb6`/`#0x16` are Dart smis, i.e. `0x5b`/`0x0b`:
`rpm = (data[6] << 8) | data[7]`, `temperature = data[8]`.

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

The 2.8.59 app forwards both switch Booleans unchanged to the builders.
Executed `vvd3BluetoothLed @ 0xaa11d4` and `vvd3tempProtect @ 0xaa0c78`
establish these polarities. Neither has a protection/indicator readback;
the library updates optimistic state only after a successful write:

| Action | Frame | Payload |
| --- | --- | --- |
| Temperature protection | `0x5a / 0x05` | `[0x30\|0x31, 0xff, 0xff]` — byte 0 = 48 (on) / 49 (off) |
| Indicator LED | `0x5a / 0x05` | `[0x33\|0x32, 0xff, 0xff]` — byte 0 = 51 (on) / 50 (off) |

Example frames (msg id `00 01`):

```text
5a 01 08 00 01 05 30 ff ff 3d   temperature protection ON
5a 01 08 00 01 05 31 ff ff 3c   temperature protection OFF
5a 01 08 00 01 05 33 ff ff 3e   indicator LED ON
5a 01 08 00 01 05 32 ff ff 3f   indicator LED OFF
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
one `90 / 9` time sync command), followed by two dosing auth commands:

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

Dosing pumps answer the `[4]`/`[5]` queries with `0x5b` uplink frames
(confirmed by the DYDOSE HCI capture, fw `07.25.18`); the vendor app's
`dosing_state_widget` compares the smi immediates `#0x3c`/`#0x44` =
`0x1e`/`0x22`:

| Header | Byte 5 | Meaning | Payload |
| --- | ---: | --- | --- |
| `0x5b` | `0x1e` / `30` | Lifetime totals | Per channel `i`: `(data[6+2i] << 8 | data[7+2i]) * 100` µL |
| `0x5b` | `0x22` / `34` | Dosed today | Same layout/scaling |

Example lifetime frame (channels `0..1` = `105.5 mL`, `0 mL`):

```text
5b 10 10 00 01 1e 04 1f 00 00
```

The Home Assistant integration stores both device-reported counters in the
coordinator and exposes them as the primary per-pump `total ml` and `dosed today`
sensors, as well as attributes (`device_total_ml`, `device_dosed_today_ml`) on the
locally tracked diagnostics. It queries the pump at setup and every five minutes:
external doses appear after the next successful readout, and values stay unknown
until the first notification. The `HA manual dose total`, `HA manual doses today`,
and `HA manual dose cycles` diagnostics count only successful manual doses
initiated by Home Assistant and keep their previous entity IDs.

### Dosing Pump Schedules, Settings, and Calibration (2.8.59 app)

Reverse-engineered from `My Chihiros` 2.8.59 (`chihiros_xapk/DOSING_CONTROL.md`);
all frames use header `165` (`0xA5`) and payload bytes are sent verbatim (no
reserved-byte escaping). Volumes ride as 0.1 mL buckets: `hi * 25.6 mL + lo *
0.1 mL` (0 to 6553.5 mL); 105.5 mL encodes as `(4, 31)`.

Schedule modes (`DosingMode`, the enum index is the wire mode byte):

| Index | Mode | Frame shape |
| ---: | --- | --- |
| 0 | single | one `(0xA5, 21)` frame per point: `[ch, 0, hour, minute, vol_hi, vol_lo]` |
| 1 | auto (24h) | one `(0xA5, 21)` frame per point: `[ch, 1, hour, minute, vol_hi, vol_lo]` |
| 2 | free | batched `(0xA5, 23)` frames: `[ch] + [sh, sm, eh, em, number] * N` |
| 3 | timer | batched `(0xA5, 21)` frames: `[ch, 3] + [hour, minute, vol_hi, vol_lo] * N` |

Free/timer records batch like the app's accumulator: records are appended
first and a frame flushes once its payload *exceeds* 50 bytes, so frames can
carry up to 54 payload bytes. Free-mode end times come from
`end = start + duration` and are *not* wrapped past midnight (23:00 + 180 min
encodes end hour 26). Free mode carries no volume: the pump splits the daily
total itself.

| Command ID | Mode | Parameters | Meaning |
| ---: | ---: | --- | --- |
| `165` | `27` | `[ch, frequency, 1, first?0:1, vol_hi, vol_lo]` | Program a channel's daily dose (`dosingSet`); byte 4 is polarity-inverted: `0` when this is the channel's first programming of the day; `vol = [255, 255]` for a null volume |
| `165` | `32` | `[ch, compensate?1:0, active?1:0]` | Enable/disable a channel and interrupt compensation (`setDosingInterruptCompensationAndActive`) |
| `165` | `22` | `[ch, time?255, vol_int, vol_frac]` | Calibration: `time` = test-dose seconds (255 = omitted); volume splits as `[int mL, 2-digit fraction]` (2.5 mL = `(2, 50)`), fraction rounded half-up (`LibcRound` @ 0xa69398) so it can be `100` = next whole mL (2.999 mL = `(2, 100)`); `255/255` when omitted |
| `165` | `5` | `[ch+25, 255, 255]` | Reset a channel's programming (`resetDosingChannel`) |
| `165` | `5` | `[ch+21, 255, 255]` | Zero a channel's lifetime counter (`resetTotalDosing`) |
| `165` | `31` | `[enabled?1:0]` | Device-level dose delay flag (`setDosingDelay`) |
| `165` | `59` | `[ch, color]` | New-generation pump channel color (`dosingChannelColor`) |
| `165` | `4` | `[4]` | Query lifetime totals (reply: `0x5B` mode `0x1E`) |
| `165` | `4` | `[5]` | Query dosed-today (reply: `0x5B` mode `0x22`) |

### Magnetic Stirrer (DYMIXR)

The magnetic stirrer (`DYMIXR...` names, app device type `MagStirrer`) speaks
the dosing-pump protocol verbatim: its eight channels are programmed in timer
mode through `dosingSet` / `setDosingInterruptCompensationAndActive` /
`dosingWorkNew`, and each timer point's "volume" encodes the stir workload at
the pump's 0.6 mL/min dosing-rate equivalence (`minutes = dosage_ml / 0.6`).
The vendor app's stirrer UI is fire-and-forget: it never parses stirrer
notifications.

| Command ID | Mode | Parameters | Meaning |
| ---: | ---: | --- | --- |
| `165` | `42` | `[ch, sec_hi, sec_lo, speed]` | Pre-stir seconds (0 to 999) and unitless app speed setting (0 to 20, default 20) — the only wire carrier for speed (`stirrerPreSecond`) |
| `165` | `20` | `[min][sec][8 channel bytes]` | Manual run/stop: duration bytes first (binary-verified 2.8.59: `[min,sec]` built then `addAll(channel bytes)`), channel bytes default `255`, overlaid with `1` (run) / `0` (stop); duration `255/255` = unlimited (`generalTempSet`, the app's `tempRun`) |

The shared encoder accepts a duration, but the app's stirrer-specific `tempRun`
at `0xaa7f64` passes only `codeString`, leaving the duration unlimited. Its
test-button callbacks start on press and explicitly stop on release/cancel.
This does not establish that stirrer firmware honors finite duration bytes.
Use explicit manual start/stop commands or programmed timer schedules rather
than relying on finite duration bytes to stop a magnetic stirrer.

The app's speed is an integer setting from 0 to 20 (default 20), not a
percentage; it is sent directly in the `speed` byte. The app's internal slider
uses 0–100 and displays/saves `floor(slider / 5)`. The model's raw default
constant `40` is a Dart tagged integer representing `20`, not a wire speed.

The app changes the speed of a running channel by stopping it, re-sending
`(0xA5, 42)`, and restarting — speed is never sent standalone.

### Heater (DYHET / DYH1T)

The heater uses standard `0x5a` command framing. The corrections below are
grounded in `DYHET-6.zip` (`uart_app.bin`, SHA-256
`a4c9f3ca11f64f24b33dcc117d8b58359f53e60620baa36f5a599d03fa02d664`),
loaded at `0x26000`, and the official-app HCI capture attached to
[issue #57](https://github.com/TheMicDiet/chihiros-led-control/issues/57).
Firmware command handlers and notification builders were executed in an ARM
emulator with peripheral/logging calls intercepted; this is not a physical
heater test. APK-only assumptions about two temperature fields, separate
manual/auto setpoint banks, and an Auto-mode heating-enable switch were wrong.

| Command ID | Mode | Parameters | Meaning |
| ---: | ---: | --- | --- |
| `90` | `43` / `0x2b` | `[flag, temp_whole, temp_hundredths, power]` | Write the shared target and configured power. Flag `0` also selects manual mode; flag `1` preserves the current mode |
| `90` | `47` / `0x2f` | `[temp_whole, temp_hundredths]` | Overheat protection limit |
| `90` | `48` / `0x30` | `[temp_whole, temp_hundredths]` | Measured reference temperature for calibration |
| `165` | `56` / `0x38` | `[level, level, level, level, 127]` | Backlight always on (`200`) / always off (`100`) |
| `165` | `56` / `0x38` | `[start_hour, start_minute, end_hour, end_minute, weekmask]` | Scheduled display backlight |
| `90` | `5` | `[11, 255, 255]` | Select manual mode |
| `90` | `5` | `[18, 255, 255]` | Select auto mode using the stored scene/schedule |
| `90` | `5` | `[40, 255, 255]` | Delete the five stored scenes; never send as a routine mode-switch prerequisite |
| `90` | `5` | `[46\|47, 255, 255]` | Enable / disable automatic configured-power increases, in either mode |
| `90` | `5` | `[44\|45, 255, 255]` | Display Celsius / Fahrenheit |
| `90` | `5` | `[58, 255, 255]` | Reset the cleaning-runtime hours/seconds counters |

The handler at `0x2fbba` decodes temperature as
`10 * whole + floor(hundredths / 10)`, internally in tenths of a degree:
36.9 °C is `[36, 90]`. Configured power is the wire byte multiplied by 10 W.
Both flag values write the same target at `0x20002af0` and power at
`0x20002af8`. A flag-1 write while in manual mode changes its active setpoints;
it does not enter auto mode or populate a separate setpoint bank.

`set_temperature`, `set_power` and `set_manual_state` send manual mode followed
by the flag-0 state frame in one 30 ms-paced batch. `set_state` sends flag `1`,
updates the same tracked pair, and preserves mode. `set_auto_mode` sends
`[18, 255, 255]`; `[3, 255, 255]` has no handler in DYHET-6. Mode switches
neither resend stale app presets nor delete the stored schedule.

Before a paired write, the client completes a status refresh under its state
lock. Temperature-only writes then resend the device's configured power, not
a stale default. Connection-prelude readback is consumed before the write's
power-generation snapshot, so old startup status cannot undo the new setting.

The regulator at `0x31596–0x315d6` uses the automatic-power flag to increase
configured wattage when its adjustment threshold is reached, even in manual
mode. Disabling this flag does not disable heating.

Backlight Boolean writes replace a prior schedule. Scheduled writes use clock
hours `0..23`, minutes `0..59`, and a seven-bit weekday mask `0..127`.
For example, `set_backlight_schedule(8, 15, 21, 30, weekdays=81)` sends
`[8, 15, 21, 30, 81]`. While scheduled, `backlight` is `None`: instantaneous
on/off state is not inferred. Failed writes leave tracked settings unchanged.

Heater notifications use header `0x5b`; offsets below are zero-based:

| Mode | Exact length | Fields | Meaning |
| ---: | ---: | --- | --- |
| `0x25` | 13 | `data[6..7]` = measured watts, `data[10..11]` = temperature × 10; both big-endian | Periodic measured power and current temperature, **not target temperature** |
| `0x0b` | 18 | `data[12] * 10` = configured watts, `data[13..16]` = big-endian heating seconds | Configured-power readback and accumulated heating time |
| `0x0a` | 16 | `data[7..8]` = big-endian runtime hours, `data[14]` = alarms | Cleaning-runtime/alarm status |

The builder at `0x26c80` packs measured power from state `+0x64`, current
temperature from `+0x4c`, configured power from `+0x50`, and runtime hours
from `+0x58`. The target is a different field (`+0x48`) and is not reported
by these notifications. `0x0a` bytes 11–12 are template constants `ff 22`,
not a firmware version; no heater firmware version is inferred from them.
The UART conversion at `0x3349c–0x334ae` populates measured power separately
from the temperature-sensor path.

The configured-power command handler stores `10 * payload_power` at state
`+0x50` (`0x2fbf0–0x2fc02`). The notification builder reads that same field,
divides it by 10, writes byte 12 of the `0x0b` buffer and sends 18 bytes
(`0x26d14–0x26d3c`). Executing the original builder with notification transmission
and delay/hardware calls intercepted confirmed round-trips at 0, 200, 600 and
2000 W. A separate periodic-builder run with configured power 600 W and measured
power 30 W emitted 30 W in `0x25`, confirming these fields are independent.
The Home Assistant **Configured power** sensor exposes only this device readback;
the writable **Power** number can also show restored or optimistic values.

Captured examples reproduced by the firmware builder:

```text
5b 06 0e 01 7b 25 00 1e 0b ff 00 fa 00
  measured power = 30 W; current temperature = 25.0 °C
5b 01 10 00 01 0b 00 00 00 00 00 00 14 00 0b 33 50 22
  configured power = 200 W; accumulated heating time = 734032 s
5b 06 0e 00 01 0a 00 00 cb ff ff ff 22 05 00 22
  cleaning runtime = 203 h; alarm bitmap = 0
```

`query_status` (`04/01`) requests `0x0a`, `0x0b` and the currently uninterpreted
`0x36` response; periodic `0x25` measurements arrive independently. Device
configured-power readback takes precedence over optimistic/restored values,
including a readback arriving while a write is awaiting completion. A periodic
measurement must never overwrite the target or the configured-power setting.

The alarm bitfield uses the app's established bit mapping:

| Bit | Value | Meaning |
| ---: | ---: | --- |
| 0 | `0x01` | No water flow detected |
| 1 | `0x02` | Power setting too low |
| 2 | `0x04` | Exceeds safe set temperature |
| 3 | `0x08` | Heater needs cleaning |
| 4 | `0x10` | Exceeds safe protection temperature |
| 5 | `0x20` | Heating device malfunction |
| 6 | `0x40` | Sensor malfunction |

The app also warns once `runtime_hours / 2160 > 0.9` (~1944 h). Resetting
cleaning runtime does not reset the separate accumulated heating-seconds
counter reported in `0x0b`. Target temperature, mode, automatic-power flag,
display unit and protection limit remain locally tracked settings.

## Decompiler Notes

For the identified 2.8.59 ELF, the Flutter app's central frame builder is
`dataMaker.dart::formatData @ 0x8e8e78` with `_addVerifyByte @ 0x8e921c`.
Addresses from other builds or old local verifiers are not interchangeable.

Many integer arguments in the decompiled Dart AOT pseudocode are tagged small
integers. A displayed first argument of `4` often means an actual parameter
length of `2`, `6` means `3`, and `0xa` means `5`. Command ID and mode values
listed in these docs are actual byte values.

Boolean decoding needs separate care: Dart `true = null + 0x20` and
`false = null + 0x30`; a set bit in `tst #0x10` therefore means **false**.
The old local verifier inverted this condition. Do not use its output as
independent evidence for switch polarity.

The old app also contains an unused `0xaa ...` frame builder. No callers were
found, so it should not be treated as the active BLE protocol.

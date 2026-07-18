# A2 Custom Schedule Protocol

This note records BLE traffic captured from a single-channel Chihiros A2 while
the official app created custom brightness schedules. It is intended as a
reference for adding custom-curve support later. The behavior described here is
not implemented by the library yet.

## Capture Context

The capture was recorded on 2026-07-11 while creating two profiles:

1. A curve shown in the app as starting at `08:00` at `0%`, increasing to
   `40%` at `11:00`, and dropping to `0%` at `12:00`.
2. A profile intended to remain at `10%` throughout the day.

The A2 has one white channel. Application data was transported over the Nordic
UART service documented in [protocol.md](protocol.md).

## Custom Curve Point Command

The app writes custom curve points using command family `0x5a`, mode `0x06`:

```text
5a 01 09 msg_hi msg_lo 06 channel hour minute level checksum
```

The four parameters are:

| Offset | Name | Observed value or range |
| ---: | --- | --- |
| `6` | Channel | `0` for the A2 white channel |
| `7` | Hour | `0..23` |
| `8` | Minute | `0..59` |
| `9` | Brightness level | `0..100` percent |

The command-length byte is `0x09`, corresponding to four parameters plus five.
The final byte uses the normal XOR checksum.

Example for channel `0`, `08:00`, `10%`:

```text
5a 01 09 00 ca 06 00 08 00 0a c6
```

This is a distinct `0x5a / 0x06` format from the older three-parameter
`[channel, time_index, level]` form. Implementations must select the format by
device/model capability; they must not assume every `0x5a / 0x06` device uses
the same payload length.

## Custom Schedule Setup Sequence

Immediately before committing a custom curve, the app sends two `0x5a / 0x05`
commands:

```text
5a / 05 [05, ff, ff]  clear or reset the current custom schedule
5a / 05 [03, ff, ff]  initialize or select custom-curve mode
```

The meaning of `[03, ff, ff]` is inferred from its position in the sequence and
needs confirmation. It consistently follows the reset and precedes the point
writes.

The first profile's final commit was:

```text
5a010800d00505ffffd9       reset
5a010800d10503ffffde       initialize custom-curve mode
5a010900ca060008000ac6     channel 0, 08:00, 10%
5a010900cb0600090014d8     channel 0, 09:00, 20%
5a010900cc06000a001ed6     channel 0, 10:00, 30%
5a010900cd06000b0028e0     channel 0, 11:00, 40%
5a010900ce06000c0000cc     channel 0, 12:00, 0%
5a010900cf0600070000c6     channel 0, 07:00, 0%
```

The points were not transmitted chronologically. A future implementation
should not rely on write order to determine curve order.

## App Curve Expansion

The curve shown as `08:00 0%` to `11:00 40%` was expanded by the app into:

| Time | Transmitted level |
| --- | ---: |
| `07:00` | `0%` |
| `08:00` | `10%` |
| `09:00` | `20%` |
| `10:00` | `30%` |
| `11:00` | `40%` |
| `12:00` | `0%` |

This does not exactly match the user-visible endpoints. The app may define a
point as the end of the preceding interpolation interval, or this may be an app
off-by-one-hour behavior. One capture is insufficient to distinguish those
possibilities.

While the curve editor was open, the app also sent live updates. For example,
it repeatedly wrote the `09:00` point as its level was changed from `11%`
through `20%`. These writes are editor updates, not additional schedule slots.
The reset/setup sequence is a better boundary for identifying the final commit.

## Constant-Level Profile

The second profile starts with:

```text
5a010800d30505ffffda       reset
5a010800d40503ffffdb       initialize custom-curve mode
5a010900d2060008000ade     channel 0, 08:00, 10%
```

The capture ends after this point. A single point may mean that the last level
persists until another point, but this is not confirmed. The traffic does not
show how the app represents the remainder of the day or midnight rollover.

## Notifications Seen During the Capture

Only two application notifications were received. Both arrived during startup,
before either new profile was written. ATT write responses followed the later
commands, but there were no application-level acknowledgements or updated
schedule snapshots after the saves.

### Runtime/status notification

```text
5b170a00010a01ffffffff13888c
```

This is the already documented `0x5b / 0x0a` response. Firmware/protocol version
is `0x17`, and bytes `6..7` decode as the currently interpreted runtime value
`0x01ff = 511` minutes. Its XOR checksum validates.

### Schedule snapshot notification

```text
5b17300001fe06113b0000000000000000000000000006113b
0d0f000d2d64150f64152d0000000000000000000000000000
```

The schedule triples beginning at byte offset 25 decode as:

| Bytes | Time | Level |
| --- | --- | ---: |
| `0d 0f 00` | `13:15` | `0%` |
| `0d 2d 64` | `13:45` | `100%` |
| `15 0f 64` | `21:15` | `100%` |
| `15 2d 00` | `21:45` | `0%` |

This is the schedule already stored when the app connected, not either newly
created profile.

The metadata contains `06 11 3b` twice. It plausibly means weekday `6` and
local time `17:59`, which exactly matches Saturday at the capture time. This
interpretation is strong but not yet proven by a capture made at another time.

## Implementation Guidance

A future custom-schedule implementation should:

- represent this as a separate capability from the `0xa5 / 0x19` sunrise and
  sunset schedule API;
- add a four-parameter `0x5a / 0x06` command builder without removing support
  for the older indexed three-parameter variant;
- send the reset/setup sequence before committing all points;
- validate channel, hour, minute, and percentage ranges;
- preserve the caller's point order only for transmission, and treat schedule
  semantics as time-sorted;
- avoid claiming that a save succeeded based on a snapshot notification,
  because this capture contains no post-save application acknowledgement; and
- keep app-side interpolation separate from raw point transmission until its
  endpoint behavior is understood.

## Follow-up Captures Needed

The highest-value follow-up captures are:

1. A complete constant-level profile covering midnight.
2. A two-point ramp with non-hour times and a level not divisible into even
   steps, to determine the app's interpolation and rounding rules.
3. Reading/reopening a newly saved profile, to capture its returned snapshot.
4. Editing or deleting one point without replacing the entire profile.
5. The same workflow on a multi-channel light, to confirm channel behavior and
   whether points are written once per channel.
6. A schedule involving weekdays, to determine whether custom curves support a
   separate weekday command or are inherently daily.

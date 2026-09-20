# Original App Verification Checklist

This document separates behavior that is now established from behavior that
still needs a live-app capture. It is based on the My Chihiros 2.8.59
arm64 build, recovered Dart/AOT code, raw instruction checks, and the
captured dosing-pump session described below. Binary evidence is stronger than
a UI observation, but it does not prove that every firmware generation accepts
the same frames.

## Evidence scope

The sibling reverse-engineering worktree was consulted for:

- `VERIFICATION.md`: command encoder and framing verification;
- `DOSING_CONTROL.md`: dosing-pump and magnetic-stirrer provider flows;
- `DOSING_PUMP.md`: model limits and volume units;
- `DOSING_WIFIHUB_SEQUENCES.md`: queue pacing and action sequences;
- `REVERSE_ENGINEERING.md`: app transport and device-family details.

The primary binary target is `libapp.so` from My Chihiros 2.8.59
(`cn.chihiros.chihiros_magic_new.apk`). The capture evidence is an HCI
session from 2025-10-22/23 involving a dosing pump and LED device; device
addresses are intentionally omitted here.

Status labels:

- **Binary-verified** — recovered source and raw AArch64 instructions agree.
- **Capture-confirmed** — observed on the wire in the documented HCI session.
- **Open** — the available evidence does not establish the behavior.

## Current implementation context

- best-effort post-dose bookkeeping and master/slave mirroring after a
  physical dose succeeds;
- stop/configure/restart behavior for an explicit running-stirrer speed
  change;
- stirrer mirroring based on the configured submitted channel count;
- strict minute-precision schedule parsing;
- dose-derived, inclusive, non-cyclic stirrer schedule-overlap validation;
- manual-dose validation with the recovered `999.9 mL` upper bound;
- dosing status refreshes that emit lifetime `[4]` then daily `[5]` counters.

Several of these are deliberate repository policies rather than exact
original-app parity. The differences are called out below.

Relevant wire-level behavior is documented in
[protocol.md](protocol.md), especially the dosing and magnetic-stirrer
sections.

## Resolved questions

### 1. Stirrer restart semantics — resolved by operation

**Binary-verified for an explicit live speed change:**

1. Send `generalTempSet` (`0xA5/20`) to stop the selected channel.
2. Send `stirrerPreSecond` (`0xA5/42`) with
   `[channel, seconds_hi, seconds_lo, speed]`.
3. Send `generalTempSet` (`0xA5/20`) to start the selected channel again.

The stop/start frame uses `0xFF` for unselected channels and an unlimited
duration (`[0xFF, 0xFF]`). The restart therefore does **not** preserve a
previous finite manual-run duration. Speed is carried only by
`stirrerPreSecond`; there is no separate speed frame.

The pre-second setting path is different. Its recovered UI handler calls
`setPreSecond(time)` directly when validation succeeds; it does not issue the
stop/configure/start sequence. Therefore the repository's `restart=True`
behavior is correct for a running speed change, but must not be generalized
to every pre-second edit. The recovered source does not establish a universal
inter-frame delay.

The recovered `dataMaker.dart` source also proves that the `generalTempSet`
payload is `[duration_min, duration_sec, channel_0 ... channel_7]`. A sibling
prose table showing the channel bytes before the duration is stale; the
repository encoder and protocol tests use the recovered duration-first order.

### 2. Manual-dose maximum — resolved for the original manual-dose UI

The original encoder stores dose volume in 0.1 mL units across two bytes. The
representable wire range is therefore `0` through `6553.5 mL`.

The recovered `DoseVolumeInputWidget` call site for the manual temporary-dose
page passes the field value that makes `maxNumberString()` return `999.9`.
The original manual-dose UI therefore caps this path at **999.9 mL**. The
The repository's `999.9 mL` upper bound matches the recovered UI. Its `0.2
mL` lower bound is a repository validation policy; this source path only
establishes the upper bound.

Other recovered input-widget paths can expose a different generated maximum,
including a `5999.9` literal, and the new-generation model metadata stores
`doseMax=99999` in its internal volume units. Those are not evidence that the
manual temporary-dose page accepts those values. Firmware can still impose
additional limits below the wire maximum; no lower limit was established.

### 3. Dosing status queries — resolved

The original app contains two distinct dosing-counter queries:

- `getDosedFromDevice`: `(0xA5, 4, [4])`, lifetime totals;
- `getDosedInDayFromDevice`: `(0xA5, 4, [5])`, today's dose.

The recovered app emits `[4]` and then `[5]` from dosing-pump state/channel
settings flows. They are event writes, not a response-blocking transaction:
the source does not wait for a notification before scheduling the next event,
and no single global refresh routine was found. The captured session happened
to show the same request order.

The captured firmware replies use `0x5B` uplink frames with modes `0x1E`
(lifetime) and `0x22` (today). In app version 2.8.59 those frames are
dispatched as generic notification events rather than connected to the dosing
UI parser, so the pulled values did not update the UI in that session.

Magnetic stirrers are separate: their UI has no notification subscription and
performs no dosing-counter pulls. It is fire-and-forget and displays locally
persisted state (**binary-verified**).

The repository now emits `[4]` then `[5]` as one fire-and-forget command
batch, matching the recovered app ordering and avoiding the generic runtime
query. Stirrers likewise do not issue a status query.

### 4. Master/slave mirror ordering — resolved

The original app's `startAsSlave` flow is now established:

1. For each master-pump channel, send
   `setDosingInterruptCompensationAndActive` (`0xA5/32`) to the stirrer.
2. Send `dosingSet` (`0xA5/27`) for that channel.
3. Send `dosingWorkNew` (`0xA5/21` or `0xA5/23`) for that channel.
4. After all channels, send `setDosingDelay` (`0xA5/31`) with the
   master's delay setting, unconditionally.

The channel frames are addressed to the stirrer. Pairing itself has no BLE
frame: `setMaster` persists the relationship on both device models, then
programs the mirror.

There is a second, important path: when a pump has a linked stirrer, the
pump's own `startWork` events use a null device target. The transport expands
that target to every connected non-AIX device, so the pump's schedule frames
are broadcast and the stirrer interprets them as its own schedule. This
behavior is binary-verified; implementations must avoid accidentally
duplicating it with a second mirror transaction.

## 5. Two-minute stirrer schedule gap — source semantics resolved

The original warning describes a two-minute minimum, but the recovered
validator is not a simple cyclic start-time gap check. `duplicateJudge`:

- converts each scheduled dose to a duration using the pump's `0.6 mL/min`
  equivalence and rounding;
- compares candidate and existing work intervals;
- treats interval endpoints inclusively, so an endpoint collision is a
  conflict;
- rejects duplicate timestamps as an overlap;
- returns `0` for free, `1` for conflict, and `2` after more than 999
  iterations;
- runs before the candidate is inserted or schedule frames are emitted.

It iterates the stored work list in its ordinary time coordinates. No
last-point-to-first-point `+1440` comparison was found, so the original app
does **not** prove the repository's cyclic midnight rule. Likewise,
`08:00` and `08:02` is not a universally accepted boundary: acceptance
depends on the rounded work durations and inclusive interval comparison, not
only on start-time separation.

The repository now uses the recovered dose-derived, inclusive, non-cyclic
interval rule. The original app's validation-before-write ordering is also
preserved.

### 6. Schedule timestamp precision — resolved

The original schedule time picker exposes hours and minutes only. Its recovered
model has a minute divider and constructs the selected `DateTime` from the
hour/minute columns; there is no seconds column or recovered parser accepting
arbitrary seconds.

Consequently, the original UI cannot produce `08:30:01`: this is not an
original-app truncation-versus-rounding decision. The repository's acceptance
of `HH:MM:00` and rejection of nonzero seconds is a safe API policy for
externally supplied values, but the rejection itself is not a recovered
original-app parser behavior. The schedule wire format stores hour/minute
precision.

### 7. Stirrer channel-count changes — transition workflow not present

The original stirrer model defaults to eight channels and stores an explicit
channel count. `startAsSingle` iterates that configured count, while
`startAsSlave` mirrors the channels present on the master pump. Both are
**binary-verified**.

No recovered UI setter or migration routine changes the count. The count is
read from persisted/device model data; it is not shown as an original-app
2 → 4 → 8 user workflow. Therefore the APK provides no evidence that:

- increasing the count immediately replays newly visible channels;
- reducing it clears hidden channels;
- hidden settings are deleted or preserved for a later increase.

The repository's configurable channel count and reload/mirroring behavior are
Home Assistant integration features. They should not be presented as proven
original-app transition semantics.

### 8. Post-dose failure behavior — resolved as fire-and-forget, no rollback

The original manual-dose flow updates local extra-dose bookkeeping and
notifies listeners **before** emitting the physical `tempDosingCode` frame.
The recovered source has no rollback or app-level confirmation path around
that ordering.

At the transport layer, failed BLE writes are retried by the app queue up to
20 times at roughly 30 ms intervals and then dropped. This is a failed-write
transport retry, not a replay of a completed/ambiguous manual-dose action.
No recovered source shows a user-level retry, counter reconciliation, or
linked-stirrer rollback after the queue gives up.

The repository's policy of never replaying the physical dose after an
ambiguous write is safer than blindly repeating a non-idempotent dose. It
differs from the app only in where transport retries are implemented; local
bookkeeping-before-write and absence of rollback match the recovered flow.

## Verification artifacts to collect

For every future capture, record:

- My Chihiros app version and APK build;
- device model and firmware;
- a redacted stable device identifier;
- configured channel count;
- exact UI action and prior device state;
- BLE writes with message IDs, payloads, and checksums where available;
- notification frames;
- inter-frame delays and connection boundaries;
- retries and visible error behavior;
- observed device behavior.

Never commit Bluetooth addresses, tokens, HCI data containing secrets, or
other credentials.

## Evidence index

The sibling notes establish these claims:

- frame length, XOR checksum, and sequence-counter `0x5A` skip:
  binary-verified;
- dosing volume encoding: two-byte 0.1 mL units, max `6553.5 mL`;
- lifetime and daily counter query IDs `[4]` and `[5]`;
- stirrer speed frame `0xA5/42` and unlimited `generalTempSet` run frames;
- stirrer timer programming through the dosing protocol;
- master/slave frame ordering and unconditional mirrored dose delay;
- stirrer UI fire-and-forget behavior;
- two-minute work-point warning and `0.6 mL/min` overlap calculation;
- live acceptance of dosing frames in the 2025-10-22/23 HCI capture.

These findings replace the earlier assumption that all questions in this
checklist were equally unverified. The remaining uncertainty is firmware
variance and behavior that is absent from the recovered APK, especially
channel-count migrations and user-visible transport-error handling.

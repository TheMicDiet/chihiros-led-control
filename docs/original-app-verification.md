# Original App Verification Checklist

This checklist records behavioral questions that remain worth verifying against the original My Chihiros app, a decompiled app build, or BLE captures. The current implementation follows the repository's reverse-engineered protocol notes and the fixes described below, but these points are not all independently confirmed from live app traffic.

## Current implementation context

Branch implementation includes:

- Best-effort post-dose bookkeeping and master/slave mirroring after the physical dose succeeds.
- Stop/configure/restart behavior for running stirrer speed and pre-run changes.
- Stirrer mirroring based on the configured submitted channel count.
- Strict minute-precision schedule parsing.
- Shared cyclic two-minute stirrer schedule-gap validation.
- Manual-dose validation from `0.2` through `999.9 mL`.
- Dosing status refreshes that query generic runtime status, lifetime totals, and daily totals.

Relevant protocol notes are in `docs/protocol.md`, especially the dosing and magnetic-stirrer sections.

## High-priority questions

### 1. Running stirrer restart semantics

Verify the exact BLE sequence when changing speed or pre-run time while a stirrer channel is already running:

- Does the app send stop, `stirrerPreSecond`, and restart as one paced transaction or as three separate transactions?
- Does the restart preserve the previous finite run duration?
- Does the restart use an unlimited duration (`255/255`) or resend the prior duration?
- Is there a required delay or notification between stop, configuration, and restart?

Current code sends:

1. `generalTempRun(false)`;
2. `stirrerPreSecond` (`0xA5/42`);
3. `generalTempRun(true)`.

The restart currently does not include a finite duration, so the wire representation is the unlimited-run form.

Recommended capture:

1. Start a channel for a finite duration.
2. Change its speed while it is running.
3. Capture all writes, message IDs, delays, and payloads.
4. Repeat with a pre-run-time change.

### 2. Manual-dose maximum

The wire encoding supports `0` through `6553.5 mL` in 0.1 mL units, while the current manual-dose UI/API limit is `0.2` through `999.9 mL`.

Verify whether `999.9 mL` is:

- an actual original-app manual-dose UI safety limit;
- a device firmware limit that is not obvious from the two-byte encoding;
- or only an integration-level safety choice.

Also verify whether scheduled daily/timer dose values use the full `6553.5 mL` wire range in the original app.

### 3. Dosing status query order

The current dosing-pump status refresh performs:

1. generic runtime/status query;
2. lifetime-total query (`[4]`);
3. dosed-today query (`[5]`).

Verify:

- whether the original app performs the generic status query for dosing pumps;
- the exact ordering of the three queries;
- whether each query waits for a notification before sending the next;
- whether query failures are independent or abort the refresh sequence.

Magnetic stirrers should continue to avoid dosing-counter queries because the protocol notes describe their UI as fire-and-forget with no parsed status notifications.

### 4. Post-dose failure behavior

A physical manual dose is non-idempotent. The current implementation therefore treats these operations as best effort after `dose_ml()` succeeds:

- local totals persistence;
- dispatcher notification;
- linked-stirrer frame mirroring.

Verify the original app's behavior when one of these post-dose operations fails:

- Does it show an error or warning?
- Does it retry only the mirror/bookkeeping operation?
- Does it rely on the next device-counter refresh?
- Does it ever retry the physical dose frame?

The implementation must not retry the physical dose after an ambiguous write result, because that can dispense twice.

## Medium-priority questions

### 5. Two-minute stirrer schedule gap

The current validator sorts points and checks every adjacent gap plus the cyclic midnight gap. It rejects duplicates and points less than two minutes apart, while accepting exactly two minutes.

Verify that the original app:

- checks the last point to the first point across midnight;
- rejects duplicate points;
- accepts exactly-two-minute gaps;
- performs validation before sending any schedule frame.

Useful boundary cases:

- `08:00`, `08:01` — reject;
- `08:00`, `08:02` — accept;
- `23:59`, `00:00` — reject because the cyclic gap is one minute;
- `23:58`, `00:00` — accept;
- duplicate timestamps — reject.

### 6. Schedule timestamp precision

The current service parser accepts `HH:MM` and `HH:MM:00`, and rejects malformed values or nonzero seconds.

Verify whether the original app:

- rejects `08:30:01`;
- truncates or rounds nonzero seconds;
- accepts a three-component form only when seconds are zero;
- uses local device time without timezone conversion.

The device protocol carries hours and minutes only, so silently truncating user input would be lossy.

### 7. Stirrer channel-count changes

The current options flow mirrors only the selected channel count when configuring a stirrer. Supported counts are 2, 4, and 8.

Verify the original app's behavior when changing counts:

- Does 2 → 4 replay channels 1–4 immediately?
- Does 4 → 8 replay channels 1–8 immediately?
- Does reducing 8 → 4 clear channels 5–8 on the device or only hide them in the UI?
- Are hidden channels' settings retained for a later 4 → 8 change?
- Is channel count derived from device metadata, a user setting, or both?

### 8. Master/slave mirror ordering

Verify the exact order used when a dosing pump is linked to a stirrer. Candidate order:

1. channel active/compensation state;
2. daily dosing settings;
3. timer schedule points;
4. device-level settings such as dose delay.

Capture a full `startAsSlave` or equivalent operation and record:

- frame ordering;
- connection boundaries;
- delays between frames;
- whether every configured channel is written;
- behavior when a channel has no schedule or no daily dose.

## Verification artifacts to collect

For each capture, record:

- My Chihiros app version;
- device model and firmware;
- device address redacted to a stable identifier;
- configured channel count;
- exact UI action;
- BLE writes including message IDs and checksums where available;
- notification frames;
- inter-frame delays;
- whether the operation was retried;
- observed device behavior.

Do not commit raw Bluetooth addresses, tokens, or other secrets.

## Existing documented evidence

The repository currently documents these behaviors as supported by reverse engineering:

- Running stirrer speed changes stop the channel, send `0xA5/42`, then restart.
- Stirrer speed is carried by `stirrerPreSecond`.
- Dosing lifetime and daily totals use separate `[4]` and `[5]` queries.
- Manual-dose wire volumes use 0.1 mL buckets and support the two-byte range.
- Stirrer timer workloads use the dosing-pump 0.6 mL/min equivalence.
- The original stirrer UI does not parse stirrer notifications.

These documented claims should be treated as the starting point for the capture work, not as a substitute for verifying the open questions above.

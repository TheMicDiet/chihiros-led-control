---
name: verify-manual-dose
description: Check that manual dosing and timed calibration are not automatically replayed after an ambiguous Bluetooth write. Use when changing these operations, command dispatch, BLE retry or write handling, or their protocol encoding.
---

# Verify manual-dose replay safety

**Property:** One call to `ChihirosDosingPump.dose_ml` or timed
`calibrate_channel` must not cause the simulated pump to accept its physical-run
command more than once, even when a write fails after acceptance. Raw slave
`ChihirosMagStirrer.send_frame` calls must likewise not resend accepted run
frames. The slave check counts accepted wire frames, not physical stirrer runs.

Run from the repository root:

```sh
uv --cache-dir .uv-cache sync
uv --cache-dir .uv-cache run --no-sync python scripts/verify_manual_dose.py
uv --cache-dir .uv-cache run --no-sync python scripts/sync_vendor.py --check
```

The verifier uses the project's `.python-version` and locked Python dependencies; no separate model-checker installation is needed. The second command must print `PASS` and exit zero. The third verifies that Home Assistant's vendored code matches the canonical library; if the vendor copy is out of sync, the verifier's result alone does not cover the deployed integration.

## What is explored

`scripts/verify_manual_dose.py` executes the actual drivers, dispatch, transport
retry loop, batch writer, and frame writer. It enumerates 729 sequences of six
abstract write outcomes for manual dosing: success, failure before acceptance,
or failure after acceptance. Six writes cover two complete attempts of the
three-frame batch. Timed calibration and each of the two raw slave run-frame
broadcasts enumerate nine sequences of two outcomes, covering two attempts of
each single-frame operation. The total is 756 scenarios.

The harness substitutes a simulated BLE connection, no-op disconnect/timer, and
instantaneous sleeps. It counts accepted `0xA5/27` manual-dose frames and
`0xA5/22` timed calibration frames. Exceeding a bound fails rather than silently
truncating exploration. An all-success path must accept the operation once, so
a missing command cannot be mistaken for replay safety.

Checks use channel 0, 1 mL for manual dosing and 10 seconds for calibration.
Write outcomes are independent and reported failures are `BleakError`. For pump
safety, each accepted physical-run frame is assumed to cause exactly one run;
the raw slave checks make no assumption about physical response.

Not covered: other channels, volumes or durations, recorded-volume calibration,
hardware, firmware deduplication, connection/prelude behavior, notification-wait
failures, cancellation, concurrent calls, other commands, caller-initiated
retries, or unlimited attempts. A pass establishes only the bounded simulated
environment, not aquarium safety.

## Interpret and maintain

On failure, inspect the `(command mode, outcome)` trace: `after` means acceptance
preceded the observed failure. Duplicate accepted pump run frames can deliver
another dose under the model; duplicate raw slave frames establish replay, not
its physical effect. Compare the trace with the implementation before deciding
whether it is a product bug or harness drift. Never suppress a real finding or
modify product code merely to make the verification pass.

Original pump exploration baseline:
`b32e91d7ea5071421fc1692916bb608e89e7e5b1`. Coverage now includes the raw slave
sender and its single-attempt policy:

- `src/chihiros_led_control/devices/dosing.py`: `ChihirosDosingPump.dose_ml` and `calibrate_channel`.
- `src/chihiros_led_control/devices/stirrer.py`: `ChihirosMagStirrer.send_frame`.
- `src/chihiros_led_control/devices/base.py`: `_send_command`.
- `src/chihiros_led_control/transport.py`: `BleTransport.send`, `_send_attempts`, `_send_attempt`, `_send_transaction`, `_write_frames`, and `_write_frame`.
- `src/chihiros_led_control/protocol/dosing.py`: manual-dose, calibration and authorization frame builders; `protocol/frame.py`: command framing.
- `scripts/verify_manual_dose.py`: simulated BLE boundary and outcome exploration.

When covered code changes, compare the implementation with these assumptions
and update the harness and bounds if the intended protocol changes. Rerun the
verifier and vendor check. Demonstrate sensitivity by temporarily forcing two
attempts for manual dosing, timed calibration, and each raw slave run-frame
send separately: every mutation must report duplicate acceptance. Runtime-only
overrides avoid changing working-tree files. Restore the original behavior and
leave no scratch mutations. Advance the baseline commit only after checking
the assumptions at the new commit.

# Architecture

`src/chihiros_led_control/` is the source of truth for the reusable Python
library and CLI. Home Assistant code lives in `custom_components/chihiros/` and
acts as an adapter around a vendored runtime copy of that library.

The proposed capability-based plugin architecture is described in
[plugin-architecture.md](plugin-architecture.md). It is a clean-break target
design; the current implementation is reference material, not a compatibility
constraint. Verification against the beta is documented in
[plugin-architecture-beta-verification.md](plugin-architecture-beta-verification.md).

The vendored package at
`custom_components/chihiros/vendor/chihiros_led_control/` exists so HACS
installs remain self-contained. Do not edit vendored files directly. Make
library changes in `src/chihiros_led_control/`, then run:

```bash
uv run python scripts/sync_vendor.py
```

CI checks the copy with:

```bash
uv run python scripts/sync_vendor.py --check
```

# Home Assistant entity and schedule decisions

## Entity model

The new implementation is free to redesign the entity model. Each HA plugin
should choose the entity types and grouping that best represent its capability.
For example, an LED plugin may expose a native RGB/RGBW entity where that is
appropriate, while retaining independent channel controls where the protocol
requires them. Entity IDs and semantics should be deterministic and documented
once the new architecture is released.

## Schedule replacement failures

The BLE protocol exposes reset and add operations but no atomic replacement or reliable read-back suitable for
rollback. The `set_schedule` service validates the complete replacement before resetting the device. If adding any
period then fails, it attempts another reset so the result is empty rather than a partial, unpredictable schedule, and
raises a Home Assistant error explaining that the old schedule cannot be restored. If that cleanup reset also fails,
the error is logged because the device may contain a partial replacement.

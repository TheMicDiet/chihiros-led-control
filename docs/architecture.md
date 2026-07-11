# Architecture

`src/chihiros_led_control/` is the source of truth for the reusable Python
library and CLI. Home Assistant code lives in `custom_components/chihiros/` and
acts as an adapter around a vendored runtime copy of that library.

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

## Color entities

The integration intentionally retains one brightness entity per physical channel. Converting existing RGB/WRGB
devices to one native RGB/RGBW entity would change unique IDs and remove entities referenced by existing dashboards
and automations. It would also be lossy: Home Assistant's RGB model normalizes three channel values through a shared
brightness value, while the device protocol supports independent channel intensities. A future combined entity may be
added as an opt-in companion only after an entity-registry migration and round-trip color semantics are designed.

## Schedule replacement failures

The BLE protocol exposes reset and add operations but no atomic replacement or reliable read-back suitable for
rollback. The `set_schedule` service validates the complete replacement before resetting the device. If adding any
period then fails, it attempts another reset so the result is empty rather than a partial, unpredictable schedule, and
raises a Home Assistant error explaining that the old schedule cannot be restored. If that cleanup reset also fails,
the error is logged because the device may contain a partial replacement.

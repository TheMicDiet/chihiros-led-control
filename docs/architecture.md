# Architecture

`src/chihiros_led_control/` is the source of truth for the reusable Python
library and CLI. Home Assistant code lives in
`custom_components/chihiros/` and imports a vendored runtime copy of that
library.

The vendored package at
`custom_components/chihiros/vendor/chihiros_led_control/` keeps HACS installs
self-contained. Do not edit vendored files directly. Make library changes in
`src/chihiros_led_control/`, then run:

```bash
uv --cache-dir .uv-cache run python scripts/sync_vendor.py
```

CI checks the copy with:

```bash
uv --cache-dir .uv-cache run python scripts/sync_vendor.py --check
```

## Schedule replacement failures

The BLE protocol exposes reset and add operations but no atomic replacement or
reliable read-back suitable for rollback. `set_schedule` validates the complete
replacement before resetting the device. If adding any period fails, it
attempts another reset so the result is empty rather than a partial,
unpredictable schedule, then raises a Home Assistant error explaining that the
old schedule cannot be restored. If that cleanup reset also fails, the error is
logged because the device may contain a partial replacement.

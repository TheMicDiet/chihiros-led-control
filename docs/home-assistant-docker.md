# Home Assistant Docker Test Environment

This compose setup runs Home Assistant with the local `custom_components/chihiros` directory mounted into `/config/custom_components/chihiros`.

## UI and Config Flow Testing

Use this on Docker Desktop or any machine where you only need to verify Home Assistant starts and loads the integration:

```bash
docker compose up
```

Open <http://localhost:8123>, complete the first-run Home Assistant onboarding, then add the Chihiros integration from **Settings -> Devices & services -> Add integration**.

The default Docker Compose setup enables development fake devices with `CHIHIROS_FAKE_DEVICES=1`. These fake devices appear in the Chihiros integration device picker and let you test entity setup, controls, firmware/runtime sensors, and schedule snapshot attributes without Bluetooth hardware. To disable them:

```bash
CHIHIROS_FAKE_DEVICES=0 docker compose up
```

## Bluetooth Hardware Testing on Linux

Real BLE discovery/control from a Home Assistant container needs access to the host Bluetooth stack. On Linux hosts with BlueZ and D-Bus available, use the Bluetooth compose file:

```bash
docker compose -f docker-compose.bluetooth.yml up
```

This uses host networking, privileged mode, and mounts `/run/dbus` read-only. Docker Desktop on Windows/macOS usually cannot expose the host Bluetooth adapter to Linux containers, so use this mode on a Linux host for real Chihiros device testing.

## Reset Local Home Assistant State

Home Assistant runtime files live under `dev/homeassistant/config/` and are ignored by git, except for `configuration.yaml`. To reset the test instance, stop the container and delete the generated files in that directory while keeping `configuration.yaml`.

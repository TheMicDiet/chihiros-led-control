# Capturing Bluetooth Traffic From Chihiros Devices

These instructions explain how to record the Bluetooth Low Energy traffic
between the My Chihiros app and a Chihiros device. The steps are
device-independent: they apply to any Chihiros light, heater, dosing pump, or
other accessory. Captures are used to reverse-engineer the device protocol and
add support to this project.

The recommended method works on most Android phones without root access. It
records the app's actual BLE traffic because logging happens at the Bluetooth
controller level rather than inside a single app. It also records other
Bluetooth traffic generated while logging is enabled, so keep the capture
focused and disable unrelated devices where possible.

## What a useful capture looks like

A good capture contains one slow, deliberate session. Which actions to perform
depends on the device, but as a rule of thumb: open the official app and
exercise every control it exposes. A typical session is:

1. Connect to the device.
2. Toggle the main power on, then off.
3. Change the main setting to a few different values (brightness, target
   temperature, dosage, speed, ...).
4. Change any other setting the device exposes (schedule, timer, calibration...).
5. Refresh or read the status.
6. Disconnect.

Keep about 5 seconds between actions and write down each action with its clock
time. Repeating the whole session once makes command patterns much easier to
decode, because each command then appears twice. Turn off other Bluetooth
devices (watch, earbuds, car, ...) before capturing to reduce unrelated traffic.
This does not guarantee that the log contains only the target device.

## Method 1 (recommended): Android Bluetooth HCI snoop log

Usually no root is required on supported Android phones. This captures the
traffic of the real My Chihiros app.

### 1. Enable developer options

Settings → About phone → tap "Build number" 7 times until
"You are now a developer" appears.

### 2. Enable the Bluetooth log

Settings → Developer options → enable **Bluetooth HCI snoop log**.

On many phones, restart Bluetooth after enabling the setting so logging begins.
Android versions and manufacturers handle existing log files differently, so do
not rely on this to clear old entries; note the capture start and end times
instead.

### 3. Perform the actions in the app

Open My Chihiros, connect to your device, and go through the actions slowly as
described above. Note the time of each action.

### 4. Stop the capture

When finished, toggle Bluetooth off and on again to help flush the capture, then
disable **Bluetooth HCI snoop log**. Wait a few seconds before copying the file.
The log slows Bluetooth down, so leave the setting off afterwards.

### 5. Get the log file

Option A — file manager or USB copy (works on many phones):

- The log is often written to internal storage as `btsnoop_hci.log`. Check the
  root of the internal storage with a file manager, or connect the phone to a
  computer over USB and copy the file.

Option B — adb (a useful fallback when the file is not visible):

- On the phone: Developer options → USB debugging → on.
- Connect the phone to a computer, install
  [platform-tools](https://developer.android.com/tools/releases/platform-tools)
  if needed, and run:

  ```bash
  adb bugreport
  ```

  This produces a zip archive after about a minute. Open it and copy
  `FS/data/misc/bluetooth/logs/btsnoop_hci.log` (search the zip for
  `btsnoop_hci.log` if the path differs).

  On many phones this shortcut also works:

  ```bash
  adb pull /sdcard/btsnoop_hci.log
  ```

Attach the log file to the GitHub issue. Rename it to `btsnoop_hci.log` if it
has no extension, so the file type is obvious. Raw HCI logs can contain
Bluetooth addresses and traffic from unrelated devices, so capture with those
devices disabled where possible and mention any privacy concerns in the issue.

### Optional: peek at the log in Wireshark

- Install [Wireshark](https://www.wireshark.org/) (free).
- File → Open → select the log and choose "All files" in the file dialog.
- Display filter `btatt` shows the GATT traffic.
- Phone → device writes: `btatt.opcode == 0x12` (Write Request) or
  `btatt.opcode == 0x52` (Write Command).
- Device → phone: `btatt.opcode == 0x1b` (Handle Value Notification).

Even without analyzing it yourself, attaching the raw log is the most helpful
thing.

## Method 2 (optional): confirm the GATT service with nRF Connect

Not needed for the capture itself, but extremely useful: it tells us whether
the device uses the same Nordic UART service as the other supported devices
(`6e400001-b5a3-f393-e0a9-e50e24dcca9e`) or a completely different one.

- Install [nRF Connect for Android](https://www.nordicsemi.com/Products/Development-tools/nrf-connect-for-mobile)
  (free, no root).
- Scan → find your device → note the advertised name shown under the device
  (prefixes like `DY...` are typical).
- Connect → take a screenshot of the service/characteristic list and include it
  in the issue.
- The app also has a built-in logger (menu → Logger) if you want to log manual
  interactions; logs can be exported for analysis.

## If you only have an iPhone

There is no broadly available equivalent to Android's no-root HCI-snoop
workflow on iOS. Mac-based logging or diagnostic options may be available, but
the fastest path is usually borrowing an Android phone for about 10 minutes and
following Method 1. If that is not possible, say so on the issue and we will
suggest alternatives.

## Checklist for the issue

- [ ] Capture file (`btsnoop_hci.log` or similar)
- [ ] Phone model and Android version
- [ ] My Chihiros app version
- [ ] Physical product/model variant (for example, A2 or WRGB II Slim)
- [ ] Device firmware version, if shown by the app
- [ ] Device name as shown in the app or in nRF Connect
- [ ] List of performed actions with approximate times
- [ ] (Optional) nRF Connect screenshot of the services

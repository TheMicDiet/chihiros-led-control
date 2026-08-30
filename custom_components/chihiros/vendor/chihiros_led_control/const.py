"""Chihiros consts module."""

# Nordic UART Service (used by SeaLed / NewBleLed / VIVID III devices)
UART_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
UART_RX_CHAR_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
UART_TX_CHAR_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"

# HM-10 write service (BleLed devices). Two hardware generations exist:
#
# - Newer classic: FFE1 is write-only; notifications arrive on the vendor-specific
#   8ec90000 service, characteristic 8ec90003 (verified against the official app,
#   provider/bluetooth.dart _prepareToTransport / _setNotify).
# - Older Telink generation (e.g. "RGB A Plus", DYARGB…, TLSR8266): FFE1 itself is
#   full duplex [notify, write, write-without-response] and there is NO 8ec90003
#   service (verified from an nRF Connect GATT dump, 2026-08). FFE2 exists on that
#   hardware but has no properties at all — it can never carry notifications.
HM10_SERVICE_UUID = "0000FFE0-0000-1000-8000-00805F9B34FB"
HM10_RX_CHAR_UUID = "0000FFE1-0000-1000-8000-00805F9B34FB"
CUSTOM_NOTIFY_SERVICE_UUID = "8EC90000-F315-4F60-9FB8-838830DAEA50"
CUSTOM_NOTIFY_CHAR_UUID = "8EC90003-F315-4F60-9FB8-838830DAEA50"

# Telink-generation vendor notify service (ffaa/ffab) is exposed by some devices
# but the official app never subscribes to it; kept for reference only.
TELINK_NOTIFY_SERVICE_UUID = "0000FFAA-0000-1000-8000-00805F9B34FB"
TELINK_NOTIFY_CHAR_UUID = "0000FFAB-0000-1000-8000-00805F9B34FB"

# Deprecated alias: the old (incorrect) FFE2 notify UUID has been replaced by
# the verified 8ec90003 notify characteristic. Kept so out-of-tree references
# resolve to the corrected UUID.
HM10_TX_CHAR_UUID = CUSTOM_NOTIFY_CHAR_UUID

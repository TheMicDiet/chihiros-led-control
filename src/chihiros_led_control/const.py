"""Chihiros consts module."""

# Nordic UART Service (used by SeaLed / NewBleLed / VIVID III devices)
UART_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
UART_RX_CHAR_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
UART_TX_CHAR_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"

# HM-10 write service (BleLed devices). Classic HM-10 hardware exposes:
#
# - FFE1 as the write characteristic; notifications arrive on the vendor-specific
#   8ec90000 service, characteristic 8ec90003 (verified against the official app,
#   provider/bluetooth.dart _prepareToTransport / _setNotify).
# - Some devices (e.g. "RGB A Plus") expose FFE1 as full duplex
#   [notify, write, write-without-response] with no 8ec90003 characteristic, so
#   the client falls back to fire-and-forget without a notify subscription.
HM10_SERVICE_UUID = "0000FFE0-0000-1000-8000-00805F9B34FB"
HM10_RX_CHAR_UUID = "0000FFE1-0000-1000-8000-00805F9B34FB"
CUSTOM_NOTIFY_SERVICE_UUID = "8EC90000-F315-4F60-9FB8-838830DAEA50"
CUSTOM_NOTIFY_CHAR_UUID = "8EC90003-F315-4F60-9FB8-838830DAEA50"

# Deprecated alias: the old (incorrect) FFE2 notify UUID has been replaced by
# the verified 8ec90003 notify characteristic. Kept so out-of-tree references
# resolve to the corrected UUID.
HM10_TX_CHAR_UUID = CUSTOM_NOTIFY_CHAR_UUID

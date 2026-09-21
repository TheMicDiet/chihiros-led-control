"""LED family driver."""

from ..client import ChihirosDevice as _LegacyLedDevice


class ChihirosDevice(_LegacyLedDevice):
    """Public LED client."""


__all__ = ["ChihirosDevice"]

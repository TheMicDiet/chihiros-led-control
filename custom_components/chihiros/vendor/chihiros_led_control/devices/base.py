"""Common runtime device surface.

The BLE lifecycle remains implemented by the established client while family
modules provide the stable import boundaries for the architecture transition.
"""

from ..client import ChihirosDevice as BaseChihirosDevice

__all__ = ["BaseChihirosDevice"]

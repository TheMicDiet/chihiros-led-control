"""Expose the doser Typer app for mounting under chihirosctl.

This file re-exports the Typer `app` defined in
`custom_components.chihiros.chihiros_ch4_control/device/ch4_device.py`
and *extends the same app instance* with a few extra helper commands.
"""

from __future__ import annotations

from typing import List

import typer
from typing_extensions import Annotated

# Import the existing doser CLI app and (optionally) the device class
from .device.ch4_device import app as app
from .device.ch4_device import Ch4Device  # used by the extra helpers below




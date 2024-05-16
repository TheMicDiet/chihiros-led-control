"""WRGB II Pro device Model."""

from .base_device import BaseDevice


class WRGBIIPro(BaseDevice):
    """Chihiros WRGB II Pro device Class."""

    _model_name = "WRGB II Pro"
    _model_codes = ["DYWPRO"]
    # FIXME: channel for white doesn't work
    _colors: dict[str, int] = {
        "white": 0,
        "red": 1,
        "green": 2,
        "blue": 3,
    }

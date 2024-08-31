"""Backlight device Model."""

from .base_device import BaseDevice


class Backlight(BaseDevice):
    """Chihiros LED Backlight device Class."""

    _model_name = "Backlight"
    _model_codes = ["DYCOM"]
    _colors: dict[str, int] = {
        "white": 0,
    }

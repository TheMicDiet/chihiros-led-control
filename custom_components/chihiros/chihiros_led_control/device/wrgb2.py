"""WRGB II device Model."""

from .base_device import BaseDevice


class WRGBII(BaseDevice):
    """Chihiros WRGB II device Class."""

    _model_name = "WRGB II"
    _model_code = ["DYNWRGB", "DYNW30", "DYNW45", "DYNW60", "DYNW90", "DYNW120"]
    _colors: dict[str, int] = {
        "white": 0,
        "red": 1,
        "green": 2,
        "blue": 3,
    }

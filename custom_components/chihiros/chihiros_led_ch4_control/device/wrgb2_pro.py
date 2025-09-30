"""WRGB II Pro device Model."""

from ...chihiros_led_control.device.base_device import BaseDevice


class WRGBIIPro(BaseDevice):
    """Chihiros WRGB II Pro device Class."""

    _model_name = "WRGB II Pro"
    _model_codes = [
        "DYWPRO30",
        "DYWPRO45",
        "DYWPRO60",
        "DYWPRO80",
        "DYWPRO90",
        "DYWPR120",
    ]
    _colors: dict[str, int] = {
        "red": 0,
        "green": 1,
        "blue": 2,
        "white": 3,
    }

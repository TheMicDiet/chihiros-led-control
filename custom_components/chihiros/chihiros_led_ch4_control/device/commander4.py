"""Commander 4 device Model."""

from ...chihiros_led_control.device.base_device import BaseDevice


class Commander4(BaseDevice):
    """Chihiros Commander 4 device Class."""

    _model_name = "Commander 4"
    _model_codes = ["DYLED"]
    _colors: dict[str, int] = {"white": 0, "red": 0, "green": 1, "blue": 2}

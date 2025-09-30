"""Commander 1 device Model."""

from ...chihiros_led_control.device.base_device import BaseDevice


class Commander1(BaseDevice):
    """Chihiros Commander 1 device Class."""

    _model_name = "Commander 1"
    _model_codes = ["DYCOM"]
    _colors: dict[str, int] = {"white": 0, "red": 0, "green": 1, "blue": 2}

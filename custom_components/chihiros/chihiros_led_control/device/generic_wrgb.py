"""Generic WRGB device Model."""

from .base_device import BaseDevice


class GenericWRGB(BaseDevice):
    """Chihiros Generic WRGB LED Class."""

    _model_name = "Generic WRGB"
    _model_codes = []
    _colors: dict[str, int] = {"white": 3, "red": 0, "green": 1, "blue": 2}

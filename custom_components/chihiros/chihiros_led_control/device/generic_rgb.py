"""Generic RGB device Model."""

from .base_device import BaseDevice


class GenericRGB(BaseDevice):
    """Chihiros GenericRGB Class."""

    _model_name = "Generic RGB"
    _model_codes = []
    _colors: dict[str, int] = {"red": 0, "green": 1, "blue": 2}

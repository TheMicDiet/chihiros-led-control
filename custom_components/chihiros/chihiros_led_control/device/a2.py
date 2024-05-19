"""A2 device Model."""

from .base_device import BaseDevice


class AII(BaseDevice):
    """Chihiros A II device Class."""

    _model_name = "A II"
    _model_codes = ["DYNA2", "DYNA2N"]
    _colors: dict[str, int] = {
        "white": 0,
    }

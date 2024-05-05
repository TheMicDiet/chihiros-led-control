"""WRGB II device Model."""

from .base_device import BaseDevice


class WRGBII(BaseDevice):
    """Chihiros WRGB II device Class."""

    _model_name = "WRGB II"
    _model_code = "DYNWRGB"
    _colors: dict[str, int] = {
        # TODO validate that
        "white": 0,
        "red": 1,
        "green": 2,
        "blue": 3,
    }

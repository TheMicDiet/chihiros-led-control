"""CII device Model."""

from .base_device import BaseDevice


class CII(BaseDevice):
    """Chihiros CII device Class."""

    _model_name = "CII"
    _model_code = ["DYNC2N"]
    _colors: dict[str, int] = {
        "white": 0,
    }

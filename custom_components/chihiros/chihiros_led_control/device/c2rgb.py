"""CII RGB device Model."""

from .base_device import BaseDevice


class CIIRGB(BaseDevice):
    """Chihiros CII RGB device Class."""

    _model_name = "C II RGB"
    _model_codes = ["DYNCRGP", "DYNCRGB"]
    _colors: dict[str, int] = {
        "red": 0,
        "green": 1,
        "blue": 2,
    }

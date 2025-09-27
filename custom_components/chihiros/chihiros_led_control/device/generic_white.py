"""Generic White LED device Model."""

from .base_device import BaseDevice


class GenericWhite(BaseDevice):
    """Chihiros Generic White LED Class."""

    _model_name = "Generic White LED"
    _model_codes = []
    _colors: dict[str, int] = {"white": 0}

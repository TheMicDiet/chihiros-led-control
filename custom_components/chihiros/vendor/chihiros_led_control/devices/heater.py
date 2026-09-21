"""Heater family driver."""

from ..client import ChihirosHeater as _LegacyHeater


class ChihirosHeater(_LegacyHeater):
    """Heater driver without LED, fan, or lighting schedule operations."""

    _LED_ONLY = frozenset(
        {
            "set_brightness",
            "turn_on",
            "turn_off",
            "add_setting",
            "remove_setting",
            "reset_settings",
            "set_auto_point",
            "set_auto_curve",
            "enable_auto_mode",
            "set_manual_mode",
            "set_fan_speed",
            "set_fan_auto",
            "set_fan_start_stop_temp",
            "set_temp_protect",
            "set_bluetooth_led",
        }
    )

    def __getattribute__(self, name: str):
        """Keep unrelated LED operations outside the heater surface."""
        if name in ChihirosHeater._LED_ONLY:
            raise AttributeError(name)
        return super().__getattribute__(name)


__all__ = ["ChihirosHeater"]

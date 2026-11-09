"""Mapping validated against the owner's commercial indoor unit."""

from __future__ import annotations

import math

from gree_local import __version__
from gree_local.types import JsonObject, Properties

MODES = ["auto", "cool", "dry", "fan_only", "heat"]
FANS = ["auto", "low", "medium-low", "medium", "medium-high", "high"]
SWINGS = [
    "default",
    "full_swing",
    "fixed_upper",
    "fixed_upper_middle",
    "fixed_middle",
    "fixed_lower_middle",
    "fixed_lower",
]
HORIZONTAL = [
    "default",
    "full_swing",
    "left",
    "left_center",
    "center",
    "right_center",
    "right",
]
# MQTT custom swing options have no HA translation catalog. Translate the
# discovery-facing labels while retaining canonical protocol values on MQTT.
SWING_LABELS_EN = {
    "default": "Default",
    "full_swing": "Full swing",
    "fixed_upper": "Top",
    "fixed_upper_middle": "Upper middle",
    "fixed_middle": "Middle",
    "fixed_lower_middle": "Lower middle",
    "fixed_lower": "Bottom",
}
HORIZONTAL_LABELS_EN = {
    "default": "Default",
    "full_swing": "Full swing",
    "left": "Left",
    "left_center": "Left center",
    "center": "Center",
    "right_center": "Right center",
    "right": "Right",
}
SWING_LABELS_IT = {
    "default": "Predefinita",
    "full_swing": "Oscillazione completa",
    "fixed_upper": "In alto",
    "fixed_upper_middle": "Centro-alto",
    "fixed_middle": "Al centro",
    "fixed_lower_middle": "Centro-basso",
    "fixed_lower": "In basso",
}
HORIZONTAL_LABELS_IT = {
    "default": "Predefinita",
    "full_swing": "Oscillazione completa",
    "left": "A sinistra",
    "left_center": "Centro-sinistra",
    "center": "Al centro",
    "right_center": "Centro-destra",
    "right": "A destra",
}
COLS = [
    "Pow",
    "Mod",
    "SetTem",
    "TemSen",
    "TemUn",
    "TemRec",
    "HalfTemEn",
    "SetDeciTem",
    "Add0.5",
    "WdSpd",
    "SwingLfRig",
    "SwUpDn",
    "SwhSlp",
    "SlpMod",
    "Lig",
    "Quiet",
    "Tur",
    "StHt",
    "SvSt",
    "Air",
    "Blo",
    "Health",
]


def state_from_properties(
    p: Properties, current_temperature: float | None = None
) -> JsonObject:
    mode = MODES[p.get("Mod", 0)] if p.get("Mod", 0) in range(5) else "auto"
    fan = FANS[p.get("WdSpd", 0)] if p.get("WdSpd", 0) in range(6) else "auto"
    temp = p.get("SetDeciTem", p.get("SetTem", 20) * 10) / 10
    preset = (
        "eco"
        if p.get("SvSt")
        else "away"
        if p.get("StHt")
        else "boost"
        if p.get("Tur")
        else "sleep"
        if p.get("SwhSlp")
        else "none"
    )
    out = {
        "mode": mode if p.get("Pow") else "off",
        "temperature": temp,
        "fan_mode": fan,
        "preset_mode": preset,
        "swing_mode": SWINGS[p.get("SwUpDn", 0)]
        if p.get("SwUpDn", 0) in range(len(SWINGS))
        else "default",
        "swing_horizontal_mode": HORIZONTAL[p.get("SwingLfRig", 0)]
        if p.get("SwingLfRig", 0) in range(7)
        else "default",
    }
    # This unit does not report TemSen. Use its existing local room sensor.
    if current_temperature is not None:
        out["current_temperature"] = current_temperature
    elif isinstance(p.get("TemSen"), (int, float)):
        out["current_temperature"] = p["TemSen"]
    return out


def commands_for(field: str, value: str, p: Properties) -> list[Properties]:
    """Return ordered property updates; power is applied after mode changes."""
    if field == "mode":
        if value == "off":
            return [{"Pow": 0}]
        if value not in MODES:
            raise ValueError("Unsupported HVAC mode")
        return [{"Mod": MODES.index(value)}, {"Pow": 1}]
    if field == "temperature":
        temp = float(value)
        if (
            not math.isfinite(temp)
            or not 8 <= temp <= 30
            or temp * 2 != round(temp * 2)
        ):
            raise ValueError("Temperature must be 8–30°C in 0.5°C steps")
        if temp % 1 and p.get("HalfTemEn") != 1:
            raise ValueError("Device has not reported half-degree support")
        # This commercial controller truncates SetDeciTem when SetTem is sent
        # in the same pack. A decimal-only command is confirmed by readback.
        if "SetDeciTem" in p:
            return [{"SetDeciTem": round(temp * 10)}]
        return [{"SetTem": int(temp), "TemRec": int(bool(temp % 1))}]
    if field == "fan_mode":
        return [{"WdSpd": FANS.index(value)}]
    if field == "swing_mode":
        return [{"SwUpDn": SWINGS.index(value)}]
    if field == "swing_horizontal_mode":
        return [{"SwingLfRig": HORIZONTAL.index(value)}]
    if field == "preset_mode":
        if value not in ["none", "eco", "away", "boost", "sleep"]:
            raise ValueError("Unsupported preset")
        return [
            {
                k: int(value == name)
                for k, name in [
                    ("SvSt", "eco"),
                    ("StHt", "away"),
                    ("Tur", "boost"),
                    ("SwhSlp", "sleep"),
                ]
            }
        ]
    switches = {
        "light": "Lig",
        "quiet": "Quiet",
        "xfan": "Blo",
        "health": "Health",
        "fresh_air": "Air",
    }
    if field in switches:
        if value not in ["ON", "OFF"]:
            raise ValueError("Expected ON or OFF")
        return [{switches[field]: int(value == "ON")}]
    raise ValueError("Unknown command")


def discovery(
    unit: str, name: str, model: str, language: str = "en"
) -> dict[str, JsonObject]:
    if language not in ("en", "it"):
        raise ValueError("language must be en or it")
    base = "gree_local/" + unit
    device = {
        "identifiers": ["gree_local_" + unit + "_climate"],
        "name": name,
        "manufacturer": "Gree",
        "model": model,
        "sw_version": "gree-local-cloud " + __version__,
    }
    climate = {
        "name": None,
        "unique_id": "gree_local_" + unit + "_climate",
        "device": device,
        "availability_topic": base + "/availability",
        "modes": MODES + ["off"],
        "fan_modes": FANS,
        "swing_modes": SWINGS,
        "swing_horizontal_modes": HORIZONTAL,
        "preset_modes": ["eco", "away", "boost", "sleep"],
        "min_temp": 8,
        "max_temp": 30,
        "temp_step": 0.5,
        "precision": 0.1,
        "temperature_unit": "C",
        "current_temperature_topic": base + "/room_temperature",
    }
    for field in [
        "mode",
        "temperature",
        "fan_mode",
        "swing_mode",
        "swing_horizontal_mode",
        "preset_mode",
    ]:
        climate[field + "_command_topic"] = base + "/command/" + field
        climate[field + "_state_topic"] = base + "/state"
        climate[field + "_state_template"] = "{{ value_json." + field + " }}"
    for field, labels in [
        ("swing_mode", SWING_LABELS_IT if language == "it" else SWING_LABELS_EN),
        (
            "swing_horizontal_mode",
            HORIZONTAL_LABELS_IT if language == "it" else HORIZONTAL_LABELS_EN,
        ),
    ]:
        climate[field + "s"] = list(labels.values())
        climate[field + "_state_template"] = (
            "{{ "
            + repr(labels)
            + ".get(value_json."
            + field
            + ", "
            + repr(labels["default"])
            + ") }}"
        )
        reverse = {label: value for value, label in labels.items()}
        climate[field + "_command_template"] = (
            "{{ " + repr(reverse) + ".get(value, value) }}"
        )
    result = {"homeassistant/climate/gree_local_" + unit + "_climate/config": climate}
    for field, label, prop in [
        ("light", "Panel light", "Lig"),
        ("quiet", "Quiet mode", "Quiet"),
        ("xfan", "XFan", "Blo"),
        ("health", "Health", "Health"),
        ("fresh_air", "Fresh air", "Air"),
    ]:
        result["homeassistant/switch/gree_local_" + unit + "_" + field + "/config"] = {
            "name": label,
            "unique_id": "gree_local_" + unit + "_" + field,
            "device": device,
            "availability_topic": base + "/availability",
            "command_topic": base + "/command/" + field,
            "state_topic": base + "/raw",
            "value_template": "{{ 'ON' if value_json.get('"
            + prop
            + "', 0) else 'OFF' }}",
        }
    return result

"""Shared helpers for Matter/Bacon (bacon_rac) entities."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

# A topics/meta payload is {"shadows": {"state": …, "schedule": …}, …}; the state
# block is the per-field capability map (type/enum/min/max/step and the ``ro``
# writability flag) that entities read.
_BACON_META_STATE_PATH: tuple[str, ...] = ("shadows", "state")

# Shadow ``*Enabled`` fields that are climate controls, not comfort features.
BACON_NON_FEATURE_FIELDS = frozenset({"powerEnabled", "hSwingEnabled", "vSwingEnabled"})

# Known comfort features -> translation_key, named after the HomeCom app (#162).
# Note: setbackEnabled is 8 °C frost heating, NOT eco or a generic setback.
BACON_FEATURE_KEYS: dict[str, str] = {
    "fullPowerEnabled": "bacon_full_power",  # "Boost"
    "ionizerEnabled": "bacon_ionizer",
    "sleepEnabled": "bacon_sleep",
    "setbackEnabled": "bacon_frost",  # "8 °C heating" (frost protection)
    "ecoEnabled": "bacon_eco",
    "breezeAwayEnabled": "bacon_breeze_away",  # "Wind avoid me"
    "savePlusEnabled": "bacon_save_plus",
}

BACON_DEHUMIDIFIER_MODES = ["auto", "night", "laundry"]
# Bosch protocol identifiers, including firmware suffixes such as _v2.
BACON_DEHUMIDIFIER_APPLIANCE_TYPES = ("dry_6000i", "dry_8000i")


def bacon_meta_state(metadata: dict | None) -> dict:
    """Return the state-field block of a topics/meta payload, or {} if absent.

    Each entry looks like ``{"type": ..., "ro": bool}`` plus ``enum`` for strings
    and ``min``/``max``/``step`` for numbers. Returning ``{}`` is normal — the
    payload is push-only and may not have arrived — and callers fall back to a
    default in that case.
    """
    node: Any = metadata
    for key in _BACON_META_STATE_PATH:
        node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            return {}
    return node if isinstance(node, dict) else {}


def humanize_feature(field: str) -> str:
    """Turn an unknown ``fooBarEnabled`` field into a readable fallback name."""
    core = field[: -len("Enabled")] if field.endswith("Enabled") else field
    spaced = re.sub(r"(?<!^)(?=[A-Z])", " ", core).strip()
    return spaced[:1].upper() + spaced[1:] if spaced else field


def bacon_feature_fields(reported: dict) -> list[str]:
    """Comfort-feature ``*Enabled`` fields this device actually reports."""
    return [
        field
        for field in reported
        if field.endswith("Enabled") and field not in BACON_NON_FEATURE_FIELDS
    ]


def bacon_is_dehumidifier(data: Any) -> bool:
    """Identify a 6000i/8000i from its appliance type or humidity capability."""
    if data is None or data.device.get("deviceType") != "bacon_rac":
        return False
    info = getattr(data, "info", None) or {}
    appliance_type = (info.get("appliance") or {}).get("type", "")
    if isinstance(appliance_type, str) and appliance_type:
        return any(
            appliance_type == model or appliance_type.startswith(f"{model}_")
            for model in BACON_DEHUMIDIFIER_APPLIANCE_TYPES
        )
    # The shadow is fetched at startup; info/meta may arrive later over MQTT.
    reported = getattr(data, "reported", None) or {}
    metadata = bacon_meta_state(getattr(data, "metadata", None))
    return (
        ("humiditySetpoint" in reported or "humiditySetpoint" in metadata)
        and "tempSetpoint" not in reported
        and "tempSetpoint" not in metadata
    )


def bacon_sensor_attributes(sensor: dict | None) -> dict[str, str]:
    """Expose the device's measurement time, when it is a valid timestamp."""
    timestamp = (sensor or {}).get("timestamp")
    if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
        try:
            return {
                "measured_at": datetime.fromtimestamp(
                    timestamp, timezone.utc
                ).isoformat()
            }
        except (ValueError, OverflowError, OSError):
            pass
    return {}

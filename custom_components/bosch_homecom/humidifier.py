"""Bosch HomeCom Custom Component."""

from __future__ import annotations

import math
from typing import Any

from homeassistant import config_entries, core
from homeassistant.components.humidifier import (
    HumidifierAction,
    HumidifierDeviceClass,
    HumidifierEntity,
    HumidifierEntityFeature,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homecom_alt import ApiError

from .bacon import (
    BACON_DEHUMIDIFIER_MODES,
    bacon_is_dehumidifier,
    bacon_meta_state,
    bacon_sensor_attributes,
)
from .coordinator import BoschComModuleCoordinatorBaconRac

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: config_entries.ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the BoschCom dehumidifiers."""
    coordinators = [
        coordinator
        for coordinator in config_entry.runtime_data
        if coordinator.data.device["deviceType"] == "bacon_rac"
    ]
    added_devices: set[str] = set()

    @callback
    def _async_add_dehumidifiers() -> None:
        entities = []
        for coordinator in coordinators:
            if coordinator.unique_id in added_devices or not bacon_is_dehumidifier(
                coordinator.data
            ):
                continue
            entities.append(BoschComBaconDehumidifier(coordinator=coordinator))
            added_devices.add(coordinator.unique_id)
        if entities:
            async_add_entities(entities)

    # Appliance identification may arrive after setup on the push-only topics.
    for coordinator in coordinators:
        config_entry.async_on_unload(
            coordinator.async_add_listener(_async_add_dehumidifiers)
        )
    _async_add_dehumidifiers()


class BoschComBaconDehumidifier(
    CoordinatorEntity[BoschComModuleCoordinatorBaconRac], HumidifierEntity
):
    """Dehumidifier controls for a Bosch 6000i/8000i using the MQTT shadow."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "bacon_dehumidifier"
    _attr_device_class = HumidifierDeviceClass.DEHUMIDIFIER
    _attr_supported_features = HumidifierEntityFeature.MODES

    def __init__(self, coordinator: BoschComModuleCoordinatorBaconRac) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.unique_id}-dehumidifier-control"
        self._attr_device_info = coordinator.device_info

    @property
    def _reported(self) -> dict:
        data = self.coordinator.data
        return (getattr(data, "reported", None) if data else None) or {}

    @property
    def _sensor(self) -> dict:
        data = self.coordinator.data
        return (getattr(data, "sensor", None) if data else None) or {}

    @property
    def _meta(self) -> dict:
        data = self.coordinator.data
        return bacon_meta_state(getattr(data, "metadata", None) if data else None)

    @property
    def available(self) -> bool:
        """Return whether the coordinator and device are online."""
        data = self.coordinator.data
        info = (getattr(data, "info", None) if data else None) or {}
        return super().available and info.get("online") is not False

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Expose when the device measured the current humidity."""
        return bacon_sensor_attributes(self._sensor)

    @property
    def is_on(self) -> bool | None:
        """Return the power state confirmed by the device."""
        value = self._reported.get("powerEnabled")
        return value if isinstance(value, bool) else None

    @property
    def current_humidity(self) -> float | None:
        """Return the measured relative humidity."""
        value = self._sensor.get("relHumidity")
        return value if type(value) in (int, float) else None

    @property
    def target_humidity(self) -> float | None:
        """Return the humidity setpoint confirmed by the device."""
        value = self._reported.get("humiditySetpoint")
        return value if type(value) in (int, float) else None

    def _setpoint_bound(self, key: str, default: float) -> float:
        """Return a numeric humiditySetpoint bound from metadata."""
        value = self._meta.get("humiditySetpoint", {}).get(key)
        return (
            value if type(value) in (int, float) and math.isfinite(value) else default
        )

    @property
    def min_humidity(self) -> float:
        """Return the minimum humidity setpoint."""
        return self._setpoint_bound("min", 40)

    @property
    def max_humidity(self) -> float:
        """Return the maximum humidity setpoint."""
        return self._setpoint_bound("max", 70)

    @property
    def target_humidity_step(self) -> float:
        """Return the humidity setpoint step."""
        step = self._setpoint_bound("step", 5)
        return step if step > 0 else 5

    @property
    def available_modes(self) -> list[str]:
        """Return supported dehumidifier modes from metadata."""
        values = self._meta.get("opMode", {}).get("enum")
        if isinstance(values, list) and values:
            return [mode for mode in BACON_DEHUMIDIFIER_MODES if mode in values]
        return list(BACON_DEHUMIDIFIER_MODES)

    @property
    def mode(self) -> str | None:
        """Return the operating mode confirmed by the device."""
        value = self._reported.get("opMode")
        return value if value in self.available_modes else None

    @property
    def action(self) -> HumidifierAction | None:
        """Return the device's current dehumidifying activity."""
        if self.is_on is False:
            return HumidifierAction.OFF
        if self.is_on is None:
            return None
        if any(
            self._sensor.get(key) is True
            for key in ("idleState", "waterTankFull", "defrostingActive")
        ):
            return HumidifierAction.IDLE
        if self._sensor.get("idleState") is False:
            return HumidifierAction.DRYING
        return None

    async def _async_set(self, payload: dict[str, Any]) -> None:
        """Publish writable settings; use reported state for confirmation."""
        for field in payload:
            capability = self._meta.get(field)
            # Moving from laundry to auto also enables target humidity.
            changing_to_auto = (
                field == "humiditySetpoint" and payload.get("opMode") == "auto"
            )
            if capability and capability.get("ro") is True and not changing_to_auto:
                raise ServiceValidationError(
                    f"{field} is read-only in the current mode"
                )
        try:
            await self.coordinator.client.async_set_desired(
                self.coordinator.unique_id, payload
            )
            await self.coordinator.async_request_refresh()
        except ApiError as err:
            raise HomeAssistantError(
                "Could not send the Bosch HomeCom command"
            ) from err

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the dehumidifier on."""
        await self._async_set({"powerEnabled": True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the dehumidifier off."""
        await self._async_set({"powerEnabled": False})

    async def async_set_mode(self, mode: str) -> None:
        """Set a supported operating mode."""
        if mode not in self.available_modes:
            raise ServiceValidationError(f"Unsupported operating mode: {mode}")
        await self._async_set({"opMode": mode})

    async def async_set_humidity(self, humidity: int) -> None:
        """Set target humidity, leaving laundry mode when necessary."""
        if type(humidity) not in (int, float) or not math.isfinite(humidity):
            raise ServiceValidationError("Target humidity must be a finite number")
        step = self.target_humidity_step
        offset = (humidity - self.min_humidity) / step
        if not self.min_humidity <= humidity <= self.max_humidity or not math.isclose(
            offset, round(offset)
        ):
            raise ServiceValidationError(
                f"Target humidity must be {self.min_humidity:g}–{self.max_humidity:g} % "
                f"in steps of {step:g} %"
            )
        payload: dict[str, Any] = {"humiditySetpoint": int(humidity)}
        if self.mode == "laundry":
            if "auto" not in self.available_modes:
                raise ServiceValidationError(
                    "Target humidity is unavailable in laundry mode"
                )
            payload["opMode"] = "auto"
        await self._async_set(payload)

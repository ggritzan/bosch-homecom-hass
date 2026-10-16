"""Bosch HomeCom Custom Component."""

from __future__ import annotations

from homeassistant import config_entries, core
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .bacon import bacon_is_dehumidifier, bacon_sensor_attributes
from .coordinator import (
    BoschComModuleCoordinatorBaconRac,
    BoschComModuleCoordinatorCommodule,
    BoschComModuleCoordinatorK40,
)

PARALLEL_UPDATES = 1

DEHUMIDIFIER_STATUS_FIELDS = (
    ("waterTankFull", "bacon_water_tank_full", BinarySensorDeviceClass.PROBLEM),
    ("defrostingActive", "bacon_defrosting", BinarySensorDeviceClass.RUNNING),
    ("condensatePumpRunning", "bacon_condensate_pump", BinarySensorDeviceClass.RUNNING),
    ("idleState", "bacon_idle", None),
)


async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: config_entries.ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the BoschCom binary sensors."""
    coordinators = config_entry.runtime_data
    entities = []
    added_dehumidifiers: set[str] = set()

    @callback
    def _async_add_dehumidifier_sensors() -> None:
        new_entities = []
        for coordinator in coordinators:
            if (
                coordinator.unique_id in added_dehumidifiers
                or not bacon_is_dehumidifier(coordinator.data)
            ):
                continue
            new_entities.extend(
                BoschComBaconDehumidifierStatus(
                    coordinator=coordinator,
                    field=field,
                    translation_key=translation_key,
                    device_class=device_class,
                )
                for field, translation_key, device_class in DEHUMIDIFIER_STATUS_FIELDS
            )
            added_dehumidifiers.add(coordinator.unique_id)
        if new_entities:
            async_add_entities(new_entities)

    for coordinator in coordinators:
        if coordinator.data.device["deviceType"] == "commodule":
            entities.append(BoschComCommoduleNetworkSensor(coordinator=coordinator))
        if coordinator.data.device["deviceType"] in ("k30", "k40"):
            for dev in coordinator.data.devices or []:
                if dev.get("rfConnectionStatus"):
                    dev_id = dev["id"].split("/")[-1]
                    entities.append(
                        BoschComThermostatRfStatusSensor(
                            coordinator=coordinator, dev_id=dev_id
                        )
                    )
        if coordinator.data.device["deviceType"] == "bacon_rac":
            # Comfort features use switch entities; add connectivity here.
            entities.append(BoschComBaconOnlineSensor(coordinator=coordinator))
            # Recheck the subtype when push-only info/meta arrives after setup.
            config_entry.async_on_unload(
                coordinator.async_add_listener(_async_add_dehumidifier_sensors)
            )
    async_add_entities(entities)
    _async_add_dehumidifier_sensors()


class BoschComCommoduleNetworkSensor(CoordinatorEntity, BinarySensorEntity):
    """Representation of a commodule network connectivity sensor."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: BoschComModuleCoordinatorCommodule,
    ) -> None:
        """Initialize binary sensor entity."""
        super().__init__(coordinator)
        self._attr_translation_key = "wb_network"
        self._attr_device_info = coordinator.device_info
        self._attr_unique_id = f"{coordinator.unique_id}-eth0-state"
        self._coordinator = coordinator

    @staticmethod
    def _get_state_value(state: dict | None) -> str | None:
        """Extract value from a state dict."""
        if isinstance(state, dict):
            return state.get("value")
        return None

    @property
    def is_on(self) -> bool | None:
        """Get network connectivity status (eth0 or wifi)."""
        eth0 = self._get_state_value(self._coordinator.data.eth0_state)
        wifi = self._get_state_value(self._coordinator.data.wifi_state)
        if eth0 is None and wifi is None:
            return None
        return eth0 in ("on", "connected") or wifi in ("on", "connected")

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Return which network interfaces are active."""
        eth0 = self._get_state_value(self._coordinator.data.eth0_state)
        wifi = self._get_state_value(self._coordinator.data.wifi_state)
        return {"eth0": eth0, "wifi": wifi}

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        eth0 = self._get_state_value(self._coordinator.data.eth0_state)
        wifi = self._get_state_value(self._coordinator.data.wifi_state)
        if eth0 is None and wifi is None:
            self._attr_is_on = None
        else:
            self._attr_is_on = eth0 in ("on", "connected") or wifi in (
                "on",
                "connected",
            )
        self.async_write_ha_state()


class BoschComThermostatRfStatusSensor(CoordinatorEntity, BinarySensorEntity):
    """Thermostat RF connection status binary sensor."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: BoschComModuleCoordinatorK40,
        dev_id: str,
    ) -> None:
        """Initialize RF status binary sensor."""
        super().__init__(coordinator)
        self._attr_translation_key = "thermostat_rf_status"
        self._attr_device_info = coordinator.device_info
        self._attr_unique_id = f"{coordinator.unique_id}-{dev_id}-rf_status"
        self._coordinator = coordinator
        self._dev_id = dev_id

    def _get_rf_status(self) -> str | None:
        """Get RF connection status value."""
        for dev in self._coordinator.data.devices or []:
            if dev.get("id", "").endswith(f"/{self._dev_id}"):
                return (dev.get("rfConnectionStatus") or {}).get("value")
        return None

    @property
    def is_on(self) -> bool | None:
        """Return True if RF connection is active."""
        status = self._get_rf_status()
        if status is None:
            return None
        return status.lower() in ("online", "connected", "on")

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        status = self._get_rf_status()
        if status is None:
            self._attr_is_on = None
        else:
            self._attr_is_on = status.lower() in ("online", "connected", "on")
        self.async_write_ha_state()


# --- Bacon (Matter-commissioned) RAC ------------------------------------------


class BoschComBaconOnlineSensor(CoordinatorEntity, BinarySensorEntity):
    """Connectivity of a bacon RAC, from the topics/info payload."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, coordinator: BoschComModuleCoordinatorBaconRac) -> None:
        """Initialize binary sensor entity."""
        super().__init__(coordinator)
        self._attr_translation_key = "bacon_online"
        self._attr_device_info = coordinator.device_info
        self._attr_unique_id = f"{coordinator.unique_id}-online"

    @property
    def _info(self) -> dict:
        data = self.coordinator.data
        return (getattr(data, "info", None) if data else None) or {}

    @property
    def is_on(self) -> bool | None:
        """Return the device's own online flag, or None before it is known."""
        value = self._info.get("online")
        return bool(value) if isinstance(value, bool) else None

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Expose the link quality the device reports alongside."""
        quality = (self._info.get("network") or {}).get("signalQuality")
        return {"signal_quality": quality} if quality else {}


class BoschComBaconDehumidifierStatus(CoordinatorEntity, BinarySensorEntity):
    """A tank, defrost, pump or idle flag from the MQTT sensor channel."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: BoschComModuleCoordinatorBaconRac,
        field: str,
        translation_key: str,
        device_class: BinarySensorDeviceClass | None = None,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._field = field
        self._attr_unique_id = f"{coordinator.unique_id}-dehumidifier-{field}"
        self._attr_device_info = coordinator.device_info
        self._attr_translation_key = translation_key
        self._attr_device_class = device_class

    @property
    def _sensor(self) -> dict:
        data = self.coordinator.data
        return (getattr(data, "sensor", None) if data else None) or {}

    @property
    def is_on(self) -> bool | None:
        """Return the reported status, or unknown before it is received."""
        value = self._sensor.get(self._field)
        return value if isinstance(value, bool) else None

    @property
    def available(self) -> bool:
        """Return whether the coordinator and device are online."""
        data = self.coordinator.data
        info = (getattr(data, "info", None) if data else None) or {}
        return super().available and info.get("online") is not False

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Expose when the device measured the status."""
        return bacon_sensor_attributes(self._sensor)

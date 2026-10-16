"""Exercise shared appliance classification, its cache and HA platform discovery."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.const import CONF_DEVICES, CONF_TOKEN
from homeassistant.helpers import entity_registry as er
from homecom_alt import BHCDeviceBaconRac
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockEntityPlatform,
)

from custom_components.bosch_homecom import (
    _async_migrate_bacon_entities,
    binary_sensor,
    climate,
    humidifier,
    sensor,
)
from custom_components.bosch_homecom.bacon import (
    BaconApplianceType,
    bacon_appliance_type,
)
from custom_components.bosch_homecom.const import (
    CONF_BACON_APPLIANCE_TYPES,
    CONF_REFRESH,
    DOMAIN,
)
from custom_components.bosch_homecom.coordinator import (
    BoschComModuleCoordinatorBaconRac,
)


def _data(**overrides):
    """Build a real SDK snapshot without assuming an appliance kind."""
    return BHCDeviceBaconRac(
        **{
            "device": {"deviceId": "TEST-DEVICE", "deviceType": "bacon_rac"},
            "firmware": "unknown",
            "reported": {},
            "desired": {},
            "sensor": None,
            "metadata": None,
            "info": None,
            **overrides,
        }
    )


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("dry_6000i", BaconApplianceType.DEHUMIDIFIER),
        ("dry_6000i_v2", BaconApplianceType.DEHUMIDIFIER),
        ("dry_8000i_v1", BaconApplianceType.DEHUMIDIFIER),
        ("climate_6000i", BaconApplianceType.AIR_CONDITIONER),
        ("climate_7000i_v2", BaconApplianceType.AIR_CONDITIONER),
        ("air_6000i", BaconApplianceType.UNKNOWN),
        ("dry_6000ix", BaconApplianceType.UNKNOWN),
        ("dry_9000i", BaconApplianceType.UNKNOWN),
    ],
)
def test_explicit_identity_takes_priority_over_capabilities(model, expected):
    data = _data(
        info={"appliance": {"type": model}},
        reported={"tempSetpoint": 23, "humiditySetpoint": 50, "opMode": "laundry"},
    )
    assert bacon_appliance_type(data) is expected


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        ({}, BaconApplianceType.UNKNOWN),
        ({"reported": {"powerEnabled": False}}, BaconApplianceType.UNKNOWN),
        ({"sensor": {"relHumidity": 50}}, BaconApplianceType.UNKNOWN),
        ({"desired": {"tempSetpoint": 23}}, BaconApplianceType.UNKNOWN),
        ({"reported": {"humiditySetpoint": 50}}, BaconApplianceType.UNKNOWN),
        ({"reported": {"tempSetpoint": 23}}, BaconApplianceType.AIR_CONDITIONER),
        (
            {"reported": {"humiditySetpoint": 50, "tempSetpoint": 23}},
            BaconApplianceType.AIR_CONDITIONER,
        ),
        (
            {"reported": {"humiditySetpoint": 50, "opMode": "laundry"}},
            BaconApplianceType.DEHUMIDIFIER,
        ),
        (
            {"metadata": {"shadows": {"state": {"tempSetpoint": {"ro": True}}}}},
            BaconApplianceType.AIR_CONDITIONER,
        ),
        (
            {
                "metadata": {
                    "shadows": {
                        "state": {
                            "humiditySetpoint": {"min": 40, "max": 70},
                            "opMode": {"enum": ["auto", "night", "laundry"]},
                        }
                    }
                }
            },
            BaconApplianceType.DEHUMIDIFIER,
        ),
    ],
)
def test_only_positive_capabilities_identify_an_unknown_appliance(snapshot, expected):
    assert bacon_appliance_type(_data(**snapshot)) is expected


@pytest.mark.parametrize(
    "info", [None, {}, {"appliance": None}, {"appliance": "invalid"}]
)
def test_missing_identity_keeps_the_known_type(info):
    assert (
        bacon_appliance_type(
            _data(info=info), known_type=BaconApplianceType.DEHUMIDIFIER
        )
        is BaconApplianceType.DEHUMIDIFIER
    )


def _coordinator(hass, entry, serial="TEST-DEVICE", *, info=None):
    """Use the real coordinator; mock only its SDK transport dependencies."""
    bhc = SimpleNamespace(
        info=info, metadata=None, sensor=None, async_update=AsyncMock()
    )
    return BoschComModuleCoordinatorBaconRac(
        hass,
        bhc,
        {"deviceId": serial, "deviceType": "bacon_rac"},
        {"value": "unknown"},
        entry,
        Mock(),
        Mock(),
        asyncio.Lock(),
        False,
    )


@pytest.mark.parametrize("cached_type", list(BaconApplianceType))
async def test_appliance_type_survives_a_restart_with_an_incomplete_shadow(
    hass, cached_type
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_BACON_APPLIANCE_TYPES: {"TEST-DEVICE": cached_type.value}},
    )
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)
    coordinator._handle_push({"reported": {"powerEnabled": False}})
    assert coordinator.appliance_type is cached_type


@pytest.mark.parametrize("cached_type", [None, "future_type", False, []])
def test_invalid_cached_type_starts_unknown(hass, cached_type):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_BACON_APPLIANCE_TYPES: {"TEST-DEVICE": cached_type}},
    )
    entry.add_to_hass(hass)
    assert _coordinator(hass, entry).appliance_type is BaconApplianceType.UNKNOWN


async def test_late_identity_is_persisted_once_without_losing_other_entry_data(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "unrelated": "preserved",
            CONF_BACON_APPLIANCE_TYPES: {"ANOTHER-DEVICE": "air_conditioner"},
        },
    )
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)
    coordinator._handle_push({"reported": {"powerEnabled": True}})
    assert coordinator.appliance_type is BaconApplianceType.UNKNOWN
    coordinator.bhc.info = {"appliance": {"type": "dry_6000i_v2"}}
    with patch.object(
        hass.config_entries,
        "async_update_entry",
        wraps=hass.config_entries.async_update_entry,
    ) as update_entry:
        coordinator._handle_push({"reported": {"humiditySetpoint": 50}})
        coordinator.bhc.info = None
        coordinator._handle_push({"desired": {"humiditySetpoint": 55}})
        update_entry.assert_called_once()
    assert entry.data["unrelated"] == "preserved"
    assert entry.data[CONF_BACON_APPLIANCE_TYPES] == {
        "ANOTHER-DEVICE": "air_conditioner",
        "TEST-DEVICE": "dehumidifier",
    }
    assert coordinator.appliance_type is BaconApplianceType.DEHUMIDIFIER
    assert coordinator.data.reported["humiditySetpoint"] == 50


async def test_first_snapshot_corrects_a_cached_type_before_platform_setup(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_BACON_APPLIANCE_TYPES: {"TEST-DEVICE": "air_conditioner"}},
    )
    entry.add_to_hass(hass)
    coordinator = _coordinator(
        hass, entry, info={"appliance": {"type": "dry_6000i_v2"}}
    )
    with patch.object(hass.config_entries, "async_schedule_reload") as reload_entry:
        coordinator._handle_push({"reported": {}})
        reload_entry.assert_not_called()
    assert coordinator.appliance_type is BaconApplianceType.DEHUMIDIFIER


async def test_late_identity_correction_reloads_once_using_the_corrected_cache(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_BACON_APPLIANCE_TYPES: {"TEST-DEVICE": "air_conditioner"}},
    )
    entry.add_to_hass(hass)
    coordinator = _coordinator(hass, entry)
    coordinator._handle_push({"reported": {}})
    coordinator.bhc.info = {"appliance": {"type": "dry_6000i_v2"}}
    with patch.object(hass.config_entries, "async_schedule_reload") as reload_entry:
        for _ in range(2):
            coordinator._handle_push({"reported": {"humiditySetpoint": 50}})
        reload_entry.assert_called_once_with(entry.entry_id)
        # The active platform kind is stable until the scheduled reload.
        assert coordinator.appliance_type is BaconApplianceType.AIR_CONDITIONER
        restored = _coordinator(hass, entry)
        restored._handle_push({"reported": {"powerEnabled": False}})
        assert restored.appliance_type is BaconApplianceType.DEHUMIDIFIER
        reload_entry.assert_called_once()


def test_corrected_ac_kind_removes_only_its_old_dehumidifier_entities(hass):
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = _coordinator(
        hass, entry, info={"appliance": {"type": "climate_6000i"}}
    )
    coordinator._handle_push({"reported": {"tempSetpoint": 23}})
    registry = er.async_get(hass)
    obsolete = [
        registry.async_get_or_create(
            domain, DOMAIN, f"TEST-DEVICE-dehumidifier-{suffix}", config_entry=entry
        )
        for domain, suffix in (
            ("humidifier", "control"),
            ("sensor", "humidity"),
            ("binary_sensor", "waterTankFull"),
            ("binary_sensor", "defrostingActive"),
            ("binary_sensor", "condensatePumpRunning"),
            ("binary_sensor", "idleState"),
        )
    ]
    preserved = [
        registry.async_get_or_create(domain, integration, unique_id, config_entry=entry)
        for domain, integration, unique_id in (
            ("climate", DOMAIN, "TEST-DEVICE-climate"),
            ("sensor", DOMAIN, "TEST-DEVICE-room-temperature"),
            ("humidifier", DOMAIN, "OTHER-DEVICE-dehumidifier-control"),
            ("humidifier", "other_integration", "TEST-DEVICE-dehumidifier-control"),
        )
    ]
    _async_migrate_bacon_entities(hass, entry, coordinator)
    assert all(registry.async_get(entity.entity_id) is None for entity in obsolete)
    assert all(registry.async_get(entity.entity_id) is not None for entity in preserved)


async def test_full_entry_setup_reuses_one_connection_and_remembers_device_kinds(hass):
    """Run the actual entry setup, SDK facades, platforms, reload and unload."""
    serials = ("DRY-6", "DRY-8", "AC-6")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_DEVICES: {f"{serial}_bacon_rac": True for serial in serials},
            CONF_TOKEN: "mock_token",
            CONF_REFRESH: "mock_refresh",
        },
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    obsolete = registry.async_get_or_create(
        "climate", DOMAIN, "DRY-6-climate", config_entry=entry
    )
    client = Mock(is_connected=True, token_expires_at=None)
    client.async_connect = AsyncMock()
    client.async_disconnect = AsyncMock()
    client.async_get_state = AsyncMock(
        return_value={"reported": {"powerEnabled": False}}
    )
    client.get_info.return_value = None
    client.get_metadata.return_value = None
    client.get_sensor.return_value = None
    manager = Mock(token="mock_token", refresh_token="mock_refresh")
    manager.async_get_devices = AsyncMock(return_value=[])
    manager.get_token = AsyncMock()
    with (
        patch(
            "custom_components.bosch_homecom.HomeComAlt.create", return_value=manager
        ),
        patch(
            "custom_components.bosch_homecom.async_get_bacon_devices",
            return_value=[
                {"deviceId": serial, "deviceType": "bacon_rac"} for serial in serials
            ],
        ),
        patch(
            "custom_components.bosch_homecom.decode_jwt_sub", return_value="test-user"
        ),
        patch(
            "custom_components.bosch_homecom.BaconMqttClient", return_value=client
        ) as mqtt_client,
    ):
        try:
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            mqtt_client.assert_called_once()
            client.async_connect.assert_awaited_once()
            assert client.register_listener.call_count == 3
            assert not hass.states.async_entity_ids("climate")
            assert not hass.states.async_entity_ids("humidifier")
            infos = {
                serial: {"appliance": {"type": model}}
                for serial, model in zip(
                    serials, ("dry_6000i_v2", "dry_8000i_v1", "climate_6000i")
                )
            }
            client.get_info.side_effect = infos.get
            for coordinator in entry.runtime_data:
                coordinator._handle_push({"reported": {"powerEnabled": False}})
            await hass.async_block_till_done()
            assert registry.async_get(obsolete.entity_id) is None
            assert len(hass.states.async_entity_ids("humidifier")) == 2
            assert len(hass.states.async_entity_ids("climate")) == 1
            entity_ids = {
                entity.entity_id
                for entity in er.async_entries_for_config_entry(
                    registry, entry.entry_id
                )
            }
            # A real reload starts with no push-only info. The persisted kinds
            # must restore the same entities without another identification.
            client.get_info.side_effect = None
            client.get_info.return_value = None
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
            assert {
                entity.entity_id
                for entity in er.async_entries_for_config_entry(
                    registry, entry.entry_id
                )
            } == entity_ids
            assert len(hass.states.async_entity_ids("humidifier")) == 2
            assert len(hass.states.async_entity_ids("climate")) == 1
        finally:
            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()


async def test_real_coordinators_discover_mixed_appliances_and_migrate_old_entries(
    hass,
):
    """Late MQTT snapshots flow through real coordinators into actual HA platforms."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinators = [
        _coordinator(hass, entry, serial) for serial in ("DRY-6", "DRY-8", "AC-6")
    ]
    entry.runtime_data = coordinators
    registry = er.async_get(hass)
    obsolete_ids = []
    for index, coordinator in enumerate(coordinators):
        coordinator._handle_push({"reported": {"powerEnabled": True}})
        registered = registry.async_get_or_create(
            "climate",
            DOMAIN,
            f"{coordinator.unique_id}-climate",
            config_entry=entry,
            disabled_by=er.RegistryEntryDisabler.USER if index == 1 else None,
        )
        if index < 2:
            obsolete_ids.append(registered.entity_id)
        else:
            ac_id = registered.entity_id
            registry.async_update_entity(ac_id, name="My existing AC")
        _async_migrate_bacon_entities(hass, entry, coordinator)

    platforms = []
    payload = json.loads(
        (Path(__file__).parent / "fixtures" / "dehumidifier.json").read_text()
    )
    try:
        for domain, module in (
            ("climate", climate),
            ("humidifier", humidifier),
            ("sensor", sensor),
            ("binary_sensor", binary_sensor),
        ):
            platform = MockEntityPlatform(
                hass, domain=domain, platform_name=DOMAIN, platform=module
            )
            platforms.append(platform)
            assert await platform.async_setup_entry(entry)
        await hass.async_block_till_done()
        assert not hass.states.async_entity_ids("climate")
        assert not hass.states.async_entity_ids("humidifier")
        for coordinator, model in zip(
            coordinators, ("dry_6000i_v2", "dry_8000i_v1", "climate_6000i")
        ):
            coordinator.bhc.info = {"appliance": {"type": model}}
            if model.startswith("dry_"):
                coordinator.bhc.metadata = deepcopy(payload["metadata"])
                coordinator.bhc.sensor = deepcopy(payload["sensor"])
                reported = deepcopy(payload["reported"])
            else:
                reported = {"powerEnabled": True, "tempSetpoint": 23, "opMode": "cool"}
            coordinator._handle_push({"reported": reported})
        await hass.async_block_till_done()
        assert all(registry.async_get(entity_id) is None for entity_id in obsolete_ids)
        assert len(hass.states.async_entity_ids("climate")) == 1
        assert len(hass.states.async_entity_ids("humidifier")) == 2
        assert hass.states.get(ac_id).attributes["temperature"] == 23
        assert registry.async_get(ac_id).name == "My existing AC"
        assert entry.data[CONF_BACON_APPLIANCE_TYPES] == {
            "DRY-6": "dehumidifier",
            "DRY-8": "dehumidifier",
            "AC-6": "air_conditioner",
        }
        for coordinator in coordinators[:2]:
            registered = er.async_entries_for_config_entry(registry, entry.entry_id)
            entities = [
                e
                for e in registered
                if e.unique_id.startswith(f"{coordinator.unique_id}-dehumidifier-")
            ]
            assert len(entities) == 6
            assert len({e.device_id for e in entities}) == 1
            assert (
                hass.states.get(
                    next(e.entity_id for e in entities if e.domain == "humidifier")
                ).attributes["device_class"]
                == "dehumidifier"
            )
    finally:
        for platform in platforms:
            await platform.async_reset()
        for coordinator in coordinators:
            await coordinator.async_shutdown()

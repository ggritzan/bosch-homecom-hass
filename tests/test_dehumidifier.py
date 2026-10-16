"""Check 6000i/8000i dehumidifier mapping and entity-ID migration.

Uses real Home Assistant 2026.9.1 entity classes and homecom_alt 1.8.2.
Only the network transport is mocked; no Bosch account is used.
The fixture is a 6000i capture; 8000i cases simulate the shared capability schema.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from homeassistant.components.humidifier import HumidifierAction, HumidifierDeviceClass
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry
from homecom_alt import BaconMqttClient
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockEntityPlatform,
)

from custom_components.bosch_homecom import (
    _async_migrate_bacon_entities,
    _async_migrate_dehumidifier_ids,
    binary_sensor,
    climate,
    humidifier,
    sensor,
)
from custom_components.bosch_homecom.bacon import (
    BaconApplianceType,
    bacon_appliance_type,
    bacon_sensor_attributes,
)
from custom_components.bosch_homecom.binary_sensor import (
    DEHUMIDIFIER_STATUS_FIELDS,
    BoschComBaconDehumidifierStatus,
    async_setup_entry as binary_sensor_async_setup_entry,
)
from custom_components.bosch_homecom.coordinator import (
    BoschComModuleCoordinatorBaconRac,
)
from custom_components.bosch_homecom.humidifier import (
    BoschComBaconDehumidifier,
    async_setup_entry as humidifier_async_setup_entry,
)
from custom_components.bosch_homecom.sensor import (
    BoschComBaconHumidity,
    async_setup_entry as sensor_async_setup_entry,
)

PLATFORM_CASES = [
    (humidifier_async_setup_entry, BoschComBaconDehumidifier, 1),
    (sensor_async_setup_entry, BoschComBaconHumidity, 1),
    (binary_sensor_async_setup_entry, BoschComBaconDehumidifierStatus, 4),
]


def make_coordinator(model="6000i", device_id="TEST-DEHUMIDIFIER-001"):
    payload = json.loads(
        (Path(__file__).parent / "fixtures" / "dehumidifier.json").read_text()
    )
    payload["info"]["appliance"]["type"] = f"dry_{model}_v2"
    coordinator = MagicMock(spec=BoschComModuleCoordinatorBaconRac)
    coordinator.unique_id = device_id
    payload["device"]["deviceId"] = device_id
    coordinator.device_info = {"identifiers": {("bosch_homecom", device_id)}}
    coordinator.data = SimpleNamespace(**payload)
    coordinator.appliance_type = BaconApplianceType.DEHUMIDIFIER
    coordinator.last_update_success = True
    coordinator.local_first = None
    coordinator.async_request_refresh = AsyncMock()
    coordinator.client = MagicMock()
    coordinator.client.async_set_desired = AsyncMock()
    return coordinator


def dehumidifier_status_entities(coordinator):
    """Construct the four status entities for mapping assertions."""
    return [
        BoschComBaconDehumidifierStatus(coordinator, field, key, device_class)
        for field, key, device_class in DEHUMIDIFIER_STATUS_FIELDS
    ]


@pytest.mark.parametrize("model", ["6000i", "8000i"])
def test_payload_mapping_and_modes(model):
    coordinator = make_coordinator(model)
    control = BoschComBaconDehumidifier(coordinator)
    humidity = BoschComBaconHumidity(coordinator, MagicMock())
    assert coordinator.appliance_type is BaconApplianceType.DEHUMIDIFIER
    assert control.device_class == HumidifierDeviceClass.DEHUMIDIFIER
    assert control.is_on is True
    assert control.target_humidity == 40
    assert control.current_humidity == humidity.native_value == 42
    assert (
        control.min_humidity,
        control.max_humidity,
        control.target_humidity_step,
    ) == (40, 70, 5)
    assert control.available_modes == ["auto", "night", "laundry"]
    assert control.mode == "auto"
    assert control.action == HumidifierAction.DRYING
    assert control.available is True
    assert all(
        status.is_on is False for status in dehumidifier_status_entities(coordinator)
    )
    assert "measured_at" in humidity.extra_state_attributes


def test_missing_data_is_unknown_and_offline_is_unavailable():
    coordinator = make_coordinator()
    coordinator.data.sensor = None
    coordinator.data.reported = {}
    control = BoschComBaconDehumidifier(coordinator)
    assert control.current_humidity is None
    assert control.target_humidity is None
    assert control.is_on is None
    assert control.action is None
    assert all(
        status.is_on is None for status in dehumidifier_status_entities(coordinator)
    )
    coordinator.data.info = {"online": False}
    assert control.available is False
    coordinator.data = None
    assert control.target_humidity is None
    assert control.current_humidity is None


def test_blocked_or_idle_device_does_not_report_drying():
    coordinator = make_coordinator()
    control = BoschComBaconDehumidifier(coordinator)
    for flag in ("idleState", "waterTankFull", "defrostingActive"):
        coordinator.data.sensor[flag] = True
        assert control.action == HumidifierAction.IDLE
        coordinator.data.sensor[flag] = False
    coordinator.data.reported["powerEnabled"] = False
    assert control.action == HumidifierAction.OFF


@pytest.mark.parametrize("model", ["6000i", "8000i"])
@pytest.mark.asyncio
async def test_exact_mqtt_commands_and_reported_confirmation(model):
    coordinator = make_coordinator(model)
    control = BoschComBaconDehumidifier(coordinator)
    await control.async_turn_off()
    await control.async_turn_on()
    await control.async_set_mode("night")
    await control.async_set_humidity(55)
    assert [
        call.args for call in coordinator.client.async_set_desired.await_args_list
    ] == [
        ("TEST-DEHUMIDIFIER-001", {"powerEnabled": False}),
        ("TEST-DEHUMIDIFIER-001", {"powerEnabled": True}),
        ("TEST-DEHUMIDIFIER-001", {"opMode": "night"}),
        ("TEST-DEHUMIDIFIER-001", {"humiditySetpoint": 55}),
    ]
    # Publishing desired settings must not pretend the device confirmed them.
    assert control.target_humidity == 40
    assert control.mode == "auto"
    coordinator.data.reported.update(humiditySetpoint=55, opMode="night")
    assert control.target_humidity == 55
    assert control.mode == "night"


@pytest.mark.parametrize(
    "target", [39, 71, 42, -1, True, "50", float("nan"), float("inf")]
)
@pytest.mark.asyncio
async def test_invalid_humidity_never_publishes(target):
    coordinator = make_coordinator()
    with pytest.raises(ServiceValidationError):
        await BoschComBaconDehumidifier(coordinator).async_set_humidity(target)
    coordinator.client.async_set_desired.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_ac_modes_and_read_only_fields_never_publish():
    coordinator = make_coordinator()
    control = BoschComBaconDehumidifier(coordinator)
    for mode in ("heat", "cool", "dry", "fan"):
        with pytest.raises(ServiceValidationError):
            await control.async_set_mode(mode)
    coordinator.data.metadata["shadows"]["state"]["powerEnabled"]["ro"] = True
    with pytest.raises(ServiceValidationError):
        await control.async_turn_on()
    coordinator.client.async_set_desired.assert_not_awaited()


@pytest.mark.asyncio
async def test_laundry_to_auto_when_setting_humidity():
    coordinator = make_coordinator()
    coordinator.data.reported["opMode"] = "laundry"
    await BoschComBaconDehumidifier(coordinator).async_set_humidity(50)
    coordinator.client.async_set_desired.assert_awaited_once_with(
        "TEST-DEHUMIDIFIER-001", {"humiditySetpoint": 50, "opMode": "auto"}
    )


@pytest.mark.asyncio
async def test_real_library_builds_the_expected_shadow_message():
    coordinator = make_coordinator()
    client = BaconMqttClient("dehumidifier-test-client")
    client._sub = "dehumidifier-test-user"
    client._client = MagicMock()
    coordinator.client = client
    await BoschComBaconDehumidifier(coordinator).async_set_humidity(50)
    topic, raw = client._client.publish.call_args.args
    assert (
        topic
        == "users/dehumidifier-test-user/devices/TEST-DEHUMIDIFIER-001/shadows/state/update"
    )
    assert json.loads(raw) == {"state": {"desired": {"humiditySetpoint": 50}}}


@pytest.mark.parametrize("setup, entity_class, count", PLATFORM_CASES)
@pytest.mark.asyncio
async def test_platform_adds_known_dehumidifiers_only_once(
    hass, setup, entity_class, count
):
    coordinator = make_coordinator()
    entry = MagicMock(runtime_data=[coordinator])
    added = []
    await setup(hass, entry, added.extend)
    coordinator.async_add_listener.call_args.args[0]()
    assert (
        len([entity for entity in added if isinstance(entity, entity_class)]) == count
    )


@pytest.mark.parametrize("setup, entity_class, count", PLATFORM_CASES)
@pytest.mark.asyncio
async def test_discovery_waits_for_device_information_and_adds_only_once(
    hass, setup, entity_class, count
):
    coordinator = make_coordinator()
    saved = deepcopy(coordinator.data)
    coordinator.appliance_type = BaconApplianceType.UNKNOWN
    coordinator.data.info = None
    coordinator.data.metadata = None
    coordinator.data.reported = {"powerEnabled": True}
    entry = MagicMock(runtime_data=[coordinator])
    added = []
    await setup(hass, entry, added.extend)
    assert not any(isinstance(entity, entity_class) for entity in added)
    discover = coordinator.async_add_listener.call_args.args[0]
    coordinator.data = saved
    coordinator.appliance_type = BaconApplianceType.DEHUMIDIFIER
    discover()
    discover()
    assert (
        len([entity for entity in added if isinstance(entity, entity_class)]) == count
    )
    entry.async_on_unload.assert_called_once()


@pytest.mark.parametrize("setup, entity_class, count", PLATFORM_CASES)
@pytest.mark.asyncio
async def test_real_air_conditioner_gets_no_dehumidifier_entities(
    hass, setup, entity_class, count
):
    coordinator = make_coordinator()
    coordinator.data.info = {"appliance": {"type": "climate_6000i"}}
    coordinator.data.reported = {"powerEnabled": True, "tempSetpoint": 23}
    coordinator.data.metadata = {"shadows": {"state": {"tempSetpoint": {"min": 16}}}}
    coordinator.appliance_type = BaconApplianceType.AIR_CONDITIONER
    added = []
    entry = MagicMock(runtime_data=[coordinator])
    await setup(hass, entry, added.extend)
    coordinator.async_add_listener.call_args.args[0]()
    assert not any(isinstance(entity, entity_class) for entity in added)


@pytest.mark.parametrize("setup, entity_class, count", PLATFORM_CASES)
@pytest.mark.asyncio
async def test_late_discovery_handles_multiple_devices_independently(
    hass, setup, entity_class, count
):
    coordinators = [
        make_coordinator("6000i", "DEHUMIDIFIER-6000I"),
        make_coordinator("8000i", "DEHUMIDIFIER-8000I"),
    ]
    saved = [deepcopy(coordinator.data) for coordinator in coordinators]
    for coordinator in coordinators:
        coordinator.appliance_type = BaconApplianceType.UNKNOWN
        coordinator.data.info = None
        coordinator.data.metadata = None
        coordinator.data.reported = {}
    entry = MagicMock(runtime_data=coordinators)
    added = []
    await setup(hass, entry, added.extend)
    for index, coordinator in enumerate(coordinators):
        coordinator.data = saved[index]
        coordinator.appliance_type = BaconApplianceType.DEHUMIDIFIER
        coordinator.async_add_listener.call_args.args[0]()
        assert (
            len([entity for entity in added if isinstance(entity, entity_class)])
            == (index + 1) * count
        )
    for coordinator in coordinators:
        coordinator.async_add_listener.call_args.args[0]()
    assert (
        len([entity for entity in added if isinstance(entity, entity_class)])
        == 2 * count
    )


@pytest.mark.parametrize("model", ["6000i", "8000i"])
@pytest.mark.asyncio
async def test_entity_and_setpoint_service_in_real_home_assistant(hass, model):
    """Exercise actual HA entity registration and its set_humidity service."""
    from homeassistant.components.humidifier import DATA_COMPONENT, async_setup

    await async_setup(hass, {})
    coordinator = make_coordinator(model)
    control = BoschComBaconDehumidifier(coordinator)
    # Exercise HA services without a full Bosch config entry or cloud session.
    control._attr_device_info = None
    control._attr_name = "Dehumidifier"
    await hass.data[DATA_COMPONENT].async_add_entities([control])
    await hass.async_block_till_done()
    state = hass.states.get(control.entity_id)
    assert state.state == "on"
    assert state.attributes["device_class"] == "dehumidifier"
    assert state.attributes["humidity"] == 40
    assert state.attributes["current_humidity"] == 42
    await hass.services.async_call(
        "humidifier",
        "set_humidity",
        {"entity_id": control.entity_id, "humidity": 55},
        blocking=True,
    )
    coordinator.client.async_set_desired.assert_awaited_once_with(
        "TEST-DEHUMIDIFIER-001", {"humiditySetpoint": 55}
    )


@pytest.mark.parametrize("model", ["6000i", "8000i"])
def test_humidity_limits_and_modes_follow_each_devices_metadata(model):
    coordinator = make_coordinator(model)
    metadata = coordinator.data.metadata["shadows"]["state"]
    metadata["humiditySetpoint"].update(min=35, max=75, step=10)
    metadata["opMode"]["enum"] = ["auto", "night"]
    control = BoschComBaconDehumidifier(coordinator)
    assert (
        control.min_humidity,
        control.max_humidity,
        control.target_humidity_step,
    ) == (35, 75, 10)
    assert control.available_modes == ["auto", "night"]


def test_known_air_conditioner_with_humidity_feature_is_not_misidentified():
    coordinator = make_coordinator()
    coordinator.data.info["appliance"]["type"] = "climate_6000i"
    assert bacon_appliance_type(coordinator.data) is BaconApplianceType.AIR_CONDITIONER


def test_humidity_capability_identifies_device_before_identity_arrives():
    coordinator = make_coordinator()
    coordinator.data.info = None
    assert bacon_appliance_type(coordinator.data) is BaconApplianceType.DEHUMIDIFIER
    coordinator.data.metadata["shadows"]["state"]["tempSetpoint"] = {"min": 16}
    assert bacon_appliance_type(coordinator.data) is BaconApplianceType.AIR_CONDITIONER


@pytest.mark.parametrize("model", ["6000i", "8000i"])
@pytest.mark.asyncio
async def test_platform_entities_share_the_device_and_load_translated_names(
    hass, model
):
    """Register the platform entities through HA, including translation loading."""
    hass.config.language = "de"
    coordinator = make_coordinator(model)
    entry = MockConfigEntry(domain="bosch_homecom")
    entry.add_to_hass(hass)
    entry.runtime_data = [coordinator]
    platforms = []
    try:
        for domain, module in [
            ("climate", climate),
            ("humidifier", humidifier),
            ("sensor", sensor),
            ("binary_sensor", binary_sensor),
        ]:
            platform = MockEntityPlatform(
                hass, domain=domain, platform_name="bosch_homecom", platform=module
            )
            platforms.append(platform)
            assert await platform.async_setup_entry(entry)
        await hass.async_block_till_done()
        registry = entity_registry.async_get(hass)
        registered = [
            entity
            for entity in entity_registry.async_entries_for_config_entry(
                registry, entry.entry_id
            )
            if "-dehumidifier-" in entity.unique_id
        ]
        assert len(registered) == 6
        assert len({entity.device_id for entity in registered}) == 1
        assert all(entity.device_id is not None for entity in registered)
        assert {entity.original_name for entity in registered} == {
            "Luftentfeuchter",
            "Luftfeuchtigkeit",
            "Wassertank voll",
            "Abtauen",
            "Kondensatpumpe",
            "Ruhezustand",
        }
        assert all(
            hass.states.get(entity.entity_id) is not None for entity in registered
        )
        control = next(
            entity for entity in registered if entity.unique_id.endswith("-control")
        )
        assert control.domain == "humidifier"
        assert (
            hass.states.get(control.entity_id).attributes["device_class"]
            == "dehumidifier"
        )
        assert not any(
            entity.domain == "climate"
            for entity in entity_registry.async_entries_for_config_entry(
                registry, entry.entry_id
            )
        )
    finally:
        for platform in platforms:
            await platform.async_reset()


@pytest.mark.parametrize("model", ["6000i", "8000i"])
@pytest.mark.asyncio
async def test_migration_removes_only_the_obsolete_climate_entry(hass, model):
    coordinator = make_coordinator(model)
    entry = MockConfigEntry(domain="bosch_homecom")
    entry.add_to_hass(hass)
    entry.runtime_data = [coordinator]
    registry = entity_registry.async_get(hass)
    obsolete = registry.async_get_or_create(
        "climate",
        "bosch_homecom",
        f"{coordinator.unique_id}-climate",
        config_entry=entry,
    )
    preserved = [
        registry.async_get_or_create(
            "climate", "bosch_homecom", "OTHER-DEVICE-climate", config_entry=entry
        ),
        registry.async_get_or_create(
            "climate",
            "another_integration",
            f"{coordinator.unique_id}-climate",
            config_entry=entry,
        ),
        registry.async_get_or_create(
            "humidifier",
            "bosch_homecom",
            f"{coordinator.unique_id}-dehumidifier-control",
            config_entry=entry,
        ),
    ]
    added = []
    _async_migrate_bacon_entities(hass, entry, coordinator)
    await climate.async_setup_entry(hass, entry, added.extend)
    assert added == []
    assert registry.async_get(obsolete.entity_id) is None
    assert all(registry.async_get(entity.entity_id) is not None for entity in preserved)


@pytest.mark.asyncio
async def test_climate_migration_respects_the_config_entry(hass):
    coordinator = make_coordinator()
    entry = MockConfigEntry(domain="bosch_homecom")
    other_entry = MockConfigEntry(domain="bosch_homecom")
    entry.add_to_hass(hass)
    other_entry.add_to_hass(hass)
    entry.runtime_data = [coordinator]
    registry = entity_registry.async_get(hass)
    foreign = registry.async_get_or_create(
        "climate",
        "bosch_homecom",
        f"{coordinator.unique_id}-climate",
        config_entry=other_entry,
    )
    _async_migrate_bacon_entities(hass, entry, coordinator)
    assert registry.async_get(foreign.entity_id) is not None


@pytest.mark.parametrize("model", ["6000i", "8000i", "climate_6000i"])
@pytest.mark.parametrize("finish_setup_first", [False, True])
@pytest.mark.asyncio
async def test_late_identity_creates_only_the_correct_control_entity(
    hass, model, finish_setup_first
):
    """Unknown devices never queue a climate, including during platform setup."""
    coordinator = make_coordinator()
    identified = deepcopy(coordinator.data)
    if model == "climate_6000i":
        identified.info["appliance"]["type"] = model
        identified.reported.update(tempSetpoint=23, opMode="cool")
        identified.metadata["shadows"]["state"]["tempSetpoint"] = {"min": 16, "max": 30}
    else:
        identified.info["appliance"]["type"] = f"dry_{model}_v2"
    coordinator.data.info = None
    coordinator.data.metadata = None
    coordinator.data.reported = {"powerEnabled": True}
    coordinator.appliance_type = BaconApplianceType.UNKNOWN
    listeners = set()

    def subscribe(listener, *args, **kwargs):
        listeners.add(listener)
        return lambda: listeners.discard(listener)

    coordinator.async_add_listener.side_effect = subscribe
    entry = MockConfigEntry(domain="bosch_homecom")
    entry.add_to_hass(hass)
    entry.runtime_data = [coordinator]
    registry = entity_registry.async_get(hass)
    platforms = []
    try:
        for domain, module in [("climate", climate), ("humidifier", humidifier)]:
            platform = MockEntityPlatform(
                hass, domain=domain, platform_name="bosch_homecom", platform=module
            )
            platforms.append(platform)
            assert await platform.async_setup_entry(entry)
        if finish_setup_first:
            await hass.async_block_till_done()
        assert not entity_registry.async_entries_for_config_entry(
            registry, entry.entry_id
        )
        coordinator.data = identified
        coordinator.appliance_type = (
            BaconApplianceType.AIR_CONDITIONER
            if model == "climate_6000i"
            else BaconApplianceType.DEHUMIDIFIER
        )
        # Repeated pushes must not duplicate native controls or remove ACs.
        for _ in range(2):
            for listener in tuple(listeners):
                listener()
            await hass.async_block_till_done()
        registered = entity_registry.async_entries_for_config_entry(
            registry, entry.entry_id
        )
        if model == "climate_6000i":
            assert [entity.domain for entity in registered] == ["climate"]
            assert (
                hass.states.get(registered[0].entity_id).attributes["temperature"] == 23
            )
        else:
            assert [entity.domain for entity in registered] == ["humidifier"]
            assert not hass.states.async_entity_ids("climate")
            assert (
                hass.states.get(registered[0].entity_id).attributes["device_class"]
                == "dehumidifier"
            )
    finally:
        for platform in platforms:
            await platform.async_reset()


@pytest.mark.asyncio
async def test_late_identity_removes_a_disabled_climate_entry(hass):
    coordinator = make_coordinator()
    identified = deepcopy(coordinator.data)
    coordinator.data.info = None
    coordinator.data.metadata = None
    coordinator.data.reported = {"powerEnabled": True}
    coordinator.appliance_type = BaconApplianceType.UNKNOWN
    entry = MockConfigEntry(domain="bosch_homecom")
    entry.add_to_hass(hass)
    entry.runtime_data = [coordinator]
    registry = entity_registry.async_get(hass)
    disabled = registry.async_get_or_create(
        "climate",
        "bosch_homecom",
        f"{coordinator.unique_id}-climate",
        config_entry=entry,
        disabled_by=entity_registry.RegistryEntryDisabler.USER,
    )
    _async_migrate_bacon_entities(hass, entry, coordinator)
    migrate = coordinator.async_add_listener.call_args.args[0]
    unsubscribe = coordinator.async_add_listener.return_value
    platform = MockEntityPlatform(
        hass, domain="climate", platform_name="bosch_homecom", platform=climate
    )
    try:
        assert await platform.async_setup_entry(entry)
        assert hass.states.get(disabled.entity_id) is None
        coordinator.data = identified
        coordinator.appliance_type = BaconApplianceType.DEHUMIDIFIER
        migrate()
        await hass.async_block_till_done()
        assert registry.async_get(disabled.entity_id) is None
        unsubscribe.assert_called_once()
    finally:
        await platform.async_reset()


@pytest.mark.asyncio
async def test_legacy_unique_ids_migrate_without_changing_entity_ids_or_names(
    hass,
):
    registry = entity_registry.async_get(hass)
    coordinator = make_coordinator()
    entry = MockConfigEntry(domain="bosch_homecom")
    entry.add_to_hass(hass)
    entry.runtime_data = [coordinator]
    created = []
    for domain, suffix in [
        ("humidifier", "control"),
        ("sensor", "humidity"),
        ("binary_sensor", "waterTankFull"),
        ("binary_sensor", "defrostingActive"),
        ("binary_sensor", "condensatePumpRunning"),
        ("binary_sensor", "idleState"),
    ]:
        registered = registry.async_get_or_create(
            domain,
            "bosch_homecom",
            f"{coordinator.unique_id}-dry-{suffix}",
            config_entry=entry,
        )
        registered = registry.async_update_entity(
            registered.entity_id, name=f"My {suffix}"
        )
        created.append((registered.entity_id, domain, suffix))
    unrelated = registry.async_get_or_create(
        "sensor", "bosch_homecom", "OTHER-DEVICE-dry-humidity", config_entry=entry
    )
    _async_migrate_dehumidifier_ids(hass, entry)
    _async_migrate_dehumidifier_ids(hass, entry)
    for entity_id, domain, suffix in created:
        current = registry.async_get(entity_id)
        assert current.name == f"My {suffix}"
        assert current.unique_id == f"{coordinator.unique_id}-dehumidifier-{suffix}"
        assert (
            registry.async_get_entity_id(domain, "bosch_homecom", current.unique_id)
            == entity_id
        )
    assert (
        registry.async_get(unrelated.entity_id).unique_id == "OTHER-DEVICE-dry-humidity"
    )
    assert len(registry.entities) == 7


@pytest.mark.asyncio
async def test_migration_does_not_overwrite_an_existing_new_entity(hass):
    registry = entity_registry.async_get(hass)
    coordinator = make_coordinator()
    entry = MockConfigEntry(domain="bosch_homecom")
    entry.add_to_hass(hass)
    entry.runtime_data = [coordinator]
    old = registry.async_get_or_create(
        "sensor",
        "bosch_homecom",
        f"{coordinator.unique_id}-dry-humidity",
        config_entry=entry,
    )
    current = registry.async_get_or_create(
        "sensor",
        "bosch_homecom",
        f"{coordinator.unique_id}-dehumidifier-humidity",
        config_entry=entry,
    )
    _async_migrate_dehumidifier_ids(hass, entry)
    assert registry.async_get(old.entity_id).unique_id.endswith("-dry-humidity")
    assert registry.async_get(current.entity_id).unique_id.endswith(
        "-dehumidifier-humidity"
    )
    assert len(registry.entities) == 2


@pytest.mark.parametrize("timestamp", [None, True, "yesterday", float("inf"), 10**100])
def test_invalid_measurement_timestamps_are_ignored(timestamp):
    assert bacon_sensor_attributes({"timestamp": timestamp}) == {}

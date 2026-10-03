"""Entity id / unique id naming (owner decision 2026-09-06,
docs/91-p4-parity-plan.md P4a): API parity with the cloud integration
(entity kinds, attributes, service names and fields) but entity ids of this
integration's own, derived from the configured heater name by slug, for all
three default heaters and the gateway. A slug that already contains "heater"
as one of its words (a discovery default like "Heater 02", or a user-given
name like "Bedroom heater") skips the `_heater` suffix entirely."""
from homeassistant.helpers import entity_registry as er

from custom_components.termoweb_local.const import DEFAULT_DEV_ID
from custom_components.termoweb_local.entity_ids import (
    heater_object_id,
    heater_schedule_object_id,
)

from .conftest import FakeCulTransport, THREE_HEATERS, setup_entry

# node_id -> (climate, temperature, power, energy, schedule, flash_display, priority, child_lock)
EXPECTED_HEATER_OBJECT_IDS = {
    2: (
        "living_room_heater",
        "living_room_heater_temperature",
        "living_room_heater_power",
        "living_room_heater_energy",
        "living_room_schedule",
        "living_room_heater_flash_display",
        "living_room_heater_priority",
        "living_room_heater_child_lock",
    ),
    3: (
        "bedroom_heater",
        "bedroom_heater_temperature",
        "bedroom_heater_power",
        "bedroom_heater_energy",
        "bedroom_schedule",
        "bedroom_heater_flash_display",
        "bedroom_heater_priority",
        "bedroom_heater_child_lock",
    ),
    4: (
        "master_bedroom_heater",
        "master_bedroom_heater_temperature",
        "master_bedroom_heater_power",
        "master_bedroom_heater_energy",
        "master_bedroom_schedule",
        "master_bedroom_heater_flash_display",
        "master_bedroom_heater_priority",
        "master_bedroom_heater_child_lock",
    ),
}
HEATER_DOMAINS_AND_KINDS = (
    ("climate", "climate"),
    ("sensor", "temperature"),
    ("sensor", "power"),
    ("sensor", "energy"),
    ("sensor", "schedule"),
    ("button", "flash_display"),
    ("number", "priority"),
    ("lock", "child_lock"),
)
GATEWAY_ENTITIES = (
    ("binary_sensor", "termoweb_local_gateway_online", "gateway_online"),
    ("sensor", "termoweb_local_total_energy", "total_energy"),
    ("button", "termoweb_local_force_refresh", "force_refresh"),
)


async def _assert_entity(hass, domain, object_id, expected_unique_id):
    entity_id = f"{domain}.{object_id}"
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    assert entry is not None, f"{entity_id} was not registered"
    assert entry.unique_id == expected_unique_id


async def test_heater_and_gateway_entity_ids(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport, heaters=THREE_HEATERS)

    for node_id, object_ids in EXPECTED_HEATER_OBJECT_IDS.items():
        for (domain, kind), object_id in zip(HEATER_DOMAINS_AND_KINDS, object_ids):
            expected_unique_id = f"termoweb_local:{DEFAULT_DEV_ID}:htr:{node_id}:{kind}"
            await _assert_entity(hass, domain, object_id, expected_unique_id)

    for domain, object_id, kind in GATEWAY_ENTITIES:
        expected_unique_id = f"termoweb_local:{DEFAULT_DEV_ID}:gateway:gateway:{kind}"
        await _assert_entity(hass, domain, object_id, expected_unique_id)


async def test_dev_id_option_changes_unique_ids(hass, monkeypatch, enable_custom_integrations):
    from custom_components.termoweb_local.const import CONF_DEV_ID

    transport = FakeCulTransport()
    await setup_entry(
        hass,
        monkeypatch,
        transport,
        heaters=THREE_HEATERS,
        options={CONF_DEV_ID: "custom-dev-id"},
    )

    registry = er.async_get(hass)
    entry = registry.async_get("climate.living_room_heater")
    assert entry.unique_id == "termoweb_local:custom-dev-id:htr:2:climate"


def test_slug_derivation_collapses_spaces_and_capitals():
    """Names with internal capitals and irregular whitespace/punctuation all
    fold to the same lowercase, single-underscore slug."""
    assert heater_object_id("Living room", "climate") == "living_room_heater"
    assert heater_object_id("  Master   Bedroom  ", "climate") == "master_bedroom_heater"
    assert heater_object_id("Kids' Room", "temperature") == "kids_room_heater_temperature"
    assert heater_object_id("UPSTAIRS-HALL", "power") == "upstairs_hall_heater_power"


def test_slug_already_containing_heater_skips_the_suffix():
    """A discovery default like "Heater 02", or a user-given name that
    already says "heater", is used as-is instead of doubling up on
    `_heater`."""
    assert heater_object_id("Heater 02", "climate") == "heater_02"
    assert heater_object_id("Heater 02", "temperature") == "heater_02_temperature"
    assert heater_object_id("Heater 02", "power") == "heater_02_power"
    assert heater_object_id("Heater 02", "energy") == "heater_02_energy"
    assert heater_object_id("Heater 02", "flash_display") == "heater_02_flash_display"
    assert heater_object_id("Heater 02", "priority") == "heater_02_priority"
    assert heater_object_id("Heater 02", "child_lock") == "heater_02_child_lock"
    assert heater_object_id("Bedroom heater", "climate") == "bedroom_heater"
    assert heater_object_id("Bedroom heater", "temperature") == "bedroom_heater_temperature"


def test_schedule_object_id_skips_the_heater_infix():
    """The schedule sensor's own object id is `<slug>_schedule`, not
    `<slug>_heater_schedule` (owner decision 2026-09-06): unlike every other
    per-heater sensor kind, it never goes through heater_object_id's own
    "_heater" infix at all, so a name already containing "heater" does not
    end up doubled either."""
    assert heater_schedule_object_id("Living room") == "living_room_schedule"
    assert heater_schedule_object_id("Heater 02") == "heater_02_schedule"
    assert heater_schedule_object_id("Bedroom heater") == "bedroom_heater_schedule"

"""Entity id migration on setup (owner decision 2026-09-06 task 3): a heater
registered under an older release's object id scheme keeps that entity_id in
the registry forever otherwise, since HA uses the registry's own stored
entity_id once a unique_id is registered, not whatever object id this
integration's entity classes compute at construction time
(entity_ids.heater_object_id's own docstring: a discovery-default heater's
name already contains "heater" as a slug word, so its climate entity used to
get `_heater` appended anyway under the old scheme -- `heater_02_heater` --
where the current scheme skips it -- `heater_02`)."""
import logging

from homeassistant.helpers import entity_registry as er

from custom_components.termoweb_local.const import (
    CONF_HEATER_ID,
    CONF_HEATER_NAME,
    DEFAULT_DEV_ID,
    DOMAIN,
)
from custom_components.termoweb_local.entity_ids import heater_unique_id

from .conftest import (
    FakeCulTransport,
    make_config_entry,
    patch_coordinator_nanocul,
    shrink_scan_timeout,
)

_OLD_SCHEME_HEATERS = [
    {CONF_HEATER_ID: 0x02, CONF_HEATER_NAME: "Heater 02"},
    {CONF_HEATER_ID: 0x04, CONF_HEATER_NAME: "Master bedroom"},
]


async def test_migration_renames_stale_heater_entity_ids_and_leaves_correct_ones_alone(
    hass, monkeypatch, enable_custom_integrations, caplog
):
    transport = FakeCulTransport(status_reply_ids={0x02, 0x04})
    entry = make_config_entry(hass, heaters=_OLD_SCHEME_HEATERS)
    registry = er.async_get(hass)

    # Heater 02 (a discovery-default name, "heater" already one of its slug
    # words): pre-registered under the old scheme, which appended `_heater`
    # regardless -- climate.heater_02_heater and
    # sensor.heater_02_heater_temperature -- exactly the live pair named in
    # the owner's own report.
    registry.async_get_or_create(
        "climate", DOMAIN,
        heater_unique_id(DEFAULT_DEV_ID, 0x02, "climate"),
        config_entry=entry, suggested_object_id="heater_02_heater",
    )
    registry.async_get_or_create(
        "sensor", DOMAIN,
        heater_unique_id(DEFAULT_DEV_ID, 0x02, "temperature"),
        config_entry=entry, suggested_object_id="heater_02_heater_temperature",
    )
    # Master bedroom's slug never contained "heater", so `_heater` has
    # always been appended for it under both schemes: already correctly
    # named, must be left alone.
    registry.async_get_or_create(
        "climate", DOMAIN,
        heater_unique_id(DEFAULT_DEV_ID, 0x04, "climate"),
        config_entry=entry, suggested_object_id="master_bedroom_heater",
    )

    patch_coordinator_nanocul(monkeypatch, transport)
    shrink_scan_timeout(monkeypatch)
    caplog.set_level(logging.INFO)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert (
        registry.async_get_entity_id(
            "climate", DOMAIN, heater_unique_id(DEFAULT_DEV_ID, 0x02, "climate")
        )
        == "climate.heater_02"
    )
    assert (
        registry.async_get_entity_id(
            "sensor", DOMAIN, heater_unique_id(DEFAULT_DEV_ID, 0x02, "temperature")
        )
        == "sensor.heater_02_temperature"
    )
    assert "renamed climate.heater_02_heater to climate.heater_02" in caplog.text
    assert (
        "renamed sensor.heater_02_heater_temperature to sensor.heater_02_temperature"
        in caplog.text
    )

    # Left alone: same entity_id as before migration, unique id untouched.
    assert (
        registry.async_get_entity_id(
            "climate", DOMAIN, heater_unique_id(DEFAULT_DEV_ID, 0x04, "climate")
        )
        == "climate.master_bedroom_heater"
    )
    assert "master_bedroom_heater" not in "".join(
        line for line in caplog.text.splitlines() if "renamed" in line
    )

    # The now-correctly-named entities are the ones actually live under this
    # setup, not stale registry leftovers.
    assert hass.states.get("climate.heater_02") is not None
    assert hass.states.get("sensor.heater_02_temperature") is not None
    assert hass.states.get("climate.master_bedroom_heater") is not None

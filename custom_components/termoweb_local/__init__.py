"""The termoweb_local integration: local, cloud-free control of Sun Ray heaters over
a nanoCUL868 stick (docs/90-phase3-plan.md P4). One DataUpdateCoordinator per config
entry owns the single NanoCul client and Network for that entry's serial port.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    CLOUD_NODE_TYPE_HEATER,
    CONF_DEV_ID,
    CONF_HEATER_ID,
    CONF_HEATERS,
    DEFAULT_DEV_ID,
    DOMAIN,
)
from .coordinator import TermowebLocalConfigEntry, TermowebLocalCoordinator
from .entity_ids import heater_object_id, heater_schedule_object_id
from .services import async_register_domain_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.CLIMATE,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.LOCK,
    Platform.SWITCH,
    Platform.SELECT,
]

# Schedule card frontend (plans/schedule-card.md WP1): one vanilla JS module,
# no build step, served straight out of this directory.
_FRONTEND_DIR = Path(__file__).parent / "frontend"
_FRONTEND_JS_FILENAME = "termoweb-local-schedule-card.js"
# Classic-script build for browsers HA's own frontend routes to the es5=True
# extra js list instead of the module import list (older WebViews that fail
# HA's isModern check); same card, transpiled and bundled, no behaviour change.
_FRONTEND_ES5_JS_FILENAME = "termoweb-local-schedule-card.es5.js"
_FRONTEND_URL_PATH = f"/{DOMAIN}_frontend"
# hass.data[DOMAIN] key guarding the registration below: keyed on hass, not
# on entry.runtime_data, since a second config entry's setup (or a reload of
# the first) must not re-register the static path or push a second
# <script> tag -- HA does not deduplicate either for us.
_FRONTEND_REGISTERED_KEY = "frontend_registered"


def _integration_version() -> str:
    manifest_path = Path(__file__).parent / "manifest.json"
    with manifest_path.open(encoding="utf-8") as handle:
        return json.load(handle)["version"]


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the schedule card's JS module and add it as an extra frontend
    module, once per hass regardless of config entry count (WP1's own
    acceptance criterion: "survives a config entry reload without a
    duplicate registration error")."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(_FRONTEND_REGISTERED_KEY):
        return
    # cache_headers=False: the card is now split across several ES modules
    # (schedule-card.js and friends) that the browser fetches with plain
    # static imports, each with no "?v=" of its own. Without this, a browser
    # that already cached an old sub-module from before a redeploy would
    # keep serving it forever; the entry file's own "?v=" below only busts
    # HA's frontend module cache for termoweb-local-schedule-card.js itself.
    await hass.http.async_register_static_paths(
        [StaticPathConfig(_FRONTEND_URL_PATH, str(_FRONTEND_DIR), cache_headers=False)]
    )
    # The version query string busts HA's own frontend module cache
    # (plan's Risks section) whenever the JS file changes and this version
    # is bumped; it is not itself a cache-buster HA interprets specially.
    js_url = f"{_FRONTEND_URL_PATH}/{_FRONTEND_JS_FILENAME}?v={_integration_version()}"
    add_extra_js_url(hass, js_url, es5=False)
    # Same version query string as the module build above: both files come
    # from the same release, so one version bump busts the cache for both.
    es5_js_url = f"{_FRONTEND_URL_PATH}/{_FRONTEND_ES5_JS_FILENAME}?v={_integration_version()}"
    add_extra_js_url(hass, es5_js_url, es5=True)
    domain_data[_FRONTEND_REGISTERED_KEY] = True


async def async_setup_entry(hass: HomeAssistant, entry: TermowebLocalConfigEntry) -> bool:
    """Set up termoweb_local from a config entry."""
    await _async_register_frontend(hass)

    coordinator = TermowebLocalCoordinator(hass, entry)
    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    # Before any platform is forwarded (owner decision 2026-09-06 task 3): a
    # heater entity already registered under an older release's object id
    # scheme is renamed here, while it is not yet loaded, so no platform ever
    # has to fight the registry for the entity_id its own code computes.
    _async_migrate_heater_entity_ids(hass, entry, coordinator)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    async_register_domain_services(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _expected_heater_object_id(name: str, kind: str) -> str:
    return heater_schedule_object_id(name) if kind == "schedule" else heater_object_id(name, kind)


def _async_migrate_heater_entity_ids(
    hass: HomeAssistant, entry: TermowebLocalConfigEntry, coordinator: TermowebLocalCoordinator
) -> None:
    """Owner decision 2026-09-06: entity_ids.py's object id scheme has moved
    on since this integration's first release (e.g. a discovery-default
    heater's climate entity used to get `_heater` appended even though its
    slug already contains "heater", producing `climate.heater_02_heater`
    instead of today's `climate.heater_02`, and its sibling sensors the same
    way); a heater registered under an older release keeps that entity_id
    forever otherwise, since once a unique_id is registered, HA uses the
    entity_id already stored for it, not whatever object id this
    integration's own entity classes compute at construction time. Renames
    every heater entity of this config entry whose current entity_id no
    longer matches its unique id's current object id; unique ids themselves
    are never touched, and an entity whose name cannot be resolved (a heater
    no longer configured) or whose target entity_id is already taken by
    something else is left alone."""
    registry = er.async_get(hass)
    for reg_entry in list(er.async_entries_for_config_entry(registry, entry.entry_id)):
        parts = (reg_entry.unique_id or "").split(":")
        if len(parts) != 5 or parts[0] != DOMAIN or parts[2] != CLOUD_NODE_TYPE_HEATER:
            continue
        try:
            node_id = int(parts[3])
        except ValueError:
            continue
        kind = parts[4]
        name = coordinator.heater_names.get(node_id)
        if name is None:
            continue
        domain = reg_entry.entity_id.split(".", 1)[0]
        expected_entity_id = f"{domain}.{_expected_heater_object_id(name, kind)}"
        if reg_entry.entity_id == expected_entity_id:
            continue
        if registry.async_get(expected_entity_id) is not None:
            _LOGGER.warning(
                "not renaming %s to %s: target entity id is already in use",
                reg_entry.entity_id, expected_entity_id,
            )
            continue
        registry.async_update_entity(reg_entry.entity_id, new_entity_id=expected_entity_id)
        _LOGGER.info(
            "renamed %s to %s (unique id unchanged)", reg_entry.entity_id, expected_entity_id
        )


async def async_unload_entry(hass: HomeAssistant, entry: TermowebLocalConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_shutdown()
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: TermowebLocalConfigEntry) -> None:
    """Options changed: reload the entry so the coordinator picks up a new poll
    interval/dev id/etc cleanly, rather than mutating a running coordinator's
    update_interval in place -- except when the coordinator itself made the
    change to persist a heater the discovery scan, a runtime unknown-id
    report, or pairing just found (or a removed heater's device deletion):
    consume_skip_reload() is True for exactly that one update, since that
    heater's entities were already created (or torn down) live, and a reload
    would only redo that by a slower path."""
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is not None and coordinator.consume_skip_reload():
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: TermowebLocalConfigEntry, device_entry
) -> bool:
    """Let a user delete one heater's device from the HA UI (its device page's
    own Delete action): removes it from this entry's persisted heater set
    (async_remove_heater), so a reload/restart does not bring it back, and
    drops it from the coordinator's live tables so its entities disappear.
    The gateway device itself (identifiers `(DOMAIN, dev_id)`, no
    `:<node_id>` suffix) is not removable this way; removing the whole
    integration is the equivalent action for it.

    HA's own caller (homeassistant/components/config/device_registry.py's
    `_async_remove_device`) only removes the device -- which is what cascades
    entity teardown, via EVENT_DEVICE_REGISTRY_UPDATED -> entity_registry's
    own listener -> each entity's async_will_remove_from_hass -- *after* this
    function returns True; there is no supported way to make that teardown
    happen synchronously from inside this callback, and HA does not offer
    one (this is a general HA hook contract, not something specific to this
    integration; verified against HA 2026.9.1's own entity_registry.py and
    entity.py). So a heater device removed here left a window, while this
    function still ran, in which coordinator.heaters had already lost the
    node but the entity (its own once-a-minute schedule tick included) had
    not yet been torn down and could still fire against the missing entry.

    The fix is ordering, not a forced teardown: remove the device from the
    registry *here*, before mutating coordinator.heaters at all, rather than
    leaving that to HA's caller afterward. HA's own caller already tolerates
    an integration doing this ("the integration might have removed the
    device already, that is fine" in its own source), and removing a device
    fires EVENT_DEVICE_REGISTRY_UPDATED synchronously to every @callback
    listener, including entity_registry's own, which enqueues each entity's
    removal as an eager-started task (hass.async_create_task_internal(...,
    eager_start=True)) -- confirmed by direct test (no `await` at all between
    the two calls) to run this entity's async_will_remove_from_hass, and so
    cancel its per-minute tick, to completion before this function's own
    next line runs. coordinator.heaters is therefore only ever mutated after
    every entity that could read it is already gone. sensor.py's own
    `_HeaterSensorBase._heater` guard (reads with .get(), never a bare
    index) stays regardless, as defence in depth for any other path that
    reaches a removed heater's dict entry -- correctness here no longer
    depends on it."""
    coordinator = getattr(entry, "runtime_data", None)
    dev_id = coordinator.dev_id if coordinator is not None else entry.options.get(
        CONF_DEV_ID, DEFAULT_DEV_ID
    )
    node_id: int | None = None
    prefix = f"{dev_id}:"
    for domain, identifier in device_entry.identifiers:
        if domain == DOMAIN and identifier.startswith(prefix):
            try:
                node_id = int(identifier[len(prefix):])
            except ValueError:
                continue
    if node_id is None:
        return False

    if coordinator is not None:
        device_registry = dr.async_get(hass)
        if device_registry.async_get(device_entry.id) is not None:
            device_registry.async_remove_device(device_entry.id)
        await coordinator.async_remove_heater(node_id)
    else:
        # The entry is not currently loaded (no running coordinator): still
        # drop the heater from persisted options directly, the same edit
        # TermowebLocalCoordinator.async_remove_heater makes -- identity
        # included, which that method keeps too, so re-pairing the same
        # heater gives it back the id it still holds.
        current_heaters = entry.options.get(CONF_HEATERS, entry.data.get(CONF_HEATERS, []))
        new_heaters = [h for h in current_heaters if h.get(CONF_HEATER_ID) != node_id]
        hass.config_entries.async_update_entry(
            entry,
            options={**entry.options, CONF_HEATERS: new_heaters},
        )
    return True

"""Entity id / unique id helpers.

Owner decision (2026-09-06): this integration's API (entity kinds,
attributes, service names and fields) mirrors the cloud `termoweb`
integration, but entity ids do not need to match it -- the two integrations
can coexist because every id differs. Heater object ids are derived from the
configured heater name by slug, `<slug>_heater` (e.g. "Living room" ->
`living_room_heater`), unless the slug already contains "heater" as one of
its underscore-separated words (e.g. the discovery default "Heater 02" slugs
to `heater_02`, and a user-given name like "Bedroom heater" slugs to
`bedroom_heater`), in which case the slug is used as-is with no `_heater`
appended. The entity kind is appended for every platform but climate itself
(`<slug>_heater_<kind>` or `<slug>_<kind>`, e.g.
`living_room_heater_temperature` / `heater_02_temperature`).
There is no cloud-verbatim table and no entity id suffix option: those only
existed to keep this integration's ids identical to (or distinguishable
from) the cloud's own, which the naming scheme no longer needs.

Unique ids keep the shape `termoweb_local:<dev_id>:<node_type>:<addr>:
<entity_kind>` (API parity: attributes/services/schemas are unchanged, only
entity ids differ from the cloud integration).
"""
from __future__ import annotations

import re

from .const import CLOUD_NODE_TYPE_GATEWAY, CLOUD_NODE_TYPE_HEATER, DOMAIN, GATEWAY_ADDR

_GATEWAY_OBJECT_IDS = {
    "gateway_online": "termoweb_local_gateway_online",
    "total_energy": "termoweb_local_total_energy",
    "force_refresh": "termoweb_local_force_refresh",
    "scan_for_heaters": "termoweb_local_scan_for_heaters",
    "pair_heater": "termoweb_local_pair_heater",
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    slug = _SLUG_RE.sub("_", name.strip().lower()).strip("_")
    return slug or "heater"


def heater_object_id(name: str, kind: str) -> str:
    """`<slug>_heater` for the climate entity itself; `<slug>_heater_<kind>`
    for every other platform, where `<slug>` is `name` lowercased with any
    run of non-alphanumeric characters (spaces, punctuation, capitals folded
    by the lowercasing) collapsed to a single underscore. `_heater` is
    omitted when the slug already contains "heater" as one of its
    underscore-separated words (a discovery default like "Heater 02", or a
    user-given name like "Bedroom heater"), so the base becomes `<slug>`
    instead of `<slug>_heater`."""
    slug = _slugify(name)
    base = slug if "heater" in slug.split("_") else f"{slug}_heater"
    return base if kind == "climate" else f"{base}_{kind}"


def heater_entity_id(domain: str, name: str, kind: str) -> str:
    return f"{domain}.{heater_object_id(name, kind)}"


def heater_schedule_object_id(name: str) -> str:
    """`<slug>_schedule`, not `<slug>_heater_schedule` (owner decision
    2026-09-06): the schedule sensor's object id and friendly name
    ("<Name> schedule") both skip the `_heater` infix every other per-heater
    sensor kind gets, so a discovery-default name like "Heater 02" reads
    "heater_02_schedule" rather than doubling the word ("heater_02_heater_
    schedule")."""
    return f"{_slugify(name)}_schedule"


def heater_schedule_entity_id(domain: str, name: str) -> str:
    return f"{domain}.{heater_schedule_object_id(name)}"


def heater_unique_id(dev_id: str, node_id: int, kind: str) -> str:
    return f"{DOMAIN}:{dev_id}:{CLOUD_NODE_TYPE_HEATER}:{node_id}:{kind}"


def gateway_object_id(kind: str) -> str:
    return _GATEWAY_OBJECT_IDS[kind]


def gateway_entity_id(domain: str, kind: str) -> str:
    return f"{domain}.{gateway_object_id(kind)}"


def gateway_unique_id(dev_id: str, kind: str) -> str:
    return f"{DOMAIN}:{dev_id}:{CLOUD_NODE_TYPE_GATEWAY}:{GATEWAY_ADDR}:{kind}"

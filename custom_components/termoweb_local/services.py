"""Domain-level services with no entity target
(docs/45-cloud-integration-api.md section 3): `import_energy_history` and
`ws_debug_probe` are cloud-only per the parity matrix, so both simply raise;
registered once per hass instance regardless of how many config entries this
integration has (docs/91-p4-parity-plan.md P4a)."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall

from .const import (
    ATTR_DEV_ID,
    ATTR_ENTRY_ID,
    ATTR_MAX_HISTORY_RETRIEVAL,
    ATTR_RESET_PROGRESS,
    DOMAIN,
    SERVICE_IMPORT_ENERGY_HISTORY,
    SERVICE_WS_DEBUG_PROBE,
)
from .errors import raise_not_applicable

_LOGGER = logging.getLogger(__name__)

IMPORT_ENERGY_HISTORY_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_RESET_PROGRESS): bool,
        vol.Optional(ATTR_MAX_HISTORY_RETRIEVAL, default=7): vol.All(int, vol.Range(min=1)),
    }
)
WS_DEBUG_PROBE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY_ID): str,
        vol.Optional(ATTR_DEV_ID): str,
    }
)


def async_register_domain_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_IMPORT_ENERGY_HISTORY):
        return  # already registered by an earlier config entry's setup

    async def _async_import_energy_history(call: ServiceCall) -> None:
        raise_not_applicable(_LOGGER, SERVICE_IMPORT_ENERGY_HISTORY)

    async def _async_ws_debug_probe(call: ServiceCall) -> None:
        raise_not_applicable(_LOGGER, SERVICE_WS_DEBUG_PROBE)

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_ENERGY_HISTORY,
        _async_import_energy_history,
        schema=IMPORT_ENERGY_HISTORY_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_WS_DEBUG_PROBE,
        _async_ws_debug_probe,
        schema=WS_DEBUG_PROBE_SCHEMA,
    )

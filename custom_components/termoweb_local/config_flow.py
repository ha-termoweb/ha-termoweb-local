"""Config flow for termoweb_local.

The user step asks only for the pyserial URL and the station id (owner direction
2026-09-06: "the heaters should be autodiscovered or there should be a workflow to
add them" -- no heater list typed by hand, anywhere); it validates the URL by
opening it through NanoCul in an executor, sending the firmware's `Q` status
command, and waiting for either the boot banner or the `Q` reply line (both start
with `# ... termoweb_rx`, firmware/README.md "Serial protocol"), rejecting with a
clear error when the port cannot be opened or neither line arrives. Heaters
themselves are found by the coordinator's own discovery scan at setup (an id-2-to-65
sweep, TermowebLocalCoordinator.async_scan_for_heaters) and its "Scan for heaters"/
"Pair heater" buttons, not by anything typed into this flow -- see coordinator.py
and button.py.

The options flow keeps the parity-specific fields (device id, heater
association) and the pairing window length, but has no heater text field of
any kind: adding a heater is the scan/pairing buttons' job, renaming is HA's own
device/entity rename (which never touches a unique id), and removing one is done by
deleting its device from the HA UI (__init__.py's async_remove_config_entry_device).
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError

from . import _vendor_compat  # noqa: F401 -- resolves termoweb_local for the lazy import below
from .const import (
    CONF_DEV_ID,
    CONF_HEATER_ASSOCIATION,
    CONF_HEATER_IDENTITIES,
    CONF_HEATERS,
    CONF_PAIR_HEATER_SECONDS,
    CONF_POLL_INTERVAL,
    CONF_SERIAL_URL,
    CONF_STATION_ID,
    DEFAULT_DEV_ID,
    DEFAULT_PAIR_HEATER_SECONDS,
    DEFAULT_POLL_INTERVAL_S,
    DEFAULT_SERIAL_URL,
    DEFAULT_STATION_ID,
    DOMAIN,
    MAX_PAIR_HEATER_SECONDS,
    MAX_POLL_INTERVAL_S,
    MIN_PAIR_HEATER_SECONDS,
    MIN_POLL_INTERVAL_S,
)

_LOGGER = logging.getLogger(__name__)

QQ_VALIDATION_TIMEOUT_S = 3.0


# "02:521A0905061730290 3, ..." -- zero or more "<hex id>:<18 hex chars>"
# entries, comma separated: the opaque 9-byte EB association-family value
# Network.startup_sequence sends that heater at station start
# (docs/PROTOCOL.md 5.6, owner direction 2026-09-06 task 2). Same shape and
# storage convention as CONF_HEATER_IDENTITIES: a node id absent here means
# startup_sequence skips the EB association frame for it (association_value
# default None), not that some other default value is sent.
_ASSOCIATION_ENTRY_RE = re.compile(r"^\s*([0-9A-Fa-f]{1,2})\s*:\s*([0-9A-Fa-f]{18})\s*$")


def parse_association(text: str) -> dict[str, str]:
    """Raises ValueError on any malformed entry or a value not exactly 9
    bytes (18 hex chars). An empty string is valid and means no heater has a
    configured association value."""
    association: dict[str, str] = {}
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        match = _ASSOCIATION_ENTRY_RE.match(chunk)
        if not match:
            raise ValueError(
                f"malformed association entry {chunk!r}, expected 'id:<18 hex chars>'"
            )
        node_id = int(match.group(1), 16)
        association[str(node_id)] = match.group(2).upper()
    return association


def format_association(association: dict[str, str]) -> str:
    """Inverse of parse_association, for pre-filling the options flow form."""
    return ", ".join(
        f"{int(node_id):02X}:{value}" for node_id, value in sorted(association.items(), key=lambda kv: int(kv[0]))
    )


def _validate_port_sync(url: str, station_id: int) -> None:
    """Blocking: open the port through NanoCul, ask for a Q status line, and wait
    for either it or the boot banner. Raises CannotConnect/InvalidStationId-style
    errors are not raised here; the caller maps exceptions to flow error codes."""
    # Imported lazily so a missing termoweb_local install fails inside this
    # function (mapped to "cannot_connect"), not at module import time.
    from termoweb_local.nanocul import NanoCul, RawLine

    nanocul = NanoCul(url=url, source_id=station_id)
    try:
        nanocul.send_raw_command("Q")
        deadline = time.time() + QQ_VALIDATION_TIMEOUT_S
        while time.time() < deadline:
            for event in nanocul.read_events():
                if isinstance(event, RawLine) and "termoweb_rx" in event.text:
                    return
        raise TimeoutError(
            f"no banner or Q reply from the stick at {url} within "
            f"{QQ_VALIDATION_TIMEOUT_S}s"
        )
    finally:
        nanocul.close()


class CannotConnect(HomeAssistantError):
    """Raised when the serial port cannot be opened or gives no banner/Q reply."""


class TermowebLocalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for termoweb_local."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            url = user_input[CONF_SERIAL_URL]
            station_id_text = user_input[CONF_STATION_ID]

            station_id: int | None = None

            try:
                station_id = int(station_id_text, 16)
                if not 0 <= station_id <= 0xFF:
                    raise ValueError("station id out of range")
            except ValueError:
                errors[CONF_STATION_ID] = "invalid_station_id"

            if not errors:
                await self.async_set_unique_id(url)
                self._abort_if_unique_id_configured()
                try:
                    await self.hass.async_add_executor_job(
                        _validate_port_sync, url, station_id
                    )
                except Exception:  # noqa: BLE001 - any failure to open/validate the port
                    _LOGGER.exception("Failed to validate nanoCUL at %s", url)
                    errors["base"] = "cannot_connect"
                else:
                    # No heaters field: the coordinator's own discovery scan
                    # (async_scan_for_heaters, run once at setup) finds them;
                    # CONF_HEATERS starts absent from entry.data and is only
                    # ever written to entry.options, by that scan, the
                    # runtime unknown-id path, or pairing (coordinator.py).
                    return self.async_create_entry(
                        title=f"Termoweb Local ({url})",
                        data={
                            CONF_SERIAL_URL: url,
                            CONF_STATION_ID: station_id_text,
                        },
                    )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SERIAL_URL,
                    default=(user_input or {}).get(CONF_SERIAL_URL, DEFAULT_SERIAL_URL),
                ): str,
                vol.Required(
                    CONF_STATION_ID,
                    default=(user_input or {}).get(CONF_STATION_ID, DEFAULT_STATION_ID),
                ): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return TermowebLocalOptionsFlow()


class TermowebLocalOptionsFlow(OptionsFlow):
    """Edit the poll interval, the "Pair heater" button's window length, and
    the parity-specific options (device id, heater association); the port
    itself is fixed once the entry is created. No heater field of any kind:
    a heater is added by the discovery scan or the "Pair heater"/"Scan for
    heaters" buttons (button.py), renamed through
    HA's own device/entity rename, and removed by deleting its device from
    the HA UI (__init__.py's async_remove_config_entry_device) -- owner
    direction 2026-09-06, "the heaters should be autodiscovered or there
    should be a workflow to add them"."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        # Untouched by this flow, carried through unchanged: an options-flow
        # async_create_entry replaces entry.options wholesale, so leaving
        # either of these out of `data` below would silently drop every
        # heater the scan/pairing workflow has already discovered.
        current_heaters = self.config_entry.options.get(
            CONF_HEATERS, self.config_entry.data.get(CONF_HEATERS, [])
        )
        current_identities = self.config_entry.options.get(CONF_HEATER_IDENTITIES, {})

        current_poll = self.config_entry.options.get(
            CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL_S
        )
        current_dev_id = self.config_entry.options.get(CONF_DEV_ID, DEFAULT_DEV_ID)
        current_association = self.config_entry.options.get(CONF_HEATER_ASSOCIATION, {})
        current_pair_seconds = self.config_entry.options.get(
            CONF_PAIR_HEATER_SECONDS, DEFAULT_PAIR_HEATER_SECONDS
        )

        if user_input is not None:
            poll_interval = user_input[CONF_POLL_INTERVAL]
            if not MIN_POLL_INTERVAL_S <= poll_interval <= MAX_POLL_INTERVAL_S:
                errors[CONF_POLL_INTERVAL] = "invalid_poll_interval"

            try:
                association = parse_association(user_input[CONF_HEATER_ASSOCIATION])
            except ValueError:
                errors[CONF_HEATER_ASSOCIATION] = "invalid_association"

            dev_id = user_input[CONF_DEV_ID].strip()
            if not dev_id:
                errors[CONF_DEV_ID] = "invalid_dev_id"

            pair_seconds = user_input[CONF_PAIR_HEATER_SECONDS]
            if not MIN_PAIR_HEATER_SECONDS <= pair_seconds <= MAX_PAIR_HEATER_SECONDS:
                errors[CONF_PAIR_HEATER_SECONDS] = "invalid_pair_seconds"

            if not errors:
                return self.async_create_entry(
                    data={
                        CONF_HEATERS: current_heaters,
                        CONF_HEATER_IDENTITIES: current_identities,
                        CONF_POLL_INTERVAL: poll_interval,
                        CONF_DEV_ID: dev_id,
                        CONF_HEATER_ASSOCIATION: association,
                        CONF_PAIR_HEATER_SECONDS: pair_seconds,
                    }
                )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=(user_input or {}).get(CONF_POLL_INTERVAL, current_poll),
                ): vol.Coerce(int),
                vol.Required(
                    CONF_DEV_ID,
                    default=(user_input or {}).get(CONF_DEV_ID, current_dev_id),
                ): str,
                vol.Optional(
                    CONF_HEATER_ASSOCIATION,
                    default=(user_input or {}).get(
                        CONF_HEATER_ASSOCIATION, format_association(current_association)
                    ),
                ): str,
                vol.Required(
                    CONF_PAIR_HEATER_SECONDS,
                    default=(user_input or {}).get(
                        CONF_PAIR_HEATER_SECONDS, current_pair_seconds
                    ),
                ): vol.Coerce(int),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

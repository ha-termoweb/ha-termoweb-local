"""button.<heater>_heater_flash_display (backed, F2 5E 01),
button.termoweb_local_force_refresh (backed), and the two discovery buttons
on the station device: button.termoweb_local_scan_for_heaters (repeats the
ids-2-to-65 scan) and button.termoweb_local_pair_heater (opens a discovery
window) -- owner direction 2026-09-06 tasks 2 and 3.
"""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .devices import gateway_device_info, heater_device_info
from .entity_ids import gateway_entity_id, gateway_unique_id, heater_entity_id, heater_unique_id
from .coordinator import TermowebLocalCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    coordinator: TermowebLocalCoordinator = entry.runtime_data

    def _build_heater_buttons(node_id: int) -> list[CoordinatorEntity]:
        return [TermowebLocalFlashDisplayButton(coordinator, node_id)]

    entities: list[CoordinatorEntity] = [
        button for node_id in coordinator.heaters for button in _build_heater_buttons(node_id)
    ]
    entities.append(TermowebLocalForceRefreshButton(coordinator))
    entities.append(TermowebLocalScanForHeatersButton(coordinator))
    entities.append(TermowebLocalPairHeaterButton(coordinator))
    async_add_entities(entities)
    # A heater discovered after this setup (scan, an unknown id's own
    # report, or pairing) gets its own flash_display button created
    # immediately through this same callback -- no reload, no restart.
    coordinator.register_platform("button", async_add_entities, _build_heater_buttons)


class TermowebLocalFlashDisplayButton(
    CoordinatorEntity[TermowebLocalCoordinator], ButtonEntity
):
    """F2 5E 01 (2026-09-06 P4b proof, notes.md 16:56:59Z): flash the
    heater's own display."""

    _attr_has_entity_name = True
    _attr_name = "Flash display"
    _attr_icon = "mdi:gesture-tap-button"

    def __init__(self, coordinator: TermowebLocalCoordinator, node_id: int) -> None:
        super().__init__(coordinator)
        self._node_id = node_id
        name = coordinator.heater_names.get(node_id, f"Heater {node_id:02X}")
        self.entity_id = heater_entity_id("button", name, "flash_display")
        self._attr_unique_id = heater_unique_id(coordinator.dev_id, node_id, "flash_display")
        self._attr_device_info = heater_device_info(
            coordinator.dev_id, node_id, name, coordinator.gateway_device_id
        )

    async def async_press(self) -> None:
        await self.coordinator.async_flash_display(self._node_id)


class TermowebLocalForceRefreshButton(
    CoordinatorEntity[TermowebLocalCoordinator], ButtonEntity
):
    """F3 B8 status request to every heater (docs/PROTOCOL.md 5.6), wrapping
    the existing poll_now coordinator method (parity matrix: "backed, wrapped
    as a button")."""

    _attr_has_entity_name = True
    _attr_name = "Force refresh"

    def __init__(self, coordinator: TermowebLocalCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_id = gateway_entity_id("button", "force_refresh")
        self._attr_unique_id = gateway_unique_id(coordinator.dev_id, "force_refresh")
        self._attr_device_info = gateway_device_info(coordinator.dev_id)

    async def async_press(self) -> None:
        await self.coordinator.async_poll_now()


class TermowebLocalScanForHeatersButton(
    CoordinatorEntity[TermowebLocalCoordinator], ButtonEntity
):
    """Repeats the ids-2-to-65 discovery scan on demand (owner direction
    2026-09-06 task 2): registers any answering id not already configured as
    a new heater, entities created immediately, same as the scan run once
    at coordinator setup (TermowebLocalCoordinator.async_scan_for_heaters)."""

    _attr_has_entity_name = True
    _attr_name = "Scan for heaters"
    _attr_icon = "mdi:radar"

    def __init__(self, coordinator: TermowebLocalCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_id = gateway_entity_id("button", "scan_for_heaters")
        self._attr_unique_id = gateway_unique_id(coordinator.dev_id, "scan_for_heaters")
        self._attr_device_info = gateway_device_info(coordinator.dev_id)

    async def async_press(self) -> None:
        await self.coordinator.async_scan_for_heaters()


class TermowebLocalPairHeaterButton(CoordinatorEntity[TermowebLocalCoordinator], ButtonEntity):
    """Opens a discovery window (Network.start_discovery via
    TermowebLocalCoordinator.start_pairing) for this entry's configured
    CONF_PAIR_HEATER_SECONDS (default 120 s): put the physical heater into
    its own pairing mode (see its manual) during the window (owner
    direction 2026-09-06 task 3, 2026-09-06 17:12:17Z pairing capture)."""

    _attr_has_entity_name = True
    _attr_name = "Pair heater"
    _attr_icon = "mdi:link-plus"

    def __init__(self, coordinator: TermowebLocalCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_id = gateway_entity_id("button", "pair_heater")
        self._attr_unique_id = gateway_unique_id(coordinator.dev_id, "pair_heater")
        self._attr_device_info = gateway_device_info(coordinator.dev_id)

    async def async_press(self) -> None:
        self.coordinator.start_pairing()

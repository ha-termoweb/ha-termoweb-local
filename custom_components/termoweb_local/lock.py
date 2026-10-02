"""lock.<heater>_child_lock -- BA, the keypad lock (2026-09-13 X-19 proof,
PROTOCOL.md 5.6), backed now; docs/91-p4-parity-plan.md P4a listed it as a
placeholder pending exactly this capture."""
from __future__ import annotations

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .devices import heater_device_info
from .entity_ids import heater_entity_id, heater_unique_id
from .coordinator import TermowebLocalCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    coordinator: TermowebLocalCoordinator = entry.runtime_data

    def _build(node_id: int) -> list[TermowebLocalChildLockLock]:
        return [TermowebLocalChildLockLock(coordinator, node_id)]

    async_add_entities(
        entity for node_id in coordinator.heaters for entity in _build(node_id)
    )
    # A heater discovered after this setup (scan, an unknown id's own
    # report, or pairing) gets its own lock entity created immediately
    # through this same callback -- no reload, no restart.
    coordinator.register_platform("lock", async_add_entities, _build)


class TermowebLocalChildLockLock(CoordinatorEntity[TermowebLocalCoordinator], LockEntity):
    """BA 01/BA 00 (2026-09-13 X-19 proof, PROTOCOL.md 5.6): the keypad
    lock, radio-confirmed on every status report (byte 24 bit 1,
    HeaterSnapshot.locked)."""

    _attr_has_entity_name = True
    _attr_name = "Child lock"

    def __init__(self, coordinator: TermowebLocalCoordinator, node_id: int) -> None:
        super().__init__(coordinator)
        self._node_id = node_id
        name = coordinator.heater_names.get(node_id, f"Heater {node_id:02X}")
        self.entity_id = heater_entity_id("lock", name, "child_lock")
        self._attr_unique_id = heater_unique_id(coordinator.dev_id, node_id, "child_lock")
        self._attr_device_info = heater_device_info(
            coordinator.dev_id, node_id, name, coordinator.gateway_device_id
        )

    @property
    def _heater(self):
        return self.coordinator.heaters.get(self._node_id)

    @property
    def is_locked(self) -> bool | None:
        heater = self._heater
        snap = heater.last_snapshot if heater else None
        return None if snap is None else snap.locked

    async def async_lock(self, **kwargs) -> None:
        await self.coordinator.async_set_child_lock(self._node_id, True)

    async def async_unlock(self, **kwargs) -> None:
        await self.coordinator.async_set_child_lock(self._node_id, False)

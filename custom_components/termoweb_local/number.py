"""number.<heater>_priority (placeholder) -- docs/91-p4-parity-plan.md P4a
parity matrix: 0-30 step 1, no radio frame captured yet (P4b), state unknown
on the live cloud system too.

number.<heater>_temperature_offset (2026-09-13 X-19 proof, PROTOCOL.md 5.6):
backed by C4's own offset field, -3.0..3.0C in 0.1C steps. Panel-confirmed at
heater 04, 2026-09-13 (docs/captures/2026-09-13-x19/link-test.md, "Panel
confirmations"): the panel's own Temp Offset shows the wire byte's own sign
(wire F6 read -1.0 on the panel), and the heater applies reported = measured
minus offset (the reading rose 23.5 to 24.5 with a -1.0 offset). This
entity's HA-facing value matches the panel's Temp Offset exactly, so it is
the persisted offset_tenths field unchanged (no sign flip): HA -1.0 sends
wire -10 (F6) and raises the reported temperature by 1.0; HA +1.0 sends wire
+10 (0A) and lowers it.
"""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .devices import heater_device_info
from .entity_ids import heater_entity_id, heater_unique_id
from .errors import raise_not_supported
from .coordinator import TermowebLocalCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    coordinator: TermowebLocalCoordinator = entry.runtime_data

    def _build(node_id: int) -> list[CoordinatorEntity]:
        return [
            TermowebLocalPriorityNumber(coordinator, node_id),
            TermowebLocalTemperatureOffsetNumber(coordinator, node_id),
        ]

    async_add_entities(
        entity for node_id in coordinator.heaters for entity in _build(node_id)
    )
    # A heater discovered after this setup (scan, an unknown id's own
    # report, or pairing) gets its own number entities created immediately
    # through this same callback -- no reload, no restart.
    coordinator.register_platform("number", async_add_entities, _build)


class TermowebLocalPriorityNumber(CoordinatorEntity[TermowebLocalCoordinator], NumberEntity):
    """Placeholder: no radio frame captured yet for this action (P4b)."""

    _attr_has_entity_name = True
    _attr_name = "Priority"
    _attr_icon = "mdi:priority-high"
    _attr_native_min_value = 0
    _attr_native_max_value = 30
    _attr_native_step = 1

    def __init__(self, coordinator: TermowebLocalCoordinator, node_id: int) -> None:
        super().__init__(coordinator)
        self._node_id = node_id
        name = coordinator.heater_names.get(node_id, f"Heater {node_id:02X}")
        self.entity_id = heater_entity_id("number", name, "priority")
        self._attr_unique_id = heater_unique_id(coordinator.dev_id, node_id, "priority")
        self._attr_device_info = heater_device_info(
            coordinator.dev_id, node_id, name, coordinator.gateway_device_id
        )

    @property
    def native_value(self) -> None:
        return None

    async def async_set_native_value(self, value: float) -> None:
        raise_not_supported(_LOGGER, f"priority on node {self._node_id:02x}")


class TermowebLocalTemperatureOffsetNumber(CoordinatorEntity[TermowebLocalCoordinator], NumberEntity):
    """C4's own offset field (2026-09-13 X-19 proof, PROTOCOL.md 5.6):
    -3.0..3.0C in 0.1C steps. Panel-confirmed 2026-09-13
    (docs/captures/2026-09-13-x19/link-test.md): this entity's value matches
    the panel's own Temp Offset, so it is the persisted offset_tenths field
    unchanged; a negative offset raises the reported temperature. Optimistic
    between writes, like every other C4 field with no independent radio
    confirmation for this entity's own displayed value (the room-temperature
    E5/E6 report the offset itself changes is the real confirmation, not
    this number)."""

    _attr_has_entity_name = True
    _attr_name = "Temperature offset"
    _attr_icon = "mdi:thermometer-plus"
    _attr_native_unit_of_measurement = "°C"
    _attr_native_min_value = -3.0
    _attr_native_max_value = 3.0
    _attr_native_step = 0.1

    def __init__(self, coordinator: TermowebLocalCoordinator, node_id: int) -> None:
        super().__init__(coordinator)
        self._node_id = node_id
        name = coordinator.heater_names.get(node_id, f"Heater {node_id:02X}")
        self.entity_id = heater_entity_id("number", name, "temperature_offset")
        self._attr_unique_id = heater_unique_id(coordinator.dev_id, node_id, "temperature_offset")
        self._attr_device_info = heater_device_info(
            coordinator.dev_id, node_id, name, coordinator.gateway_device_id
        )

    @property
    def native_value(self) -> float:
        offset_tenths = self.coordinator.get_advanced_setup(self._node_id)["offset_tenths"]
        return round(offset_tenths / 10.0, 1)

    async def async_set_native_value(self, value: float) -> None:
        offset_tenths = round(value * 10)
        await self.coordinator.async_write_advanced_setup(
            self._node_id, offset_tenths=offset_tenths
        )

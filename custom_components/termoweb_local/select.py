"""select.<heater>_control_mode/_units (2026-09-13 X-19 proof, PROTOCOL.md 5.6,
docs/captures/2026-09-13-x19/notes.md): two of the C4 advanced-setup record's
eight fields. control_mode's own 0-4 wire-to-label map was panel-confirmed at
heater 04 on 2026-09-13 (docs/captures/2026-09-13-x19/link-test.md, "Panel
confirmations"); units still has no radio readback at all ("the wire encoding
does not change with the display unit, panel check owed"). State is read from
the persisted record (Coordinator.get_advanced_setup), optimistic between
writes, like the two switches with the same caveat (switch.py's
open_window_detection/true_radiant).
"""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .devices import heater_device_info
from .entity_ids import heater_entity_id, heater_unique_id
from .coordinator import TermowebLocalCoordinator

_LOGGER = logging.getLogger(__name__)

# control_mode's own 0-4 wire-to-label map was panel-confirmed at heater 04,
# 2026-09-13 (docs/captures/2026-09-13-x19/link-test.md, "Panel
# confirmations": wire 0/1/2/3/4 read Hysteresis .25/.35/.50/.75/PID on the
# panel's own Control Type display).
CONTROL_MODE_OPTIONS: dict[str, int] = {
    "PID": 4,
    "Hysteresis 0.25C": 0,
    "Hysteresis 0.35C": 1,
    "Hysteresis 0.5C": 2,
    "Hysteresis 0.75C": 3,
}
_CONTROL_MODE_VALUES = {value: option for option, value in CONTROL_MODE_OPTIONS.items()}

# units: "the wire encoding does not change with the display unit, panel
# check owed" (docs/captures/2026-09-13-x19/notes.md) -- accepted on this
# heater but unconfirmed at the panel either way.
UNITS_OPTIONS: dict[str, int] = {"C": 0, "F": 1}
_UNITS_VALUES = {value: option for option, value in UNITS_OPTIONS.items()}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    coordinator: TermowebLocalCoordinator = entry.runtime_data

    def _build(node_id: int) -> list[CoordinatorEntity]:
        return [
            TermowebLocalControlModeSelect(coordinator, node_id),
            TermowebLocalUnitsSelect(coordinator, node_id),
        ]

    async_add_entities(
        entity for node_id in coordinator.heaters for entity in _build(node_id)
    )
    # A heater discovered after this setup (scan, an unknown id's own
    # report, or pairing) gets its own selects created immediately through
    # this same callback -- no reload, no restart.
    coordinator.register_platform("select", async_add_entities, _build)


class _AdvancedSetupFieldSelect(CoordinatorEntity[TermowebLocalCoordinator], SelectEntity):
    _attr_has_entity_name = True

    _kind: str  # entity_ids.py kind key, set by each subclass
    _field: str  # _DEFAULT_ADVANCED_SETUP key, set by each subclass
    _values_by_option: dict[str, int]  # option string -> wire value, set by each subclass
    _options_by_value: dict[int, str]  # wire value -> option string, set by each subclass

    def __init__(self, coordinator: TermowebLocalCoordinator, node_id: int) -> None:
        super().__init__(coordinator)
        self._node_id = node_id
        name = coordinator.heater_names.get(node_id, f"Heater {node_id:02X}")
        self.entity_id = heater_entity_id("select", name, self._kind)
        self._attr_unique_id = heater_unique_id(coordinator.dev_id, node_id, self._kind)
        self._attr_device_info = heater_device_info(
            coordinator.dev_id, node_id, name, coordinator.gateway_device_id
        )

    @property
    def current_option(self) -> str | None:
        value = self.coordinator.get_advanced_setup(self._node_id)[self._field]
        return self._options_by_value.get(value)

    async def async_select_option(self, option: str) -> None:
        value = self._values_by_option[option]
        await self.coordinator.async_write_advanced_setup(self._node_id, **{self._field: value})


class TermowebLocalControlModeSelect(_AdvancedSetupFieldSelect):
    """C4's own control_mode field: PID vs one of four hysteresis steps
    (2026-09-13 X-19 proof); the 0-4 mapping is panel-confirmed
    (CONTROL_MODE_OPTIONS's own comment, docs/captures/2026-09-13-x19/
    link-test.md)."""

    _kind = "control_mode"
    _attr_name = "Control mode"
    _attr_icon = "mdi:thermostat"
    _field = "control_mode"
    _values_by_option = CONTROL_MODE_OPTIONS
    _options_by_value = _CONTROL_MODE_VALUES
    _attr_options = list(CONTROL_MODE_OPTIONS)


class TermowebLocalUnitsSelect(_AdvancedSetupFieldSelect):
    """C4's own units field: accepted on this heater with no on-air change
    in encoding (2026-09-13 X-19 proof, panel check owed)."""

    _kind = "units"
    _attr_name = "Units"
    _attr_icon = "mdi:temperature-celsius"
    _field = "units"
    _values_by_option = UNITS_OPTIONS
    _options_by_value = _UNITS_VALUES
    _attr_options = list(UNITS_OPTIONS)

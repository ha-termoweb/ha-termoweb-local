"""switch.<heater>_boost/_easy_mode/_runback/_open_window_detection/_true_radiant
(2026-09-13 X-19 proof, PROTOCOL.md 5.6, docs/captures/2026-09-13-x19/notes.md):
boost/easy_mode/runback read their state from byte 24's own flag bits
(HeaterSnapshot.boost/easy/runback, termoweb_local.heater), radio-confirmed on
every status report. open_window_detection/true_radiant have no radio
readback at all, so they read the persisted C4 record instead
(Coordinator.get_advanced_setup) and are optimistic between writes.
open_window_detection's panel toggle is its only confirmation (notes.md's own
caveat); true_radiant was panel-confirmed 2026-09-13 to have no panel menu at
all on the Sun Ray RF (docs/captures/2026-09-13-x19/link-test.md, "Panel
confirmations"), so its own state has no confirmation path either way.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .devices import heater_device_info
from .entity_ids import heater_entity_id, heater_unique_id
from .coordinator import TermowebLocalCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    coordinator: TermowebLocalCoordinator = entry.runtime_data

    def _build(node_id: int) -> list[CoordinatorEntity]:
        return [
            TermowebLocalBoostSwitch(coordinator, node_id),
            TermowebLocalEasyModeSwitch(coordinator, node_id),
            TermowebLocalRunbackSwitch(coordinator, node_id),
            TermowebLocalOpenWindowDetectionSwitch(coordinator, node_id),
            TermowebLocalTrueRadiantSwitch(coordinator, node_id),
        ]

    async_add_entities(
        entity for node_id in coordinator.heaters for entity in _build(node_id)
    )
    # A heater discovered after this setup (scan, an unknown id's own
    # report, or pairing) gets its own switches created immediately through
    # this same callback -- no reload, no restart.
    coordinator.register_platform("switch", async_add_entities, _build)


class _HeaterSwitchBase(CoordinatorEntity[TermowebLocalCoordinator], SwitchEntity):
    _attr_has_entity_name = True

    _kind: str  # entity_ids.py kind key, set by each subclass

    def __init__(self, coordinator: TermowebLocalCoordinator, node_id: int) -> None:
        super().__init__(coordinator)
        self._node_id = node_id
        name = coordinator.heater_names.get(node_id, f"Heater {node_id:02X}")
        self.entity_id = heater_entity_id("switch", name, self._kind)
        self._attr_unique_id = heater_unique_id(coordinator.dev_id, node_id, self._kind)
        self._attr_device_info = heater_device_info(
            coordinator.dev_id, node_id, name, coordinator.gateway_device_id
        )

    @property
    def _heater(self):
        """The coordinator's own heater record, or None once it is gone from
        coordinator.heaters; see sensor.py's _HeaterSensorBase._heater for
        why a bare `[self._node_id]` index is not safe here either."""
        return self.coordinator.heaters.get(self._node_id)

    @property
    def _snapshot(self):
        heater = self._heater
        return heater.last_snapshot if heater else None


class _RadioConfirmedToggleSwitch(_HeaterSwitchBase):
    """boost/easy_mode/runback: state comes straight from byte 24's own flag
    bit, confirmed on every status report -- no optimism needed, unlike the
    two switches below."""

    _flag_attr: str  # HeaterSnapshot property name, set by each subclass

    @property
    def is_on(self) -> bool | None:
        snap = self._snapshot
        return None if snap is None else getattr(snap, self._flag_attr)


class TermowebLocalBoostSwitch(_RadioConfirmedToggleSwitch):
    """D2 01/D2 00 (2026-09-13 X-19 proof): boost start/cancel, the fixed
    60-minute duration coming from the heater's own boost-time setting, not
    from this switch. A DA read follows every toggle so
    climate.py's own boost_temperature attribute reflects it
    (Coordinator.async_start_boost/async_cancel_boost)."""

    _kind = "boost"
    _attr_name = "Boost"
    _attr_icon = "mdi:rocket-launch"
    _flag_attr = "boost"

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_start_boost(self._node_id)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_cancel_boost(self._node_id)


class TermowebLocalEasyModeSwitch(_RadioConfirmedToggleSwitch):
    """D6 01/D6 00 (2026-09-13 X-19 proof): forces mode heat, setpoint
    unchanged; EASY off restores the previous mode."""

    _kind = "easy_mode"
    _attr_name = "Easy mode"
    _attr_icon = "mdi:knob"
    _flag_attr = "easy"

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_easy_mode(self._node_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_easy_mode(self._node_id, False)


class TermowebLocalRunbackSwitch(_RadioConfirmedToggleSwitch):
    """D4 01/D4 00 (2026-09-13 X-19 proof): Runback Config forces mode heat
    and the setpoint to the anti-frost preset; the coordinator caches the
    pre-toggle mode/setpoint on the way on and always restores both on the
    way off, whatever the cached mode was -- off/auto included, not just
    manual/override (Coordinator.async_set_runback's own docstring)."""

    _kind = "runback"
    _attr_name = "Runback"
    _attr_icon = "mdi:restore"
    _flag_attr = "runback"

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_runback(self._node_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_runback(self._node_id, False)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # None: this coordinator has never turned Runback off for this node
        # (never toggled, or only ever toggled on); once it has, True if the
        # cached mode/setpoint were restored and False only if a restore
        # command failed, per Coordinator.get_runback_restored's own
        # docstring.
        return {"restored": self.coordinator.get_runback_restored(self._node_id)}


class _AdvancedSetupFlagSwitch(_HeaterSwitchBase):
    """open_window_detection/true_radiant: no radio readback exists for
    either field (docs/captures/2026-09-13-x19/notes.md: "byte 24 bit 4/3 is
    the activity flag and stayed clear with the heater off, so the panel
    toggle is the only readback, owner check owed"), so state is read from
    the persisted C4 record -- what this integration last asked for, not a
    radio-confirmed fact -- and optimistic between writes, like every other
    C4 field with the same caveat (select.py's control_mode/units)."""

    _field: str  # _DEFAULT_ADVANCED_SETUP key, set by each subclass

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.get_advanced_setup(self._node_id)[self._field])

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_write_advanced_setup(self._node_id, **{self._field: 1})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_write_advanced_setup(self._node_id, **{self._field: 0})


class TermowebLocalOpenWindowDetectionSwitch(_AdvancedSetupFlagSwitch):
    """C4's own window_mode field (2026-09-13 X-19 proof): optimistic, no
    radio confirmation exists for this field (feature-gap matrix's own
    caveat)."""

    _kind = "open_window_detection"
    _attr_name = "Open window detection"
    _attr_icon = "mdi:window-open-variant"
    _field = "window_mode"


class TermowebLocalTrueRadiantSwitch(_AdvancedSetupFlagSwitch):
    """C4's own true_radiant field (2026-09-13 X-19 proof): panel-confirmed
    at heater 04, 2026-09-13, that the Sun Ray RF panel has no True Radiant
    menu at all (docs/captures/2026-09-13-x19/link-test.md, "Panel
    confirmations") -- the heater accepts the byte with no panel effect.
    Kept for API parity with the cloud integration; state is read from the
    persisted record (Coordinator.get_advanced_setup) and optimistic between
    writes, since there is no panel or radio readback to confirm it against
    either way."""

    _kind = "true_radiant"
    _attr_name = "True radiant"
    _attr_icon = "mdi:radiator"
    _field = "true_radiant"

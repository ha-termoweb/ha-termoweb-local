"""Gateway online binary sensor (docs/91-p4-parity-plan.md P4a): one
gateway-level entity, not per-heater,
`binary_sensor.termoweb_local_gateway_online`, matching the cloud
integration's `ws_status`-style attribute set (docs/45-cloud-integration-api.md
section 1) even though the entity id itself does not need to match.

Derived only from the serial link being open and report cadence
(TermowebLocalCoordinator.gateway_connected, docs/PROTOCOL.md section 6): RSSI
and LQI are receiver-side diagnostics, never a link-status signal, and are not
read anywhere in this module. `ws_status`/`ws_last_event_at`/`ws_healthy_minutes`
have no websocket here, so their local equivalents are `link_status`/
`last_frame_at`/`link_healthy_minutes`.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import _vendor_compat  # noqa: F401 -- resolves termoweb_local before the import below
from termoweb_local.heater import MODE_OFF

from .devices import gateway_device_info, heater_device_info
from .entity_ids import gateway_entity_id, gateway_unique_id, heater_entity_id, heater_unique_id
from .const import GATEWAY_DEVICE_NAME, GATEWAY_MODEL
from .coordinator import TermowebLocalCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    coordinator: TermowebLocalCoordinator = entry.runtime_data

    def _build(node_id: int) -> list[TermowebLocalHeatingWhileOffBinarySensor]:
        return [TermowebLocalHeatingWhileOffBinarySensor(coordinator, node_id)]

    entities: list[CoordinatorEntity] = [TermowebLocalGatewayOnlineBinarySensor(coordinator)]
    entities.extend(
        entity for node_id in coordinator.heaters for entity in _build(node_id)
    )
    async_add_entities(entities)
    # A heater discovered after this setup (scan, an unknown id's own
    # report, or pairing) gets its own heating_while_off sensor created
    # immediately through this same callback -- no reload, no restart.
    coordinator.register_platform("binary_sensor", async_add_entities, _build)


class TermowebLocalGatewayOnlineBinarySensor(
    CoordinatorEntity[TermowebLocalCoordinator], BinarySensorEntity
):
    """On when the serial link is open and at least one heater has reported
    within 2 of its own expected report periods."""

    _attr_has_entity_name = True
    _attr_name = "Gateway online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: TermowebLocalCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_id = gateway_entity_id("binary_sensor", "gateway_online")
        self._attr_unique_id = gateway_unique_id(coordinator.dev_id, "gateway_online")
        self._attr_device_info = gateway_device_info(coordinator.dev_id)

    @property
    def is_on(self) -> bool:
        return self.coordinator.gateway_connected()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        connected = self.is_on
        last_frame_at = self.coordinator.gateway_last_frame_at()
        return {
            "dev_id": self.coordinator.dev_id,
            "name": GATEWAY_DEVICE_NAME,
            "connected": connected,
            "model": GATEWAY_MODEL,
            "link_status": "healthy" if connected else "unhealthy",
            "last_frame_at": (
                dt.datetime.fromtimestamp(last_frame_at, tz=dt.timezone.utc).isoformat(
                    timespec="seconds"
                )
                if last_frame_at is not None
                else None
            ),
            "link_healthy_minutes": self.coordinator.gateway_link_healthy_minutes(),
            # Pairing window state (owner direction 2026-09-06 task 3): an
            # attribute here rather than a separate binary_sensor entity,
            # per the task's own "(or an attribute on gateway_online)".
            "pairing_active": self.coordinator.network.discovery_active(),
            # Stall watchdog reconnect count (2026-09-06 live stall
            # postmortem, task 3): how many times this coordinator has had
            # to reopen the port since it was loaded.
            "reconnect_count": self.coordinator.reconnect_count,
        }


class TermowebLocalHeatingWhileOffBinarySensor(
    CoordinatorEntity[TermowebLocalCoordinator], BinarySensorEntity
):
    """On when a heater looks idle by every field except pcb_temp: mode off,
    duty 0, but pcb_temp has climbed more than `_RISE_THRESHOLD_C` over the
    last `_WINDOW_S` (docs/80-handover.md list C item 11). This is the shape
    of the 2026-09-12 20:28-21:20 incident (PROTOCOL.md 5.10's "Incident"
    paragraph): a panel Boost left the element running for an hour while
    every report read mode off, duty 0, no active/boost flag, and only
    pcb_temp (29 to 57 over that hour) showed anything was wrong.

    The rolling window lives on the entity itself, one (received_at,
    pcb_temp_c) sample per new snapshot: coordinator-level state would
    outlive this entity's own lifecycle for no benefit, since nothing else
    needs the history."""

    _attr_has_entity_name = True
    _attr_name = "Heating while off"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _RISE_THRESHOLD_C = 3.0
    _WINDOW_S = 10 * 60

    def __init__(self, coordinator: TermowebLocalCoordinator, node_id: int) -> None:
        super().__init__(coordinator)
        self._node_id = node_id
        name = coordinator.heater_names.get(node_id, f"Heater {node_id:02X}")
        self.entity_id = heater_entity_id("binary_sensor", name, "heating_while_off")
        self._attr_unique_id = heater_unique_id(coordinator.dev_id, node_id, "heating_while_off")
        self._attr_device_info = heater_device_info(
            coordinator.dev_id, node_id, name, coordinator.gateway_device_id
        )
        self._samples: list[tuple[float, int]] = []
        self._last_sampled_at: float | None = None

    @property
    def _heater(self):
        """See climate.py's own `_heater`: None once this node id is gone
        from coordinator.heaters, on the same removal-vs-teardown ordering
        window as every other per-heater entity on this coordinator."""
        return self.coordinator.heaters.get(self._node_id)

    def _record_sample(self) -> None:
        heater = self._heater
        snap = heater.last_snapshot if heater is not None else None
        if snap is None or snap.received_at == self._last_sampled_at:
            return
        self._last_sampled_at = snap.received_at
        self._samples.append((snap.received_at, snap.raw_byte25))
        cutoff = snap.received_at - self._WINDOW_S
        self._samples = [sample for sample in self._samples if sample[0] >= cutoff]

    @property
    def is_on(self) -> bool | None:
        heater = self._heater
        snap = heater.last_snapshot if heater is not None else None
        if snap is None:
            return None
        if snap.mode_code != MODE_OFF or snap.duty_candidate != 0:
            return False
        if len(self._samples) < 2:
            return False
        oldest_pcb_temp = self._samples[0][1]
        return (snap.raw_byte25 - oldest_pcb_temp) > self._RISE_THRESHOLD_C

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        heater = self._heater
        snap = heater.last_snapshot if heater is not None else None
        return {
            "addr": self._node_id,
            "pcb_temp": None if snap is None else snap.raw_byte25,
            "window_minutes": self._WINDOW_S // 60,
        }

    async def async_added_to_hass(self) -> None:
        # A baseline sample as soon as this entity is added: CoordinatorEntity's
        # own async_added_to_hass only registers the update listener, it never
        # calls _handle_coordinator_update itself, so without this the snapshot
        # setup's own first refresh already recorded would never enter the
        # window at all -- the rise this sensor looks for would need two
        # coordinator updates after add before it could ever be seen.
        await super().async_added_to_hass()
        self._record_sample()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._record_sample()
        super()._handle_coordinator_update()

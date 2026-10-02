"""Climate entity per heater.

hvac modes off/heat/auto map onto the F2 mode payload (docs/PROTOCOL.md 5.2,
termoweb_local.network.Network.set_mode): off -> MODE_OFF, heat -> MODE_MANUAL,
auto -> MODE_AUTO. The temporary_override preset maps onto the F1 override payload
(PROTOCOL.md 5.1/5.2, phase3 fact): selecting it sends `F1 B4 03 <half-degrees>`
with the entity's current target temperature; selecting "none" sends the plain mode
heat command, per docs/90-phase3-plan.md P4's own wording ("selecting
temporary_override with the current target sends the F1 B4 03 frame and none
returns to heat"). A set_temperature call while temporary_override is already the
active preset re-sends the override frame with the new value instead of the plain
setpoint frame (F1 B4 02), so adjusting the temperature does not silently drop back
to mode heat; this is not a new protocol claim, it is choosing between the two
already-given payload shapes based on which one is currently in effect.

hvac_action comes from the heating-flag candidate byte (E5 logical byte 24,
Heater.HeaterSnapshot.heating_flag_candidate) when a snapshot exists; before any
report has arrived there is no snapshot at all, so hvac_action is None rather than
guessed.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
    PRESET_NONE,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_platform
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import _vendor_compat  # noqa: F401 -- resolves termoweb_local before the import below
from termoweb_local.heater import MODE_AUTO, MODE_MANUAL, MODE_OFF, MODE_OVERRIDE
from termoweb_local.network import (
    DAYS_PER_WEEK,
    SLOTS_PER_DAY_HALF_HOURLY,
    SLOTS_PER_DAY_HOURLY,
)

from .devices import heater_device_info
from .entity_ids import heater_entity_id, heater_unique_id
from .errors import raise_not_applicable
from .const import (
    ATTR_COLD,
    ATTR_DAY,
    ATTR_MINUTES,
    ATTR_NIGHT,
    ATTR_PROG,
    ATTR_PTEMP,
    ATTR_TEMPERATURE_FIELD,
    PRESET_TEMPORARY_OVERRIDE,
    SERVICE_CANCEL_BOOST,
    SERVICE_POLL_NOW,
    SERVICE_SET_ACM_PRESET,
    SERVICE_SET_PRESET_TEMPERATURES,
    SERVICE_SET_SCHEDULE,
    SERVICE_START_BOOST,
    SERVICE_SYNC_CLOCK,
)
from .coordinator import TermowebLocalCoordinator

_LOGGER = logging.getLogger(__name__)

# The service takes one week at either resolution the read side already hands
# back: Coordinator.get_prog (fa62247) returns each node's own native slots,
# 168 values (one an hour) or 336 (one a half hour), Monday first, this
# project's HA surface order -- not Network.program_record's own .slots,
# which stays wire order (day 0 Sunday, docs/PROTOCOL.md 5.6/5.7); get_prog()
# is where that rotation happens (network.rotate_week(), X-17 day-order fix).
# Returning native resolution rather than a folded 168-value projection means
# a half-hourly write now survives the read path end to end and there is no
# reason left to refuse originating one here.
#
# Resolution is read off the payload's own length rather than looked up on
# the target heater's cached record or asked for explicitly: the two valid
# lengths cannot be confused with each other or with a mistake, and
# write_program (network.py) re-derives the same resolution from week_slots
# the same way, so length is already the one source of truth the write path
# itself relies on. Any other length is rejected here, by the schema, before
# a frame is ever built.
#
# A 168-value write onto a node whose own last program came back half-hourly
# with a real boundary inside some hour is rejected too, but not here:
# write_program's own _check_hourly_write_keeps_the_schedule (network.py)
# raises for that, because flattening it would silently discard the boundary
# and hand the heater a schedule it does not hold; that guard is unchanged by
# this widening and is not duplicated here. There is no rejection the other
# way: PROTOCOL.md 5.6's "one schedule store, at two resolutions" finding is
# that a B2 write is always the same 85-byte nibble frame regardless of which
# resolution a node's own B0 read has answered with, so network.py places no
# guard on a 336-value write aimed at a node whose reads have only ever come
# back hourly (C9), and none is invented here either -- PROTOCOL.md 5.6 only
# records that a genuinely half-hourly write to such a node is unproven
# against real hardware, not that the protocol forbids it.
_HOURLY_PROG_LENGTH = DAYS_PER_WEEK * SLOTS_PER_DAY_HOURLY  # 168, Monday hour 0 first
_HALF_HOURLY_PROG_LENGTH = DAYS_PER_WEEK * SLOTS_PER_DAY_HALF_HOURLY  # 336
_VALID_PROG_LENGTHS = (_HOURLY_PROG_LENGTH, _HALF_HOURLY_PROG_LENGTH)


def _validate_prog_length(value: list[int]) -> list[int]:
    if len(value) not in _VALID_PROG_LENGTHS:
        raise vol.Invalid(
            f"expected {_HOURLY_PROG_LENGTH} values (hourly, one a day-hour) "
            f"or {_HALF_HOURLY_PROG_LENGTH} values (half-hourly, one a "
            f"day-half-hour), got {len(value)}"
        )
    return value


SET_SCHEDULE_SCHEMA = {
    vol.Required(ATTR_PROG): vol.All(
        [vol.All(int, vol.In([0, 1, 2]))], _validate_prog_length
    )
}
SET_PRESET_TEMPERATURES_SCHEMA = {
    # docs/45-cloud-integration-api.md section 3: the cloud service takes
    # either the three named fields or a `ptemp` list of exactly three
    # (cold, night, day, the same order HeaterSnapshot.anti_frost_c/eco_c/
    # comfort_c and network.write_presets already use); this keeps both
    # forms so automations built against either shape keep working.
    vol.Optional(ATTR_PTEMP): vol.All([vol.Coerce(float)], vol.Length(min=3, max=3)),
    vol.Optional(ATTR_COLD): vol.Coerce(float),
    vol.Optional(ATTR_NIGHT): vol.Coerce(float),
    vol.Optional(ATTR_DAY): vol.Coerce(float),
}
SET_ACM_PRESET_SCHEMA = {
    vol.Required(ATTR_MINUTES): vol.All(int, vol.Range(min=60, max=600)),
    vol.Required(ATTR_TEMPERATURE_FIELD): vol.Coerce(float),
}
START_BOOST_SCHEMA = {vol.Required(ATTR_MINUTES): vol.Coerce(int)}
CANCEL_BOOST_SCHEMA: dict = {}

_HA_TO_NETWORK_MODE = {
    HVACMode.OFF: "off",
    HVACMode.HEAT: "heat",
    HVACMode.AUTO: "auto",
}
_MODE_CODE_TO_HVAC_MODE = {
    MODE_AUTO: HVACMode.AUTO,
    MODE_MANUAL: HVACMode.HEAT,
    MODE_OVERRIDE: HVACMode.HEAT,  # override is a temporary heat setpoint, not its own hvac mode
    MODE_OFF: HVACMode.OFF,
}

MIN_TEMP_C = 7.0
# 35.0, not the 26.0 these heaters were observed applying (2026-09-06 proof,
# f3-and-edges-results.md section 2): matches the range the Tevolve app
# itself offers (owner direction 2026-09-06). A value above 26.0 is still
# acked by the heater exactly like an accepted one, then silently ignored:
# with optimistic state (list C item 10 step 2) target_temperature shows the
# requested value until the follow-up status request or the heater's own next
# report lands, at which point it reverts to the heater's real, unchanged
# setpoint -- reconciliation in _handle_coordinator_update always defers to
# that real value, never to what was merely asked for.
MAX_TEMP_C = 35.0
TEMP_STEP_C = 0.5


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    coordinator: TermowebLocalCoordinator = entry.runtime_data

    def _build(node_id: int) -> list[TermowebLocalClimate]:
        return [TermowebLocalClimate(coordinator, node_id)]

    async_add_entities(
        entity for node_id in coordinator.heaters for entity in _build(node_id)
    )
    # A heater discovered after this setup (scan, an unknown id's own
    # report, or pairing) gets its own climate entity created immediately
    # through this same callback -- no reload, no restart.
    coordinator.register_platform("climate", async_add_entities, _build)

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(SERVICE_POLL_NOW, {}, "async_poll_now")
    platform.async_register_entity_service(SERVICE_SYNC_CLOCK, {}, "async_sync_clock")
    platform.async_register_entity_service(
        SERVICE_SET_SCHEDULE, SET_SCHEDULE_SCHEMA, "async_set_schedule"
    )
    platform.async_register_entity_service(
        SERVICE_SET_PRESET_TEMPERATURES,
        SET_PRESET_TEMPERATURES_SCHEMA,
        "async_set_preset_temperatures",
    )
    platform.async_register_entity_service(
        SERVICE_SET_ACM_PRESET, SET_ACM_PRESET_SCHEMA, "async_set_acm_preset"
    )
    platform.async_register_entity_service(
        SERVICE_START_BOOST, START_BOOST_SCHEMA, "async_start_boost"
    )
    platform.async_register_entity_service(
        SERVICE_CANCEL_BOOST, CANCEL_BOOST_SCHEMA, "async_cancel_boost"
    )


class TermowebLocalClimate(CoordinatorEntity[TermowebLocalCoordinator], ClimateEntity):
    """One Sun Ray heater."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO]
    _attr_preset_modes = [PRESET_NONE, PRESET_TEMPORARY_OVERRIDE]
    _attr_min_temp = MIN_TEMP_C
    _attr_max_temp = MAX_TEMP_C
    _attr_target_temperature_step = TEMP_STEP_C
    # docs/45-cloud-integration-api.md section 1: the cloud's own
    # supported_features value is 17 (TARGET_TEMPERATURE | PRESET_MODE, no
    # TURN_ON/TURN_OFF). This local entity adds TURN_ON | TURN_OFF (401 total)
    # because HA since 2024.2 expects any climate entity exposing hvac modes
    # to declare and implement them for dashboard on/off controls to work.
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator: TermowebLocalCoordinator, node_id: int) -> None:
        super().__init__(coordinator)
        self._node_id = node_id
        name = coordinator.heater_names.get(node_id, f"Heater {node_id:02X}")
        self.entity_id = heater_entity_id("climate", name, "climate")
        self._attr_unique_id = heater_unique_id(coordinator.dev_id, node_id, "climate")
        self._attr_device_info = heater_device_info(
            coordinator.dev_id, node_id, name, coordinator.gateway_device_id
        )
        # Optimistic state (docs/80-handover.md list C item 10, step 2): set by
        # a command method right after the coordinator call it awaited
        # returns `True`, read in preference to the real snapshot by the
        # properties below, and cleared by _handle_coordinator_update once a
        # snapshot recorded at or after `_optimistic_set_at` arrives --
        # whether that snapshot confirms the commanded value or corrects it,
        # either way the real data is authoritative again from that point on.
        # This is a safety net for the coordinator's own post-command status
        # request timing out or its E6 reply going missing (list C item 10
        # step 1 already covers the common case, where that request lands
        # before this entity's command method returns and the snapshot is
        # already current); it is not a substitute for it.
        self._optimistic: dict[str, Any] = {}
        self._optimistic_set_at: float | None = None
        # The last setpoint target_temperature should show, kept separately
        # from the raw snapshot because an OFF heater's own E5 report can
        # read the anti-frost preset in the setpoint field instead of the
        # real held value; see _handle_coordinator_update.
        self._held_setpoint_c: float | None = None

    @property
    def _heater(self):
        """The coordinator's own heater record, or None once it is gone from
        coordinator.heaters. See sensor.py's _HeaterSensorBase._heater: the
        same removal-vs-teardown ordering window applies here, and this
        entity is on the same coordinator.async_update_listeners() fan-out,
        so a bare `[self._node_id]` index would KeyError just the same."""
        return self.coordinator.heaters.get(self._node_id)

    @property
    def _snapshot(self):
        heater = self._heater
        return heater.last_snapshot if heater else None

    @property
    def current_temperature(self) -> float | None:
        snap = self._snapshot
        return snap.room_temp_c if snap else None

    @property
    def target_temperature(self) -> float | None:
        if "temperature" in self._optimistic:
            return self._optimistic["temperature"]
        if self._held_setpoint_c is not None:
            return self._held_setpoint_c
        # Nothing recorded through _handle_coordinator_update yet (this
        # entity's very first state write, before the coordinator's first
        # post-add update): fall back to the live snapshot directly, same
        # as before the held-setpoint guard existed.
        snap = self._snapshot
        return snap.setpoint_c if snap else None

    @property
    def _preset_temps_c(self) -> list[float] | None:
        if "ptemp" in self._optimistic:
            return self._optimistic["ptemp"]
        snap = self._snapshot
        return None if snap is None else [snap.anti_frost_c, snap.eco_c, snap.comfort_c]

    @property
    def hvac_mode(self) -> HVACMode:
        if "hvac_mode" in self._optimistic:
            return self._optimistic["hvac_mode"]
        snap = self._snapshot
        if snap is None:
            return HVACMode.OFF
        return _MODE_CODE_TO_HVAC_MODE.get(snap.mode_code, HVACMode.OFF)

    @property
    def preset_mode(self) -> str:
        if "preset_mode" in self._optimistic:
            return self._optimistic["preset_mode"]
        snap = self._snapshot
        if snap is not None and snap.mode_code == MODE_OVERRIDE:
            return PRESET_TEMPORARY_OVERRIDE
        return PRESET_NONE

    @property
    def hvac_action(self) -> HVACAction | None:
        snap = self._snapshot
        if snap is None:
            return None
        if snap.boost:
            # Boost energises the element even with mode off (byte 24 bit 5,
            # 2026-09-13 X-19 verify-04.log/notes.md; the same shape as the
            # 2026-09-12 incident), so this must be checked before the mode-off
            # short-circuit below, not folded into the active/boost check that
            # used to run after it.
            return HVACAction.HEATING
        if snap.mode_code == MODE_OFF:
            return HVACAction.OFF
        if snap.heating_flag_candidate is not None:
            # active (bit 0), not the whole byte's own truthiness: a bit such
            # as locked or runback set alone must not read as heating either
            # (docs/captures/2026-09-13-x19/notes.md; the plan's own Risks
            # section on this byte's pre-fix whole-byte truthiness).
            return HVACAction.HEATING if snap.active else HVACAction.IDLE
        # heating_flag_candidate is always populated by HeaterSnapshot.from_frame today;
        # this branch only guards a future decoder that leaves it unset.
        return HVACAction.IDLE

    @property
    def icon(self) -> str:
        """mdi:radiator / mdi:radiator-disabled / mdi:radiator-off by mode
        (docs/45-cloud-integration-api.md section 1). Boost is checked before
        the mode-off short-circuit for the same reason as hvac_action above:
        the element is energised during boost even with mode off."""
        snap = self._snapshot
        if snap is None:
            return "mdi:radiator-off"
        if snap.boost:
            return "mdi:radiator"
        if snap.mode_code == MODE_OFF:
            return "mdi:radiator-off"
        if snap.active:
            return "mdi:radiator"
        return "mdi:radiator-disabled"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # dev_id/addr/units/max_power/ptemp/prog are always present, mirroring
        # the cloud's attribute set (docs/45-cloud-integration-api.md section 1)
        # even before any report has arrived; max_power and ptemp read None
        # until the first E5 report, since HeaterSnapshot is what carries both
        # (WP0, plans/schedule-card.md).
        snap = self._snapshot
        attrs: dict[str, Any] = {
            "dev_id": self.coordinator.dev_id,
            "addr": self._node_id,
            "units": "C",
            "max_power": None if snap is None else snap.measured_power_w,
            "ptemp": self._preset_temps_c,
            "ptemp_supported": True,
            ATTR_PROG: self.coordinator.get_prog(self._node_id),
        }
        heater = self._heater
        if snap is not None:
            attrs["mode_code"] = snap.mode_code
            attrs["last_report_time"] = (
                dt.datetime.fromtimestamp(
                    heater.last_report_time, tz=dt.timezone.utc
                ).isoformat(timespec="seconds")
                if heater.last_report_time is not None
                else None
            )
            attrs["duty_byte"] = snap.duty_candidate
            if snap.boost:
                # Only while boost is active (2026-09-13 X-19 proof): the
                # tail fields are meaningless once it has ended, and
                # boost_temperature comes from the F3 DA read
                # async_start_boost/async_cancel_boost trigger, not from E5/E6.
                attrs["boost_end_day"] = snap.boost_end_day
                attrs["boost_end_min"] = snap.boost_end_min
                record = heater.last_advanced_record if heater is not None else None
                attrs["boost_temperature"] = None if record is None else record.boost_temp_c
        return attrs

    # Each of the three command methods below now sets optimistic state
    # (docs/80-handover.md list C item 10, step 2) once its coordinator call
    # returns `True`, so this entity's own state is already correct before
    # the service call returns, on top of the coordinator's own post-command
    # status request (step 1, TermowebLocalCoordinator._async_send_confirm_and_refresh)
    # that already updates the real snapshot in the common case. A `False`
    # return clears any optimistic value instead: the heater never accepted
    # the command, so nothing here may keep showing what was asked for,
    # only what the last real snapshot actually held.

    def _set_optimistic(self, **values: Any) -> None:
        self._optimistic.update(values)
        self._optimistic_set_at = time.time()
        self.async_write_ha_state()

    def _clear_optimistic(self) -> None:
        self._optimistic.clear()
        self._optimistic_set_at = None
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Reconcile optimistic state (docs/80-handover.md list C item 10,
        step 2): once a snapshot recorded at or after `_optimistic_set_at`
        exists, defer to it unconditionally, whether it confirms the
        commanded value or corrects it -- either way the real data is
        authoritative again, and holding onto a stale optimistic guess past
        that point would risk masking a correction the handover explicitly
        requires ("must not paper over a genuinely failed command")."""
        snap = self._snapshot
        if snap is None:
            # The heater itself is gone from coordinator.heaters (removed,
            # not just off): nothing left to hold onto, so target_temperature
            # must fall back to None like every other snapshot-derived
            # property here, not keep showing a stale value forever.
            self._held_setpoint_c = None
        else:
            if (
                self._held_setpoint_c is not None
                and snap.mode_code == MODE_OFF
                and snap.setpoint_c == snap.anti_frost_c
            ):
                # Entering a panel menu after a Runback on/off cycle makes an
                # OFF heater's own report carry the anti-frost preset in the
                # setpoint field instead of the real value: MANUAL mode
                # still holds the real setpoint, and a mode-heat command
                # uses it, so this is a reporting artefact, not an actual
                # change. Keeping the previously held value here (never
                # touching the snapshot itself) stops a later turn-on from
                # writing anti-frost back to the heater as if it had been
                # requested.
                _LOGGER.debug(
                    "node %02x setpoint held at %.1fC while off; report reads "
                    "anti-frost %.1fC",
                    self._node_id, self._held_setpoint_c, snap.setpoint_c,
                )
            else:
                self._held_setpoint_c = snap.setpoint_c
        if self._optimistic_set_at is not None:
            if snap is not None and snap.received_at >= self._optimistic_set_at:
                self._optimistic.clear()
                self._optimistic_set_at = None
        super()._handle_coordinator_update()

    def _setpoint_mode(self, requested_hvac_mode: HVACMode | None) -> str | None:
        """Which network.py mode string ("off"/"heat"/"auto") the setpoint
        frame's own mode byte should carry, so the write leaves the heater
        in the mode it should actually end up in rather than
        network.set_setpoint's own hardcoded heat (PROTOCOL.md 5.1). An
        explicit hvac_mode always wins; otherwise this only acts on a mode
        already confirmed by a real snapshot -- before any report has
        arrived there is nothing to preserve, so None here falls back to
        coordinator.async_set_setpoint's own unmodified (heat) frame rather
        than guessing off from hvac_mode's own display-only default."""
        if requested_hvac_mode is not None:
            return _HA_TO_NETWORK_MODE.get(requested_hvac_mode)
        snap = self._snapshot
        if snap is None:
            return None
        return {"off": "off", "auto": "auto", "manual": "heat"}.get(snap.mode)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        requested_hvac_mode = kwargs.get(ATTR_HVAC_MODE)
        if self.preset_mode == PRESET_TEMPORARY_OVERRIDE and requested_hvac_mode is None:
            ok = await self.coordinator.async_set_override(self._node_id, temperature)
        else:
            ok = await self.coordinator.async_set_setpoint(
                self._node_id, temperature, mode=self._setpoint_mode(requested_hvac_mode)
            )
        if ok:
            optimistic: dict[str, Any] = {"temperature": temperature}
            if requested_hvac_mode is not None:
                optimistic["hvac_mode"] = requested_hvac_mode
            self._set_optimistic(**optimistic)
        else:
            _LOGGER.warning(
                "node %02x rejected set_temperature %.1fC; entity keeps its last "
                "confirmed value",
                self._node_id, temperature,
            )
            self._clear_optimistic()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        mode = _HA_TO_NETWORK_MODE.get(hvac_mode)
        if mode is None:
            raise ValueError(f"unsupported hvac_mode {hvac_mode!r}")
        ok = await self.coordinator.async_set_mode(self._node_id, mode)
        if ok:
            # A plain mode command always leaves override (network.py's
            # set_mode never sends MODE_OVERRIDE), so preset optimistically
            # follows hvac_mode back to none here too.
            self._set_optimistic(hvac_mode=hvac_mode, preset_mode=PRESET_NONE)
        else:
            _LOGGER.warning(
                "node %02x rejected set_hvac_mode %s; entity keeps its last "
                "confirmed value",
                self._node_id, hvac_mode,
            )
            self._clear_optimistic()

    async def async_turn_off(self) -> None:
        """Turn the heater off (hvac_mode off)."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_turn_on(self) -> None:
        """Turn the heater on into plain manual heat.

        Auto mode is not reachable from turn_on; select it via
        async_set_hvac_mode(HVACMode.AUTO) instead.
        """
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode == PRESET_TEMPORARY_OVERRIDE:
            target = self.target_temperature or MIN_TEMP_C
            ok = await self.coordinator.async_set_override(self._node_id, target)
            if ok:
                self._set_optimistic(
                    preset_mode=PRESET_TEMPORARY_OVERRIDE,
                    hvac_mode=HVACMode.HEAT,
                    temperature=target,
                )
        elif preset_mode == PRESET_NONE:
            ok = await self.coordinator.async_set_mode(self._node_id, "heat")
            if ok:
                self._set_optimistic(preset_mode=PRESET_NONE, hvac_mode=HVACMode.HEAT)
        else:
            raise ValueError(f"unsupported preset_mode {preset_mode!r}")
        if not ok:
            _LOGGER.warning(
                "node %02x rejected set_preset_mode %s; entity keeps its last "
                "confirmed value",
                self._node_id, preset_mode,
            )
            self._clear_optimistic()

    async def async_poll_now(self) -> None:
        await self.coordinator.async_poll_now(self._node_id)

    async def async_sync_clock(self) -> None:
        await self.coordinator.async_sync_clock_one(self._node_id)

    # ---- docs/45-cloud-integration-api.md section 3 services ----

    async def async_set_schedule(self, prog: list[int]) -> None:
        """set_schedule: backed via write_program/read_program
        (docs/91-p4-parity-plan.md P4a parity matrix). Marks the schedule
        sensor's own `source` attribute "written" right after the call
        returns: Coordinator.async_set_schedule's own read-back already
        tagged network.py's program_source() "read" for this node, since a
        B0 read-back is not itself a different decode outcome from any
        other F3 B0 read -- this is the one caller (the only caller of
        Coordinator.async_set_schedule) that knows a write, not a plain
        read, is what just happened."""
        # The schema already refused any length but 168 or 336 (its own
        # _VALID_PROG_LENGTHS), so the slot count a day is read straight off
        # len(prog) rather than carried separately -- there is nothing else
        # for a valid length to mean, and nothing here can drift out of step
        # with what the schema just accepted.
        slots_per_day = (
            SLOTS_PER_DAY_HOURLY if len(prog) == _HOURLY_PROG_LENGTH
            else SLOTS_PER_DAY_HALF_HOURLY
        )
        week_slots = [
            prog[day * slots_per_day : (day + 1) * slots_per_day]
            for day in range(DAYS_PER_WEEK)
        ]
        await self.coordinator.async_set_schedule(self._node_id, week_slots)
        self.coordinator.network.mark_program_written(self._node_id)
        # async_set_schedule's own listener push (above) already happened
        # with "read" as the not-yet-corrected source; push again now so the
        # schedule sensor's `source` attribute reflects "written" without
        # waiting for its own once-a-minute tick.
        self.coordinator.async_update_listeners()

    async def async_set_preset_temperatures(self, **kwargs: Any) -> None:
        """set_preset_temperatures: B6 preset write (2026-09-12 X-18 proof,
        docs/captures/2026-09-12-x18-s7/notes.md), backed now that a radio
        frame has been captured for it. Accepts either the `ptemp` list
        (cold, night, day) or the three named fields; a field left out of a
        named-field call keeps that preset's own current value (the optimistic
        one if a previous preset write is still unreconciled, else the last
        snapshot's), the same fallback async_set_temperature/async_set_preset_mode
        use for the values a call does not touch."""
        ptemp = kwargs.get(ATTR_PTEMP)
        current = self._preset_temps_c
        if ptemp is not None:
            cold, night, day = (float(value) for value in ptemp)
        else:
            cold = kwargs.get(ATTR_COLD, current[0] if current else None)
            night = kwargs.get(ATTR_NIGHT, current[1] if current else None)
            day = kwargs.get(ATTR_DAY, current[2] if current else None)
        if cold is None or night is None or day is None:
            raise ValueError(
                f"node {self._node_id:02x} has no current preset values to fall "
                "back on (no status report yet); pass ptemp or all three fields"
            )
        ok = await self.coordinator.async_set_preset_temperatures(self._node_id, cold, night, day)
        if ok:
            self._set_optimistic(ptemp=[cold, night, day])
        else:
            _LOGGER.warning(
                "node %02x rejected set_preset_temperatures %.1f/%.1f/%.1fC; "
                "entity keeps its last confirmed value",
                self._node_id, cold, night, day,
            )
            self._clear_optimistic()

    async def async_set_acm_preset(self, **kwargs: Any) -> None:
        raise_not_applicable(_LOGGER, f"set_acm_preset on node {self._node_id:02x}")

    async def async_start_boost(self, **kwargs: Any) -> None:
        """D2 01 (2026-09-13 X-19 proof): backed now that a radio frame has
        been captured for it. `minutes` (the schema's own required field,
        kept for cloud-service-shape parity) is not sent anywhere: the
        60-minute duration is the heater's own boost-time setting, not a
        payload value (docs/captures/2026-09-13-x19/notes.md)."""
        ok = await self.coordinator.async_start_boost(self._node_id)
        if not ok:
            _LOGGER.warning(
                "node %02x rejected start_boost; entity keeps its last "
                "confirmed value",
                self._node_id,
            )

    async def async_cancel_boost(self, **kwargs: Any) -> None:
        """D2 00 (2026-09-13 X-19 proof): backed now that a radio frame has
        been captured for it."""
        ok = await self.coordinator.async_cancel_boost(self._node_id)
        if not ok:
            _LOGGER.warning(
                "node %02x rejected cancel_boost; entity keeps its last "
                "confirmed value",
                self._node_id,
            )

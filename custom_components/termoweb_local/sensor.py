"""Per-heater temperature/power/energy sensors, and the gateway total-energy
sensor (docs/91-p4-parity-plan.md P4a parity matrix).

Room temperature is E5 logical bytes 18-19 (Heater.HeaterSnapshot.room_temp_c),
proven against Home Assistant per docs/PROTOCOL.md section 5.4 -- usable without
a caveat in the entity itself, but handover item 1's reference-thermometer
proof is still open (docs/90-phase3-plan.md prerequisites table).

Power is E5 logical byte 23 (Heater.HeaterSnapshot.duty_candidate) scaled by
the heater's own measured full-load power, E5 logical bytes 21-22
(Heater.HeaterSnapshot.measured_power_w, PROTOCOL.md 5.4):
`duty_candidate / 100 * measured_power_w`. Every heater reports that rating in
every report and holds its last value while idle, so this sensor needs no owner
configuration and is available as soon as any report has arrived. It stays
marked provisional because which of bytes 23 and 24 carries the duty percent is
still open (PROTOCOL.md section 9 list A); the rating half is measured.

Energy is the heater's own cumulative watt-hour meter, read with F3 BC and
carried by the EF reply (PROTOCOL.md 5.6, Heater.last_energy_wh), divided by
1000 for kWh. Nothing about it is derived from power or elapsed time here: the
counter is authored and kept by the heater, so TOTAL_INCREASING is exact and
the value survives a station restart, a re-pairing, and a factory reset of the
station side. Both energy sensors read "unknown" only until the first F3 BC
poll of a heater has actually answered.

The gateway total is the sum of the per-heater counters, over whichever heaters
have reported one; it reads "unknown" while none has.

The schedule sensor reports the active slot's own target temperature (docs/
80-handover.md list C item 2), not the slot's bare preset name: the program cache
(F3 B0/9E, Coordinator.get_prog) gives a slot code already normalised to 0 cold,
1 night, 2 day or None regardless of which of the two on-air encodings a heater
answers with (network.py's own nibble and 2-bit decoders both reduce to this before
the sensor ever sees it), at that heater's own native resolution -- 24 hourly slots
a day for a C9 reply, 48 half-hourly ones for a 9E/9F reply (docs/PROTOCOL.md 5.6;
docs/80-handover.md "Owed" list, item 7) -- and that code selects one of the heater's own anti-frost/
eco/comfort presets (E5 logical bytes 14-16, HeaterSnapshot.preset_target_c) in the
same order the cloud's `ptemp` array uses (docs/20-cloud-api-summary.md). The state
reads "unknown" whenever that lookup cannot produce a real number: no E5 has been
received yet (the presets themselves are unread), the schedule has not been read,
or the current hour's slot code is not one of the three recognised values -- never
a guessed number. The slot's own name (cold/night/day) survives as the `preset`
attribute, the same "state becomes an attribute" pattern the power sensor already
uses for `full_load_power_w` -- and, because an attribute cannot be charted, also
as its own ENUM-device-class entity (TermowebLocalSchedulePresetSensor) so the
weekly schedule shape is visible as a history-panel timeline, not only as the
numeric sensor's step chart.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import _vendor_compat  # noqa: F401 -- resolves termoweb_local before the import below
from termoweb_local.network import (
    DAYS_PER_WEEK,
    HOURS_PER_DAY,
    PROGRAM_RESOLUTIONS,
    SLOTS_PER_DAY_HOURLY,
)

from .const import ATTR_COLD, ATTR_DAY, ATTR_NIGHT, ATTR_PROG
from .devices import gateway_device_info, heater_device_info
from .entity_ids import (
    gateway_entity_id,
    gateway_unique_id,
    heater_entity_id,
    heater_schedule_entity_id,
    heater_unique_id,
)
from .coordinator import TermowebLocalCoordinator

POWER_PROVISIONAL_REASON = "duty percent byte not proven (E5 byte 23 vs 24)"

WH_PER_KWH = 1000.0

MINUTES_PER_HOUR = 60


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    coordinator: TermowebLocalCoordinator = entry.runtime_data

    def _build(node_id: int) -> list[CoordinatorEntity]:
        return [
            TermowebLocalTemperatureSensor(coordinator, node_id),
            TermowebLocalPowerSensor(coordinator, node_id),
            TermowebLocalEnergySensor(coordinator, node_id),
            TermowebLocalScheduleSensor(coordinator, node_id),
            TermowebLocalSchedulePresetSensor(coordinator, node_id),
        ]

    entities: list[CoordinatorEntity] = [
        entity for node_id in coordinator.heaters for entity in _build(node_id)
    ]
    entities.append(TermowebLocalGatewayTotalEnergySensor(coordinator))
    async_add_entities(entities)
    # A heater discovered after this setup (scan, an unknown id's own
    # report, or pairing) gets its own sensors created immediately through
    # this same callback -- no reload, no restart.
    coordinator.register_platform("sensor", async_add_entities, _build)


class _HeaterSensorBase(CoordinatorEntity[TermowebLocalCoordinator], SensorEntity):
    _attr_has_entity_name = True

    _kind: str  # entity_ids.py kind key, set by each subclass

    def __init__(self, coordinator: TermowebLocalCoordinator, node_id: int) -> None:
        super().__init__(coordinator)
        self._node_id = node_id
        name = coordinator.heater_names.get(node_id, f"Heater {node_id:02X}")
        self.entity_id = heater_entity_id("sensor", name, self._kind)
        self._attr_unique_id = heater_unique_id(coordinator.dev_id, node_id, self._kind)
        self._attr_device_info = heater_device_info(
            coordinator.dev_id, node_id, name, coordinator.gateway_device_id
        )

    @property
    def _heater(self):
        """The coordinator's own heater record, or None once it is gone from
        coordinator.heaters. async_remove_config_entry_device (__init__.py)
        deletes it synchronously and only afterwards does Home Assistant tear
        this entity down (async_will_remove_from_hass, which is what cancels
        the schedule sensors' own per-minute tick); a plain `[self._node_id]`
        index here would KeyError on anything -- a CoordinatorEntity update or
        that tick -- landing in that window."""
        return self.coordinator.heaters.get(self._node_id)

    @property
    def _snapshot(self):
        heater = self._heater
        return heater.last_snapshot if heater else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "dev_id": self.coordinator.dev_id,
            "addr": self._node_id,
            "units": "C",
        }


class TermowebLocalTemperatureSensor(_HeaterSensorBase):
    """Room temperature (E5 bytes 18-19)."""

    _kind = "temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_translation_key = "room_temperature"

    @property
    def native_value(self) -> float | None:
        snap = self._snapshot
        return snap.room_temp_c if snap else None


class TermowebLocalPowerSensor(_HeaterSensorBase):
    """Power in W: the duty byte (E5 byte 23) scaled by the heater's own
    measured full-load power (E5 bytes 21-22)."""

    _kind = "power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_translation_key = "power_provisional"

    @property
    def native_value(self) -> float | None:
        snap = self._snapshot
        if snap is None or snap.measured_power_w is None:
            return None
        return round(snap.duty_candidate / 100.0 * snap.measured_power_w, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = dict(super().extra_state_attributes)
        snap = self._snapshot
        attrs["full_load_power_w"] = None if snap is None else snap.measured_power_w
        attrs["provisional"] = True
        attrs["provisional_reason"] = POWER_PROVISIONAL_REASON
        return attrs


class TermowebLocalEnergySensor(_HeaterSensorBase):
    """Cumulative energy in kWh: the heater's own watt-hour meter, read with F3 BC
    and carried by the EF reply (PROTOCOL.md 5.6)."""

    _kind = "energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_translation_key = "energy"

    @property
    def native_value(self) -> float | None:
        heater = self._heater
        energy_wh = heater.last_energy_wh if heater else None
        return None if energy_wh is None else energy_wh / WH_PER_KWH


_PROGRAM_VALUE_TEXT = {0: ATTR_COLD, 1: ATTR_NIGHT, 2: ATTR_DAY}
_DAY_ATTR_NAMES = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)


def _cache_resolution(prog: list[int | None] | None) -> int | None:
    """`prog`'s own resolution in slots a day, read off the very list this
    module is about to index rather than off the node's record.

    `Coordinator.get_prog` now hands back `ProgramRecord.slots`, at that
    node's own native resolution (24 for a C9 reply, 48 for a 9E/9F one,
    docs/PROTOCOL.md 5.6), so this agrees with `Network.program_resolution
    (node_id)` today; it is still derived from the list itself rather than
    read off the node's record, so a mismatch between the two -- were one
    ever introduced again -- fails as an honest "unknown" here instead of an
    out-of-range index.

    None for a list whose length is not 7 days of one of the gateway's own two
    resolutions (network.py's PROGRAM_RESOLUTIONS): there is no honest slot
    number for such a list, so callers report "unknown" rather than a number
    derived from a length nobody has explained."""
    if not prog:
        return None
    resolution, remainder = divmod(len(prog), DAYS_PER_WEEK)
    if remainder or resolution not in PROGRAM_RESOLUTIONS:
        return None
    return resolution


def _current_slot_index(now: dt.datetime, resolution: int) -> int:
    """The index, into a week held at `resolution` slots a day, of the slot
    `now` falls in. Monday slot 0 first, matching Python's own `weekday()`
    (Monday 0 .. Sunday 6) directly, because the array this indexes is
    `Coordinator.get_prog()`'s own return value, which is this project's HA
    surface order (Monday first, matching the cloud's own `prog` array), not
    the wire's own day order (day 0 Sunday, docs/PROTOCOL.md 5.6/5.7): the one
    rotation between the two happens once, at get_prog() itself
    (network.rotate_week(), X-17 day-order fix), so nothing downstream of it,
    including this function, needs to know the wire's day order at all.
    Unlike the EB clock-sync payload's DOW byte (Sunday 0 .. Saturday 6,
    network.py's own comment), this is not the radio protocol's own day
    numbering, just the program cache's.

    `resolution` is 24 or 48, the gateway's own prog_resolution
    (network.py's PROGRAM_RESOLUTIONS). At 24 this reduces to
    `weekday() * 24 + hour`, the whole of what this function used to compute,
    because `minute * 1 // 60` is 0 for every minute of an hour; at 48 the same
    expression adds the half hour, `minute * 2 // 60` being 1 from minute 30 on.
    Both of those are pinned by tests rather than asserted here."""
    slots_per_hour = resolution // HOURS_PER_DAY
    return (
        now.weekday() * resolution
        + now.hour * slots_per_hour
        + now.minute * slots_per_hour // MINUTES_PER_HOUR
    )


def _next_change_at(
    prog: list[int | None], now: dt.datetime, resolution: int
) -> dt.datetime | None:
    """The local start time of the next slot, up to one full week ahead, this
    schedule is known to change value at, or None when that is not knowable.

    The time returned is the start of the slot the change happens at, at this
    week's own resolution: the start of the hour for a 24-slot-a-day week, which
    is what this function has always returned, and the start of the half hour
    for a 48-slot-a-day one. Both the slot the walk starts from and the step it
    walks by come from `resolution`, so a half-hourly week can actually report a
    change on the half hour.

    An undecodable slot is unknown, and is treated as neither the same value as
    the current slot nor a different one. network.py leaves a None wherever it
    has no value: an hour whose two half hours disagree in a 9F/9E record, or
    the never observed code 3. So the walk stops at the first None it meets and
    reports "not knowable", rather than stepping over it as "no change" when the
    change may be exactly there; and a current slot that is itself None returns
    None straight away, there being no value for a later slot to differ from.

    None therefore carries two different facts, and this deliberately does not
    invent a way to tell them apart: either the whole week genuinely holds one
    value (nothing ever changes), or the walk met a slot this station could not
    decode. The `prog` attribute published beside this one carries those
    unknowns as its own None entries, so a consumer needing the distinction
    reads it there instead of having it guessed at here."""
    slot_minutes = MINUTES_PER_HOUR * HOURS_PER_DAY // resolution
    current_index = _current_slot_index(now, resolution) % len(prog)
    current_value = prog[current_index]
    if current_value is None:
        return None
    slot_start = now.replace(
        minute=now.minute // slot_minutes * slot_minutes, second=0, microsecond=0
    )
    for offset in range(1, len(prog) + 1):
        value = prog[(current_index + offset) % len(prog)]
        if value is None:
            return None
        if value != current_value:
            return slot_start + dt.timedelta(minutes=offset * slot_minutes)
    return None


class _ScheduleSlotSensorBase(_HeaterSensorBase):
    """Shared plumbing for any per-heater sensor whose state is derived from
    the active schedule hour (F3 B0/9E program cache): the once-a-minute tick
    lifecycle and the current slot's normalised code lookup. Factored out of
    TermowebLocalScheduleSensor so TermowebLocalSchedulePresetSensor (the
    categorical companion added alongside it) reuses both rather than
    re-deriving them.
    """

    def __init__(self, coordinator: TermowebLocalCoordinator, node_id: int) -> None:
        super().__init__(coordinator, node_id)
        self._unsub_minute_tick: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # CoordinatorEntity's own update (above) already catches every
        # program cache change that also calls async_set_updated_data/
        # async_update_listeners (an E5 report; set_schedule's own push,
        # climate.py's async_set_schedule); the 9E program-report push does
        # neither (no program-changed signal for it), so this once-a-minute
        # tick is what actually surfaces that case within a bounded delay --
        # and it doubles as the current-hour state's own clock, since
        # cold/night/day changes on the hour regardless of whether the
        # cache itself just changed.
        self._unsub_minute_tick = async_track_time_change(
            self.hass, self._handle_minute_tick, second=0
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_minute_tick is not None:
            self._unsub_minute_tick()
            self._unsub_minute_tick = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_minute_tick(self, now: dt.datetime) -> None:
        self.async_write_ha_state()

    @property
    def _prog(self) -> list[int | None] | None:
        return self.coordinator.get_prog(self._node_id)

    @property
    def _current_slot_code(self) -> int | None:
        """The current slot's normalised program value (0 cold, 1 night, 2 day),
        or None when the schedule has not been read, the cached week is not 7 days
        of a resolution this station recognises (_cache_resolution), or the slot's
        own code was not one of those three (network.py's decoders already collapse
        both the nibble and the 2-bit on-air encodings to that same None)."""
        prog = self._prog
        resolution = _cache_resolution(prog)
        if resolution is None:
            return None
        return prog[_current_slot_index(dt_util.now(), resolution) % len(prog)]


class TermowebLocalScheduleSensor(_ScheduleSlotSensorBase):
    """The active schedule slot's own target temperature, plus the full week
    (F3 B0 program cache, the same one climate.py's own `prog` attribute reads
    from `Coordinator.get_prog`) as attributes.

    The state is a temperature (docs/80-handover.md list C item 2), looked up from
    this heater's own anti-frost/eco/comfort presets via HeaterSnapshot.preset_target_c
    rather than the slot's bare code, because a schedule slot selects a preset, not
    a stored temperature (module docstring). The slot's own name is the `preset`
    attribute, and also stands on its own as TermowebLocalSchedulePresetSensor
    below, so it can be charted (a text attribute cannot be).

    Friendly name and entity id both skip the `_heater` infix every other
    per-heater sensor kind gets (owner decision 2026-09-06,
    entity_ids.heater_schedule_object_id's own docstring), so this overrides
    `_HeaterSensorBase.__init__` instead of reusing it verbatim.
    """

    _kind = "schedule"
    _attr_has_entity_name = False
    _attr_icon = "mdi:calendar-clock"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: TermowebLocalCoordinator, node_id: int) -> None:
        super().__init__(coordinator, node_id)
        name = coordinator.heater_names.get(node_id, f"Heater {node_id:02X}")
        self.entity_id = heater_schedule_entity_id("sensor", name)
        self._attr_name = f"{name} schedule"

    @property
    def native_value(self) -> float | None:
        """The current slot's own preset target temperature, or None (state
        "unknown") when it cannot be produced confidently: this heater has not
        reported an E5 yet (HeaterSnapshot.preset_target_c needs a snapshot to read
        the presets off), or the current slot code is not one of the three
        recognised values (schedule unread, or an hour network.py could not decode).
        Never a guessed number."""
        snap = self._snapshot
        if snap is None:
            return None
        return snap.preset_target_c(self._current_slot_code)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = dict(super().extra_state_attributes)
        prog = self._prog
        # Read off the cached week itself (see _cache_resolution) rather than
        # taken as a second argument, so this can never disagree with what
        # `prog` below actually holds.
        resolution = _cache_resolution(prog)
        now = dt_util.now()
        attrs[ATTR_PROG] = prog
        for day_index, day_name in enumerate(_DAY_ATTR_NAMES):
            attrs[day_name] = (
                None if resolution is None
                else prog[day_index * resolution : (day_index + 1) * resolution]
            )
        # A plain clock reading, published whether or not a program has ever
        # been read (its own test pins that). With no cached week there is no
        # resolution to read it at, so it falls back to the hourly one it has
        # always used; with one, it is an index into that week and nothing else.
        attrs["current_slot_index"] = _current_slot_index(
            now, SLOTS_PER_DAY_HOURLY if resolution is None else resolution
        )
        next_change_at = (
            None if resolution is None else _next_change_at(prog, now, resolution)
        )
        attrs["next_change"] = (
            None if next_change_at is None else next_change_at.isoformat(timespec="seconds")
        )
        attrs["source"] = self.coordinator.network.program_source(self._node_id)
        # The slot's own preset name, the sensor's former state (docs/80-handover.md
        # list C item 2): a real fact the numeric state above no longer carries,
        # kept as an attribute the same way the power sensor keeps
        # `full_load_power_w` alongside its own scaled state.
        attrs["preset"] = _PROGRAM_VALUE_TEXT.get(self._current_slot_code)
        return attrs


class TermowebLocalSchedulePresetSensor(_ScheduleSlotSensorBase):
    """The active schedule slot's own preset name (cold/night/day), as a
    first-class state rather than only the `preset` attribute on
    TermowebLocalScheduleSensor above. An attribute cannot be charted, and
    Home Assistant has no long-term statistics for a text state either way,
    so `device_class=ENUM` with an explicit three-value `options` list is
    the correct modelling: it renders as a timeline (history panel /
    history-graph card), the right visualisation for a weekly schedule.
    Deliberately not a state_class or a unit -- statistics do not exist for
    ENUM sensors and Home Assistant logs an error if either is set. A
    parallel numeric 0/1/2 sensor was deliberately not added either: each
    preset already maps to a distinct temperature, so the existing schedule
    sensor's own step chart already carries the same shape for statistics
    purposes; a second numeric copy would be redundant.

    Reuses `_current_slot_code` and `_PROGRAM_VALUE_TEXT` (shared via
    `_ScheduleSlotSensorBase`, which also supplies the once-a-minute tick)
    rather than re-deriving the slot-index/decode logic, so the two entities
    can never disagree about which hour or which code is current.

    Unlike TermowebLocalScheduleSensor, this follows the *general*
    per-heater naming convention (`_HeaterSensorBase.__init__` used as-is,
    no override): the schedule sensor's own `_heater`-infix skip is a named,
    scoped owner decision for that one entity, to avoid doubling the word
    "heater" in its object id (entity_ids.heater_schedule_object_id's own
    docstring), not a general rule for anything schedule-related. The
    general convention (`heater_object_id`) already avoids that same
    doubling on its own (it omits `_heater` whenever the slug already
    contains "heater" as a word), so there is nothing here for a scoped
    exception to fix, and every other sensor kind (temperature/power/energy)
    already uses this same convention. So this entity is
    `sensor.<slug>_heater_schedule_preset` in general, e.g. `sensor.living_
    room_heater_schedule_preset` for "Living room", except when the slug
    already contains "heater" as a word (the general convention's own
    infix-skip), e.g. `sensor.heater_02_schedule_preset` for the discovery
    default "Heater 02" -- friendly name "<device
    name> Schedule preset" (has_entity_name, inherited True from
    `_HeaterSensorBase`, plus a plain `_attr_name` rather than a
    translation_key: strings.json/translations are outside this change's
    file ownership, and a bare `_attr_name` still gets the device name
    prefixed the same way).
    """

    _kind = "schedule_preset"
    _attr_icon = "mdi:calendar-clock-outline"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [ATTR_COLD, ATTR_NIGHT, ATTR_DAY]
    _attr_name = "Schedule preset"

    @property
    def native_value(self) -> str | None:
        """The current slot's own preset name, or None (state "unknown")
        when the schedule has not been read or the slot code is not one of
        the three recognised values -- never a guessed value. "unknown" is
        a valid ENUM state regardless of `options` (Home Assistant's own
        unavailable/unknown states are exempt from the options-membership
        check), so this needs no extra handling beyond what
        `_current_slot_code` already gives."""
        return _PROGRAM_VALUE_TEXT.get(self._current_slot_code)


class TermowebLocalGatewayTotalEnergySensor(
    CoordinatorEntity[TermowebLocalCoordinator], SensorEntity
):
    """Sum of every heater's own energy meter, in kWh."""

    _attr_has_entity_name = True
    _attr_name = "Total energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator: TermowebLocalCoordinator) -> None:
        super().__init__(coordinator)
        self.entity_id = gateway_entity_id("sensor", "total_energy")
        self._attr_unique_id = gateway_unique_id(coordinator.dev_id, "total_energy")
        self._attr_device_info = gateway_device_info(coordinator.dev_id)

    @property
    def native_value(self) -> float | None:
        """None (state "unknown") only while no heater has answered an F3 BC yet.
        A heater that has not answered is left out of the sum rather than counted
        as zero, so the total never dips when one heater goes quiet."""
        counters = [
            heater.last_energy_wh
            for heater in self.coordinator.heaters.values()
            if heater.last_energy_wh is not None
        ]
        return sum(counters) / WH_PER_KWH if counters else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "dev_id": self.coordinator.dev_id,
            "heaters_reporting": sum(
                1 for heater in self.coordinator.heaters.values()
                if heater.last_energy_wh is not None
            ),
        }

"""Schedule sensor: state/attributes from the coordinator's F3 B0 program
cache, per heater (docs/PROTOCOL.md 5.7 program format; sensor.py's own
TermowebLocalScheduleSensor)."""
import datetime as dt
import random

from homeassistant.util import dt as dt_util

from termoweb_local.network import (
    DAYS_PER_WEEK,
    PROGRAM_READ_OPCODE,
    PROGRAM_SLOT_NIBBLE,
    SLOTS_PER_DAY_HALF_HOURLY,
    SLOTS_PER_DAY_HOURLY,
    rotate_week,
)

from custom_components.termoweb_local.const import DOMAIN
from custom_components.termoweb_local.sensor import (
    _cache_resolution,
    _current_slot_index,
    _next_change_at,
)

from .conftest import FakeCulTransport, setup_entry

MASTER_BEDROOM = 0x04  # TEST_HEATERS's second entry, "Master bedroom"
ENTITY_ID = "sensor.master_bedroom_schedule"
# TermowebLocalSchedulePresetSensor: general per-heater naming (the "_heater"
# infix), unlike ENTITY_ID above -- see the class's own docstring for why.
PRESET_ENTITY_ID = "sensor.master_bedroom_heater_schedule_preset"
_PROGRAM_READ_REQUEST_PAYLOAD = bytes([PROGRAM_READ_OPCODE])
_PROG_LENGTH = 168
# MASTER_BEDROOM answers every F3 B0 in this fake transport with a 9F (nibble)
# reply, so Coordinator.get_prog for it is always native 48-slot-a-day, 336
# values a week (docs/80-handover.md "Owed" list, item 7: get_prog no longer
# hands back the 168-value hourly projection ProgramRecord.hourly used to be
# the only shape this station kept).
_NATIVE_PROG_LENGTH = DAYS_PER_WEEK * SLOTS_PER_DAY_HALF_HOURLY


def _to_native(hourly_values: list[int]) -> list[int]:
    """Each hourly value repeated across its own two native half-hour slots --
    the shape a 9F/9E nibble reply always decodes to when both of an hour's
    halves agree, which is every hourly test program in this file (no heater
    on this bench schedules on the half hour, PROTOCOL.md 5.6/5.7) -- so this
    is what Coordinator.get_prog returns for any of them today."""
    return [value for value in hourly_values for _ in range(2)]


def _to_wire_hourly(values_monday_first: list[int]) -> list[int]:
    """168 hourly values, Monday first (this file's own fixtures, matching
    Coordinator.get_prog's HA-surface order), rotated to the wire's own day
    order (day 0 Sunday, PROTOCOL.md 5.6/5.7) for building a fake F3 B0 reply
    -- the inverse of the rotation get_prog applies on the way out
    (network.rotate_week(), X-17 day-order fix)."""
    return rotate_week(values_monday_first, SLOTS_PER_DAY_HOURLY, to_wire=True)


def _encode_program_nibbles(values: list[int]) -> bytes:
    """Inverse of network.py's own _decode_program_nibbles: two slot values
    per byte, high nibble first hour."""
    assert len(values) == _PROG_LENGTH
    out = bytearray()
    for i in range(0, _PROG_LENGTH, 2):
        out.append((PROGRAM_SLOT_NIBBLE[values[i]] << 4) | PROGRAM_SLOT_NIBBLE[values[i + 1]])
    return bytes(out)


def _encode_program_bits(values: list[int]) -> bytes:
    """Inverse of network.py's own _decode_program_bits: four 2-bit slot
    codes per byte, MSB first (C9's own encoding, PROTOCOL.md 5.6) -- the
    raw slot value itself (0/1/2) is the 2-bit code, unlike the nibble
    encoding above."""
    assert len(values) == _PROG_LENGTH
    out = bytearray()
    for i in range(0, _PROG_LENGTH, 4):
        byte = 0
        for j, shift in enumerate((6, 4, 2, 0)):
            byte |= values[i + j] << shift
        out.append(byte)
    return bytes(out)


def _live_entity(hass, entity_id: str):
    """The live entity object behind `entity_id`, the same way the schedule
    sensors' own per-minute tick reaches them -- unlike hass.states.get,
    which only ever returns whatever was last written and cannot be used to
    force a fresh read."""
    for platform in hass.data.get("entity_platform", {}).get(DOMAIN, []):
        for entity in platform.entities.values():
            if entity.entity_id == entity_id:
                return entity
    return None


class _NoProgramTransport(FakeCulTransport):
    """Suppresses the F3 B0 program-read reply for one node id only, so its
    program cache stays empty after setup (the "unknown before any read"
    case); every other node and request behaves exactly like the base
    FakeCulTransport."""

    def __init__(self, *, silent_program_node: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self._silent_program_node = silent_program_node

    def _application_reply_payload(self, parsed):
        if (
            parsed.payload == _PROGRAM_READ_REQUEST_PAYLOAD
            and parsed.dst == self._silent_program_node
        ):
            return None
        return super()._application_reply_payload(parsed)


async def test_schedule_sensor_unknown_before_any_read(hass, monkeypatch, enable_custom_integrations):
    transport = _NoProgramTransport(silent_program_node=MASTER_BEDROOM)
    await setup_entry(hass, monkeypatch, transport)

    state = hass.states.get(ENTITY_ID)
    assert state is not None
    assert state.state == "unknown"
    assert state.attributes["prog"] is None
    assert state.attributes["monday"] is None
    assert state.attributes["sunday"] is None
    assert state.attributes["next_change"] is None
    assert state.attributes["source"] is None
    assert state.attributes["preset"] is None
    # current_slot_index is a plain clock reading, independent of whether a
    # program has ever been read.
    assert isinstance(state.attributes["current_slot_index"], int)
    assert 0 <= state.attributes["current_slot_index"] < _PROG_LENGTH


async def test_schedule_sensor_state_and_attributes_at_fixed_hour(hass, monkeypatch, enable_custom_integrations):
    # Monday 08:15 local: hours 0-7 cold, 8-15 day, 16-23 night; every other
    # day cold all day (irrelevant here beyond the day-attribute slices
    # checked below).
    monday = [0] * 8 + [2] * 8 + [1] * 8
    prog = monday + [0] * 24 * 6  # HA surface order, Monday first
    assert len(prog) == _PROG_LENGTH

    transport = FakeCulTransport()
    transport._programs[MASTER_BEDROOM] = _encode_program_nibbles(_to_wire_hourly(prog))

    fixed_now = dt_util.now().replace(
        year=2026, month=9, day=7, hour=8, minute=15, second=0, microsecond=0
    )
    monkeypatch.setattr(dt_util, "now", lambda: fixed_now)

    await setup_entry(hass, monkeypatch, transport)

    state = hass.states.get(ENTITY_ID)
    assert state is not None
    # Slot 16 (Monday 08:00-08:29, hour 8's own first native half-hour slot)
    # == 2 ("day"): the state is the slot's own target temperature
    # (docs/80-handover.md list C item 2, commit 2411c36), looked up off this
    # heater's own comfort preset; the slot's bare name survives as the
    # `preset` attribute below. MASTER_BEDROOM's F3 B0 reply is a 9F nibble
    # record, native 48 slots a day, so every hourly value here reaches the
    # cache doubled across its own two equal half-hour slots (_to_native).
    assert state.state == "23.5"
    assert state.attributes["preset"] == "day"
    assert state.attributes["prog"] == _to_native(prog)
    assert state.attributes["monday"] == _to_native(monday)
    assert state.attributes["tuesday"] == [0] * SLOTS_PER_DAY_HALF_HOURLY
    assert state.attributes["sunday"] == [0] * SLOTS_PER_DAY_HALF_HOURLY
    assert state.attributes["current_slot_index"] == 16
    assert state.attributes["source"] == "read"
    # Slot 32 (Monday 16:00) is the next native slot whose value (night, 1)
    # differs from slot 16's (day, 2); slots 17-31 are also day, so nothing
    # changes before then -- the same wall-clock time the old hourly cache
    # reported, since this test's own schedule never varies within an hour.
    expected_next_change = fixed_now.replace(hour=16, minute=0, second=0, microsecond=0)
    assert state.attributes["next_change"] == expected_next_change.isoformat(timespec="seconds")


async def test_schedule_sensor_saturday_1546_regression(hass, monkeypatch, enable_custom_integrations):
    """docs/captures/2026-09-12-schedule/notes.md: the schedule sensor
    reported slot 271 at 15:46 local on a Saturday, one day early -- Friday's
    own half-slot instead of Saturday's -- because Coordinator.get_prog handed
    back the wire array (day 0 Sunday, PROTOCOL.md 5.6/5.7) unrotated and
    Python's own weekday() (Monday 0) indexed it as if it were Monday first.
    This reproduces it against the real sensor pipeline: Friday (wire day 5)
    15:00-15:59 is "night", Saturday (wire day 6) 15:00-15:59 is "day"; at
    15:46 local on a real Saturday (2026-09-12, today) the fix must read
    Saturday's own "day", not Friday's "night"."""
    from termoweb_local import network as network_module

    resolution = SLOTS_PER_DAY_HALF_HOURLY
    wire_week = [[0] * resolution for _ in range(DAYS_PER_WEEK)]
    wire_week[5][30:32] = [1, 1]  # Friday (wire day 5): night
    wire_week[6][30:32] = [2, 2]  # Saturday (wire day 6): day
    raw = network_module._encode_program_slots(wire_week)
    assert len(raw) == 84

    transport = FakeCulTransport()
    transport._programs[MASTER_BEDROOM] = raw

    fixed_now = dt_util.now().replace(
        year=2026, month=9, day=12, hour=15, minute=46, second=0, microsecond=0
    )
    assert fixed_now.weekday() == 5  # a real Saturday
    monkeypatch.setattr(dt_util, "now", lambda: fixed_now)

    await setup_entry(hass, monkeypatch, transport)

    state = hass.states.get(ENTITY_ID)
    assert state is not None
    assert state.attributes["current_slot_index"] == 271
    assert state.attributes["preset"] == "day"  # Saturday's own slot, not Friday's "night"
    assert state.attributes["saturday"][30:32] == [2, 2]
    assert state.attributes["friday"][30:32] == [1, 1]


async def test_schedule_sensor_updates_after_program_read(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    # FakeCulTransport's own default program (all-zero nibbles) already made
    # the sensor "cold" (0) -> the anti-frost preset temperature, 7.0C, with
    # source "read" from the startup F3 B0. The slot's own name survives as
    # the `preset` attribute.
    initial_state = hass.states.get(ENTITY_ID)
    assert initial_state.state == "7.0"
    assert initial_state.attributes["preset"] == "cold"
    assert initial_state.attributes["source"] == "read"

    # Store an all-"night" program for this node, then read it back through
    # the fake transport exactly like the real F3 B0 round trip does;
    # every slot holds the same value so this assertion does not depend on
    # whatever hour the test actually runs at.
    transport._programs[MASTER_BEDROOM] = _encode_program_nibbles([1] * _PROG_LENGTH)
    result = await coordinator.async_read_program(MASTER_BEDROOM)
    # async_read_program now returns get_prog()'s own native-slot shape (336
    # values, 48 a day), not the 168-value hourly projection it used to.
    assert result == [1] * _NATIVE_PROG_LENGTH

    # async_read_program only updates the cache; push the update out the
    # same way climate.py's own async_set_schedule does for its own
    # coordinator-mutating call, the "existing coordinator listener
    # mechanism" CoordinatorEntity's schedule sensor already subscribes to.
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    state = hass.states.get(ENTITY_ID)
    # Night (1) -> the eco preset temperature, 23.0C on this fake heater.
    assert state.state == "23.0"
    assert state.attributes["preset"] == "night"
    assert state.attributes["prog"] == [1] * _NATIVE_PROG_LENGTH
    assert state.attributes["source"] == "read"
    assert state.attributes["next_change"] is None  # every slot is identical


async def test_heater_sensors_survive_a_heater_missing_from_coordinator_heaters(
    hass, monkeypatch, enable_custom_integrations
):
    """sensor.py's own `_HeaterSensorBase._heater` guard (commit 4387163),
    exercised directly rather than through async_remove_config_entry_device:
    that function's own ordering fix (__init__.py) now closes the one window
    it used to leave open, by tearing entities down before it ever mutates
    coordinator.heaters (see the ordering test below), so calling it here
    would no longer reach a live entity with a missing heater at all. The
    guard is defence in depth for *any* path that could leave
    coordinator.heaters missing a node an entity is still wired up to read,
    today's ordering fix included -- simulated directly here rather than
    tying this test's survival to one specific caller.

    Writes each sensor entity's own state directly (not
    coordinator.async_update_listeners(), which would also notify
    climate.py's own TermowebLocalClimate -- its `_heater` property has the
    same bare-index bug, out of scope here, see this change's own report).
    TermowebLocalSchedulePresetSensor is deliberately not asserted here: it
    reads coordinator.get_prog(), never coordinator.heaters, so it is not
    exposed to this particular guard's own scenario at all (checked
    separately: this deletion does not change its state)."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    assert hass.states.get(ENTITY_ID).state != "unknown"
    preset_state_before = hass.states.get(PRESET_ENTITY_ID).state
    assert preset_state_before != "unknown"

    del coordinator.heaters[MASTER_BEDROOM]
    sensor_entity_ids = (
        ENTITY_ID,
        PRESET_ENTITY_ID,
        "sensor.master_bedroom_heater_energy",
        "sensor.master_bedroom_heater_temperature",
    )
    for entity_id in sensor_entity_ids:
        entity = _live_entity(hass, entity_id)
        assert entity is not None
        entity.async_write_ha_state()

    assert hass.states.get(ENTITY_ID).state == "unknown"
    assert hass.states.get("sensor.master_bedroom_heater_energy").state == "unknown"
    assert hass.states.get("sensor.master_bedroom_heater_temperature").state == "unknown"
    assert hass.states.get(PRESET_ENTITY_ID).state == preset_state_before


async def test_schedule_preset_sensor_unknown_before_any_read(hass, monkeypatch, enable_custom_integrations):
    """Requirement 4's degenerate case, and requirement 3's modelling: ENUM
    device class with an explicit three-value `options` list, no unit and no
    state_class (statistics do not exist for ENUM sensors)."""
    transport = _NoProgramTransport(silent_program_node=MASTER_BEDROOM)
    await setup_entry(hass, monkeypatch, transport)

    state = hass.states.get(PRESET_ENTITY_ID)
    assert state is not None
    assert state.state == "unknown"
    assert state.attributes["device_class"] == "enum"
    assert state.attributes["options"] == ["cold", "night", "day"]
    assert "unit_of_measurement" not in state.attributes
    assert "state_class" not in state.attributes


async def test_schedule_preset_sensor_reads_unknown_for_an_unrecognised_slot_code(
    hass, monkeypatch, enable_custom_integrations
):
    """An hour network.py's own decoder could not read (nibble 0xF is not one
    of PROGRAM_SLOT_NIBBLE's three values) must read "unknown", never a
    guessed preset -- the same degenerate case TermowebLocalScheduleSensor's
    own numeric state already covers, shared through _current_slot_code."""
    fixed_now = dt_util.now().replace(
        year=2026, month=9, day=7, hour=5, minute=0, second=0, microsecond=0
    )
    monkeypatch.setattr(dt_util, "now", lambda: fixed_now)

    # All-zero nibbles ("cold" everywhere) except slot 5 (Monday 05:00, the
    # low nibble of byte 2 of the wire's day 1: wire day 0 is Sunday,
    # PROTOCOL.md 5.6/5.7, so Monday sits one day-chunk (12 bytes) in): 0xF, a
    # code PROGRAM_SLOT_NIBBLE does not know.
    raw = bytearray(84)
    raw[12 + 2] = 0x0F
    transport = FakeCulTransport()
    transport._programs[MASTER_BEDROOM] = bytes(raw)

    await setup_entry(hass, monkeypatch, transport)

    preset_state = hass.states.get(PRESET_ENTITY_ID)
    assert preset_state.state == "unknown"
    # The numeric sensor shares the same slot lookup, so it agrees.
    assert hass.states.get(ENTITY_ID).state == "unknown"


async def test_schedule_preset_sensor_agrees_whether_the_program_came_from_nibbles_or_bits(
    hass, monkeypatch, enable_custom_integrations
):
    """network.py's own nibble (9F) and 2-bit (C9) program decoders already
    collapse to the same normalised 0/1/2/None codes (proven directly against
    each other here, and against preset_target_c in test_heater.py); this
    shows the *sensor* itself is a pure function of that normalised list by
    driving it from both encodings in turn (monkeypatching Coordinator.get_prog
    directly, the same seam a real C9-hourly heater's 168-value week would
    reach the sensor through) and checking it lands on the same state either
    way."""
    from termoweb_local import network as network_module

    monday = [0] * 8 + [2] * 8 + [1] * 8  # 0-7 cold, 8-15 day, 16-23 night
    prog = monday + [0] * 24 * 6

    decoded_from_nibbles = network_module._decode_program_nibbles(_encode_program_nibbles(prog))
    decoded_from_bits = network_module._decode_program_bits(_encode_program_bits(prog))
    assert decoded_from_nibbles == decoded_from_bits == prog

    fixed_now = dt_util.now().replace(
        year=2026, month=9, day=7, hour=10, minute=0, second=0, microsecond=0
    )
    monkeypatch.setattr(dt_util, "now", lambda: fixed_now)

    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    preset_entity = _live_entity(hass, PRESET_ENTITY_ID)
    assert preset_entity is not None

    for decoded in (decoded_from_nibbles, decoded_from_bits):
        monkeypatch.setattr(coordinator, "get_prog", lambda node_id, decoded=decoded: decoded)
        preset_entity._handle_minute_tick(dt_util.utcnow())
        assert hass.states.get(PRESET_ENTITY_ID).state == "day"  # hour 10 -> slot value 2


async def test_schedule_preset_sensor_all_three_values_and_hourly_rollover(
    hass, monkeypatch, enable_custom_integrations
):
    """All three preset values, and the hourly rollover: the hour changes with
    no coordinator push at all (module docstring on TermowebLocalScheduleSensor
    -- the 9E program-report push emits no program-changed signal), so only
    the once-a-minute tick notices."""
    monday = [0] * 8 + [2] * 8 + [1] * 8  # 0-7 cold, 8-15 day, 16-23 night
    prog = monday + [0] * 24 * 6  # HA surface order, Monday first

    transport = FakeCulTransport()
    transport._programs[MASTER_BEDROOM] = _encode_program_nibbles(_to_wire_hourly(prog))

    now_box = {
        "value": dt_util.now().replace(
            year=2026, month=9, day=7, hour=7, minute=59, second=0, microsecond=0
        )
    }
    monkeypatch.setattr(dt_util, "now", lambda: now_box["value"])

    await setup_entry(hass, monkeypatch, transport)
    preset_entity = _live_entity(hass, PRESET_ENTITY_ID)
    assert preset_entity is not None

    assert hass.states.get(PRESET_ENTITY_ID).state == "cold"  # hour 7

    now_box["value"] = now_box["value"].replace(hour=8)
    preset_entity._handle_minute_tick(dt_util.utcnow())
    assert hass.states.get(PRESET_ENTITY_ID).state == "day"  # hour 8

    now_box["value"] = now_box["value"].replace(hour=16)
    preset_entity._handle_minute_tick(dt_util.utcnow())
    assert hass.states.get(PRESET_ENTITY_ID).state == "night"  # hour 16


async def test_schedule_and_preset_sensors_unsubscribe_their_minute_tick_on_removal(
    hass, monkeypatch, enable_custom_integrations
):
    """Both schedule-aware sensor kinds (_ScheduleSlotSensorBase) install a
    once-a-minute tick in async_added_to_hass and must cancel it in
    async_will_remove_from_hass -- proven here for real, rather than just by
    reading the source, by wrapping async_track_time_change and counting
    cancellations across an entry unload. TEST_HEATERS has two heaters, each
    with two schedule-aware sensors (the numeric schedule sensor and the new
    preset sensor), so four subscriptions must be cancelled."""
    from custom_components.termoweb_local import sensor as sensor_module

    real_track_time_change = sensor_module.async_track_time_change
    cancelled = []

    def _tracking_track_time_change(hass_, action, **kwargs):
        unsub = real_track_time_change(hass_, action, **kwargs)

        def _wrapped_unsub():
            cancelled.append(True)
            unsub()

        return _wrapped_unsub

    monkeypatch.setattr(sensor_module, "async_track_time_change", _tracking_track_time_change)

    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    assert len(cancelled) == 0

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert len(cancelled) == 4


async def test_removing_a_heater_device_tears_entities_down_before_forgetting_it(
    hass, monkeypatch, enable_custom_integrations
):
    """The ordering fix in __init__.py's async_remove_config_entry_device:
    it must remove the device (which HA tears the entities down for,
    synchronously -- entity_registry's own EVENT_DEVICE_REGISTRY_UPDATED
    listener enqueues an eager-started removal task per entity, see that
    function's own docstring) before it ever mutates coordinator.heaters,
    not merely happen to be safe to read afterward. Assert on the ordering
    itself: by the time this function returns, both schedule-aware
    entities' own per-minute ticks must already be unsubscribed, which can
    only be true if entity teardown ran before coordinator.heaters lost the
    node -- not on the absence of an exception, which sensor.py's own
    `_HeaterSensorBase._heater` guard (commit 4387163) would paper over
    either way."""
    from homeassistant.helpers import device_registry as dr

    from custom_components.termoweb_local import async_remove_config_entry_device
    from custom_components.termoweb_local.const import DOMAIN as INTEGRATION_DOMAIN

    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    schedule_entity = _live_entity(hass, ENTITY_ID)
    preset_entity = _live_entity(hass, PRESET_ENTITY_ID)
    assert schedule_entity is not None
    assert preset_entity is not None
    assert schedule_entity._unsub_minute_tick is not None
    assert preset_entity._unsub_minute_tick is not None

    device = dr.async_get(hass).async_get_device_by_identifier(
        (INTEGRATION_DOMAIN, f"{coordinator.dev_id}:{MASTER_BEDROOM}"), entry.entry_id
    )
    assert device is not None

    assert await async_remove_config_entry_device(hass, entry, device) is True

    # The ordering itself, checked with no intervening await at all: both
    # ticks are already gone, proving entity teardown ran to completion
    # strictly before -- not concurrently with, not after -- this call
    # returned and coordinator.heaters lost the node.
    assert schedule_entity._unsub_minute_tick is None
    assert preset_entity._unsub_minute_tick is None
    assert MASTER_BEDROOM not in coordinator.heaters

    await hass.async_block_till_done()


# ---- slot indexing and next-change, at both resolutions ----------------------
#
# Coordinator.get_prog now hands these sensors each node's own native slots
# (docs/80-handover.md "Owed" list, item 7): 24 a day for a C9-hourly heater
# (02, 03 on this bench), 48 for a 9E/9F-nibble one (04, MASTER_BEDROOM, and
# every heater in this file's own FakeCulTransport fixture). The pure
# _current_slot_index/_next_change_at functions below still take resolution
# as an explicit argument rather than reading it off a node, so both are
# exercised directly; the half-hourly ones say in their own docstrings
# exactly what the capture corpus does and does not prove about half-hour
# boundaries meaning what the gateway's own record-length rule says they do.


def _previous_current_slot_index(now: dt.datetime) -> int:
    """`sensor._current_slot_index` exactly as it stood before it took a
    resolution: weekday times 24, plus the hour. Kept here so the "unchanged for
    an hourly week" property below is checked against the expression it
    replaces, not against a table rewritten by hand."""
    return now.weekday() * 24 + now.hour


def _previous_next_change_at(prog, now: dt.datetime):
    """`sensor._next_change_at` exactly as it stood before this change, for the
    same reason: one quantisation to the hour, one step of whole hours, and a
    bare `!=` that reads an undecodable slot as a different value."""
    current_index = _previous_current_slot_index(now) % len(prog)
    current_value = prog[current_index]
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    for offset in range(1, len(prog) + 1):
        if prog[(current_index + offset) % len(prog)] != current_value:
            return hour_start + dt.timedelta(hours=offset)
    return None


_MONDAY = dt.datetime(2026, 9, 7)


def _every_minute_of_a_week():
    for minute in range(7 * 24 * 60):
        yield _MONDAY + dt.timedelta(minutes=minute)


def _four_times_an_hour_for_a_week():
    for hour in range(7 * 24):
        for minute in (0, 15, 30, 45):
            yield _MONDAY + dt.timedelta(hours=hour, minutes=minute)


def test_current_slot_index_hourly_is_unchanged_minute_for_minute():
    """Safety property, not a claim about the radio: for the 168-value hourly
    week Coordinator.get_prog returns today, the resolution-aware index must
    equal the bare `weekday() * 24 + hour` it replaces, at every one of the
    10080 minutes of a week. This is the whole reason the resolution argument
    can be introduced at all, so it is a test rather than an assertion in a
    docstring."""
    assert _MONDAY.weekday() == 0
    for now in _every_minute_of_a_week():
        assert _current_slot_index(now, SLOTS_PER_DAY_HOURLY) == (
            _previous_current_slot_index(now)
        )


def test_current_slot_index_half_hourly_is_a_firmware_derived_expectation():
    """FIRMWARE-DERIVED EXPECTATION, not a captured one.

    What the corpus proves is that a heater stores an hour's two 2-bit halves
    independently and hands them back unchanged: the `10` nibble written into
    the master bedroom heater's Monday hour 12 and read back verbatim
    (docs/captures/2026-09-06-proof/program-nibble-b0-read.log, pinned by
    tests/test_network.py::test_read_program_decodes_84_byte_nibble_payload).
    What it does not prove is that those two halves are two half hours: that is
    the gateway's own rule, the record length alone choosing 24 or 48 slots a
    day (docs/captures/2026-09-05-gateway-dump/analysis9.md section 8,
    docs/PROTOCOL.md 5.6). No heater on this bench is scheduled on the half
    hour, so there is no capture of a half-hour boundary to check this against.

    So this pins the mapping that rule implies and nothing further: slot
    `weekday() * 48 + hour * 2`, plus one from minute 30 of the hour on."""
    for now in _every_minute_of_a_week():
        expected = (
            now.weekday() * 48 + now.hour * 2 + (1 if now.minute >= 30 else 0)
        )
        assert _current_slot_index(now, SLOTS_PER_DAY_HALF_HOURLY) == expected


def test_next_change_at_hourly_is_unchanged_for_a_fully_decoded_week():
    """The other half of the safety property: for a 168-value week holding no
    undecodable slot, the rewritten function must return exactly what the
    previous one did, at four times an hour across a whole week, over a spread
    of schedules including 20 pseudo-random ones. Weeks holding a None are
    deliberately excluded here, since changing that case is the point of the
    change (the tests below cover it)."""
    rng = random.Random(20260910)
    progs = [
        [0] * _PROG_LENGTH,
        [i % 3 for i in range(_PROG_LENGTH)],
        [0] * 8 + [2] * 8 + [1] * 8 + [0] * (_PROG_LENGTH - 24),
        *(
            [rng.choice((0, 1, 2)) for _ in range(_PROG_LENGTH)]
            for _ in range(20)
        ),
    ]
    for prog in progs:
        assert len(prog) == _PROG_LENGTH
        for now in _four_times_an_hour_for_a_week():
            assert _next_change_at(prog, now, SLOTS_PER_DAY_HOURLY) == (
                _previous_next_change_at(prog, now)
            )


def test_next_change_at_is_unknown_when_the_current_slot_is_unknown():
    """An hour this station could not decode has no value, so there is nothing
    for a later hour to differ from and no honest next-change time. The previous
    implementation compared `None != 0` and reported the very next hour as a
    change it had no way to know about."""
    prog = [0] * _PROG_LENGTH
    prog[10] = None
    now = _MONDAY.replace(hour=10, minute=30)

    assert _previous_next_change_at(prog, now) == _MONDAY.replace(hour=11)
    assert _next_change_at(prog, now, SLOTS_PER_DAY_HOURLY) is None


def test_next_change_at_stops_at_an_unknown_slot_rather_than_stepping_over_it():
    """Walking forward, an undecodable hour is neither the same value as the
    current one nor a different one, so the walk stops there and reports "not
    knowable": the change may be exactly at that hour. The previous
    implementation announced it as a change outright."""
    prog = [0] * _PROG_LENGTH
    prog[12] = None
    prog[20] = 1
    now = _MONDAY.replace(hour=9)

    assert _previous_next_change_at(prog, now) == _MONDAY.replace(hour=12)
    assert _next_change_at(prog, now, SLOTS_PER_DAY_HOURLY) is None


def test_next_change_at_does_not_read_a_run_of_unknown_slots_as_no_change():
    """The reported defect in its plainest form: with the current hour unknown
    and a run of unknown hours after it, the previous implementation skipped
    over all of them as "the same" and announced the first decodable hour as the
    change. Nothing about that hour is known to be a change."""
    prog = [None] * _PROG_LENGTH
    prog[40] = 2
    now = _MONDAY.replace(hour=0)

    assert _previous_next_change_at(prog, now) == _MONDAY + dt.timedelta(hours=40)
    assert _next_change_at(prog, now, SLOTS_PER_DAY_HOURLY) is None


def test_next_change_at_still_reports_a_change_that_precedes_an_unknown_slot():
    """Refusing to walk past an unknown hour is not refusing to answer: a change
    the walk reaches first is still reported normally."""
    prog = [0] * _PROG_LENGTH
    prog[11] = 1
    prog[12] = None
    now = _MONDAY.replace(hour=9, minute=20)

    expected = _MONDAY.replace(hour=11)
    assert _next_change_at(prog, now, SLOTS_PER_DAY_HOURLY) == expected
    assert _previous_next_change_at(prog, now) == expected


def test_next_change_at_half_hourly_is_a_firmware_derived_expectation():
    """FIRMWARE-DERIVED EXPECTATION, not a captured one. The corpus contains no
    heater scheduled on the half hour, so no capture shows a schedule changing
    at :30; the same caveat as
    test_current_slot_index_half_hourly_is_a_firmware_derived_expectation
    applies in full, including that program-nibble-b0-read.log proves only that
    an hour's two halves are stored independently, not that they are half hours.

    What this pins is that the function can express such a change at all. The
    previous implementation could not, whatever the cache held: it quantised to
    the start of the hour and stepped by whole hours, so every time it could
    ever return had minute 0."""
    prog = [0] * (7 * SLOTS_PER_DAY_HALF_HOURLY)
    prog[21] = 2  # Monday 10:30-10:59

    at_ten_oh_five = _MONDAY.replace(hour=10, minute=5)
    assert _current_slot_index(at_ten_oh_five, SLOTS_PER_DAY_HALF_HOURLY) == 20
    assert _next_change_at(prog, at_ten_oh_five, SLOTS_PER_DAY_HALF_HOURLY) == (
        _MONDAY.replace(hour=10, minute=30)
    )
    assert _previous_next_change_at(prog, at_ten_oh_five).minute == 0

    at_ten_forty = _MONDAY.replace(hour=10, minute=40)
    assert _current_slot_index(at_ten_forty, SLOTS_PER_DAY_HALF_HOURLY) == 21
    assert _next_change_at(prog, at_ten_forty, SLOTS_PER_DAY_HALF_HOURLY) == (
        _MONDAY.replace(hour=11, minute=0)
    )


def test_cache_resolution_is_read_off_the_week_being_indexed():
    """The resolution comes from the list this module is about to index, never
    from Network.program_resolution: those two disagree today. The master
    bedroom heater answers F3 B0 with the 85-byte 9F record, so its
    prog_resolution is 48 (PROTOCOL.md 5.6), while the cache reached through
    Coordinator.get_prog holds ProgramRecord.hourly, 168 values. Deriving the
    index from the record instead of from the list would misindex that node by a
    factor of two, silently and plausibly, which is the whole failure this
    guards against.

    A length that is not 7 days of one of the gateway's own two resolutions has
    no honest slot number, so it reads as unknown rather than as something
    derived from a length nobody has explained."""
    assert _cache_resolution(None) is None
    assert _cache_resolution([]) is None
    assert _cache_resolution([0] * _PROG_LENGTH) == SLOTS_PER_DAY_HOURLY
    assert _cache_resolution([0] * (7 * SLOTS_PER_DAY_HALF_HOURLY)) == (
        SLOTS_PER_DAY_HALF_HOURLY
    )
    assert _cache_resolution([0] * 112) is None  # 7 days of 16 is not a resolution
    assert _cache_resolution([0] * (_PROG_LENGTH + 1)) is None


# The 84-byte record a real F3 B0 read returned for the master bedroom heater
# after the 2026-09-06 nibble patch and restore
# (docs/captures/2026-09-06-proof/program-nibble-b0-read.log, notes.md line 109;
# the same bytes tests/test_network.py::_program_read_reply_payload uses). Monday
# byte 6 (0-indexed; nibble-per-hour byte 7 in the capture's own 1-indexed
# numbering) is `10`, the patch's asymmetric nibble, stored and read back
# verbatim: hour 12's high half-hour slot code 0 ("cold"), low half-hour slot
# code 1 ("night"), disagreeing. ProgramRecord.hourly still folds a
# disagreeing hour to None (network.py's _fold_to_hourly, still what
# set_schedule's own hourly write path and _check_hourly_write_keeps_the_schedule
# read); Coordinator.get_prog no longer does (docs/80-handover.md "Owed" list,
# item 7) -- it hands back ProgramRecord.slots, so both of this hour's real
# half-hour values reach the sensor rather than one shared None. The capture
# proves the heater keeps an hour's two halves independently; it does not
# prove those halves are half hours (see the firmware-derived tests above).
_CAPTURED_PATCHED_MONDAY = bytes.fromhex("555555500000100000000055")
_CAPTURED_UNPATCHED_DAY = bytes.fromhex("555555500000000000000055")
# Wire day 0 is Sunday (PROTOCOL.md 5.6/5.7), so the patched day -- Monday --
# sits one day-chunk (12 bytes) into the wire record, not at its start.
_CAPTURED_PATCHED_WEEK = (
    _CAPTURED_UNPATCHED_DAY + _CAPTURED_PATCHED_MONDAY + _CAPTURED_UNPATCHED_DAY * 5
)


async def test_schedule_sensor_reveals_the_half_hour_boundary_in_a_captured_asymmetric_nibble(
    hass, monkeypatch, enable_custom_integrations
):
    """The captured asymmetric nibble no longer folds away to "unknown": at
    native (half-hourly) resolution it is two ordinary, fully decodable slots,
    0 ("cold") then 1 ("night") -- slot 24 (Monday 12:00-12:29) and slot 25
    (12:30-12:59) -- which is the whole point of moving the cache to native
    slots (docs/80-handover.md "Owed" list, item 7)."""
    transport = FakeCulTransport()
    transport._programs[MASTER_BEDROOM] = _CAPTURED_PATCHED_WEEK

    fixed_now = dt_util.now().replace(
        year=2026, month=9, day=7, hour=12, minute=15, second=0, microsecond=0
    )
    monkeypatch.setattr(dt_util, "now", lambda: fixed_now)

    await setup_entry(hass, monkeypatch, transport)

    state = hass.states.get(ENTITY_ID)
    assert state is not None
    assert state.attributes["current_slot_index"] == 24
    assert state.attributes["prog"][24] == 0
    assert state.attributes["prog"][25] == 1
    assert state.attributes["monday"][24] == 0
    assert state.attributes["monday"][25] == 1
    assert state.state == "7.0"  # anti-frost, the "cold" preset's own target
    assert state.attributes["preset"] == "cold"
    expected_next_change = fixed_now.replace(hour=12, minute=30, second=0, microsecond=0)
    assert state.attributes["next_change"] == expected_next_change.isoformat(timespec="seconds")
    assert hass.states.get(PRESET_ENTITY_ID).state == "cold"


async def test_schedule_sensor_finds_the_half_hour_change_several_slots_ahead(
    hass, monkeypatch, enable_custom_integrations
):
    """Same captured record, almost three hours before the asymmetric nibble's
    own low half. Walking forward at native (half-hourly) resolution crosses
    six unchanging half-hour slots (Monday 09:30 through 12:00, all still
    "cold") before finding a real change at 12:30 -- the low half of the
    formerly-undecodable hour, now an ordinary slot (see the test above).
    Before this station kept native slots, the same capture's hourly cache
    reported this as "not knowable" purely because hour 12 folded to None."""
    transport = FakeCulTransport()
    transport._programs[MASTER_BEDROOM] = _CAPTURED_PATCHED_WEEK

    fixed_now = dt_util.now().replace(
        year=2026, month=9, day=7, hour=9, minute=20, second=0, microsecond=0
    )
    monkeypatch.setattr(dt_util, "now", lambda: fixed_now)

    await setup_entry(hass, monkeypatch, transport)

    state = hass.states.get(ENTITY_ID)
    assert state is not None
    assert state.attributes["current_slot_index"] == 18
    assert state.attributes["preset"] == "cold"
    expected_next_change = fixed_now.replace(hour=12, minute=30, second=0, microsecond=0)
    assert state.attributes["next_change"] == expected_next_change.isoformat(timespec="seconds")

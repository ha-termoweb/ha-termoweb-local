"""Climate entity commands, asserting the exact outgoing on-air bytes against
termoweb_frame worked values (docs/90-phase3-plan.md P4 verification line)."""
import logging

from homeassistant.components.climate import HVACAction, HVACMode, PRESET_NONE
from homeassistant.const import ATTR_TEMPERATURE

from termoweb_local import frame as tf

from .conftest import FakeCulTransport, setup_entry
from custom_components.termoweb_local.const import PRESET_TEMPORARY_OVERRIDE

MASTER_BEDROOM = 0x04  # matches TEST_HEATERS's second entry


def _last_sent_hex(transport: FakeCulTransport) -> str:
    """The last *command* frame written, skipping the F3 B8 status request
    that now follows every successful setpoint/mode/override command (docs/
    80-handover.md list C item 10 step 1): that status request is the actual
    last T write, not the command under test, so it is filtered out here by
    payload rather than assumed away by position."""
    sent = [w for w in transport.written if w.startswith(b"T")]
    for raw in reversed(sent):
        hexpart = raw.strip()[1:].decode()
        if tf.parse_frame(bytes.fromhex(hexpart)).payload != bytes([0xB8]):
            return hexpart
    raise AssertionError("no non-status-request frame found in transport.written")


async def test_set_temperature_sends_plain_setpoint_frame_when_already_heating(
    hass, monkeypatch, enable_custom_integrations
):
    """docs/PROTOCOL.md 5.1 worked example for 25.5C: unchanged since list C
    item 2's mode-preserving fix, because the heater is already in
    manual/heat here (RX line below) -- there is no mode to preserve."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    transport.queue_line(
        "RX 1000 -44.0 60 0 E59C885DB6A1C825565F4A9C5850E47E05BCB4F086AE28C3E7BEE3933D"
    )
    state = await _wait_for_entity_state(hass, "climate.master_bedroom_heater", "heat")
    assert state.attributes["preset_mode"] == PRESET_NONE  # mode byte 02, manual, not override
    transport.written.clear()

    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": "climate.master_bedroom_heater", ATTR_TEMPERATURE: 25.5},
        blocking=True,
    )

    expected = tf.build_frame(0x01, MASTER_BEDROOM, tf.setpoint_payload(25.5))
    assert _last_sent_hex(transport) == expected.hex().upper()
    assert expected.hex().upper() == "F19C8858B3A1CD20575E4B9CBAEBD9F6E5"


async def test_set_temperature_preserves_off_mode(hass, monkeypatch, enable_custom_integrations):
    """docs/80-handover.md list C item 2: network.set_setpoint's own F1
    frame always carries the manual/heat mode byte (PROTOCOL.md 5.1), so a
    plain setpoint write to an off heater has to carry the off mode byte in
    its place instead, in the same single frame, to actually leave the
    heater off. Setup's own first refresh already leaves the entity off
    (conftest.py's _STATUS_REPLY_PAYLOAD), so no RX line is needed here."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    transport.written.clear()

    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": "climate.master_bedroom_heater", ATTR_TEMPERATURE: 25.5},
        blocking=True,
    )

    sent_air = bytes.fromhex(_last_sent_hex(transport))
    assert tf.parse_frame(sent_air).payload == bytes([0xB4, 0x04, 0x33])


async def test_set_temperature_with_explicit_hvac_mode_sends_that_mode(
    hass, monkeypatch, enable_custom_integrations
):
    """An explicit hvac_mode passed alongside temperature wins over whatever
    mode the heater was already in (list C item 2's own "unless the service
    call also carries hvac_mode" carve-out)."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    transport.written.clear()

    await hass.services.async_call(
        "climate",
        "set_temperature",
        {
            "entity_id": "climate.master_bedroom_heater",
            ATTR_TEMPERATURE: 25.5,
            "hvac_mode": HVACMode.AUTO,
        },
        blocking=True,
    )

    sent_air = bytes.fromhex(_last_sent_hex(transport))
    assert tf.parse_frame(sent_air).payload == bytes([0xB4, 0x01, 0x33])


def _e5_report_line(node_id, mode_code, room_temp_c, setpoint_c, anti_frost_c, eco_c=18.5, comfort_c=21.0):
    """A synthetic E5 report air line for queue_line, built from
    tf.build_frame rather than a capture: byte offsets checked against
    tests/test_heater.py's own worked frame (56 b9 <anti-frost> <eco>
    <comfort> <mode> <room hi> <room lo> <setpoint> 00 00 00 00 00 00)."""
    payload = bytes(
        [
            0x56, 0xB9,
            int(round(anti_frost_c * 2)), int(round(eco_c * 2)), int(round(comfort_c * 2)),
            mode_code,
            (int(round(room_temp_c * 10)) >> 8) & 0xFF, int(round(room_temp_c * 10)) & 0xFF,
            int(round(setpoint_c * 2)),
            0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        ]
    )
    air = tf.build_frame(node_id, 0x01, payload)
    return f"RX 1000 -44.0 60 0 {air.hex().upper()}"


async def test_target_temperature_holds_through_an_off_antifrost_reporting_artefact(
    hass, monkeypatch, enable_custom_integrations, caplog
):
    """A heater OFF with its own real setpoint held at 24.0C reads the
    anti-frost preset (7.0C) in the setpoint field instead after a panel
    menu entered following a Runback on/off cycle: MANUAL mode still holds
    the real value on the heater itself, so this is a reporting artefact,
    not an actual change. target_temperature must keep showing 24.0C
    (never the momentary 7.0C), and a later command built off the entity's
    displayed target must still carry 24.0C, not the artefact -- while the
    coordinator's own raw snapshot is left reading exactly what the heater
    reported, unmodified."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    transport.queue_line(
        _e5_report_line(MASTER_BEDROOM, mode_code=0x04, room_temp_c=23.1, setpoint_c=24.0, anti_frost_c=7.0)
    )
    state = await _wait_for_attribute(hass, "climate.master_bedroom_heater", "current_temperature", 23.1)
    assert state.state == "off"
    assert state.attributes["temperature"] == 24.0

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        transport.queue_line(
            _e5_report_line(MASTER_BEDROOM, mode_code=0x04, room_temp_c=19.8, setpoint_c=7.0, anti_frost_c=7.0)
        )
        state = await _wait_for_attribute(hass, "climate.master_bedroom_heater", "current_temperature", 19.8)

    assert state.state == "off"
    assert state.attributes["temperature"] == 24.0
    assert "setpoint held at 24.0C while off" in caplog.text

    # The raw snapshot is untouched: it reads exactly what the heater sent.
    assert coordinator.heaters[MASTER_BEDROOM].last_snapshot.setpoint_c == 7.0

    transport.written.clear()
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {
            "entity_id": "climate.master_bedroom_heater",
            ATTR_TEMPERATURE: state.attributes["temperature"],
            "hvac_mode": HVACMode.HEAT,
        },
        blocking=True,
    )

    sent_air = bytes.fromhex(_last_sent_hex(transport))
    assert tf.parse_frame(sent_air).payload == bytes([0xB4, 0x02, 0x30])  # heat, 24.0C -- not 7.0C


async def test_set_hvac_mode_auto_sends_mode_auto_frame(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    transport.written.clear()

    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": "climate.master_bedroom_heater", "hvac_mode": HVACMode.AUTO},
        blocking=True,
    )

    expected_logical = "0D 1B 30 01 04 00 01 04 00 00 00 00 B4 01 42 60".replace(" ", "")
    sent_air = bytes.fromhex(_last_sent_hex(transport))
    assert tf.descramble(sent_air).hex().upper() == expected_logical


async def test_set_hvac_mode_off_sends_mode_off_frame(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    transport.written.clear()

    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": "climate.master_bedroom_heater", "hvac_mode": HVACMode.OFF},
        blocking=True,
    )

    assert _last_sent_hex(transport) == "F29C8858B3A1CD20575E4B9CBAEDF895"


async def test_turn_off_service_reaches_coordinator_async_set_mode_off(
    hass, monkeypatch, enable_custom_integrations
):
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    calls = []
    original_async_set_mode = coordinator.async_set_mode

    async def _spy(node_id, mode):
        calls.append((node_id, mode))
        return await original_async_set_mode(node_id, mode)

    monkeypatch.setattr(coordinator, "async_set_mode", _spy)

    await hass.services.async_call(
        "climate",
        "turn_off",
        {"entity_id": "climate.master_bedroom_heater"},
        blocking=True,
    )

    assert calls == [(MASTER_BEDROOM, "off")]


async def test_turn_on_service_reaches_coordinator_async_set_mode_heat(
    hass, monkeypatch, enable_custom_integrations
):
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    calls = []
    original_async_set_mode = coordinator.async_set_mode

    async def _spy(node_id, mode):
        calls.append((node_id, mode))
        return await original_async_set_mode(node_id, mode)

    monkeypatch.setattr(coordinator, "async_set_mode", _spy)

    await hass.services.async_call(
        "climate",
        "turn_on",
        {"entity_id": "climate.master_bedroom_heater"},
        blocking=True,
    )

    assert calls == [(MASTER_BEDROOM, "heat")]


async def test_set_preset_mode_override_sends_f1_b4_03_frame(hass, monkeypatch, enable_custom_integrations):
    """docs/90-phase3-plan.md P4: selecting temporary_override with the current
    target sends the F1 B4 03 frame."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    # Give the entity a known target_temperature via a real E5 report first (24.0C,
    # mode heat), the same worked frame test_heater.py's own suite decodes.
    transport.queue_line(
        "RX 1000 -44.0 60 0 E59C885DB6A1C825565F4A9C5850E47E05BCB4F086AE28C3E7BEE3933D"
    )
    await _wait_for_entity_state(hass, "climate.master_bedroom_heater", "heat")

    transport.written.clear()
    await hass.services.async_call(
        "climate",
        "set_preset_mode",
        {"entity_id": "climate.master_bedroom_heater", "preset_mode": PRESET_TEMPORARY_OVERRIDE},
        blocking=True,
    )

    expected_logical = "0E 1B 30 01 04 00 01 04 00 00 00 00 B4 03 30 A5 9D".replace(" ", "")
    sent_air = bytes.fromhex(_last_sent_hex(transport))
    assert tf.descramble(sent_air).hex().upper() == expected_logical


async def test_set_preset_mode_none_sends_mode_heat_frame(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    transport.written.clear()

    await hass.services.async_call(
        "climate",
        "set_preset_mode",
        {"entity_id": "climate.master_bedroom_heater", "preset_mode": PRESET_NONE},
        blocking=True,
    )

    assert _last_sent_hex(transport) == "F29C8858B3A1CD20575E4B9CBAEB9853"


async def test_hvac_action_reflects_heating_flag_candidate(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    # Real worked frame (tests/test_heater.py::test_snapshot_after_temporary_override_command):
    # master bedroom, override, room 23.2C, setpoint 24.0C, heating-flag candidate
    # (byte 24) = 1.
    transport.queue_line(
        "RX 1000 -44.0 60 0 E59C885DB6A1C825565F4A9C5850E47E05BDB4F386AD13A1E786E3227A"
    )
    state = await _wait_for_entity_state(hass, "climate.master_bedroom_heater", "heat")
    assert state.attributes["hvac_action"] == HVACAction.HEATING
    assert state.attributes["current_temperature"] == 23.2


async def test_climate_attribute_set_matches_cloud_shape(hass, monkeypatch, enable_custom_integrations):
    """docs/45-cloud-integration-api.md section 1 attribute set: dev_id, addr,
    units, max_power/ptemp (from setup's own status snapshot), prog (from
    read_program at setup)."""
    from custom_components.termoweb_local.const import DEFAULT_DEV_ID

    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    state = hass.states.get("climate.master_bedroom_heater")
    assert state.attributes["dev_id"] == DEFAULT_DEV_ID
    assert state.attributes["addr"] == 0x04
    assert state.attributes["units"] == "C"
    # max_power/ptemp are the measured power and anti-frost/eco/comfort presets
    # from setup's own status snapshot (PROTOCOL.md 5.4), not placeholders, once
    # a report has arrived; setup_entry's F3 B8 request gets FakeCulTransport's
    # fixed E6 reply (conftest.py _STATUS_REPLY_PAYLOAD) so these are fixed too.
    assert state.attributes["max_power"] == 749.0
    assert state.attributes["ptemp"] == [7.0, 23.0, 23.5]
    assert state.attributes["ptemp_supported"] is True
    # setup's own F3 B0 read (FakeCulTransport's default all-zero 9F reply,
    # 84 nibble bytes) already populates prog with all-"cold" (0) values, at
    # this heater's own native 48-slot-a-day resolution -- 336 values, not
    # the 168-value hourly projection Coordinator.get_prog used to return
    # (docs/80-handover.md "Owed" list, item 7).
    assert state.attributes["prog"] == [0] * 336
    assert state.attributes["hvac_modes"] == ["off", "heat", "auto"]
    assert state.attributes["preset_modes"] == ["none", "temporary_override"]
    assert state.attributes["min_temp"] == 7.0
    assert state.attributes["max_temp"] == 35.0
    # TARGET_TEMPERATURE | PRESET_MODE | TURN_ON | TURN_OFF (HA 2024.2+ requires
    # TURN_ON/TURN_OFF for dashboard on/off controls on an entity with hvac modes).
    assert state.attributes["supported_features"] == 1 | 16 | 128 | 256


async def test_climate_icon_reflects_mode(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    # Off (the fixed F3 B8 status reply used at setup): mdi:radiator-off.
    off_state = hass.states.get("climate.master_bedroom_heater")
    assert off_state.attributes["icon"] == "mdi:radiator-off"

    # heating-flag candidate byte = 1 (see test_hvac_action... above).
    transport.queue_line(
        "RX 1000 -44.0 60 0 E59C885DB6A1C825565F4A9C5850E47E05BDB4F386AD13A1E786E3227A"
    )
    state = await _wait_for_entity_state(hass, "climate.master_bedroom_heater", "heat")
    assert state.attributes["icon"] == "mdi:radiator"


async def test_hvac_action_heating_with_boost_only_active_clear(
    hass, monkeypatch, enable_custom_integrations
):
    """Regression for the byte24 whole-byte truthiness bug the plan's Risks
    section names: flags 0x24 (presence+boost, active clear) must still read
    heating -- the boost-while-active-clear shape PROTOCOL.md line 409's own
    corpus records ("24 presence and boost without active at duty 0"), built
    from the real captured frame test_set_temperature_optimism_is_corrected_
    by_the_next_real_report uses (mode manual/heat, setpoint 24.0C), CRC
    recomputed with tf.build_frame over the modified duty/flags bytes rather
    than a hand-typed hex literal."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    transport.queue_line(
        "RX 1000 -44.0 60 0 E59C885DB6A1C825565F4A9C5850E47E05BCB4F086AE28F1C2BEE33F8A"
    )
    state = await _wait_for_entity_state(hass, "climate.master_bedroom_heater", "heat")
    assert state.attributes["hvac_action"] == HVACAction.HEATING


async def test_hvac_action_idle_with_locked_only(hass, monkeypatch, enable_custom_integrations):
    """Regression for the same bug's other direction: flags 0x02 (locked
    alone, driven on air by BA, 2026-09-13 X-19 proof) must read idle, not
    heating -- a set bit other than active/boost must never flip this."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    transport.queue_line(
        "RX 1000 -44.0 60 0 E59C885DB6A1C825565F4A9C5850E47E05BCB4F086AE28F1E4BEE30BEC"
    )
    state = await _wait_for_entity_state(hass, "climate.master_bedroom_heater", "heat")
    assert state.attributes["hvac_action"] == HVACAction.IDLE


async def test_hvac_action_heating_with_mode_off_and_boost(
    hass, monkeypatch, enable_custom_integrations
):
    """Regression for verify-04.log (2026-09-13 X-19): mode off with boost
    running (byte 24 bit 5 set) still energises the element, so hvac_action
    must read heating, not off -- the boost check has to run before the
    mode-off short-circuit. Same worked frame as test_hvac_action_heating_
    with_boost_only_active_clear with mode set to 0x04 (off) and flags to
    0x20 (boost alone), CRC recomputed with tf.build_frame."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    transport.queue_line(
        "RX 1000 -44.0 60 0 E59C885DB6A1C825565F4A9C5850E47E05BAB4F086AE28F1C6BEE390F5"
    )
    # Setup's own default status reply is already mode off with hvac_action
    # off (conftest.py _STATUS_REPLY_PAYLOAD), so waiting for state "off"
    # would return before this frame is even processed; wait on hvac_action
    # itself flipping to heating instead.
    state = await _wait_for_attribute(
        hass, "climate.master_bedroom_heater", "hvac_action", HVACAction.HEATING
    )
    assert state.state == "off"
    assert state.attributes["icon"] == "mdi:radiator"


async def test_hvac_action_off_with_mode_off_and_no_boost(
    hass, monkeypatch, enable_custom_integrations
):
    """Same frame as above with flags 0x00 (no boost): mode off alone must
    still read off."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    transport.queue_line(
        "RX 1000 -44.0 60 0 E59C885DB6A1C825565F4A9C5850E47E05BAB4F086AE28F1E6BEE31633"
    )
    # Setup's own default status reply is already mode off with no boost, so
    # wait on this frame's distinct room temperature (23.5C, vs the default
    # status reply's 24.7C) to know it has actually been processed.
    state = await _wait_for_attribute(
        hass, "climate.master_bedroom_heater", "current_temperature", 23.5
    )
    assert state.state == "off"
    assert state.attributes["hvac_action"] == HVACAction.OFF
    assert state.attributes["icon"] == "mdi:radiator-off"


async def test_boost_attributes_absent_when_boost_is_not_active(
    hass, monkeypatch, enable_custom_integrations
):
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    state = hass.states.get("climate.master_bedroom_heater")
    assert "boost_end_day" not in state.attributes
    assert "boost_end_min" not in state.attributes
    assert "boost_temperature" not in state.attributes


async def test_boost_attributes_present_only_while_boost_active(
    hass, monkeypatch, enable_custom_integrations
):
    """Real captured E3 frame (tests/test_heater.py's own e3 worked example):
    flags 0x24 (presence+boost), boost tail Sunday 19:01. boost_temperature
    comes from setup's own F3 DA read, not from this E3 (FakeCulTransport's
    fixed DB reply, conftest.py)."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    transport.queue_line(
        "RX 1000 -44.0 60 0 E39C885DB6A1C825565F4A9C5850E47E05BAB4F587AD3BF1C286E341886B6A"
    )
    state = await _wait_for_attribute(hass, "climate.master_bedroom_heater", "boost_end_min", 1141)
    assert state.attributes["boost_end_day"] == 0
    assert state.attributes["boost_temperature"] == 21.0


async def test_set_temperature_is_optimistic_before_any_correction(
    hass, monkeypatch, enable_custom_integrations
):
    """docs/80-handover.md list C item 10 step 2: right after the
    set_temperature service call returns, the entity already shows the
    commanded value, not merely whatever the coordinator's own post-command
    status request happened to read back. FakeCulTransport's fixed E6 reply
    always reports setpoint 25.0C (see test_climate_attribute_set_matches_
    cloud_shape and conftest.py's own _STATUS_REPLY_PAYLOAD), so a state of
    25.5C here can only be the optimistic value, not the status request's own
    (already-landed) real one."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": "climate.master_bedroom_heater", ATTR_TEMPERATURE: 25.5},
        blocking=True,
    )

    state = hass.states.get("climate.master_bedroom_heater")
    assert state.attributes[ATTR_TEMPERATURE] == 25.5


async def test_set_temperature_optimism_is_corrected_by_the_next_real_report(
    hass, monkeypatch, enable_custom_integrations
):
    """docs/80-handover.md list C item 10 step 2 acceptance criterion: the
    optimistic value "either matches or is corrected by the E6 or E5 that
    follows". A genuine E5 report queued after the command (setpoint 24.0C,
    the same worked frame test_set_preset_mode_override_sends_f1_b4_03_frame
    uses) must win over the stale 25.5C optimistic guess once it arrives."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": "climate.master_bedroom_heater", ATTR_TEMPERATURE: 25.5},
        blocking=True,
    )
    assert hass.states.get("climate.master_bedroom_heater").attributes[ATTR_TEMPERATURE] == 25.5

    transport.queue_line(
        "RX 2000 -44.0 60 0 E59C885DB6A1C825565F4A9C5850E47E05BCB4F086AE28C3E7BEE3933D"
    )
    state = await _wait_for_attribute(
        hass, "climate.master_bedroom_heater", ATTR_TEMPERATURE, 24.0
    )
    assert state.attributes[ATTR_TEMPERATURE] == 24.0


async def test_failed_set_temperature_does_not_strand_optimistic_state(
    hass, monkeypatch, enable_custom_integrations
):
    """docs/80-handover.md list C item 10: "a command that fails must not
    leave the entity showing a value the heater never accepted". Setup's own
    first refresh already leaves a real setpoint of 25.0C on record (the
    fixed E6 reply); a command the heater never acks must not overwrite the
    displayed value with the one that was merely asked for."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    assert coordinator.heaters[MASTER_BEDROOM].last_snapshot.setpoint_c == 25.0

    original_send_frame = coordinator._nanocul.send_frame

    def _refuse(dst, air_bytes, *args, **kwargs):
        if dst == MASTER_BEDROOM:
            from termoweb_local.nanocul import AckResult

            return AckResult(ok=False, attempts=1)
        return original_send_frame(dst, air_bytes, *args, **kwargs)

    monkeypatch.setattr(coordinator._nanocul, "send_frame", _refuse)

    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": "climate.master_bedroom_heater", ATTR_TEMPERATURE: 25.5},
        blocking=True,
    )

    state = hass.states.get("climate.master_bedroom_heater")
    assert state.attributes[ATTR_TEMPERATURE] == 25.0


async def test_climate_survives_a_heater_missing_from_coordinator_heaters(
    hass, monkeypatch, enable_custom_integrations
):
    """TermowebLocalClimate._heater had the same bare-index bug sensor.py's
    _HeaterSensorBase._heater was guarded against in commit 4387163: found
    when a regression test for that fix called
    coordinator.async_update_listeners() (which fans out to every subscribed
    entity, this climate entity included) and got KeyError straight out of
    climate.py, once coordinator.heaters no longer had the node. Reproduced
    the same way here rather than through async_remove_config_entry_device,
    whose own ordering fix (__init__.py, commit 70c774c) now closes the one
    window that made this reachable in practice: this guards the entity
    itself, whatever the ordering elsewhere leaves coordinator.heaters
    holding.

    hvac_mode returning None here would be wrong: ClimateEntity.state (HA's
    .venv-hacs pin) derives from `self.hvac_mode` and treats None as "no
    state" the same way a sensor's native_value of None does, but this file
    already has its own convention for "no data yet" on hvac_mode -- a None
    snapshot falls back to HVACMode.OFF (see hvac_mode above) -- so a missing
    heater is folded into that same existing branch rather than adding a
    second, None-returning convention next to it."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    # Real worked frame (test_hvac_action_reflects_heating_flag_candidate
    # above), so there is a live snapshot to lose, not just the setup-time
    # default.
    transport.queue_line(
        "RX 1000 -44.0 60 0 E59C885DB6A1C825565F4A9C5850E47E05BDB4F386AD13A1E786E3227A"
    )
    state = await _wait_for_entity_state(hass, "climate.master_bedroom_heater", "heat")
    assert state.attributes["current_temperature"] == 23.2

    del coordinator.heaters[MASTER_BEDROOM]
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    state = hass.states.get("climate.master_bedroom_heater")
    assert state is not None
    assert state.state == "off"
    assert state.attributes["current_temperature"] is None
    assert state.attributes[ATTR_TEMPERATURE] is None
    # hvac_action is only added to state_attributes when truthy (climate/
    # __init__.py's own `if hvac_action := self.hvac_action:`), so a None
    # here means the key is absent, not present with value None.
    assert state.attributes.get("hvac_action") is None
    assert state.attributes["icon"] == "mdi:radiator-off"
    assert state.attributes["preset_mode"] == PRESET_NONE
    assert "mode_code" not in state.attributes
    assert "last_report_time" not in state.attributes
    assert "duty_byte" not in state.attributes


async def _wait_for_entity_state(hass, entity_id, expected_state, timeout=2.0, step=0.02):
    import asyncio

    elapsed = 0.0
    while elapsed < timeout:
        state = hass.states.get(entity_id)
        if state is not None and state.state == expected_state:
            return state
        await asyncio.sleep(step)
        elapsed += step
    raise AssertionError(
        f"{entity_id} did not reach state {expected_state!r} before timeout "
        f"(last seen: {hass.states.get(entity_id)})"
    )


async def _wait_for_attribute(hass, entity_id, attribute, expected_value, timeout=2.0, step=0.02):
    import asyncio

    elapsed = 0.0
    while elapsed < timeout:
        state = hass.states.get(entity_id)
        if state is not None and state.attributes.get(attribute) == expected_value:
            return state
        await asyncio.sleep(step)
        elapsed += step
    raise AssertionError(
        f"{entity_id}.{attribute} did not reach {expected_value!r} before timeout "
        f"(last seen: {hass.states.get(entity_id)})"
    )

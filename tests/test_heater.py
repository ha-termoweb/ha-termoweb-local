"""HeaterSnapshot decode, checked against frames actually captured in
docs/captures/2026-09-06-phase3/nano-rx-101909.log (independently re-decoded in this
test from the raw on-air hex, not copied from notes.md) rather than trusted by
assertion. See docs/captures/2026-09-06-phase3/notes.md for the ha_trigger.py
timeline these frames correspond to."""
from termoweb_local import frame as tf
from termoweb_local import network as network_module
from termoweb_local.heater import (
    Heater,
    HeaterSnapshot,
    LinkState,
    decode_energy_wh,
    decode_measured_power_w,
)


def _decode(air_hex):
    return tf.parse_frame(bytes.fromhex(air_hex))


def test_snapshot_from_initial_off_report():
    """10:19:25.420, master bedroom (node 4) at power-up: mode off, room 23.1C,
    setpoint 25.0C, matching notes.md's stated start state."""
    parsed = _decode("E59C885DB6A1C825565F4A9C5850E47E05BAB4FC84AE07F1E686E38616")
    snap = HeaterSnapshot.from_frame(parsed, received_at=0.0)
    assert snap.node_id == 0x04
    assert snap.mode_code == 0x04
    assert snap.mode == "off"
    assert snap.room_temp_c == 23.1
    assert snap.setpoint_c == 25.0


def test_snapshot_after_mode_auto_command():
    """10:20:46.572 F2 B4 01 (mode auto) to node 4; the next report at 10:20:49.072
    shows mode code 01, confirming F2 B4 01 = mode auto (docs/90-phase3-plan.md P2
    task brief, item confirmed against this log rather than assumed)."""
    parsed = _decode("E59C885DB6A1C825565F4A9C5850E47E05BFB4FCB8AD13F1E686E30C3C")
    snap = HeaterSnapshot.from_frame(parsed, received_at=0.0)
    assert snap.mode_code == 0x01
    assert snap.mode == "auto"


def test_snapshot_after_temporary_override_command():
    """10:21:46.876 F1 B4 03 30 (override setpoint 24.0C) to node 4; the next report
    at 10:21:51.757 shows mode code 03 and setpoint 24.0C."""
    parsed = _decode("E59C885DB6A1C825565F4A9C5850E47E05BDB4F386AD13A1E786E3227A")
    snap = HeaterSnapshot.from_frame(parsed, received_at=0.0)
    assert snap.mode_code == 0x03
    assert snap.mode == "override"
    assert snap.setpoint_c == 24.0
    # byte 23 duty candidate and byte 24 heating-flag candidate, unproven but observed
    assert snap.duty_candidate == 0x50
    assert snap.heating_flag_candidate == 0x01


def test_snapshot_after_mode_off_command():
    """10:23:47.581 F2 B4 04 (mode off) to node 4; the next report shows mode code 04
    and the duty/heating-flag candidates back at 0."""
    parsed = _decode("E59C885DB6A1C825565F4A9C5850E47E05BAB4F384AD11F1E686E358EB")
    snap = HeaterSnapshot.from_frame(parsed, received_at=0.0)
    assert snap.mode_code == 0x04
    assert snap.mode == "off"
    assert snap.duty_candidate == 0x00
    assert snap.heating_flag_candidate == 0x00


def test_rejects_wrong_length_payload():
    parsed = _decode("F19C8858B3A1CD20575E4B9CBAEBD9F6E5")  # F1, not E5
    import pytest

    with pytest.raises(ValueError):
        HeaterSnapshot.from_frame(parsed)


def test_heater_link_state_derived_from_cadence_and_retries_only():
    heater = Heater(node_id=0x04)
    assert heater.link_state(now=0.0) is LinkState.STALE  # never reported yet

    snap = HeaterSnapshot(
        node_id=0x04,
        mode_code=0x04,
        room_temp_c=23.1,
        setpoint_c=25.0,
        identifier=b"\x56\xB9\x0E\x2E\x2F",
        measured_power_w=750.0,
        duty_candidate=0x00,
        heating_flag_candidate=0x00,
        raw_byte25=0x1C,
        raw_byte26=0x00,
        received_at=1000.0,
    )
    heater.record_snapshot(snap)
    assert heater.link_state(now=1010.0) is LinkState.OK  # 10s later, idle cadence 300s
    assert heater.link_state(now=1000.0 + 700.0) is LinkState.STALE  # > 2x idle period

    heater.record_snapshot(snap)
    for _ in range(3):
        heater.record_retry()
    assert heater.link_state(now=1010.0) is LinkState.LOST

    heater.record_ack()
    assert heater.retry_count == 0


def _snapshot_with_flags(heating_flag_candidate):
    return HeaterSnapshot(
        node_id=0x04,
        mode_code=0x04,
        room_temp_c=23.1,
        setpoint_c=25.0,
        identifier=b"\x56\xB9\x0E\x2E\x2F",
        measured_power_w=750.0,
        duty_candidate=0x00,
        heating_flag_candidate=heating_flag_candidate,
        raw_byte25=0x1C,
        raw_byte26=0x00,
        received_at=1000.0,
    )


def test_byte24_flag_properties_decode_boost_alone():
    """Byte 24 bit 5, driven on air by D2 (2026-09-13 X-19 proof, PROTOCOL.md
    line 409): boost reads True and every other flag reads False, not the
    whole-byte truthiness climate.py's own pre-fix hvac_action used."""
    snap = _snapshot_with_flags(0x20)
    assert snap.boost is True
    assert snap.active is False
    assert snap.locked is False
    assert snap.presence is False
    assert snap.window_open is False
    assert snap.true_radiant_active is False
    assert snap.easy is False
    assert snap.runback is False


def test_byte24_flag_properties_locked_alone():
    """Byte 24 bit 1, driven on air by BA (2026-09-13 X-19 proof): locked
    alone must read active False, the regression the plan's Risks section
    names for climate.py's own hvac_action fix."""
    snap = _snapshot_with_flags(0x02)
    assert snap.locked is True
    assert snap.active is False
    assert snap.boost is False


def test_byte24_flag_properties_runback_and_boost_together():
    """The captured 0xA0 flags word: runback and boost together, active and
    presence both clear (2026-09-12 Runback session, PROTOCOL.md line 409:
    "the two are not independent on this firmware")."""
    snap = _snapshot_with_flags(0xA0)
    assert snap.runback is True
    assert snap.boost is True
    assert snap.active is False
    assert snap.presence is False


def test_byte24_flag_properties_none_when_candidate_is_none():
    """The guard branch: every property reads None rather than False when
    heating_flag_candidate itself is None, consistent with the field's own
    type even though a real decode never leaves it unset."""
    snap = _snapshot_with_flags(None)
    assert snap.active is None
    assert snap.boost is None
    assert snap.runback is None


# Real E5 reports, one per heater, taken straight off the wire and re-decoded
# here rather than trusted: docs/captures/2026-09-06-phase3/nano-rx-101909.log
# (nodes 02 and 04) and docs/captures/2026-09-05-nano-rx/
# nano-rx-869.54M-v2-213452.log (node 03). Nameplates are 1800 W for node 02
# and 750 W for nodes 03 and 04 (docs/00-hardware-inventory.md); every reading
# sits a few percent above nameplate, consistent with mains above 230 V.
E5_MEASURED_POWER_FRAMES = [
    ("E59C885BB6A1CE25565F4A9C5850E47E05BCB4F89FF87CF1E684E38282", 0x02, 1846.5),
    ("E59C885AB6A1CF25565F4A9C5850E47A05BCB4C29FAEE7F1E68DE3379B", 0x03, 786.6),
    ("E59C885DB6A1C825565F4A9C5850E47E05BAB4F384AD11F1E686E358EB", 0x04, 750.0),
]


def test_measured_power_decoded_per_heater_from_real_e5_reports():
    """E5 logical bytes 21-22, big-endian deciwatts: the heater's own measured
    full-load power. Node 02 reads 2.4 times what nodes 03 and 04 read, the
    same ratio as their 1800 W and 750 W nameplates."""
    for air_hex, node_id, watts in E5_MEASURED_POWER_FRAMES:
        snap = HeaterSnapshot.from_frame(_decode(air_hex), received_at=0.0)
        assert snap.node_id == node_id
        assert snap.measured_power_w == watts


def test_measured_power_from_e6_matches_the_same_heater_own_e5_value():
    """E6 drops E5's leading marker byte, so the field sits at logical bytes
    20-21. 14:12:25.766 node 02 and 16:52:07.877 node 03, both F3 B8 replies in
    docs/captures/2026-09-06-phase3/: each reads exactly what that heater's own
    E5 reports carry."""
    e6_02 = _decode("E69C885BB6A1CE25565F4A9CB7E7C47F2EBE5432FE915DF1F89A3003")
    e6_03 = _decode("E69C885AB6A1CF25565F4A9CB7E7C07F2EBE5031A80A5DF1FB9AD8D3")
    assert HeaterSnapshot.from_e6(e6_02, received_at=0.0).measured_power_w == 1846.5
    assert HeaterSnapshot.from_e6(e6_03, received_at=0.0).measured_power_w == 786.6


def test_measured_power_from_e3_matches_that_heater_e5_values():
    """E3 carries E5's fields in E5's own positions as far as byte 22
    (PROTOCOL.md 5.10). 2026-09-06 17:01:10.576, node 04: 752.6 W, a value that
    heater's own E5 reports also carry."""
    e3 = _decode("E39C885DB6A1C825565F4A9C5850E47E05BAB4F587AD3BF1C286E341886B6A")
    assert decode_measured_power_w(e3.payload) == 752.6


def test_snapshot_from_e3_decodes_the_boost_tail():
    """PROTOCOL.md 5.10's own worked example, node 04: mode off, room 23.8C,
    setpoint 24.5C, 752.6 W, boost ending Sunday 19:01 (tail `04 75`)."""
    e3 = _decode("E39C885DB6A1C825565F4A9C5850E47E05BAB4F587AD3BF1C286E341886B6A")
    snap = HeaterSnapshot.from_frame(e3, received_at=0.0)
    assert snap.mode_code == 0x04
    assert snap.room_temp_c == 23.8
    assert snap.setpoint_c == 24.5
    assert snap.measured_power_w == 752.6
    assert (snap.boost_end_day, snap.boost_end_min) == (0, 1141)  # Sunday 19:01


def _e4_frame():
    """Node 04, 2026-09-12 19:16-19:17 local, Runback Config ON with Max Temp
    26.0 (docs/captures/2026-09-12-schedule/notes.md "Max Temp found, and a new
    status reply class E4 under Runback"): every F3 B8 in that window came back
    as this 16-byte E4 instead of the ordinary 14-byte E6. No raw on-air E4
    frame was captured (Route A only), so this rebuilds one from the logged
    application payload with the same station/node ids and hop count every
    other reply in that capture used."""
    payload = bytes.fromhex("b90e24340200ec341daf00a01d0064a3")
    air = tf.build_frame(0x04, 0x01, payload, hops=(1, 1, 1))
    return tf.parse_frame(air)


def test_snapshot_from_e4_matches_the_runback_capture():
    """The captured E4 payload decoded field for field: anti-frost 7.0, eco
    18.0, byte 16 (comfort/Max Temp under Runback) 26.0, mode 2, room 23.6,
    setpoint 26.0, 759.9 W, duty 0, flags runback+boost, pcb 29, error 0."""
    snap = HeaterSnapshot.from_e6(_e4_frame(), received_at=0.0)
    assert snap.anti_frost_c == 7.0
    assert snap.eco_c == 18.0
    assert snap.comfort_c == 26.0
    assert snap.mode_code == 2
    assert snap.room_temp_c == 23.6
    assert snap.setpoint_c == 26.0
    assert snap.measured_power_w == 759.9
    assert snap.duty_candidate == 0
    assert snap.heating_flag_candidate == 0xA0
    assert bool(snap.heating_flag_candidate & 0x80)  # runback
    assert bool(snap.heating_flag_candidate & 0x20)  # boost
    assert snap.raw_byte25 == 29  # pcb_temp
    assert snap.raw_byte26 == 0  # error_code
    assert (snap.boost_end_day, snap.boost_end_min) == (6, 1187)  # Saturday 19:47


def test_e4_rejected_by_from_frame_and_e3_rejected_by_from_e6():
    """E4 has no `56` marker (it is E6's own shape, PROTOCOL.md 5.10-style),
    and E3 has one (it is E5's own shape): each belongs to exactly one of the
    two decode paths, not both."""
    import pytest

    with pytest.raises(ValueError):
        HeaterSnapshot.from_frame(_e4_frame())
    e3 = _decode("E39C885DB6A1C825565F4A9C5850E47E05BAB4F587AD3BF1C286E341886B6A")
    with pytest.raises(ValueError):
        HeaterSnapshot.from_e6(e3)


def test_measured_power_absent_from_e2():
    """E2's own 18-byte payload has no such field (PROTOCOL.md 5.6), so the
    length-keyed offset table declines it rather than reading a wrong offset.
    Node 04's E2, docs/captures/2026-09-05-nano-rx/registration-burst.md."""
    e2_payload = bytes.fromhex("56DB002E2A032F0100000300000000003106")
    assert decode_measured_power_w(e2_payload) is None


def test_measured_power_matches_the_live_integration_reports():
    """The same three payloads the running integration logged on 2026-09-09
    (docs/captures/2026-09-09-pairing/reset1.log, "rx #N: class=e5 ...
    payload=..."), pinning each heater's value by number rather than by a
    rebuild of this module's own output."""
    live = [
        ("56b90e2e2f0400d92948210000 1d00", 1846.5),
        ("d0b90e2a2f0400da2a1eba0000 1c00", 786.6),
        ("56b90e2e2f0400da321d1f0000 1b00", 745.5),
    ]
    for payload_hex, watts in live:
        assert decode_measured_power_w(bytes.fromhex(payload_hex.replace(" ", ""))) == watts


# The real EF energy replies, straight off the wire and re-decoded here rather
# than trusted. The whole capture corpus holds 12 EF frames, one per node across
# four hourly sweeps in docs/captures/2026-09-06-phase3/nano-rx-123955.log
# (13:00), nano-rx-145636.log (15:00) and nano-rx-162127.log (17:00 and 18:00);
# each is the reply to an F3 BC to that same node 41 to 58 ms earlier. Nodes 02
# and 03 never heated that day, so all four of each are byte-identical and one
# row covers them here. Node 04's counter moves, so its own sweeps are listed
# individually.
EF_ENERGY_FRAMES = [
    ("EF9C885BB6A1CE25565F4A9CB3E9F2E8034AD8", 0x02, 1620009),  # 13:00:01
    ("EF9C885AB6A1CF25565F4A9CB3E9CBEB2962BF", 0x03, 2210563),  # 13:00:01
    ("EF9C885DB6A1C825565F4A9CB3E9FA531F08A0", 0x04, 1049397),  # 13:00:01
    ("EF9C885DB6A1C825565F4A9CB3E9FA541F9137", 0x04, 1049653),  # 15:00:02
    ("EF9C885DB6A1C825565F4A9CB3E9FA541F9137", 0x04, 1049653),  # 17:00:00
    ("EF9C885DB6A1C825565F4A9CB3E9FA541350BB", 0x04, 1049657),  # 18:00:00
]


def test_energy_counter_decoded_per_heater_from_real_ef_replies():
    """EF payload byte 0 is the `BD` opcode byte and bytes 1-4 are a 32-bit
    big-endian cumulative watt-hour counter, pinned here by the number each
    captured frame actually carries."""
    for air_hex, node_id, watt_hours in EF_ENERGY_FRAMES:
        parsed = _decode(air_hex)
        assert parsed.src == node_id
        assert parsed.payload[0] == 0xBD
        assert decode_energy_wh(parsed.payload) == watt_hours


def test_energy_counter_deltas_match_the_heating_seen_in_the_same_window():
    """Node 04's 15:00 and 17:00 counters are equal, over a window in which its
    own E5 reports carry duty 0 throughout; the 17:00 to 18:00 delta is 4 Wh,
    which at that heater's own measured full-load power is about 19 s of element
    time, matching the brief pulses its reports show in that hour."""
    at_1500 = decode_energy_wh(_decode("EF9C885DB6A1C825565F4A9CB3E9FA541F9137").payload)
    at_1700 = decode_energy_wh(_decode("EF9C885DB6A1C825565F4A9CB3E9FA541F9137").payload)
    at_1800 = decode_energy_wh(_decode("EF9C885DB6A1C825565F4A9CB3E9FA541350BB").payload)
    assert at_1700 - at_1500 == 0
    assert at_1800 - at_1700 == 4


def test_energy_counter_is_monotonic_across_two_independent_sessions():
    """Heater 04's five readings in time order. The middle one is the payload the
    2026-09-06 proof session recorded as unexplained
    (docs/captures/2026-09-06-proof/f3-and-edges-results.md section 1), captured
    by a different tool in a different session between the 13:00 and 15:00 hourly
    sweeps; read as a counter it lands exactly where it has to."""
    readings = [
        decode_energy_wh(bytes.fromhex("bd00100335")),  # 13:00 sweep
        decode_energy_wh(bytes.fromhex("bd00100416")),  # proof session F3 BC
        decode_energy_wh(bytes.fromhex("bd00100435")),  # 15:00 sweep
        decode_energy_wh(bytes.fromhex("bd00100435")),  # 17:00 sweep
        decode_energy_wh(bytes.fromhex("bd00100439")),  # 18:00 sweep
    ]
    assert readings == [1049397, 1049622, 1049653, 1049653, 1049657]
    assert readings == sorted(readings)


def test_energy_counter_absent_from_every_other_frame_class():
    """The length-keyed offset table declines any payload that is not an EF's own
    5 bytes, rather than reading a wrong offset out of an E5 or an E6."""
    e5 = _decode("E59C885DB6A1C825565F4A9C5850E47E05BAB4F384AD11F1E686E358EB")
    e6 = _decode("E69C885BB6A1CE25565F4A9CB7E7C47F2EBE5432FE915DF1F89A3003")
    assert decode_energy_wh(e5.payload) is None
    assert decode_energy_wh(e6.payload) is None


# Anti-frost/eco/comfort presets, E5 logical bytes 14-16 (PROTOCOL.md 5.4), read off
# the same real frames E5_MEASURED_POWER_FRAMES already pins by another field, so the
# worked examples double-check against a second, independently-documented source
# (PROTOCOL.md's own "56 B9 0E 2E 2F"/"56 B9 0E 2A 2F" worked examples): anti-frost
# is 7.0 C on every heater, eco and comfort differ per room.
E5_PRESET_FRAMES = [
    ("E59C885BB6A1CE25565F4A9C5850E47E05BCB4F89FF87CF1E684E38282", 0x02, 7.0, 23.0, 23.5),
    ("E59C885AB6A1CF25565F4A9C5850E47A05BCB4C29FAEE7F1E68DE3379B", 0x03, 7.0, 21.0, 23.5),
    ("E59C885DB6A1C825565F4A9C5850E47E05BAB4F384AD11F1E686E358EB", 0x04, 7.0, 23.0, 23.5),
]


def test_presets_decoded_per_heater_from_real_e5_reports():
    """E5 logical bytes 14, 15 and 16: anti-frost, eco and comfort, half-degree
    Celsius plain. Anti-frost is identical across heaters (7.0 C, matching
    PROTOCOL.md 5.4's own worked examples); eco and comfort are per-room."""
    for air_hex, node_id, anti_frost_c, eco_c, comfort_c in E5_PRESET_FRAMES:
        snap = HeaterSnapshot.from_frame(_decode(air_hex), received_at=0.0)
        assert snap.node_id == node_id
        assert snap.anti_frost_c == anti_frost_c
        assert snap.eco_c == eco_c
        assert snap.comfort_c == comfort_c


def test_preset_target_c_maps_recognised_slot_codes_to_the_matching_preset():
    """0 cold -> anti-frost, 1 night -> eco, 2 day -> comfort (docs/80-handover.md
    list C item 2; docs/20-cloud-api-summary.md's ptemp[cold, night, day] order),
    read off a real heater whose three presets differ from one another so a mixed-up
    mapping would be caught rather than accidentally matching."""
    snap = HeaterSnapshot.from_frame(
        _decode("E59C885AB6A1CF25565F4A9C5850E47A05BCB4C29FAEE7F1E68DE3379B"),
        received_at=0.0,
    )
    assert (snap.anti_frost_c, snap.eco_c, snap.comfort_c) == (7.0, 21.0, 23.5)
    assert snap.preset_target_c(0) == snap.anti_frost_c == 7.0
    assert snap.preset_target_c(1) == snap.eco_c == 21.0
    assert snap.preset_target_c(2) == snap.comfort_c == 23.5


def test_preset_target_c_degenerate_slot_codes_return_none_not_a_guess():
    """An unknown/out-of-range slot code -- the schedule sensor's own case for an
    hour the program cache could not decode, or a bare programming mistake -- must
    never fall back to a plausible-looking preset. None (a code that decoded to
    "no value", the same one network.py's own nibble/bit decoders return for an
    hour they could not read), 3 (C9's own never-observed 2-bit code), and any
    other out-of-range int are covered."""
    snap = HeaterSnapshot.from_frame(
        _decode("E59C885AB6A1CF25565F4A9C5850E47A05BCB4C29FAEE7F1E68DE3379B"),
        received_at=0.0,
    )
    for slot_code in (None, 3, -1, 4, 99):
        assert snap.preset_target_c(slot_code) is None


def test_preset_target_c_agrees_whether_the_slot_code_came_from_nibbles_or_bits():
    """The 9E/9F nibble family and C9's 2-bit family are two different on-air
    encodings of the same three schedule states (docs/80-handover.md list C item 2).
    Both, decoded through network.py's own decoders, must hand preset_target_c the
    same normalised code for the same logical hour, and therefore the same preset
    temperature -- proving the sensor genuinely does not care which encoding a given
    heater answers with."""
    snap = HeaterSnapshot.from_frame(
        _decode("E59C885AB6A1CF25565F4A9C5850E47A05BCB4C29FAEE7F1E68DE3379B"),
        received_at=0.0,
    )
    for slot_value, preset_c in ((0, snap.anti_frost_c), (1, snap.eco_c), (2, snap.comfort_c)):
        nibble_byte = network_module.PROGRAM_SLOT_NIBBLE[slot_value] << 4  # hour 0 = high nibble
        bits_byte = slot_value << 6  # hour 0 = the top 2 bits (MSB first)
        from_nibbles = network_module._decode_program_nibbles(bytes([nibble_byte]))[0]
        from_bits = network_module._decode_program_bits(bytes([bits_byte]))[0]
        assert from_nibbles == from_bits == slot_value
        assert snap.preset_target_c(from_nibbles) == snap.preset_target_c(from_bits) == preset_c


def test_heater_records_the_energy_counter_verbatim():
    """The counter is the heater's own meter: stored as given, never scaled or
    reset by this station, and independent of the snapshot cadence."""
    heater = Heater(node_id=0x04)
    assert heater.last_energy_wh is None

    heater.record_energy(1049653)
    assert heater.last_energy_wh == 1049653
    assert heater.last_snapshot is None  # an energy read is not a report

    heater.record_energy(1049657)
    assert heater.last_energy_wh == 1049657

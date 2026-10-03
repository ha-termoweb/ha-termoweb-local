"""Network command encoders, checked against the phase3 facts and, for the program
write and EB clock-sync payloads, against the actual captured frames in
docs/captures/2026-09-06-phase3/nano-rx-101909.log rather than trusted by assertion."""
import datetime as dt

import pytest
from fake_serial import FakeClock, FakeSerialTransport

from termoweb_local import frame as tf
from termoweb_local import network as network_module
from termoweb_local.nanocul import NanoCul
from termoweb_local.network import Network

STATION_ID = 0x01
HEATER_ID = 0x04


def _network():
    return Network(node_ids=(2, 3, 4), station_id=0x01)


def _network_with_transport(transport, **kwargs):
    """A Network bound to a NanoCul driven by `transport`, for the round-trip
    methods (request_status, read_program, request, wait_processed) that need
    one -- the fake-transport counterpart to test_nanocul.py's own
    _make_nanocul, with a fast retry_interval so a deliberate timeout test does
    not need real wall-clock time (FakeClock advances on every fruitless read
    instead)."""
    clock = transport.clock
    nanocul = NanoCul(
        url="fake://",
        source_id=STATION_ID,
        reset_wait=0,
        retries=1,
        retry_interval=0.05,
        transport=transport,
        clock=clock,
        sleep=clock.sleep,
    )
    net = Network(node_ids=(2, HEATER_ID), station_id=STATION_ID, nanocul=nanocul, **kwargs)
    return net, nanocul


def test_set_mode_auto_matches_captured_frame():
    """10:20:46.572: F2 B4 01 gateway (01) to node 4, mode auto (phase3 fact,
    confirmed against nano-rx-101909.log in test_heater.py)."""
    net = _network()
    air = net.set_mode(0x04, "auto")
    expected_logical = "0D 1B 30 01 04 00 01 04 00 00 00 00 B4 01 42 60".replace(" ", "")
    assert tf.descramble(air).hex().upper() == expected_logical


def test_set_mode_heat_and_off_match_protocol_md():
    net = _network()
    assert net.set_mode(0x04, "heat").hex().upper() == "F29C8858B3A1CD20575E4B9CBAEB9853"
    assert net.set_mode(0x04, "off").hex().upper() == "F29C8858B3A1CD20575E4B9CBAEDF895"


def test_set_override_matches_captured_frame():
    """10:21:46.876: F1 B4 03 30 gateway (01) to node 4, override setpoint 24.0C
    (phase3 fact)."""
    net = _network()
    air = net.set_override(0x04, 24.0)
    expected_logical = "0E 1B 30 01 04 00 01 04 00 00 00 00 B4 03 30 A5 9D".replace(" ", "")
    assert tf.descramble(air).hex().upper() == expected_logical


def test_set_setpoint_matches_protocol_md():
    net = _network()
    air = net.set_setpoint(0x04, 25.5)
    assert air.hex().upper() == "F19C8858B3A1CD20575E4B9CBAEBD9F6E5"


def test_set_setpoint_mode_byte_matches_a_frame_built_by_hand_for_every_mode():
    """set_setpoint's own mode byte must be byte-identical to a frame built
    directly from the WRITE_MARKER/mode-byte/half-degree wire values, for
    every mode string coordinator.py's async_set_setpoint passes through --
    the F1 setpoint frame coordinator.py used to build inline before it
    moved into this method."""
    net = _network()
    celsius = 21.5
    half_degrees = int(round(celsius * 2))
    mode_bytes = {
        "off": network_module.MODE_OFF,
        "heat": network_module.MODE_HEAT,
        "auto": network_module.MODE_AUTO,
    }
    for mode, mode_byte in mode_bytes.items():
        by_hand = tf.build_frame(
            net.station_id, 0x04, bytes([network_module.WRITE_MARKER, mode_byte, half_degrees])
        )
        assert net.set_setpoint(0x04, celsius, mode=mode) == by_hand

    # mode=None (the default) still matches the fixed manual/heat frame every
    # existing caller relies on.
    assert net.set_setpoint(0x04, celsius) == net.set_setpoint(0x04, celsius, mode="heat")

    with pytest.raises(ValueError):
        net.set_setpoint(0x04, celsius, mode="bogus")


def test_write_presets_matches_captured_frame():
    """23:19:25 local: F0 9C 88 58 B3 A1 CD 20 57 5E 4B 9C B8 E7 CF 7A 32 4D
    gateway (01) to node 4, preset write anti-frost 7.0/eco 18.5/comfort 21.0
    (2026-09-12 X-18 proof, docs/captures/2026-09-12-x18-s7/notes.md)."""
    net = _network()
    air = net.write_presets(0x04, 7.0, 18.5, 21.0)
    assert air.hex().upper() == "F09C8858B3A1CD20575E4B9CB8E7CF7A324D"
    expected_logical = "0F 1B 30 01 04 00 01 04 00 00 00 00 B6 0E 25 2A 18 F3".replace(" ", "")
    assert tf.descramble(air).hex().upper() == expected_logical


def test_poll_matches_protocol_md():
    net = _network()
    air = net.poll(0x04)
    assert air.hex().upper() == "F29C8858B3A1CD20575E4B9C59BCF7A0"


def test_sync_clock_matches_provenance_worked_example():
    """docs/80-handover.md / corpus-rebuild.md worked EB clock payload:
    `52 1A 09 05 06 17 30 29 03` for 2026-09-05 23:48:41 local (a Saturday)."""
    net = _network()
    when = dt.datetime(2026, 9, 5, 23, 48, 41)
    assert when.isoweekday() == 6  # Saturday, matches DOW byte 06
    air = net.sync_clock(0x02, when)
    logical = tf.descramble(air)
    payload = logical[12:21]
    assert payload.hex().upper() == "52 1A 09 05 06 17 30 29 03".replace(" ", "")


def test_sync_clock_registration_uses_51_prefix():
    net = _network()
    when = dt.datetime(2026, 9, 5, 21, 5, 30)
    air = net.sync_clock(0x02, when, registering=True)
    payload = tf.descramble(air)[12:21]
    assert payload[0] == 0x51


def _original_master_bedroom_program():
    """The real weekly program read back from HA at 10:27:28
    (docs/captures/2026-09-06-phase3/notes.md, "program read via HA after force
    refresh"): 168 hourly slot values, day-uniform (the same 24-hour pattern every
    day), so which day order the 7-way reshape below uses makes no difference to
    any byte this test checks. Reshaped into 7 lists of 24 for
    Network.write_program."""
    prog = [
        1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1,
        1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1,
        1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1,
        1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1,
        1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1,
        1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1,
        1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1,
    ]
    assert len(prog) == 168
    return [prog[i * 24 : (i + 1) * 24] for i in range(7)]


def test_write_program_matches_captured_truncated_frames():
    """The two truncated 99-byte program-write frames captured at 10:28:22 and
    10:29:03 (docs/captures/2026-09-06-phase3/nano-rx-101909.log, class=9F len=64,
    crc=n/a because the fixed 64-byte read cut the 99-byte frame short): only the
    first 64 on-air bytes -> first 64 logical bytes are recoverable. The two writes
    are the real master-bedroom program (see _original_master_bedroom_program,
    itself read back from HA and logged in notes.md), day-uniform except one
    changed hour, for the first write, then restored for the second; they differ
    only at logical byte 19.

    That changed hour sits at wire day 0, hour 12. Day order is settled
    Sunday-first as of 2026-09-12 (PROTOCOL.md 5.6/5.7), so wire day 0 is
    Sunday; at this project's Monday-first HA surface (write_program's own
    input shape) that is day index 6, and write_program's own rotation
    (X-17 day-order fix) is what brings it back to wire day 0 for the frame
    this test checks against -- the notes.md capture itself predates the
    day-order proof and called it "Monday 12:00", which this test no longer
    repeats."""
    net = _network()
    days = _original_master_bedroom_program()

    changed_days = [list(day) for day in days]
    changed_days[6][12] = 2  # Sunday (Monday-first index 6) 12:00 -> slot 2 (nibble 0xA)
    changed_air = net.write_program(0x04, changed_days)
    changed_logical = tf.descramble(changed_air)
    assert changed_logical[0] == 0x60  # length byte: 96 -> total on-air 99 bytes
    changed_truncated = changed_logical[:64]
    expected_changed = (
        "60 1B 30 01 04 00 01 04 00 00 00 00 "
        "B2 55 55 55 50 00 00 A0 00 00 00 00 "
        "55 55 55 55 50 00 00 00 00 00 00 00 "
        "55 55 55 55 50 00 00 00 00 00 00 00 "
        "55 55 55 55 50 00 00 00 00 00 00 00 "
        "55 55 55 55"
    ).replace(" ", "")
    assert changed_truncated.hex().upper() == expected_changed

    restore_air = net.write_program(0x04, days)
    restore_truncated = tf.descramble(restore_air)[:64]
    expected_restore = (
        "60 1B 30 01 04 00 01 04 00 00 00 00 "
        "B2 55 55 55 50 00 00 00 00 00 00 00 "
        "55 55 55 55 50 00 00 00 00 00 00 00 "
        "55 55 55 55 50 00 00 00 00 00 00 00 "
        "55 55 55 55 50 00 00 00 00 00 00 00 "
        "55 55 55 55"
    ).replace(" ", "")
    assert restore_truncated.hex().upper() == expected_restore
    # the two writes differ only at logical byte 19 (Monday 12:00's nibble pair)
    diffs = [i for i in range(64) if changed_truncated[i] != restore_truncated[i]]
    assert diffs == [19]


def test_write_program_rejects_wrong_shape():
    import pytest

    net = _network()
    with pytest.raises(ValueError):
        net.write_program(0x04, [[0] * 24] * 6)  # only 6 days
    with pytest.raises(ValueError):
        net.write_program(0x04, [[0] * 23] * 7)  # only 23 hours in a day
    with pytest.raises(ValueError):
        net.write_program(0x04, [[9] * 24] * 7)  # unknown slot value


def test_ack_builder_matches_worked_example():
    """docs/PROTOCOL.md section 5.5: master bedroom heater (04) acking the gateway
    (01)."""
    air = tf.build_ack(0x04, 0x01)
    assert air.hex().upper() == "FA9C885DB621FBD1"


def test_confirm_report_matches_poll_and_protocol_md():
    """confirm_report is poll's 2026-09-06 canonical name (notes.md 13:18:42Z:
    F2 57 55 is a report confirmation, not a poll); both must still build the
    identical, already-proven frame."""
    net = _network()
    expected = "F29C8858B3A1CD20575E4B9C59BCF7A0"
    assert net.confirm_report(0x04).hex().upper() == expected
    assert net.poll(0x04) == net.confirm_report(0x04)


def test_set_setpoint_rejects_out_of_range():
    """2026-09-06 proof, f3-and-edges-results.md section 2: 5.0, 30.0, 35.0 and
    26.5 were all acked and silently ignored by the heaters on this bench;
    7.0 and 26.0 were applied (setpoint-bisect.log). The *accepted* range
    Network.set_setpoint checks is nonetheless 7.0-35.0 C inclusive (owner
    direction 2026-09-06), matching the Tevolve app's own range rather than
    what these particular heaters were observed applying: the heater itself
    is what silently ignores anything above 26.0, not this check."""
    net = _network()
    with pytest.raises(ValueError):
        net.set_setpoint(0x04, 6.5)
    with pytest.raises(ValueError):
        net.set_setpoint(0x04, 35.5)
    net.set_setpoint(0x04, 7.0)  # inclusive lower bound: no raise
    net.set_setpoint(0x04, 26.5)  # ignored by the heater, but accepted here: no raise
    net.set_setpoint(0x04, 35.0)  # inclusive upper bound: no raise
    with pytest.raises(ValueError):
        net.set_override(0x04, 35.5)


def test_write_presets_accepts_heater_02_presets():
    """Heater 02's own presets (docs/captures/2026-09-12-schedule/notes.md S1,
    heater 02: anti-frost 7.0, eco 23.0, comfort 23.5) are outside heater 04's
    fixed per-preset ranges but satisfy the ordering rule, so both the
    as-read values and the owner's live edit (anti-frost 7.0 -> 10.0) must be
    accepted."""
    net = _network()
    net.write_presets(0x02, 7.0, 23.0, 23.5)
    net.write_presets(0x02, 10.0, 23.0, 23.5)


def test_write_presets_rejects_bad_order_and_non_half_degree():
    """Ordering rule: 7.0 <= anti-frost < eco < comfort <= 35.0, each a
    multiple of 0.5C."""
    net = _network()
    with pytest.raises(ValueError):
        net.write_presets(0x04, 7.0, 23.5, 23.0)  # eco >= comfort
    with pytest.raises(ValueError):
        net.write_presets(0x04, 6.5, 18.5, 21.0)  # anti-frost below floor
    with pytest.raises(ValueError):
        net.write_presets(0x04, 7.0, 18.5, 35.5)  # comfort above ceiling
    with pytest.raises(ValueError):
        net.write_presets(0x04, 7.25, 18.5, 21.0)  # not a multiple of 0.5
    with pytest.raises(ValueError):
        net.write_presets(0x04, 7.0, 18.25, 21.0)  # not a multiple of 0.5
    net.write_presets(0x04, 7.0, 7.5, 8.0)  # inclusive lower bound: no raise
    net.write_presets(0x04, 34.0, 34.5, 35.0)  # inclusive upper bound: no raise


def test_fire_and_forget_sets_flags_byte_and_skips_nothing_else():
    """2026-09-06 proof, filtering-results.md "i-flags-80-setpoint": flags 0x80
    (PROTOCOL.md section 4's flags byte, logical byte 5) suppresses the ack but
    the command is still processed and applied."""
    net = _network()
    normal = net.set_setpoint(0x04, 24.0)
    forced = net.set_setpoint(0x04, 24.0, fire_and_forget=True)
    assert tf.descramble(normal)[5] == 0x00
    assert tf.descramble(forced)[5] == 0x80
    # every other logical byte (everything but the flags byte and the CRC that
    # covers it) is unchanged
    assert tf.descramble(normal)[:5] == tf.descramble(forced)[:5]
    assert tf.descramble(normal)[6:-2] == tf.descramble(forced)[6:-2]

    forced_mode = net.set_mode(0x04, "heat", fire_and_forget=True)
    assert tf.descramble(forced_mode)[5] == 0x80
    forced_override = net.set_override(0x04, 24.0, fire_and_forget=True)
    assert tf.descramble(forced_override)[5] == 0x80


def test_request_status_decodes_e6_reply():
    """2026-09-06 proof: F3 B8 answered by an E6 whose payload is the E5 payload
    with its leading marker byte dropped. Real captured E6 frame from heater 02
    (docs/90-phase3-plan.md task brief): mode 04, room 22.4C, setpoint 20.5C."""
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    net, nanocul = _network_with_transport(transport)

    ack_air = tf.build_ack(0x02, STATION_ID)
    transport.schedule_rx(f"RX 100 -44.0 60 0 {ack_air.hex().upper()}")
    e6_air_hex = "E69C885BB6A1CE25565F4A9CB7E7C47F2EBE5432FE915DF1F89A3003"
    transport.schedule_rx(f"RX 200 -44.0 60 0 {e6_air_hex}")

    snapshot = net.request_status(0x02, timeout=1.0)

    assert snapshot is not None
    assert snapshot.node_id == 0x02
    assert snapshot.mode_code == 0x04
    assert snapshot.room_temp_c == 22.4
    assert snapshot.setpoint_c == 20.5


def test_request_status_returns_none_when_no_reply_arrives():
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    net, nanocul = _network_with_transport(transport)

    ack_air = tf.build_ack(HEATER_ID, STATION_ID)
    transport.schedule_rx(f"RX 100 -44.0 60 0 {ack_air.hex().upper()}")

    assert net.request_status(HEATER_ID, timeout=0.05) is None


def test_wait_processed_true_when_b5_55_arrives():
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    net, nanocul = _network_with_transport(transport)

    ack_air = tf.build_ack(HEATER_ID, STATION_ID)
    transport.schedule_rx(f"RX 100 -44.0 60 0 {ack_air.hex().upper()}")
    reply_air = tf.build_frame(HEATER_ID, STATION_ID, bytes([0xB5, 0x55]), hops=(1, 1, 1))
    transport.schedule_rx(f"RX 200 -44.0 60 0 {reply_air.hex().upper()}")

    setpoint_air = net.set_setpoint(HEATER_ID, 24.0)
    result = nanocul.send_frame(HEATER_ID, setpoint_air)
    assert result.ok

    assert net.wait_processed(HEATER_ID, timeout=1.0) is True


def test_wait_processed_false_on_timeout():
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    net, nanocul = _network_with_transport(transport)

    ack_air = tf.build_ack(HEATER_ID, STATION_ID)
    transport.schedule_rx(f"RX 100 -44.0 60 0 {ack_air.hex().upper()}")

    setpoint_air = net.set_setpoint(HEATER_ID, 24.0)
    result = nanocul.send_frame(HEATER_ID, setpoint_air)
    assert result.ok

    assert net.wait_processed(HEATER_ID, timeout=0.05) is False


def test_wait_processed_true_when_b7_55_arrives():
    """B7 55 is write_presets' own processed reply (2026-09-12 X-18 proof,
    docs/captures/2026-09-12-x18-s7/notes.md), the third member of
    PROCESSED_REPLY_PAYLOADS alongside B5 55/B3 55."""
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    net, nanocul = _network_with_transport(transport)

    ack_air = tf.build_ack(HEATER_ID, STATION_ID)
    transport.schedule_rx(f"RX 100 -44.0 60 0 {ack_air.hex().upper()}")
    reply_air = tf.build_frame(HEATER_ID, STATION_ID, bytes([0xB7, 0x55]), hops=(1, 1, 1))
    transport.schedule_rx(f"RX 200 -44.0 60 0 {reply_air.hex().upper()}")

    presets_air = net.write_presets(HEATER_ID, 7.0, 18.5, 21.0)
    result = nanocul.send_frame(HEATER_ID, presets_air)
    assert result.ok

    assert net.wait_processed(HEATER_ID, timeout=1.0) is True


def _program_read_reply_payload():
    """The 84-byte nibble payload a real F3 B0 read returns for the master
    bedroom heater's program after the 2026-09-06 nibble-patch-and-restore
    (notes.md line 109, program-nibble-b0-read.log): Monday given verbatim in
    notes.md ("55 55 55 50 00 00 10 00 00 00 00 55", the patch's asymmetric
    nibble -- hour 12's high nibble left at the test value 1, not a real slot --
    stored and read back unchanged), the other six days the standard uniform
    pattern also proven in test_write_program_matches_captured_truncated_frames
    (_original_master_bedroom_program(), unaffected by the patch)."""
    monday = bytes.fromhex("555555500000100000000055")
    other_day = bytes.fromhex("555555500000000000000055")
    assert len(monday) == len(other_day) == 12
    return monday + other_day * 6


def test_sync_clock_weekday_sunday_is_zero_not_isoweekday():
    """2026-09-06 phase3 capture, notes.md 16:53:30Z: the clock EB sent at
    2026-09-06 17:52:14 local (a Sunday) carried DOW byte `00`, and the
    previous day's own clock EB (a Saturday) carried `06` -- the DOW byte is
    0 = Sunday .. 6 = Saturday, not ISO weekday (which would give Sunday `7`)."""
    net = _network()
    when = dt.datetime(2026, 9, 6, 17, 52, 14)
    assert when.isoweekday() == 7  # Sunday
    air = net.sync_clock(0x02, when)
    payload = tf.descramble(air)[12:21]
    assert payload.hex().upper() == "52 1A 09 06 00 11 34 0E 03".replace(" ", "")


def test_flash_display_returns_true_on_5f_55_reply():
    """2026-09-06 P4b proof, notes.md 16:56:59Z: F2 5E 01 answered by F2 5F 55
    about 96 ms after the ack."""
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    net, nanocul = _network_with_transport(transport)

    ack_air = tf.build_ack(HEATER_ID, STATION_ID)
    transport.schedule_rx(f"RX 100 -44.0 60 0 {ack_air.hex().upper()}")
    reply_air = tf.build_frame(HEATER_ID, STATION_ID, bytes([0x5F, 0x55]), hops=(1, 1, 1))
    transport.schedule_rx(f"RX 200 -44.0 60 0 {reply_air.hex().upper()}")

    assert net.flash_display(HEATER_ID, timeout=1.0) is True

    sent_air = bytes.fromhex(transport.written[0].decode().strip()[1:])
    sent = tf.parse_frame(sent_air)
    assert sent.payload == bytes([0x5E, 0x01])


def test_flash_display_returns_false_on_timeout():
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    net, nanocul = _network_with_transport(transport)

    ack_air = tf.build_ack(HEATER_ID, STATION_ID)
    transport.schedule_rx(f"RX 100 -44.0 60 0 {ack_air.hex().upper()}")

    assert net.flash_display(HEATER_ID, timeout=0.05) is False


def _startup_sequence_replies(node_id, station_id):
    """Every reply the gateway's own captured power-up burst gets, in the
    order startup_sequence sends the requests that trigger them (2026-09-06
    phase3 capture, notes.md 16:53:30Z; reply payload bytes from PROTOCOL.md
    5.6's opcode table)."""
    raw_nibbles = _program_read_reply_payload()
    program_reply = bytes([0xB1]) + raw_nibbles
    c2_reply = bytes([0xC3, 0x55])
    # Heater 04's whole 20-byte E0 payload as captured, not the first 6 bytes:
    # startup_sequence now matches each reply on its own payload length as well
    # as its opcode+1 byte, so a stand-in of the wrong length is no longer an
    # E0 as far as the identity step is concerned.
    identity_reply = bytes.fromhex("5b7701010104a1b2c3d4e5f6071829304a5b0a1a")
    capability_reply = bytes([0xD1, 0x00, 0x05, 0x01, 0x01, 0x00, 0x27, 0x10])
    c6_reply = bytes([0xC7, 0x04, 0x00, 0x00, 0x00, 0x14, 0x00, 0x00, 0x00])
    e2_reply = bytes([0x56, 0xDB]) + bytes(16)
    return raw_nibbles, program_reply, c2_reply, identity_reply, capability_reply, c6_reply, e2_reply


def test_startup_sequence_sends_captured_burst_and_decodes_every_reply():
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    net, nanocul = _network_with_transport(transport)
    association_value = bytes.fromhex("112233445566778899")

    (
        raw_nibbles,
        program_reply,
        c2_reply,
        identity_reply,
        capability_reply,
        c6_reply,
        e2_reply,
    ) = _startup_sequence_replies(HEATER_ID, STATION_ID)

    ack_air = tf.build_ack(HEATER_ID, STATION_ID)

    def _ack():
        transport.schedule_rx(f"RX 100 -44.0 60 0 {ack_air.hex().upper()}")

    def _reply(payload):
        reply_air = tf.build_frame(HEATER_ID, STATION_ID, payload, hops=(1, 1, 1))
        transport.schedule_rx(f"RX 101 -44.0 60 0 {reply_air.hex().upper()}")

    # Scheduled in the exact order startup_sequence itself sends/waits, since
    # FakeSerialTransport hands back queued lines in insertion order: the
    # association frame (ack only, no application reply), then B0/CA 00/C2/
    # 5A/D0/C6, each ack immediately followed by its own reply, C6's own
    # reply followed by the unsolicited E2 report right behind it.
    _ack()  # association
    _ack(); _reply(program_reply)  # F3 B0
    _ack(); _reply(bytes([0xCB, 0x55]))  # F2 CA 00
    _ack(); _reply(c2_reply)  # F3 C2
    _ack(); _reply(identity_reply)  # F3 5A
    _ack(); _reply(capability_reply)  # F3 D0
    _ack(); _reply(c6_reply); _reply(e2_reply)  # F3 C6, then the E2 report

    result = net.startup_sequence(HEATER_ID, association_value=association_value, timeout=1.0)

    assert result.association_sent is True
    assert result.association_ack is True
    assert result.program_raw == raw_nibbles
    assert len(result.program) == 168
    assert result.burst_terminator_ok is True
    assert result.c2_reply_payload == c2_reply
    assert result.identity_payload == identity_reply
    assert result.capability_payload == capability_reply
    assert result.c6_reply_payload == c6_reply
    assert result.e2_payload == e2_reply

    sent_payloads = [
        tf.parse_frame(bytes.fromhex(w.decode().strip()[1:])).payload
        for w in transport.written
    ]
    assert sent_payloads == [
        association_value,
        bytes([0xB0]),
        bytes([0xCA, 0x00]),
        bytes([0xC2]),
        bytes([0x5A]),
        bytes([0xD0]),
        bytes([0xC6]),
    ]


def test_startup_sequence_completes_against_busy_printing_fake():
    """2026-09-06 live failure (HA host log 21:13:51): the F3 B0 program read's
    reply is a 99-byte frame the stick prints as a 230-character line; the very
    next request used to be written with nothing pacing it, landing while the
    stick's own uart_getc_nonblock (no interrupt ring buffer,
    firmware/termoweb_rx/main.c) was still busy printing that line, and coming
    back "TXERR empty or bad hex" (_send_tx's old behaviour: raise straight
    away). This drives the same captured burst against a fake that rejects any
    write for DRAIN_QUIET_S (30 ms) after that reply is actually read
    (fake_serial.py's busy_for), proving NanoCul's own drain-before-write
    waits out exactly that window and startup_sequence completes without ever
    needing the TXERR-retry fallback."""
    from termoweb_local.nanocul import DRAIN_QUIET_S

    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    net, nanocul = _network_with_transport(transport)
    association_value = bytes.fromhex("112233445566778899")

    (
        raw_nibbles,
        program_reply,
        c2_reply,
        identity_reply,
        capability_reply,
        c6_reply,
        e2_reply,
    ) = _startup_sequence_replies(HEATER_ID, STATION_ID)

    ack_air = tf.build_ack(HEATER_ID, STATION_ID)

    def _ack():
        transport.schedule_rx(f"RX 100 -44.0 60 0 {ack_air.hex().upper()}")

    def _reply(payload, busy_for=None):
        reply_air = tf.build_frame(HEATER_ID, STATION_ID, payload, hops=(1, 1, 1))
        transport.schedule_rx(
            f"RX 101 -44.0 60 0 {reply_air.hex().upper()}", busy_for=busy_for
        )

    _ack()  # association
    # busy_for=DRAIN_QUIET_S: once this reply is read, the transport rejects
    # any write for the next 30 ms, matching the "silent for 30 ms" bound
    # NanoCul._drain() itself waits for before writing.
    _ack(); _reply(program_reply, busy_for=DRAIN_QUIET_S)  # F3 B0
    _ack(); _reply(bytes([0xCB, 0x55]))  # F2 CA 00
    _ack(); _reply(c2_reply)  # F3 C2
    _ack(); _reply(identity_reply)  # F3 5A
    _ack(); _reply(capability_reply)  # F3 D0
    _ack(); _reply(c6_reply); _reply(e2_reply)  # F3 C6, then the E2 report

    result = net.startup_sequence(HEATER_ID, association_value=association_value, timeout=1.0)

    assert result.association_sent is True
    assert result.association_ack is True
    assert result.program_raw == raw_nibbles
    assert result.burst_terminator_ok is True
    assert result.c2_reply_payload == c2_reply
    assert result.identity_payload == identity_reply
    assert result.capability_payload == capability_reply
    assert result.c6_reply_payload == c6_reply
    assert result.e2_payload == e2_reply

    # Every T write succeeded on its first try; the busy window never
    # actually caught one, so no TXERR-triggered resend was needed.
    sent = [w for w in transport.written if w.startswith(b"T")]
    assert len(sent) == 7


def test_startup_sequence_skips_association_when_not_configured():
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    net, nanocul = _network_with_transport(transport)

    # Nothing acked or replied at all: every step should time out cleanly
    # rather than raise, and no association frame should be sent.
    result = net.startup_sequence(HEATER_ID, association_value=None, timeout=0.05)

    assert result.association_sent is False
    assert result.association_ack is False
    assert result.program is None
    assert result.program_raw is None
    assert result.burst_terminator_ok is False
    assert result.c2_reply_payload is None
    assert result.identity_payload is None
    assert result.capability_payload is None
    assert result.c6_reply_payload is None
    assert result.e2_payload is None

    sent_payloads = [
        tf.parse_frame(bytes.fromhex(w.decode().strip()[1:])).payload
        for w in transport.written
    ]
    # every step is still attempted (send_frame retries exhausted, no ack),
    # just none of them produced a reply
    assert bytes([0xB0]) in sent_payloads
    # No association frame (a 9-byte EB payload) goes out when none is configured.
    assert not any(len(payload) == 9 for payload in sent_payloads)


DISCOVERY_IDENTITY = bytes.fromhex("A1B2C3D4E5F6071829304A5B")  # 12 bytes, 2026-09-06 17:12:17Z capture


def _e7_frame(identity, dst=STATION_ID, src=0xFF):
    air = tf.build_frame(src, dst, bytes([0x77]) + identity, hops=(1, 1, 1))
    return tf.parse_frame(air)


# The gateway's own id assignment, on air, from nano-rx-162127.log at
# 17:12:17.407; logical `0c 1b 30 01 ff 00 01 ff 00 00 00 04 04`, byte 11 = 04.
GATEWAY_ASSIGNMENT_AIR = bytes.fromhex("F39C885848A1CDDB575E4B980A03E7")


def test_handle_discovery_frame_reuses_id_and_matches_worked_example():
    """2026-09-06 17:12:17Z pairing capture, notes.md: heater 04's own
    identity re-announced itself over E7 and got its own id, 04, back
    ("the same identity got the same id back"); the assignment frame the
    gateway sent to FF matches this worked example byte for byte, header
    byte 11 included."""
    net = Network(node_ids=(2, 3, 4), station_id=STATION_ID, known_identities={DISCOVERY_IDENTITY: 0x04})
    net.start_discovery()

    result = net.handle_discovery_frame(_e7_frame(DISCOVERY_IDENTITY))

    assert result is not None
    assert result.node_id == 0x04
    assert result.is_new is False
    assert result.identity == DISCOVERY_IDENTITY
    assert result.assignment_air == GATEWAY_ASSIGNMENT_AIR
    assert tf.parse_frame(result.assignment_air).logical[11] == 0x04


def test_handle_discovery_frame_assigns_lowest_free_id_for_new_identity():
    net = Network(
        node_ids=(2, 4),
        station_id=STATION_ID,
        known_identities={b"a" * 12: 2, b"b" * 12: 4},
    )
    net.start_discovery()
    new_identity = bytes(12)

    result = net.handle_discovery_frame(_e7_frame(new_identity))

    assert result.is_new is True
    assert result.node_id == 3  # lowest free id: 2 and 4 already configured heaters
    assert net.known_identities[new_identity] == 3


def test_handle_discovery_frame_refuses_a_new_identity_while_a_heater_is_unidentified():
    """2026-09-09 bench attempt 1 (PROTOCOL.md 5.9): heater 04 had been
    registered by the scan, so its identity was not recorded, and its own
    announcement was given the lowest free id, 05, which it refused. An
    identity that cannot be told apart from a heater already on the network
    gets no id at all until that heater's identity is known."""
    net = Network(node_ids=(2, 3, 4), station_id=STATION_ID)
    net.start_discovery()

    result = net.handle_discovery_frame(_e7_frame(DISCOVERY_IDENTITY))

    assert result is not None
    assert result.node_id is None
    assert result.assignment_air is None
    assert result.unidentified_node_ids == (2, 3, 4)
    assert DISCOVERY_IDENTITY not in net.known_identities


def test_handle_discovery_frame_accepts_a_swept_destination():
    """The announcement sweeps its destination across the id range about
    200 ms at a time (PROTOCOL.md 5.9), so the copy this station receives is
    usually addressed elsewhere."""
    net = Network(
        node_ids=(2, 3, 4), station_id=STATION_ID, known_identities={DISCOVERY_IDENTITY: 0x04}
    )
    net.start_discovery()

    result = net.handle_discovery_frame(_e7_frame(DISCOVERY_IDENTITY, dst=0x07))

    assert result is not None
    assert result.node_id == 0x04


def test_handle_discovery_frame_accepts_a_relayed_announcement():
    """An already-paired heater relays the announcement to the station under
    its own source id (PROTOCOL.md 5.9)."""
    net = Network(
        node_ids=(2, 3, 4), station_id=STATION_ID, known_identities={DISCOVERY_IDENTITY: 0x04}
    )
    net.start_discovery()

    result = net.handle_discovery_frame(_e7_frame(DISCOVERY_IDENTITY, src=0x02))

    assert result is not None
    assert result.node_id == 0x04


def _relayed_e7_frame(identity, relay, path, originator=0xFF):
    """An announcement that reached this station through `relay`: the link
    sender is the relay and the end-to-end path in logical bytes 6-10 still
    names the announcer as its originator, which is how heaters 02 and 03
    relayed heater 04's F4 probe on 2026-09-06 (analysis8.md section 4, byte 6
    held at the originator and byte 7 at the relay's own id). No relayed E7's
    own bytes 6-10 are in the corpus, so `path` here is constructed from that
    forwarding rule, not copied from a capture."""
    air = tf.build_frame(relay, STATION_ID, bytes([0x77]) + identity, path=path)
    assert tf.parse_frame(air).logical[6] == originator
    return tf.parse_frame(air)


def test_is_relayed_announcement_reads_the_three_real_relay_frames_as_relayed():
    """The only real relayed headers on disk (analysis8.md section 4,
    docs/captures/2026-09-06-proof/): heater 04's forwarded F1 and heaters 02
    and 03 each relaying heater 04's F4 probe. They are not E7s, so they say
    nothing about what an announcement's own path holds, but they are genuine
    captured relay headers and the predicate has to read all three the same
    way: byte 3 rewritten by the relay, byte 6 still the originator."""
    for src, dst, path in (
        (0x04, 0x01, bytes([0x01, 0x04, 0x01, 0x01, 0x01])),
        (0x02, 0x01, bytes([0x04, 0x02, 0x01, 0x01, 0x01])),
        (0x03, 0x01, bytes([0x04, 0x03, 0x01, 0x01, 0x01])),
    ):
        frame = tf.parse_frame(tf.build_frame(src, dst, b"\x00", path=path))
        assert network_module.is_relayed_announcement(frame) is True


def test_is_relayed_announcement_reads_a_direct_announcement_and_a_swept_copy_as_direct():
    """The captured direct announcement and a swept copy addressed at another
    id both carry the announcer in byte 3 and in byte 6 (docs/PROTOCOL.md 5.9),
    so neither is relayed and both keep the broadcast answer."""
    for frame in (_e7_frame(DISCOVERY_IDENTITY), _e7_frame(DISCOVERY_IDENTITY, dst=0x07)):
        assert network_module.is_relayed_announcement(frame) is False


def test_reverse_routing_path_reverses_the_captured_announcement_path():
    """FUN_400ff6c8's own worked example (analysis7.md section 6): for the
    2026-09-06 17:12:17Z announcement's buffer `FF 01 01 01 01`, the first `01`
    in indices 1 to 4 is at index 1, so the reversal is `[buf[1], buf[0]]` and
    the tail is zero-filled, giving `01 FF 00 00 00`. Note what happens to the
    padding: the received `01 01 01` becomes `00 00 00`, because the firmware
    writes zero for every index past the one it found rather than carrying the
    received padding across."""
    assert network_module.reverse_routing_path(
        bytes.fromhex("FF01010101"), STATION_ID
    ) == bytes.fromhex("01FF000000")


def test_reverse_routing_path_reverses_a_real_two_entry_relay_header():
    """`04 02 01 01 01`, an F4 route probe from heater 04 that heater 02
    relayed to the gateway at 16:07:44.846 (analysis8.md section 4,
    proof-confirm-listener-160712.log). This is a real captured relayed header,
    not an E7: it pins the shape a relayed path has (originator, relay, this
    station's id, then padding) and therefore where the firmware's scan stops,
    but the gateway never reverses this frame, so it constrains the input side
    of the rule and not the assignment it would produce."""
    assert network_module.reverse_routing_path(
        bytes.fromhex("0402010101"), STATION_ID
    ) == bytes.fromhex("0102040000")


def test_reverse_routing_path_stops_at_the_first_hop_that_is_this_station():
    """A path shorter than five entries never grows one: the reversal is
    exactly as long as the path up to this station's id, and the rest is zero.
    Three real hops here, so three bytes out and two zeros."""
    assert network_module.reverse_routing_path(
        bytes([0xFF, 0x02, 0x03, 0x01, 0x01]), STATION_ID
    ) == bytes([0x01, 0x03, 0x02, 0xFF, 0x00])


def test_reverse_routing_path_has_no_reversal_for_a_path_that_misses_this_station():
    """FUN_400ff6c8 returns 0xffffffff when indices 1 to 4 hold no `1`, and
    FUN_400ff880 then abandons the assignment rather than sending one."""
    assert network_module.reverse_routing_path(bytes.fromhex("FF07000000"), STATION_ID) is None


def test_reverse_routing_path_rejects_a_path_that_is_not_five_bytes():
    with pytest.raises(ValueError):
        network_module.reverse_routing_path(bytes.fromhex("FF0101"), STATION_ID)


def test_handle_discovery_frame_direct_announcement_is_unchanged_by_the_reversal():
    """The safety property of deriving the assignment's route from the
    announcement: for a direct announcement the derived route is the broadcast
    route, so the frame is byte for byte the one this station built before the
    reversal existed, which is itself the gateway's captured assignment
    (GATEWAY_ASSIGNMENT_AIR). A swept copy addressed at another id is answered
    with that same broadcast frame too."""
    broadcast_build = tf.build_frame(
        STATION_ID,
        network_module.DISCOVERY_BROADCAST_ID,
        bytes([0x04]),
        tag=network_module.DISCOVERY_ASSIGNMENT_TAG,
    )
    assert broadcast_build == GATEWAY_ASSIGNMENT_AIR

    for frame in (_e7_frame(DISCOVERY_IDENTITY), _e7_frame(DISCOVERY_IDENTITY, dst=0x07)):
        net = Network(
            node_ids=(2, 3, 4), station_id=STATION_ID,
            known_identities={DISCOVERY_IDENTITY: 0x04},
        )
        net.start_discovery()

        result = net.handle_discovery_frame(frame)

        assert result.assignment_air == broadcast_build
        assert result.assignment_dst == network_module.DISCOVERY_BROADCAST_ID


def test_handle_discovery_frame_answers_a_relayed_announcement_through_the_relay():
    """FIRMWARE-DERIVED EXPECTATION, NOT A CAPTURE. No relayed E7's own logical
    bytes 6-11 exist in the corpus (analysis7.md section 11 item 1, the open
    item; section 9 is the corrections applied to the earlier passes). The
    coordinator now logs every E7's whole logical frame, so a pairing run can
    close it, but no such run has happened yet: the announcement here is
    still synthesised from analysis8.md section 4's forwarding rule, and the
    expected assignment is what
    FUN_400ff6c8 plus FUN_400ff728 would produce from it, per analysis7.md
    divergence 2: from `[FF, relay, 1, ...]` the gateway builds
    `[1, relay, FF, 00, 00]` and addresses the frame at the relay. Only a tap
    capture of a real relayed announcement and of the gateway's answer to it
    can turn this into ground truth."""
    net = Network(
        node_ids=(2, 3, 4), station_id=STATION_ID,
        known_identities={DISCOVERY_IDENTITY: 0x04},
    )
    net.start_discovery()
    announcement = _relayed_e7_frame(
        DISCOVERY_IDENTITY, relay=0x02, path=bytes([0xFF, 0x02, 0x01, 0x01, 0x01])
    )

    result = net.handle_discovery_frame(announcement)

    assert result.assignment_dst == 0x02
    logical = tf.parse_frame(result.assignment_air).logical
    assert logical[:13].hex() == "0c1b30010200" "0102ff0000" "0404"
    assert logical[3:5] == bytes([STATION_ID, 0x02])  # this hop: station to relay
    assert logical[6:11] == bytes([0x01, 0x02, 0xFF, 0x00, 0x00])  # the reversed path
    assert logical[11] == network_module.DISCOVERY_ASSIGNMENT_TAG
    assert logical[12] == 0x04  # the assigned id, the one-byte payload


def test_a_station_id_other_than_01_cannot_reverse_a_relayed_path_and_says_so():
    """FIRMWARE-DERIVED SHAPE, NOT A CAPTURE (see the test above). The scan in
    reverse_routing_path looks for this station's own id in the received path,
    and a heater builds that path with the gateway's `01` in it, so a station
    configured with any other id finds nothing to match and every relayed
    announcement falls back to the broadcast assignment. The fallback is
    fail-safe but inert on that route, so the result has to carry the reason
    rather than look like an ordinary broadcast answer."""
    net = Network(
        node_ids=(2, 3, 4), station_id=0x05,
        known_identities={DISCOVERY_IDENTITY: 0x04},
    )
    net.start_discovery()
    announcement = _relayed_e7_frame(
        DISCOVERY_IDENTITY, relay=0x02, path=bytes([0xFF, 0x02, 0x01, 0x01, 0x01])
    )

    result = net.handle_discovery_frame(announcement)

    assert result.assignment_dst == network_module.DISCOVERY_BROADCAST_ID
    assert result.relay_reversal_failed is True


def test_a_broadcast_answer_that_is_not_a_reversal_failure_is_not_flagged():
    """The direct announcement, a swept copy addressed elsewhere and a relay
    that rewrote byte 6 all broadcast too, and none of them is a failure: the
    flag has to separate the inert route from the three deliberate ones, or a
    caller cannot warn about the one that matters."""
    for frame in (
        _e7_frame(DISCOVERY_IDENTITY),
        _e7_frame(DISCOVERY_IDENTITY, dst=0x07),
        _relayed_e7_frame(
            DISCOVERY_IDENTITY, relay=0x02, path=bytes([0x02, 0x01, 0x01, 0x01, 0x01]),
            originator=0x02,
        ),
    ):
        net = Network(
            node_ids=(2, 3, 4), station_id=STATION_ID,
            known_identities={DISCOVERY_IDENTITY: 0x04},
        )
        net.start_discovery()

        result = net.handle_discovery_frame(frame)

        assert result.assignment_dst == network_module.DISCOVERY_BROADCAST_ID
        assert result.relay_reversal_failed is False


def test_handle_discovery_frame_answers_a_two_hop_relay_through_its_last_hop():
    """FIRMWARE-DERIVED EXPECTATION, NOT A CAPTURE (see the test above). Two
    relays deep, the reversal names both of them and the assignment goes to the
    one that handed the announcement to this station, not to the one nearest
    the announcer."""
    net = Network(
        node_ids=(2, 3, 4), station_id=STATION_ID,
        known_identities={DISCOVERY_IDENTITY: 0x04},
    )
    net.start_discovery()
    announcement = _relayed_e7_frame(
        DISCOVERY_IDENTITY, relay=0x03, path=bytes([0xFF, 0x02, 0x03, 0x01, 0x01])
    )

    result = net.handle_discovery_frame(announcement)

    assert result.assignment_dst == 0x03
    logical = tf.parse_frame(result.assignment_air).logical
    assert logical[6:11] == bytes([0x01, 0x03, 0x02, 0xFF, 0x00])


def test_handle_discovery_frame_broadcasts_when_a_relayed_path_cannot_be_reversed():
    """A DELIBERATE DIVERGENCE, NOT THE FIRMWARE RULE. FUN_400ff880 sends
    nothing at all when the path holds no route back to this station. Answering
    on the broadcast id instead keeps the one behaviour that has actually
    assigned an id on this bench, and costs one frame that the announcer either
    hears directly or does not."""
    net = Network(
        node_ids=(2, 3, 4), station_id=STATION_ID,
        known_identities={DISCOVERY_IDENTITY: 0x04},
    )
    net.start_discovery()
    announcement = _relayed_e7_frame(
        DISCOVERY_IDENTITY, relay=0x02, path=bytes([0xFF, 0x02, 0x00, 0x00, 0x00])
    )

    result = net.handle_discovery_frame(announcement)

    assert result.assignment_dst == network_module.DISCOVERY_BROADCAST_ID
    assert result.assignment_air == GATEWAY_ASSIGNMENT_AIR


def test_handle_discovery_frame_reports_no_destination_with_no_assignment():
    net = Network(node_ids=(2, 3, 4), station_id=STATION_ID)
    net.start_discovery()

    result = net.handle_discovery_frame(_e7_frame(DISCOVERY_IDENTITY))

    assert result.assignment_air is None
    assert result.assignment_dst is None


def test_handle_discovery_frame_answers_one_copy_per_sweep_dwell():
    """One pairing press puts the sweep plus every relay of it on the air, all
    carrying the same identity; answering each one would spend the whole
    sweep transmitting into destinations the heater has already left."""
    net = Network(
        node_ids=(2, 3, 4), station_id=STATION_ID, known_identities={DISCOVERY_IDENTITY: 0x04}
    )
    net.start_discovery(now=1000.0)

    assert net.handle_discovery_frame(_e7_frame(DISCOVERY_IDENTITY), now=1000.0) is not None
    assert net.handle_discovery_frame(_e7_frame(DISCOVERY_IDENTITY, dst=0x02), now=1000.05) is None
    assert net.handle_discovery_frame(_e7_frame(DISCOVERY_IDENTITY, src=0x03), now=1000.1) is None
    assert net.handle_discovery_frame(_e7_frame(DISCOVERY_IDENTITY, dst=0x08), now=1000.3) is not None


def test_handle_discovery_frame_ignores_a_payload_that_is_not_an_identity():
    net = Network(node_ids=(), station_id=STATION_ID)
    net.start_discovery()
    air = tf.build_frame(0xFF, STATION_ID, bytes([0x56]) + bytes(12), hops=(1, 1, 1))

    assert net.handle_discovery_frame(tf.parse_frame(air)) is None


def test_lowest_free_heater_id_skips_a_reserved_id():
    """A heater whose device was just deleted is still out there on its id
    (coordinator REMOVED_HEATER_QUARANTINE_S), so that id is not free."""
    net = Network(
        node_ids=(2,), station_id=STATION_ID, known_identities={b"a" * 12: 2}
    )
    net.reserved_node_ids.add(3)
    net.start_discovery()

    result = net.handle_discovery_frame(_e7_frame(bytes(12)))

    assert result.node_id == 4


def test_identity_from_identity_reply_matches_the_e0_worked_example():
    """PROTOCOL.md 5.9: E0's payload is 5B, the E7 marker 77, 01 01 01 <id>,
    the same 12 bytes an E7 announcement carries, then 0A 1A."""
    payload = bytes.fromhex("5b7701010104a1b2c3d4e5f6071829304a5b0a1a")

    assert Network.identity_from_identity_reply(payload) == (0x04, DISCOVERY_IDENTITY)
    assert Network.identity_from_identity_reply(payload[:-1]) is None
    assert Network.identity_from_identity_reply(bytes(20)) is None


def test_identity_tails_differ_per_heater_though_the_node_marker_does_not():
    """docs/captures/2026-09-05-nano-rx/registration-burst.md: node 3's and
    node 4's E0 frames both carry the node marker `04`, so the identity has
    to be recorded under the id that was addressed, but their 12-byte tails
    differ, which is what makes the tail usable as an identity."""
    node_3 = bytes.fromhex("5b7701010104c1d2e3f4a5b6978879605a4b0a1a")
    node_4 = bytes.fromhex("5b7701010104a1b2c3d4e5f6071829304a5b0a1a")

    marker_3, identity_3 = Network.identity_from_identity_reply(node_3)
    marker_4, identity_4 = Network.identity_from_identity_reply(node_4)

    assert marker_3 == marker_4 == 0x04
    assert identity_3 != identity_4


def test_unidentified_node_ids_lists_only_heaters_with_no_identity():
    net = Network(node_ids=(2, 3, 4), station_id=STATION_ID, known_identities={b"a" * 12: 3})

    assert net.unidentified_node_ids() == (2, 4)


def test_read_identity_asks_f3_5a_and_decodes_the_e0_reply():
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    net, _nanocul = _network_with_transport(transport)
    identity_reply = bytes.fromhex("5b7701010104a1b2c3d4e5f6071829304a5b0a1a")
    transport.schedule_rx(
        f"RX 100 -44.0 60 0 {tf.build_ack(HEATER_ID, STATION_ID).hex().upper()}"
    )
    reply_air = tf.build_frame(HEATER_ID, STATION_ID, identity_reply, hops=(1, 1, 1))
    transport.schedule_rx(f"RX 101 -44.0 60 0 {reply_air.hex().upper()}")

    identity = net.read_identity(HEATER_ID, timeout=1.0)

    assert identity == DISCOVERY_IDENTITY
    sent = tf.parse_frame(bytes.fromhex(transport.written[0].decode().strip()[1:]))
    assert bytes(sent.payload) == bytes([0x5A])


def test_read_identity_returns_none_when_the_heater_does_not_answer():
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    net, _nanocul = _network_with_transport(transport)

    assert net.read_identity(HEATER_ID, timeout=0.05) is None


def test_handle_discovery_frame_skips_ids_already_known_to_other_identities():
    net = Network(
        node_ids=(),
        station_id=STATION_ID,
        known_identities={b"x" * 12: 2, b"y" * 12: 3},
    )
    net.start_discovery()

    result = net.handle_discovery_frame(_e7_frame(b"z" * 12))

    assert result.node_id == 4
    assert result.is_new is True


def test_handle_discovery_frame_returns_none_outside_the_discovery_window():
    net = Network(node_ids=(2, 3, 4), station_id=STATION_ID)
    assert net.discovery_active() is False
    assert net.handle_discovery_frame(_e7_frame(DISCOVERY_IDENTITY)) is None

    net.start_discovery(window_s=1.0, now=1000.0)
    assert net.discovery_active(now=1000.5) is True
    assert net.handle_discovery_frame(_e7_frame(DISCOVERY_IDENTITY), now=1000.5) is not None
    assert net.discovery_active(now=1002.0) is False
    assert net.handle_discovery_frame(_e7_frame(DISCOVERY_IDENTITY), now=1002.0) is None


def test_stop_discovery_closes_the_window_immediately():
    net = Network(node_ids=(2, 3, 4), station_id=STATION_ID)
    net.start_discovery()
    assert net.discovery_active() is True
    net.stop_discovery()
    assert net.discovery_active() is False


def test_read_program_decodes_84_byte_nibble_payload():
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    net, nanocul = _network_with_transport(transport)

    ack_air = tf.build_ack(HEATER_ID, STATION_ID)
    transport.schedule_rx(f"RX 100 -44.0 60 0 {ack_air.hex().upper()}")
    raw_nibbles = _program_read_reply_payload()
    reply_payload = bytes([0xB1]) + raw_nibbles
    reply_air = tf.build_frame(HEATER_ID, STATION_ID, reply_payload, hops=(1, 1, 1))
    transport.schedule_rx(f"RX 200 -44.0 60 0 {reply_air.hex().upper()}")

    result = net.read_program(HEATER_ID, timeout=1.0)

    assert result is not None
    hourly, raw = result
    assert raw == raw_nibbles
    assert len(hourly) == 168
    monday = hourly[0:24]
    assert monday[0:7] == [1, 1, 1, 1, 1, 1, 1]
    assert monday[7:12] == [0, 0, 0, 0, 0]
    assert monday[12] is None  # the patched, out-of-slot-range nibble (0x1)
    assert monday[13:22] == [0] * 9
    assert monday[22:24] == [1, 1]
    # every other day is untouched by the patch
    for day_index in range(1, 7):
        day = hourly[day_index * 24 : (day_index + 1) * 24]
        assert day == [1, 1, 1, 1, 1, 1, 1] + [0] * 15 + [1, 1]


def _c9_reply_payload():
    """Heater 02's whole 43-byte C9 payload as captured, the frame the real
    gateway's own F3 B0 to that heater was answered with 56 to 90 ms later in
    every capture that holds one (nano-rx-869.54M-v3-bench-215209.log,
    nano-rx-162127.log 16:52:05.720): `B1` then 42 bytes of four 2-bit hourly
    codes each."""
    payload = bytes.fromhex(
        "b100056aaaaa5000016aaaaa5000016aaaaa5000016aaaaa"
        "5000016aaaaa5000016aaaaa500001aaaaaa50"
    )
    assert len(payload) == 43
    return payload


def test_read_program_decodes_a_c9_reply_at_two_bits_per_hour():
    """Heaters 02 and 03 answer F3 B0 with a C9 frame, not the 9F above, and
    the two share the same `B1` first payload byte: matching on that byte
    alone put C9's 42 data bytes through the nibble decoder and produced 84
    entries instead of 168."""
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    net, nanocul = _network_with_transport(transport)

    ack_air = tf.build_ack(0x02, STATION_ID)
    transport.schedule_rx(f"RX 100 -44.0 60 0 {ack_air.hex().upper()}")
    reply_payload = _c9_reply_payload()
    reply_air = tf.build_frame(0x02, STATION_ID, reply_payload, hops=(1, 1, 1))
    transport.schedule_rx(f"RX 200 -44.0 60 0 {reply_air.hex().upper()}")

    result = net.read_program(0x02, timeout=1.0)

    assert result is not None
    hourly, raw = result
    assert raw == reply_payload[1:]
    assert len(hourly) == 168
    assert None not in hourly
    # Monday's own ramp reaches slot 2 an hour before Tuesday's and Sunday's an
    # hour after, the day-varying pattern this heater's payload carries and the
    # reason its 84-entry misdecode was visible at all.
    assert hourly[0:24] == [0] * 6 + [1] * 3 + [2] * 11 + [1] * 2 + [0] * 2
    for day_index in range(1, 6):
        day = hourly[day_index * 24 : (day_index + 1) * 24]
        assert day == [0] * 7 + [1] * 2 + [2] * 11 + [1] * 2 + [0] * 2
    assert hourly[144:168] == [0] * 7 + [1] + [2] * 12 + [1] * 2 + [0] * 2


# Heater 04's `prog` array as Home Assistant reported it (168 hourly values).
_RECORDED_PROG = [1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1]


def test_c9_two_bit_decode_matches_the_recorded_prog_reference():
    """The 2-bits-per-hour reading of a C9 payload, checked value by value
    against the one program reference this repository holds: heater 04's own
    `prog` array from Home Assistant
    (docs/captures/2026-09-06-phase3/notes.md 10:28:21.265), against that
    heater's C9 payload as captured, truncated by the firmware 3.1 fixed
    64-byte read to 28 data bytes, so 112 of the 168 hours. MSB-first matches
    every one of them; LSB-first does not, which is what rules that ordering
    out rather than mere preference."""
    prog = _RECORDED_PROG
    assert len(prog) == 168
    truncated_c9 = bytes.fromhex("55540000000555540000000555540000000555540000000555540000")

    hourly = network_module._decode_program_bits(truncated_c9)

    assert len(hourly) == 112
    assert hourly == prog[:112]
    lsb_first = [
        (byte >> shift) & 0x3 for byte in truncated_c9 for shift in (0, 2, 4, 6)
    ]
    assert lsb_first != prog[:112]


def test_read_program_ignores_a_b1_frame_of_neither_program_class():
    """A frame opening with the same `B1` byte but in neither program class
    (here a C9 payload cut short of its 43 bytes) is not a program: it is left
    on the queue for the reader loop rather than decoded as one."""
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    net, nanocul = _network_with_transport(transport)

    ack_air = tf.build_ack(0x02, STATION_ID)
    transport.schedule_rx(f"RX 100 -44.0 60 0 {ack_air.hex().upper()}")
    reply_air = tf.build_frame(0x02, STATION_ID, _c9_reply_payload()[:20], hops=(1, 1, 1))
    transport.schedule_rx(f"RX 200 -44.0 60 0 {reply_air.hex().upper()}")

    assert net.read_program(0x02, timeout=0.05) is None


def test_is_program_reply_payload_accepts_both_classes_and_nothing_else():
    assert network_module.is_program_reply_payload(_c9_reply_payload()) is True
    assert network_module.is_program_reply_payload(
        bytes([0xB1]) + _program_read_reply_payload()
    ) is True
    assert network_module.is_program_reply_payload(_c9_reply_payload()[:20]) is False
    assert network_module.is_program_reply_payload(b"") is False
    # The 9E program report's own payload, which carries the `56` report
    # marker ahead of the same `B1`, is a report and not this.
    assert network_module.is_program_reply_payload(
        bytes([0x56, 0xB1]) + _program_read_reply_payload()
    ) is False


# ---- prog_resolution: one schedule store at two resolutions
# (docs/PROTOCOL.md 5.6 "One schedule store, at two resolutions",
# docs/captures/2026-09-05-gateway-dump/analysis9.md section 8).


def test_program_resolution_comes_from_the_record_length_alone():
    """FUN_400eda30 computes `record length * 4 / 7` and stores it at its
    structure's offset 0x154 before decoding anything, so the record length is
    the only thing that picks a resolution. The 43-byte C9 form gives 24 slots a
    day and the 85-byte 9E/9F form 48; the length pair is also the frame class,
    so no other length is a program at all."""
    resolution_for = network_module.program_resolution_for_payload_len
    assert resolution_for(network_module.PROGRAM_BITS_PAYLOAD_LEN) == 24
    assert resolution_for(network_module.PROGRAM_NIBBLE_PAYLOAD_LEN) == 48
    # 42 and 84 (the data bytes without their opcode) round to 24 and 48 under
    # the same formula, and are still not program records: the class gate is
    # what keeps them out.
    for payload_len in (0, 42, 44, 84, 86, 168):
        assert resolution_for(payload_len) is None


def test_c9_record_decodes_at_24_slots_a_day():
    """The captured C9 payload, read as its own record: 24 slots a day is one
    an hour, so its slots are its hourly values and it can draw no boundary
    inside an hour."""
    record = network_module.decode_program_record(_c9_reply_payload())

    assert record.resolution == 24
    assert len(record.slots) == 24 * 7
    assert record.slots == record.hourly
    assert record.hourly_only is True


def test_9f_record_decodes_at_48_slots_a_day_firmware_derived():
    """FIRMWARE-DERIVED EXPECTATION, NOT A CAPTURE for the half-hour reading.

    The bytes are captured: the 2026-09-06 read-back in
    docs/captures/2026-09-06-proof/program-nibble-b0-read.log wrote the
    asymmetric nibble `10` into Monday hour 12 and read it back verbatim, which
    proves only that the heater stores an hour's two 2-bit halves
    independently. That those halves are the hour's two half hours is the
    gateway's rule (an 85-byte record is 48 slots a day, FUN_400eda30), not
    something any heater on this bench has been observed scheduling. No heater
    here is set on the half hour.

    So `10` decodes as first half hour 0, second half hour 1, and hour 12 has no
    single hourly value: it folds to None, which is exactly what this station
    already reported for that hour when it read the nibble as an unrecognised
    one instead."""
    record = network_module.decode_program_record(
        bytes([0xB1]) + _program_read_reply_payload()
    )

    assert record.resolution == 48
    assert len(record.slots) == 48 * 7
    monday = record.slots[0:48]
    assert monday[24:26] == [0, 1]  # hour 12's two half hours, the nibble `10`
    assert record.hourly[12] is None
    assert record.hourly_only is False
    # Every other hour of the week has both halves equal, so folding loses
    # nothing there.
    assert record.hourly[:12] == [1] * 7 + [0] * 5
    assert record.hourly[13:24] == [0] * 9 + [1, 1]


def test_read_program_records_the_replys_own_resolution():
    """A node's prog_resolution is per node and set by the reply it actually
    sends: heater 02 answers C9 (24) and heater 04 answers 9F (48), in every
    capture that holds one."""
    for node_id, reply_payload, expected in (
        (0x02, _c9_reply_payload(), 24),
        (HEATER_ID, bytes([0xB1]) + _program_read_reply_payload(), 48),
    ):
        clock = FakeClock()
        transport = FakeSerialTransport(clock)
        net, _nanocul = _network_with_transport(transport)
        assert net.program_resolution(node_id) is None

        transport.schedule_rx(
            f"RX 100 -44.0 60 0 {tf.build_ack(node_id, STATION_ID).hex().upper()}"
        )
        reply_air = tf.build_frame(node_id, STATION_ID, reply_payload, hops=(1, 1, 1))
        transport.schedule_rx(f"RX 200 -44.0 60 0 {reply_air.hex().upper()}")

        assert net.read_program(node_id, timeout=1.0) is not None
        assert net.program_resolution(node_id) == expected
        assert net.program_record(node_id).raw == reply_payload[1:]


def test_hourly_write_is_byte_identical_to_the_previous_nibble_encoding():
    """The safety property. write_program() now packs 2-bit slots rather than
    nibble pairs, so every hourly write it builds is checked here against the
    nibble encoding it replaced (_program_byte, still the encoder
    tools/proof_batch.py builds its own frames with), over every combination of
    slot values in a day and over a whole week of them. Byte for byte, on the
    air, for all 3 ** 2 nibble pairs."""
    net = _network()
    combinations = [
        [first, second] for first in (0, 1, 2) for second in (0, 1, 2)
    ]
    day = [slot for pair in combinations for slot in pair] + [0, 1, 2, 2, 1, 0]
    assert len(day) == 24
    week = [list(day) for _ in range(7)]
    week[3] = [2, 0, 1] * 8
    week[6] = [0] * 24

    # write_program's own input is Monday first; rotate to wire order (day 0
    # Sunday, PROTOCOL.md 5.6/5.7) before building the expected on-air bytes,
    # the same rotation write_program applies internally (X-17 day-order fix).
    wire_week = week[-1:] + week[:-1]
    expected_body = bytes([network_module.PROGRAM_OPCODE]) + bytes(
        network_module._program_byte(one_day[hour], one_day[hour + 1])
        for one_day in wire_week
        for hour in range(0, 24, 2)
    )

    air = net.write_program(HEATER_ID, week)

    assert tf.descramble(air)[12:-2] == expected_body
    assert len(expected_body) == network_module.PROGRAM_NIBBLE_PAYLOAD_LEN


def test_slot_decoders_are_unchanged_over_every_byte_value():
    """The other half of the safety property: the 2-bit expander and the hourly
    fold laid over it reproduce, for all 256 byte values, exactly what the two
    separate decoders they replaced produced. The half-hourly fold is written
    here as the nibble lookup it used to be, the hourly one as the plain
    MSB-first 2-bit expansion."""
    nibble_to_slot = {
        nibble: slot for slot, nibble in network_module.PROGRAM_SLOT_NIBBLE.items()
    }
    for value in range(256):
        raw = bytes([value])
        assert network_module._decode_program_nibbles(raw) == [
            nibble_to_slot.get((value >> 4) & 0xF),
            nibble_to_slot.get(value & 0xF),
        ]
        assert network_module._decode_program_bits(raw) == [
            code if code in (0, 1, 2) else None
            for code in ((value >> shift) & 0x3 for shift in (6, 4, 2, 0))
        ]


def test_write_program_accepts_a_half_hourly_week_firmware_derived():
    """FIRMWARE-DERIVED EXPECTATION, NOT A CAPTURE. No heater on this bench is
    scheduled on the half hour, so no capture holds a half-hourly week to check
    this against; the expectation is the gateway's own rule that an 85-byte
    record is 48 slots a day, four 2-bit slots to a byte MSB first, and that its
    inbound `prog` parser accepts a day array of exactly 24 or 48 values.

    A 48-value day goes on the air at that resolution and comes back off it
    unchanged, half-hour boundaries and all."""
    net = _network()
    half_hourly_day = [0] * 13 + [1] + [2] * 20 + [1] * 6 + [0] * 8
    assert len(half_hourly_day) == 48
    # Slots 12 and 13 are hour 6's two halves, so this day changes at 06:30.
    assert half_hourly_day[12] != half_hourly_day[13]
    week = [list(half_hourly_day) for _ in range(7)]

    air = net.write_program(HEATER_ID, week)
    payload = tf.descramble(air)[12:-2]

    assert payload[0] == network_module.PROGRAM_OPCODE
    assert len(payload) == network_module.PROGRAM_NIBBLE_PAYLOAD_LEN
    record = network_module.decode_program_record(bytes([0xB1]) + payload[1:])
    assert record.resolution == 48
    assert record.slots == [slot for day in week for slot in day]
    assert record.hourly_only is False
    assert record.hourly[6] is None  # the half-hour boundary has no hourly value


def test_write_program_refuses_an_hourly_week_over_a_half_hourly_schedule_firmware_derived():
    """FIRMWARE-DERIVED EXPECTATION, NOT A CAPTURE (see the test above).

    This is the silent-corruption case, and the reason to refuse rather than
    send. An hourly write expands each hour into two equal half hours, so
    sending one to a heater that holds a half-hour boundary replaces that
    boundary with a schedule the heater does not hold, and the read-back would
    then agree with the write. The station cannot express such a week through a
    24-value day, so it says so instead of writing."""
    net = _network()
    half_hourly = network_module.decode_program_record(
        bytes([0xB1]) + _program_read_reply_payload()
    )
    net._record_program(HEATER_ID, half_hourly, network_module.PROGRAM_SOURCE_READ)

    with pytest.raises(ValueError, match="half-hourly"):
        net.write_program(HEATER_ID, [[0] * 24 for _ in range(7)])

    # The same node at 48 slots a day is writable, which is what the refusal
    # points the caller at.
    assert net.write_program(HEATER_ID, [[0] * 48 for _ in range(7)]) is not None


def test_write_program_still_accepts_an_hourly_week_for_an_hourly_schedule():
    """The refusal is on content, not on resolution: a heater whose record is
    half-hourly but whose every hour has two equal halves (which is every
    program in the whole capture corpus, all three heaters here scheduling on
    the hour) takes an hourly write exactly as before, and so does a heater
    nothing has been read from yet."""
    net = _network()
    hourly_week = [[1] * 24 for _ in range(7)]
    untouched = net.write_program(HEATER_ID, hourly_week)

    restored_9f = bytes.fromhex("555555500000000000000055") * 7
    for record_payload in (bytes([0xB1]) + restored_9f, _c9_reply_payload()):
        record = network_module.decode_program_record(record_payload)
        assert record.hourly_only is True
        net._record_program(HEATER_ID, record, network_module.PROGRAM_SOURCE_READ)
        assert net.write_program(HEATER_ID, hourly_week) == untouched


def test_write_program_rejects_a_day_of_neither_resolution():
    net = _network()
    for week in ([[0] * 23] * 7, [[0] * 25] * 7, [[0] * 47] * 7, [[0] * 336] * 7):
        with pytest.raises(ValueError, match="24 or 48"):
            net.write_program(HEATER_ID, week)


# ---- X-17 day-order fix: the wire's own day order is Sunday first
# (PROTOCOL.md 5.6/5.7, corrected 2026-09-12 from the earlier Monday-first
# reading); this project's HA surface (Coordinator.get_prog, write_program's
# own input, the cloud's own `prog` array) stays Monday first, and
# rotate_week() is the one place that rotation happens.


def test_rotate_week_moves_whole_days_both_ways_at_24_and_48_slots():
    """Wire day 0 (Sunday) is HA day 6 (Monday first); every other day slides
    forward by one from wire to HA. Tagging each day with its own index (not
    its slot values) makes a day-permutation bug show up as the wrong tag
    landing at an index, not as a coincidentally-equal value."""
    for resolution in (network_module.SLOTS_PER_DAY_HOURLY, network_module.SLOTS_PER_DAY_HALF_HOURLY):
        monday_first = [day for day in range(7) for _ in range(resolution)]

        wire = network_module.rotate_week(monday_first, resolution, to_wire=True)
        assert wire[0 * resolution] == 6  # wire day 0 (Sunday) <- Monday-first day 6
        assert wire[1 * resolution] == 0  # wire day 1 (Monday) <- Monday-first day 0
        assert wire[6 * resolution] == 5  # wire day 6 (Saturday) <- Monday-first day 5

        back = network_module.rotate_week(wire, resolution, to_wire=False)
        assert back == monday_first


def test_rotate_week_rejects_a_length_that_is_not_seven_days():
    with pytest.raises(ValueError):
        network_module.rotate_week([0] * 47, 24, to_wire=True)


def test_rotate_week_matches_the_saturday_1546_regression_case():
    """docs/captures/2026-09-12-schedule/notes.md: the schedule sensor reported
    slot 271 at 15:46 local on a Saturday, one day early, because
    Coordinator.get_prog handed back the wire array (day 0 Sunday) unrotated
    and the sensor indexed it with Python's own weekday() (Monday 0), which
    landed on Friday's half-slot instead of Saturday's.

    Saturday 15:46 is half-slot 31 (hour 15's second half hour). On the wire
    (day 0 Sunday) Saturday is day 6, so the real half-slot is wire index
    6 * 48 + 31 = 319. Rotated to this project's Monday-first HA surface,
    Saturday is day 5 -- matching Python's own weekday() directly -- so the
    same half-slot lands at index 5 * 48 + 31 = 271, the very index the
    sensor reported: the number was never wrong, only which array it indexed
    into was."""
    resolution = network_module.SLOTS_PER_DAY_HALF_HOURLY
    wire = [f"day{day}-slot{slot}" for day in range(7) for slot in range(resolution)]
    assert wire[6 * resolution + 31] == "day6-slot31"  # Saturday's own half-slot, wire order

    monday_first = network_module.rotate_week(wire, resolution, to_wire=False)
    assert monday_first[5 * resolution + 31] == "day6-slot31"  # same slot, Monday-first index 5


def test_program_record_slots_monday_first_rotates_from_wire_order():
    record = network_module.decode_program_record(_c9_reply_payload())
    assert record.resolution == 24
    assert record.slots_monday_first == network_module.rotate_week(
        record.slots, record.resolution, to_wire=False
    )
    # This heater's own payload varies by day (docs/PROTOCOL.md 5.6 worked
    # example), so the rotation is not a no-op here: at least one day differs
    # between the wire array and the Monday-first one.
    assert record.slots_monday_first != record.slots


def test_write_program_round_trips_a_day_varying_week_at_48_slots():
    """set_schedule's own contract end to end: a Monday-first week in, the
    same Monday-first week back out after write_program's to-wire rotation,
    on-air encoding, decode, and ProgramRecord.slots_monday_first's from-wire
    rotation (network.rotate_week() run both ways)."""
    net = _network()
    week = [[(day * 5 + slot) % 3 for slot in range(48)] for day in range(7)]

    air = net.write_program(HEATER_ID, week)
    payload = tf.descramble(air)[12:-2]
    record = network_module.decode_program_record(bytes([0xB1]) + payload[1:])

    assert record.resolution == 48
    assert record.slots_monday_first == [slot for day in week for slot in day]


def test_write_program_round_trips_a_day_varying_week_at_24_slots():
    """Same round trip for an hourly write: write_program expands each hour
    into its own two equal native half hours before rotating and encoding
    (its own hourly branch, unchanged by this fix), so the decoded
    Monday-first week is the input with every hour doubled, not the input
    itself."""
    net = _network()
    week = [[(day * 5 + hour) % 3 for hour in range(24)] for day in range(7)]

    air = net.write_program(HEATER_ID, week)
    payload = tf.descramble(air)[12:-2]
    record = network_module.decode_program_record(bytes([0xB1]) + payload[1:])

    assert record.resolution == 48
    expected = [slot for day in week for slot in day for _ in range(2)]
    assert record.slots_monday_first == expected
    with pytest.raises(ValueError, match="24 or 48"):
        net.write_program(HEATER_ID, [[0] * 24] * 6 + [[0] * 48])


def test_scan_for_heaters_registers_only_ids_that_answer():
    """Owner direction 2026-09-06 task 2: scan_for_heaters probes every id
    in the given range with a single F3 B8 each and returns only the ones
    that answered within the timeout, unanswered ids simply absent (never
    an error).

    Scans a single id per NanoCul/transport pair, not a multi-id range: a
    FakeSerialTransport has no per-node addressing (readline() is a plain
    FIFO), so a reply pre-scheduled for a later id in the range would sit
    ahead of an earlier id's own no-answer probe in the queue, get pulled
    into that earlier probe's _pending_events as a mismatch, and then never
    leave it -- read_events() keeps re-returning it from _pending_events
    without ever calling the transport's own readline() again, so the
    FakeClock this test's timeout relies on to expire never advances
    either. Real hardware/a real clock has no such hazard (the deadline is
    wall-clock time, not gated on a fresh read); this is purely a
    FakeClock/FakeSerialTransport interaction, so the fix is to keep this
    test's own queue single-id, not to change scan_for_heaters."""
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    net, nanocul = _network_with_transport(transport)
    original_retries, original_retry_interval = nanocul.retries, nanocul.retry_interval

    ack_air = tf.build_ack(0x03, STATION_ID)
    transport.schedule_rx(f"RX 100 -44.0 60 0 {ack_air.hex().upper()}")
    e6_air_hex = "E69C885BB6A1CE25565F4A9CB7E7C47F2EBE5432FE915DF1F89A3003"
    e6_payload = tf.parse_frame(bytes.fromhex(e6_air_hex)).payload
    reply_air = tf.build_frame(0x03, STATION_ID, e6_payload, hops=(1, 1, 1))
    transport.schedule_rx(f"RX 200 -44.0 60 0 {reply_air.hex().upper()}")

    found = net.scan_for_heaters(min_id=3, max_id=3, timeout=0.05)

    assert set(found) == {0x03}
    assert found[0x03].node_id == 0x03
    # The temporary no-retry override is undone once the scan returns.
    assert nanocul.retries == original_retries
    assert nanocul.retry_interval == original_retry_interval


def test_scan_for_heaters_finds_nothing_when_no_id_answers():
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    net, nanocul = _network_with_transport(transport)

    found = net.scan_for_heaters(min_id=2, max_id=2, timeout=0.02)

    assert found == {}


def test_scan_for_heaters_needs_a_bound_nanocul():
    net = _network()
    with pytest.raises(RuntimeError):
        net.scan_for_heaters(min_id=2, max_id=3)


def test_scan_for_heaters_default_timeout_reads_the_module_global(monkeypatch):
    """timeout=None resolves SCAN_REPLY_TIMEOUT_S at call time, not as a
    bound default argument, so a caller (or a test) can shrink the module
    global and have it take effect immediately."""
    import termoweb_local.network as network_module

    monkeypatch.setattr(network_module, "SCAN_REPLY_TIMEOUT_S", 0.01)
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    net, nanocul = _network_with_transport(transport)

    net.scan_for_heaters(min_id=2, max_id=2)

    assert nanocul.retry_interval == 0.05  # restored, not left at the shrunk 0.01


# The real EF energy replies, on-air, from the 2026-09-06 phase3 hourly sweeps
# (docs/captures/2026-09-06-phase3/): node 02 at 1620009 Wh, node 03 at
# 2210563 Wh and node 04 at 1049657 Wh in the 18:00 sweep. Pinned by the number
# each frame carries, so a wrong offset fails on the value rather than on a
# rebuild of this module's own output.
EF_ENERGY_REPLIES = [
    ("EF9C885BB6A1CE25565F4A9CB3E9F2E8034AD8", 0x02, 1620009),
    ("EF9C885AB6A1CF25565F4A9CB3E9CBEB2962BF", 0x03, 2210563),
    ("EF9C885DB6A1C825565F4A9CB3E9FA541350BB", 0x04, 1049657),
]


def test_read_energy_sends_f3_bc_and_decodes_the_ef_reply():
    """F3 BC answered by an EF carrying that node's own watt-hour counter, per
    heater, checked against the real captured frames."""
    for air_hex, node_id, watt_hours in EF_ENERGY_REPLIES:
        clock = FakeClock()
        transport = FakeSerialTransport(clock)
        net, _nanocul = _network_with_transport(transport)

        ack_air = tf.build_ack(node_id, STATION_ID)
        transport.schedule_rx(f"RX 100 -44.0 60 0 {ack_air.hex().upper()}")
        transport.schedule_rx(f"RX 200 -44.0 60 0 {air_hex}")

        assert net.read_energy(node_id, timeout=1.0) == watt_hours

        sent = [w for w in transport.written if w.decode(errors="replace").startswith("T")]
        request = tf.parse_frame(bytes.fromhex(sent[0].decode().strip()[1:]))
        assert request.dst == node_id
        assert bytes(request.payload) == bytes([network_module.ENERGY_OPCODE])


def test_read_energy_returns_none_when_no_reply_arrives():
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    net, _nanocul = _network_with_transport(transport)

    ack_air = tf.build_ack(HEATER_ID, STATION_ID)
    transport.schedule_rx(f"RX 100 -44.0 60 0 {ack_air.hex().upper()}")

    assert net.read_energy(HEATER_ID, timeout=0.05) is None


def test_read_energy_ignores_a_reply_of_the_wrong_frame_class():
    """The reply is matched on payload length as well as its `BD` byte, which is
    the frame-class check (see _request_and_wait): an E6 status reply arriving in
    the window is not an energy counter."""
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    net, _nanocul = _network_with_transport(transport)

    ack_air = tf.build_ack(0x02, STATION_ID)
    transport.schedule_rx(f"RX 100 -44.0 60 0 {ack_air.hex().upper()}")
    e6_air_hex = "E69C885BB6A1CE25565F4A9CB7E7C47F2EBE5432FE915DF1F89A3003"
    transport.schedule_rx(f"RX 200 -44.0 60 0 {e6_air_hex}")

    assert net.read_energy(0x02, timeout=0.05) is None


def test_is_energy_reply_payload_accepts_only_the_ef_class():
    """An EF is routed on its own payload, and is never a report: its first byte
    is `BD`, not the `56` marker, so it takes no F2 57 55 confirmation."""
    ef_payload = tf.parse_frame(bytes.fromhex(EF_ENERGY_REPLIES[0][0])).payload
    assert network_module.is_energy_reply_payload(ef_payload) is True
    assert network_module.is_report_payload(ef_payload) is False
    assert network_module.is_program_reply_payload(ef_payload) is False

    assert network_module.is_energy_reply_payload(b"") is False
    assert network_module.is_energy_reply_payload(ef_payload[:4]) is False
    assert network_module.is_energy_reply_payload(bytes([0x56]) + ef_payload[1:]) is False


# ---- D2/D6/D4/BA toggles and C4/DA, 2026-09-13 X-19 proof (PROTOCOL.md 5.6) ----


def test_set_toggle_boost_matches_notes():
    """D2 01/D2 00: boost start/cancel, a two-byte F2-shaped payload
    (2026-09-13 X-19 proof, PROTOCOL.md 5.6). No on-air command hex was
    logged this session (only the reply timing/codes were), so the payload
    is checked at its own logical offset rather than against a hand-typed
    hex literal nothing has verified."""
    net = _network()
    on_payload = tf.descramble(net.set_toggle(HEATER_ID, network_module.TOGGLE_BOOST_OPCODE, True))[12:14]
    off_payload = tf.descramble(net.set_toggle(HEATER_ID, network_module.TOGGLE_BOOST_OPCODE, False))[12:14]
    assert on_payload.hex().upper() == "D201"
    assert off_payload.hex().upper() == "D200"


def test_set_toggle_easy_matches_notes():
    """D6 01/D6 00: EASY mode (2026-09-13 X-19 proof, PROTOCOL.md 5.6)."""
    net = _network()
    on_payload = tf.descramble(net.set_toggle(HEATER_ID, network_module.TOGGLE_EASY_OPCODE, True))[12:14]
    off_payload = tf.descramble(net.set_toggle(HEATER_ID, network_module.TOGGLE_EASY_OPCODE, False))[12:14]
    assert on_payload.hex().upper() == "D601"
    assert off_payload.hex().upper() == "D600"


def test_set_toggle_runback_matches_notes():
    """D4 01/D4 00: Runback Config (2026-09-13 X-19 proof, PROTOCOL.md 5.6)."""
    net = _network()
    on_payload = tf.descramble(net.set_toggle(HEATER_ID, network_module.TOGGLE_RUNBACK_OPCODE, True))[12:14]
    off_payload = tf.descramble(net.set_toggle(HEATER_ID, network_module.TOGGLE_RUNBACK_OPCODE, False))[12:14]
    assert on_payload.hex().upper() == "D401"
    assert off_payload.hex().upper() == "D400"


def test_set_toggle_lock_matches_notes():
    """BA 01/BA 00: keypad lock (2026-09-13 X-19 proof, PROTOCOL.md 5.6)."""
    net = _network()
    on_payload = tf.descramble(net.set_toggle(HEATER_ID, network_module.TOGGLE_LOCK_OPCODE, True))[12:14]
    off_payload = tf.descramble(net.set_toggle(HEATER_ID, network_module.TOGGLE_LOCK_OPCODE, False))[12:14]
    assert on_payload.hex().upper() == "BA01"
    assert off_payload.hex().upper() == "BA00"


def test_write_advanced_setup_matches_notes():
    """Window 2 worked examples, heater 04, 2026-09-13 X-19
    (docs/captures/2026-09-13-x19/notes.md): the all-defaults frame, each
    single-field write, and the signed-offset rows, checked against the
    logical payload bytes the table gives verbatim."""
    net = _network()

    def _payload_hex(**fields):
        merged = dict(
            control_mode=4, units=0, offset_byte=0, away_mode=0, away_offset=0,
            modified_auto_span=0, window_mode=0, true_radiant=0,
        )
        merged.update(fields)
        air = net.write_advanced_setup(HEATER_ID, **merged)
        return tf.descramble(air)[12:21].hex().upper()

    assert _payload_hex() == "C40400000000000000"
    assert _payload_hex(true_radiant=1) == "C40400000000000001"
    assert _payload_hex(window_mode=1) == "C40400000000000100"
    assert _payload_hex(units=1) == "C40401000000000000"
    assert _payload_hex(offset_byte=0x0A) == "C404000A0000000000"
    assert _payload_hex(offset_byte=0x14) == "C40400140000000000"
    assert _payload_hex(offset_byte=-10) == "C40400F60000000000"
    assert _payload_hex(offset_byte=5) == "C40400050000000000"
    assert _payload_hex(control_mode=0) == "C40000000000000000"


def test_read_advanced_record_decodes_eco_comfort_boost_temp():
    """Heater 04's own F3 DA reply (2026-09-13 X-19 proof,
    docs/captures/2026-09-13-x19/notes.md): eco 18.0C (index 2), comfort
    21.0C (index 5), boost temperature 21.0C (index 15)."""
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    net, nanocul = _network_with_transport(transport)

    ack_air = tf.build_ack(HEATER_ID, STATION_ID)
    transport.schedule_rx(f"RX 100 -44.0 60 0 {ack_air.hex().upper()}")
    reply_air = tf.build_frame(
        HEATER_ID, STATION_ID,
        bytes.fromhex("DB00242A032A0100000300000000002A06"),
        hops=(1, 1, 1),
    )
    transport.schedule_rx(f"RX 200 -44.0 60 0 {reply_air.hex().upper()}")

    record = net.read_advanced_record(HEATER_ID, timeout=1.0)

    assert record is not None
    assert record.eco_c == 18.0
    assert record.comfort_c == 21.0
    assert record.boost_temp_c == 21.0


def test_read_advanced_record_does_not_match_e3_status():
    """An E3 status reply is 17 bytes too (E5 plus the boost tail), but its
    own byte 0 is 0x56 (the E5/E2 report marker), never 0xDB, so
    _request_and_wait's own payload[0] == opcode + 1 check keeps this from
    ever misreading one as the DA record. Real captured E3 frame
    (tests/test_heater.py's own e3 worked example, node 4 to station 1)."""
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    net, nanocul = _network_with_transport(transport)

    ack_air = tf.build_ack(HEATER_ID, STATION_ID)
    transport.schedule_rx(f"RX 100 -44.0 60 0 {ack_air.hex().upper()}")
    e3_air_hex = "E39C885DB6A1C825565F4A9C5850E47E05BAB4F587AD3BF1C286E341886B6A"
    transport.schedule_rx(f"RX 200 -44.0 60 0 {e3_air_hex}")

    assert net.read_advanced_record(HEATER_ID, timeout=0.05) is None


def test_wait_processed_generalises_by_opcode():
    """A toggle's own processed reply (D3 55 for D2) is matched by
    wait_processed's expected_payloads= parameter, and a plain B5 55 -- the
    generic reply -- does not satisfy it, since D2/D6/D4/BA never answer
    with B5 55 (2026-09-13 X-19 proof, PROTOCOL.md 5.6)."""
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    net, nanocul = _network_with_transport(transport)

    ack_air = tf.build_ack(HEATER_ID, STATION_ID)
    transport.schedule_rx(f"RX 100 -44.0 60 0 {ack_air.hex().upper()}")
    reply_air = tf.build_frame(HEATER_ID, STATION_ID, bytes([0xD3, 0x55]), hops=(1, 1, 1))
    transport.schedule_rx(f"RX 200 -44.0 60 0 {reply_air.hex().upper()}")

    toggle_air = net.set_toggle(HEATER_ID, network_module.TOGGLE_BOOST_OPCODE, True)
    result = nanocul.send_frame(HEATER_ID, toggle_air)
    assert result.ok

    assert net.wait_processed(
        HEATER_ID, timeout=1.0, expected_payloads=network_module.TOGGLE_REPLY_PAYLOADS[network_module.TOGGLE_BOOST_OPCODE]
    ) is True


def test_wait_processed_generalises_by_opcode_rejects_the_generic_reply():
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    net, nanocul = _network_with_transport(transport)

    ack_air = tf.build_ack(HEATER_ID, STATION_ID)
    transport.schedule_rx(f"RX 100 -44.0 60 0 {ack_air.hex().upper()}")
    reply_air = tf.build_frame(HEATER_ID, STATION_ID, bytes([0xB5, 0x55]), hops=(1, 1, 1))
    transport.schedule_rx(f"RX 200 -44.0 60 0 {reply_air.hex().upper()}")

    toggle_air = net.set_toggle(HEATER_ID, network_module.TOGGLE_BOOST_OPCODE, True)
    result = nanocul.send_frame(HEATER_ID, toggle_air)
    assert result.ok

    assert net.wait_processed(
        HEATER_ID, timeout=0.05,
        expected_payloads=network_module.TOGGLE_REPLY_PAYLOADS[network_module.TOGGLE_BOOST_OPCODE],
    ) is False


def test_wait_advanced_setup_reply_distinguishes_accept_reject_timeout():
    """C4's own three-way reply (2026-09-13 X-19 proof, PROTOCOL.md 5.6):
    accepted (C5 55), rejected (C5 56) and no reply at all must not collapse
    onto the same bool the way wait_processed's own True/False would."""
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    net, nanocul = _network_with_transport(transport)

    ack_air = tf.build_ack(HEATER_ID, STATION_ID)

    transport.schedule_rx(f"RX 100 -44.0 60 0 {ack_air.hex().upper()}")
    accepted_air = tf.build_frame(HEATER_ID, STATION_ID, bytes([0xC5, 0x55]), hops=(1, 1, 1))
    transport.schedule_rx(f"RX 200 -44.0 60 0 {accepted_air.hex().upper()}")
    air = net.write_advanced_setup(HEATER_ID, 4, 0, 0, 0, 0, 0, 0, 0)
    assert nanocul.send_frame(HEATER_ID, air).ok
    assert net.wait_advanced_setup_reply(HEATER_ID, timeout=1.0) is True

    transport.schedule_rx(f"RX 300 -44.0 60 0 {ack_air.hex().upper()}")
    rejected_air = tf.build_frame(HEATER_ID, STATION_ID, bytes([0xC5, 0x56]), hops=(1, 1, 1))
    transport.schedule_rx(f"RX 400 -44.0 60 0 {rejected_air.hex().upper()}")
    air = net.write_advanced_setup(HEATER_ID, 4, 0, 0, 0, 0, 0, 0, 1)
    assert nanocul.send_frame(HEATER_ID, air).ok
    assert net.wait_advanced_setup_reply(HEATER_ID, timeout=1.0) is False

    transport.schedule_rx(f"RX 500 -44.0 60 0 {ack_air.hex().upper()}")
    air = net.write_advanced_setup(HEATER_ID, 4, 0, 0, 0, 0, 0, 1, 0)
    assert nanocul.send_frame(HEATER_ID, air).ok
    assert net.wait_advanced_setup_reply(HEATER_ID, timeout=0.05) is None

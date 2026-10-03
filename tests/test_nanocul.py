"""NanoCul, exercised entirely against the in-memory FakeSerialTransport (tests/
fake_serial.py) -- no serial port is opened anywhere in this suite, per
docs/90-phase3-plan.md P2's own verification line."""
import pytest
from fake_serial import FakeClock, FakeSerialTransport

from termoweb_local import frame as tf
from termoweb_local.nanocul import NanoCul, Piece, RawLine, classify_and_split, parse_rx_line

STATION_ID = 0x01
HEATER_ID = 0x04


def _make_nanocul(transport, retries=3, retry_interval=0.16, reset_wait=2.5, logging_hook=None):
    clock = transport.clock
    return NanoCul(
        url="fake://",
        source_id=STATION_ID,
        reset_wait=reset_wait,
        retries=retries,
        retry_interval=retry_interval,
        transport=transport,
        logging_hook=logging_hook,
        clock=clock,
        sleep=clock.sleep,
    )


def test_open_waits_for_reset_and_clears_input_buffer():
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    transport.schedule_rx("boot banner leftover from a previous run")
    nanocul = _make_nanocul(transport, reset_wait=2.5)
    assert clock.now == 2.5
    assert transport.rx_queue == []  # reset_input_buffer() cleared the leftover line
    nanocul.close()
    assert transport.closed


def test_send_frame_gets_acked_first_try():
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    nanocul = _make_nanocul(transport)

    ack_air = tf.build_ack(HEATER_ID, STATION_ID)
    transport.schedule_rx(f"RX 500 -44.0 60 0 {ack_air.hex().upper()}")

    setpoint_air = tf.build_frame(STATION_ID, HEATER_ID, tf.setpoint_payload(24.0))
    result = nanocul.send_frame(HEATER_ID, setpoint_air)

    assert result.ok is True
    assert result.attempts == 1
    assert result.piece.cls == 0xFA
    assert transport.written == [b"T" + setpoint_air.hex().upper().encode() + b"\n"]


def test_send_frame_retries_and_reports_failure_with_no_ack():
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    nanocul = _make_nanocul(transport, retries=3, retry_interval=0.05)

    setpoint_air = tf.build_frame(STATION_ID, HEATER_ID, tf.setpoint_payload(24.0))
    result = nanocul.send_frame(HEATER_ID, setpoint_air)

    assert result.ok is False
    assert result.attempts == 3
    sent = [w for w in transport.written if w.startswith(b"T")]
    assert len(sent) == 3
    assert all(w == sent[0] for w in sent)  # retries are byte-identical


def test_send_frame_succeeds_on_second_attempt():
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    nanocul = _make_nanocul(transport, retries=3, retry_interval=0.05)

    ack_air = tf.build_ack(HEATER_ID, STATION_ID)
    # only becomes available after the first retry window has elapsed
    transport.schedule_rx(f"RX 900 -44.0 60 0 {ack_air.hex().upper()}", at=clock.now + 0.06)

    setpoint_air = tf.build_frame(STATION_ID, HEATER_ID, tf.setpoint_payload(24.0))
    result = nanocul.send_frame(HEATER_ID, setpoint_air)

    assert result.ok is True
    assert result.attempts == 2


def test_send_frame_does_not_swallow_late_ack_arriving_before_resends_tx_confirmation():
    """2026-09-06 filtering-batch defect: a late ack for attempt 1, still in flight
    when attempt 2's resend goes out, is genuinely ahead of attempt 2's own TX
    confirmation on the wire (a real UART is a strict FIFO -- see fake_serial.py's
    write()). _send_tx used to scan for that TX confirmation with a raw readline
    loop that discarded anything else unclassified, silently losing exactly this
    ack; it must now come through _read_and_classify_one() and be queued for the
    _await_ack call right after, not thrown away."""
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    nanocul = _make_nanocul(transport, retries=3, retry_interval=0.05)

    ack_air = tf.build_ack(HEATER_ID, STATION_ID)
    # due after attempt 1's own 0.05s ack-wait window has already timed out (each
    # failed wait costs the fake's full 0.2s read_timeout, not just retry_interval,
    # since one readline() call with nothing due always advances the clock that
    # far), but before attempt 2 writes its resend -- so it sorts ahead of attempt
    # 2's own TX confirmation in the fake's queue, reproducing a real UART where
    # this ack's bytes physically arrived first.
    transport.schedule_rx(f"RX 900 -44.0 60 0 {ack_air.hex().upper()}", at=clock.now + 0.08)

    setpoint_air = tf.build_frame(STATION_ID, HEATER_ID, tf.setpoint_payload(24.0))
    result = nanocul.send_frame(HEATER_ID, setpoint_air)

    assert result.ok is True
    assert result.attempts == 2
    assert result.piece.cls == 0xFA


def test_read_events_logs_nano_rx_format_lines():
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    logged = []
    nanocul = _make_nanocul(transport, logging_hook=logged.append)

    e5_air = bytes.fromhex("E59C885DB6A1C825565F4A9C5850E47E05BAB4FC84AE07F1E686E38616")
    transport.schedule_rx(f"RX 1234 -44.0 60 0 {e5_air.hex().upper()}")

    events = nanocul.read_events()
    assert len(events) == 1
    piece = events[0]
    assert piece.verdict == "ok"
    assert piece.cls == 0xE5
    assert piece.src == HEATER_ID
    assert piece.dst == STATION_ID
    assert len(logged) == 1
    assert "T" in logged[0].split(" ", 1)[0]  # an ISO timestamp, from the fake clock
    assert " RX 1234 -44.0 60 0 " in logged[0]
    assert "crc=ok" in logged[0] and "class=E5" in logged[0]


def test_read_events_carries_the_rx_line_signal_fields_onto_the_piece():
    """The stick measures RSSI and LQI per receive event and parse_rx_line has always
    read them; before this they were parsed and dropped. The coordinator's own rx log
    line needs them, so a Piece carries them the same way it carries micros."""
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    nanocul = _make_nanocul(transport)

    e5_air = bytes.fromhex("E59C885DB6A1C825565F4A9C5850E47E05BAB4FC84AE07F1E686E38616")
    transport.schedule_rx(f"RX 1234 -56.5 41 0 {e5_air.hex().upper()}")

    piece = nanocul.read_events()[0]
    assert piece.rssi_dbm == -56.5
    assert piece.lqi == "41"


def test_every_piece_split_out_of_one_read_shares_its_signal_fields():
    """The radio measured the receive event, not each frame recovered out of it, so a
    split carries the same pair its first piece does -- exactly as micros already
    does."""
    # One raw read holding an E5 and, after a second sync word, the ack that
    # followed it: the shape the corpus records as an `E5 ...` line plus an
    # `FA ... split` line at the same micros (2026-09-06 phase3 nano-rx-101909.log
    # 10:19:25.420).
    e5_air = bytes.fromhex("E59C885DB6A1C825565F4A9C5850E47E05BAB4FC84AE07F1E686E38616")
    ack_air = tf.build_ack(HEATER_ID, STATION_ID)
    byte_strs = [f"{b:02X}" for b in e5_air + b"\x2d\xe5" + ack_air]

    pieces = classify_and_split(byte_strs, micros=1234, rssi_dbm=-44.0, lqi="60")

    assert [p.cls for p in pieces] == [0xE5, 0xFA]
    assert all(p.rssi_dbm == -44.0 and p.lqi == "60" for p in pieces)


def test_a_piece_built_without_signal_fields_defaults_them_to_none():
    """Positional construction still works, and a Piece from any future source that
    has no measurement reads as having none rather than as a zero."""
    piece = Piece(0xE5, "ok", ["E5"], 0x04, 0x01, 1234)
    assert piece.rssi_dbm is None and piece.lqi is None


def test_read_events_tolerates_unknown_non_rx_lines():
    """Firmware 3.2 adds a Q status line; the client must not choke on it."""
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    nanocul = _make_nanocul(transport)
    transport.schedule_rx("Q firmware=3.2 pa=0xC0 autoack=A0 station=01")

    events = nanocul.read_events()
    assert len(events) == 1
    assert isinstance(events[0], RawLine)
    assert events[0].text.startswith("Q firmware=3.2")


def test_send_raw_command_for_firmware_3_2_extensions():
    clock = FakeClock()
    transport = FakeSerialTransport(clock, auto_confirm_tx=False)
    nanocul = _make_nanocul(transport)
    nanocul.send_raw_command("A1")
    nanocul.send_raw_command("I04")
    assert transport.written == [b"A1\n", b"I04\n"]


def test_send_raw_command_drains_pending_input_first():
    """2026-09-06 fix (live failure, HA host log 21:13:51): before writing any
    command line, drain whatever the stick already has queued instead of
    leaving it sitting in the transport to race the write -- every classified
    event must still reach the caller, via _pending_events."""
    clock = FakeClock()
    transport = FakeSerialTransport(clock, auto_confirm_tx=False)
    nanocul = _make_nanocul(transport)
    transport.schedule_rx("Q firmware=3.2 pa=0xC0 autoack=A0 station=01")

    nanocul.send_raw_command("A1")

    assert transport.rx_queue == []  # drained ahead of the write, not left racing it
    events = nanocul.read_events()
    assert len(events) == 1
    assert isinstance(events[0], RawLine)
    assert events[0].text.startswith("Q firmware=3.2")


def test_send_frame_retries_once_on_txerr_bad_hex_then_succeeds():
    """2026-09-06 fix: firmware/termoweb_rx/main.c's uart_getc_nonblock has no
    interrupt ring buffer, so a T line racing the stick's own busy-printing of
    a long RX line can arrive mangled ("TXERR empty or bad hex"); _send_tx
    resends the identical bytes once, after waiting and draining again,
    rather than raising immediately -- and folds that resend into
    AckResult.attempts."""
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    nanocul = _make_nanocul(transport, reset_wait=0, retries=1)
    # busy_until models the stick still being busy when the first T write
    # lands (fake_serial.py's write()/schedule_rx docstrings); it clears well
    # before the retry's own write reaches the transport.
    transport.busy_until = 0.05

    setpoint_air = tf.build_frame(STATION_ID, HEATER_ID, tf.setpoint_payload(24.0))
    result = nanocul.send_frame(HEATER_ID, setpoint_air, wait_ack=False)

    assert result.ok is True
    assert result.attempts == 2  # the original send plus the one allowed resend
    sent = [w for w in transport.written if w.startswith(b"T")]
    assert len(sent) == 2
    assert sent[0] == sent[1]  # byte-identical resend


def test_send_frame_raises_after_second_txerr_bad_hex():
    """Only one resend is allowed; a second "TXERR empty or bad hex" for the
    same T still raises, rather than retrying forever."""
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    nanocul = _make_nanocul(transport, reset_wait=0, retries=1)
    transport.busy_until = 10.0  # stays busy through both the send and its resend

    setpoint_air = tf.build_frame(STATION_ID, HEATER_ID, tf.setpoint_payload(24.0))
    with pytest.raises(RuntimeError, match="TXERR empty or bad hex"):
        nanocul.send_frame(HEATER_ID, setpoint_air, wait_ack=False)

    sent = [w for w in transport.written if w.startswith(b"T")]
    assert len(sent) == 2  # the original attempt plus the one allowed resend, no more


def test_min_command_gap_enforced_from_end_of_last_stick_line():
    """A command line must not go out less than MIN_COMMAND_GAP_S after the
    end of the last line the stick printed: draining an otherwise-empty port
    only proves DRAIN_QUIET_S (30 ms) of silence, less than the 40 ms floor,
    so _enforce_min_gap() must still top up the remaining 10 ms itself."""
    from termoweb_local.nanocul import MIN_COMMAND_GAP_S

    clock = FakeClock()
    transport = FakeSerialTransport(clock, auto_confirm_tx=False)
    nanocul = _make_nanocul(transport, reset_wait=0)
    transport.schedule_rx("Q one", at=0.0)  # the stick's last output, at t=0

    nanocul.send_raw_command("A1")  # drains "Q one", then writes

    assert clock.now >= MIN_COMMAND_GAP_S


def test_drain_returns_at_once_when_the_stick_is_already_quiet():
    """The reply to a pairing announcement or a registration frame has to be
    on the air inside the heater's own window (PROTOCOL.md 5.9), so a drain
    that only re-observes silence the client has already observed is pure
    delay: with nothing read for longer than DRAIN_QUIET_S, the write goes
    out without spending another poll interval on it."""
    from termoweb_local.nanocul import DRAIN_QUIET_S

    clock = FakeClock()
    transport = FakeSerialTransport(clock, auto_confirm_tx=False)
    nanocul = _make_nanocul(transport, reset_wait=0)
    transport.schedule_rx("Q one", at=0.0)
    nanocul.read_events()  # the stick's last output, at t=0
    clock.advance(1.0)
    before = clock.now

    nanocul.send_raw_command("A1")

    assert clock.now - before < DRAIN_QUIET_S


def test_await_ack_does_not_overshoot_its_own_deadline():
    """An ack wait bounded by retry_interval used to cost a whole port read
    timeout per attempt (0.2 s against a 0.16 s interval), so three attempts
    ran 120 ms past the retry budget with the assignment window already
    closed."""
    clock = FakeClock()
    transport = FakeSerialTransport(clock, read_timeout=0.2)
    nanocul = _make_nanocul(transport, retries=1, retry_interval=0.05)
    air = tf.build_frame(STATION_ID, HEATER_ID, tf.setpoint_payload(24.0))

    start = clock.now
    result = nanocul.send_frame(HEATER_ID, air)

    assert result.ok is False
    assert clock.now - start < 0.2
    assert transport.timeout == 0.2  # restored


def test_send_frame_honours_a_per_call_retry_limit():
    """The pairing assignment is only worth anything while the announcing
    heater still dwells on the destination that produced it (PROTOCOL.md
    5.9), so that call sends once and lets the next announcement be the
    retry."""
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    nanocul = _make_nanocul(transport, retries=3, retry_interval=0.05)
    air = tf.build_frame(STATION_ID, 0xFF, bytes([0x04]))

    result = nanocul.send_frame(0xFF, air, retries=1)

    assert result.ok is False
    assert len([w for w in transport.written if w.startswith(b"T")]) == 1


def test_send_frame_to_the_discovery_broadcast_id_ignores_a_relayed_heater_ack():
    """During a pairing sweep an already-paired heater acks the announcement
    it relays, putting `FA src=02 dst=FF` on the air right beside our
    assignment's own send (PROTOCOL.md 5.9). The ack wait is for `FA
    src=FF dst=01`, the announcing heater's own ack of the assignment, so
    the relay ack must neither satisfy it nor be swallowed by it."""
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    nanocul = _make_nanocul(transport, retries=3, retry_interval=0.05)

    relay_ack = tf.build_ack(0x02, 0xFF)
    transport.schedule_rx(f"RX 500 -55.0 60 0 {relay_ack.hex().upper()}")
    air = tf.build_frame(STATION_ID, 0xFF, bytes([0x04]), tag=0x04)

    result = nanocul.send_frame(0xFF, air, retries=1)

    assert result.ok is False
    assert [event.cls for event in nanocul.read_events()] == [0xFA]


def test_send_frame_to_the_discovery_broadcast_id_takes_the_announcing_heaters_ack():
    clock = FakeClock()
    transport = FakeSerialTransport(clock)
    nanocul = _make_nanocul(transport, retries=3, retry_interval=0.05)

    transport.schedule_rx(f"RX 500 -55.0 60 0 {tf.build_ack(0xFF, STATION_ID).hex().upper()}")
    air = tf.build_frame(STATION_ID, 0xFF, bytes([0x04]), tag=0x04)

    result = nanocul.send_frame(0xFF, air, retries=1)

    assert result.ok is True
    assert result.piece.src == 0xFF


def test_classify_and_split_matches_nano_rx_on_one_worked_line():
    """A quick sanity check on this module's own port of nano_rx.py's algorithm;
    tests/test_nanocul_rx_parity.py checks the whole corpus against the real
    nano_rx.py."""
    hexstr = "E59C885DB6A1C825565F4A9C5850E47E05BAB4FC84AE07F1E686E38616"
    byte_strs = [hexstr[i : i + 2] for i in range(0, len(hexstr), 2)]
    pieces = classify_and_split(byte_strs)
    assert len(pieces) == 1
    piece = pieces[0]
    assert isinstance(piece, Piece)
    assert piece.verdict == "ok"
    assert piece.cls == 0xE5
    assert piece.src == HEATER_ID
    assert piece.dst == STATION_ID


def test_parse_rx_line_matches_nano_rx_format():
    parsed = parse_rx_line("RX 12345 -44.0 60 0 E59C88")
    assert parsed["micros"] == 12345
    assert parsed["rssi_dbm"] == -44.0
    assert parsed["bytes"] == ["E5", "9C", "88"]
    assert parse_rx_line("Q firmware=3.2") is None


def test_await_ack_reads_the_port_while_unrelated_events_are_pending():
    """Regression for the 2026-09-06 live scan: with one unrelated frame already
    queued, every ack wait used to spin on the pending list until its deadline
    without ever reading the port, so the real ack was never seen."""
    transport = FakeSerialTransport(FakeClock())
    nanocul = _make_nanocul(transport, retries=1)
    nanocul._pending_events.append(RawLine("# unrelated", "2026-09-06T00:00:00.000"))
    transport.schedule_rx("RX 17000 -44.0 60 0 FA9C885DB621FBD1", at=transport.clock.now + 0.02)
    result = nanocul.send_frame(4, bytes.fromhex("F29C8858B3A1CD20575E4B9C59BCF7A0"))
    assert result.ok
    assert any(isinstance(e, RawLine) for e in nanocul._pending_events)

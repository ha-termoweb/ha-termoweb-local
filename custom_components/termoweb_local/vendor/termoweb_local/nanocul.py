"""NanoCul: a serial client for the nanoCUL868 running firmware/termoweb_rx.

Port handling matches nano_tx.py: open with `serial.serial_for_url`, then wait for the
firmware's DTR reset before touching the port (nano_tx.py waits 2.5 s; nano_rx.py's own
wait is 2 s -- this client defaults to the longer, already-proven-safe 2.5 s and takes
`reset_wait` as a constructor argument so a caller can match either tool exactly).

RX line parsing reproduces nano_rx.py's `classify_and_split` bit-for-bit: trim a raw
read to its first frame's own length (on-air byte 0 XOR 0xFF, termoweb_frame.frame_length),
check that frame's CRC, and only then search the remaining bits for the next embedded
sync word, recursing into whatever follows. This module does not import nano_rx.py
itself (its CLI, argparse and stdout table are irrelevant to a library client, and the
package must work with only termoweb_local installed, without the rest of this
repository, e.g. as a Home Assistant custom component dependency); the two
implementations are checked for identical behaviour over the capture corpus in
tests/test_nanocul_rx_parity.py, which does import nano_rx.py by path, so this
docstring's claim is a tested fact, not an assumption.

Transmit follows PROTOCOL.md section 7 and section 6: build the on-air frame with
`termoweb_local.frame.build_frame`, send it as `T<hex>\\n`, wait for the stick's own
`TX ...` confirmation, then wait for an ack (an FA frame addressed back to our source
id); on no ack within one retry interval, resend the identical bytes, up to 3 retries
total, 160 ms apart (PROTOCOL.md section 6).

Firmware 3.2 tolerance: `read_events` passes through any line that is not an `RX ...`
line (a `Q` status line, an unsolicited banner, or anything else) as a `RawLine` event
instead of raising or dropping it, and `send_raw_command` sends any command line
(`Q`, `A0`, `A1`, `I<hex>`, or a raw `T<hex>`) without needing a subcommand for each one.
"""
import collections
import contextlib
import dataclasses
import time

from . import frame as tf

RETRY_COUNT = 3
RETRY_INTERVAL_S = 0.160
RESET_WAIT_S = 2.5
DEFAULT_READ_TIMEOUT_S = 0.2

# 2026-09-06 live failure (HA host log 21:13:51): firmware/termoweb_rx's
# uart_getc_nonblock polls the hardware register with no interrupt ring buffer,
# so a command byte that arrives while the stick is still busy printing a long
# RX line (a 99-byte frame prints as a 230-character line) can be lost, and a
# `T` sent right then comes back "TXERR empty or bad hex". DRAIN_QUIET_S/
# DRAIN_MAX_S bound _drain() (read and classify pending input, queuing every
# event, until the port has been silent this long -- or this total budget runs
# out); MIN_COMMAND_GAP_S is the floor on how soon after the end of the last
# line the stick printed a new command line may go out; TXERR_RETRY_WAIT_S is
# the pause before the one allowed resend of an identical T that came back
# with exactly this error. The proof session's own batches never hit this
# because they paced commands by seconds, not milliseconds.
DRAIN_QUIET_S = 0.03
DRAIN_MAX_S = 0.5
MIN_COMMAND_GAP_S = 0.04
TXERR_RETRY_WAIT_S = 0.05
TXERR_BAD_HEX_TEXT = "TXERR empty or bad hex"

# rssi_dbm and lqi ride alongside micros for the same reason micros does: they are
# properties of the hardware receive event, not of a frame recovered out of it, so
# every piece split out of one raw read carries the same pair. They default to None
# so a caller that builds a Piece without them (a test, or any future non-RX source)
# still constructs positionally.
Piece = collections.namedtuple(
    "Piece", "cls verdict bytes src dst micros rssi_dbm lqi",
    defaults=(None, None, None),
)

ACK_ON_AIR_CLASS = 0xFA  # on-air byte 0 for an FA ack (logical length 0x05 XOR 0xFF)

_TXERR_BAD_HEX = object()  # sentinel: _await_tx_confirmation saw TXERR_BAD_HEX_TEXT

# 0x16F2 is 0x2DE5 read one bit early (PROTOCOL.md section 2); a match on it skips 17
# bits, not 16, to land on the same true frame boundary a 0x2DE5 match would.
SYNC_BIT_SPECS = ((format(0x2DE5, "016b"), 16), (format(0x16F2, "016b"), 17))


def find_sync(bits, start_bit):
    """First occurrence of either sync pattern at or after start_bit; returns
    (bit_position, bits_to_skip_to_reach_the_next_byte) or (None, None)."""
    for start in range(start_bit, len(bits) - 15):
        for pattern, skip in SYNC_BIT_SPECS:
            if bits[start:start + 16] == pattern:
                return start, skip
    return None, None


def classify_and_split(byte_strs, micros=None, rssi_dbm=None, lqi=None):
    """Reproduces nano_rx.py's classify_and_split: trim to the first frame's implied
    length, check its CRC, then resync on the bits that follow. byte_strs is a sequence
    of two-hex-digit strings (as read off the wire); returns a list of
    Piece(cls, verdict, bytes, src, dst, micros), verdict one of "ok"/"bad"/"n/a".
    `micros` is the originating RX line's own micros field (the stick's own clock,
    used for the on-stick "ms after TX" delta tools/nano_tx.py computes); every piece
    split out of the same raw read carries that same value, since they all arrived in
    one hardware receive event. `rssi_dbm` and `lqi` are that same line's own signal
    fields and are carried the same way, for the same reason: the radio measured the
    receive event, not each frame recovered from it."""
    if not byte_strs:
        return []
    try:
        vals = [int(b, 16) for b in byte_strs]
    except ValueError:
        return [Piece(None, "n/a", byte_strs, None, None, micros, rssi_dbm, lqi)]

    cls = vals[0]
    length = tf.frame_length(cls)
    piece = byte_strs[:length]
    bits = "".join(f"{v:08b}" for v in vals)
    src = dst = None
    if len(piece) < length:
        verdict = "n/a"
    else:
        frame = tf.parse_frame(bytes(vals[:length]))
        verdict = "ok" if frame.crc_ok else "bad"
        src, dst = frame.src, frame.dst

    found, skip = find_sync(bits, length * 8)
    if verdict == "bad":
        piece = byte_strs[:found // 8] if found is not None else byte_strs

    result = [Piece(cls, verdict, piece, src, dst, micros, rssi_dbm, lqi)]
    if found is not None:
        rest_bits = bits[found + skip:]
        rest_len = (len(rest_bits) // 8) * 8
        piece2 = [f"{int(rest_bits[i:i + 8], 2):02X}" for i in range(0, rest_len, 8)]
        result.extend(
            classify_and_split(piece2, micros=micros, rssi_dbm=rssi_dbm, lqi=lqi)
        )
    return result


def parse_rx_line(text):
    """Parse one 'RX <micros> <rssi> <lqi> <crc> <hex>[ <rawstatus>]' line, the format
    firmware/termoweb_rx writes and nano_rx.py reads. Returns None for a line that is
    not an RX line at all (firmware 3.2's Q status line included)."""
    parts = text.split()
    if len(parts) < 5 or parts[0] != "RX":
        return None
    micros, rssi, lqi, crc = parts[1], parts[2], parts[3], parts[4]
    hexstr = parts[5] if len(parts) > 5 else ""
    byte_strs = [hexstr[i:i + 2] for i in range(0, len(hexstr) - 1, 2)]
    try:
        rssi_dbm = float(rssi)
    except ValueError:
        rssi_dbm = None
    return {
        "micros": int(micros),
        "rssi": rssi,
        "rssi_dbm": rssi_dbm,
        "lqi": lqi,
        "crc": crc,
        "bytes": byte_strs,
    }


def format_rx_log_line(stamp, parsed, piece, is_split):
    """Format one recovered piece as a nano_rx.py-format log line, byte for byte the
    same shape nano_rx.py itself writes, so a caller's logging hook can produce a log
    tools/acceptance_check.py (and nano_rx.py's own --table) can read."""
    cls, verdict, trimmed, src, dst = piece[:5]
    cls_label = f"{cls:02X}" if cls is not None else "--"
    src_label = f"{src:02X}" if src is not None else "--"
    dst_label = f"{dst:02X}" if dst is not None else "--"
    entry = (
        f"{stamp} RX {parsed['micros']} {parsed['rssi']} {parsed['lqi']} {parsed['crc']} "
        f"{''.join(trimmed)} crc={verdict} len={len(trimmed)} class={cls_label} "
        f"src={src_label} dst={dst_label}"
    )
    if is_split:
        entry += " split"
    return entry


@dataclasses.dataclass
class RawLine:
    """A line from the stick that was not an RX line: a Q status line, a boot banner,
    an ack/TX confirmation from our own send, or anything firmware 3.2 adds that this
    client does not know about yet. Never dropped or raised on."""

    text: str
    stamp: str


@dataclasses.dataclass
class AckResult:
    ok: bool
    attempts: int
    piece: object = None
    tx_micros: object = None  # the TX confirmation line's own micros, for callers
    # that wait for their own ack outside send_frame (e.g. a spoofed-source ack)
    stick_delay_ms: object = None  # ack RX micros minus tx_micros, /1000: the
    # stick's own clock delta, same figure tools/nano_tx.py prints as "+N ms after TX"


class NanoCul:
    """One serial connection to the stick. One process owns the port at a time; see
    termoweb_local/README.md for the coordination rule (this client's logging_hook
    replaces running nano_rx.py alongside it)."""

    def __init__(
        self,
        url,
        source_id,
        baudrate=115200,
        reset_wait=RESET_WAIT_S,
        retries=RETRY_COUNT,
        retry_interval=RETRY_INTERVAL_S,
        read_timeout=DEFAULT_READ_TIMEOUT_S,
        logging_hook=None,
        transport=None,
        transport_factory=None,
        clock=time.time,
        sleep=time.sleep,
    ):
        self.source_id = source_id
        self.retries = retries
        self.retry_interval = retry_interval
        self.logging_hook = logging_hook
        self._clock = clock
        self._sleep = sleep
        self._pending_events = collections.deque()
        # The clock() value at which we last actually read a non-empty line
        # from the stick; None until the first one. _enforce_min_gap() uses
        # this, not when we started waiting, so a caller that has been idle
        # for a while never pays MIN_COMMAND_GAP_S needlessly.
        self._last_stick_output_at = None
        self._last_send_tx_retries = 0  # set by _send_tx; see AckResult.attempts
        self._line_buffer = b""  # bytes of an incomplete line; see _readline()

        if transport is not None:
            self._transport = transport
        else:
            factory = transport_factory
            if factory is None:
                import serial

                factory = lambda u: serial.serial_for_url(  # noqa: E731
                    u, baudrate=baudrate, timeout=read_timeout
                )
            self._transport = factory(url)

        self._sleep(reset_wait)
        reset_input_buffer = getattr(self._transport, "reset_input_buffer", None)
        if reset_input_buffer is not None:
            reset_input_buffer()

    def close(self):
        self._flush_partial_line()
        self._transport.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def _log(self, text):
        if self.logging_hook is not None:
            self.logging_hook(text)

    def _readline(self):
        """Line-buffered read: `self._transport.readline()` (real pyserial
        included -- see its own generic io.RawIOBase.readline()) returns
        whatever it has accumulated as soon as one underlying read comes back
        empty, newline or not; over the ser2net TCP bridge a chunk gap longer
        than the port's own timeout routinely lands mid-line (2026-09-06
        firmware 3.4 live defect: a 99-byte reply's ~230-character line cut
        this way fed a truncated hex string straight into classify_and_split).
        This keeps whatever arrives with no terminating `\\n` yet in
        `self._line_buffer` across calls instead of handing it to the caller,
        and only ever returns a complete, newline-terminated line.

        Also the one place that knows whether *anything* arrived on this
        call, complete line or not, which _drain() needs (a partial line is
        activity, not silence -- see _drain()'s own docstring) -- it does so
        by updating self._last_stick_output_at itself whenever `raw` is
        non-empty, rather than leaving that to _read_and_classify_one(),
        which only ever sees a complete line."""
        raw = self._transport.readline()
        if not raw:
            return None
        self._last_stick_output_at = self._clock()
        self._line_buffer += raw
        newline_at = self._line_buffer.find(b"\n")
        if newline_at == -1:
            return None  # a partial line: more of it may still be coming
        line = self._line_buffer[:newline_at]
        self._line_buffer = self._line_buffer[newline_at + 1:]
        return line.decode(errors="replace").strip()

    def _flush_partial_line(self):
        """Whatever is left in self._line_buffer when the transport closes
        was never terminated by the stick itself and can never be completed
        now; log it (nano_rx.py-format, like any other line) so it is not
        silently lost, rather than trying to classify what is, by
        definition, an incomplete frame. Not called from _readline() itself
        -- only close() knows the transport is actually done, as opposed to
        merely quiet for now."""
        if not self._line_buffer:
            return
        text = self._line_buffer.decode(errors="replace").strip()
        self._line_buffer = b""
        if text:
            stamp = _timestamp(self._clock)
            self._log(f"{stamp} {text} (partial line, transport closed)")

    def read_events(self):
        """Read whatever the transport's readline() returns right now (respecting its
        own timeout) and yield the resulting events: Piece instances for a recovered
        RX frame (one raw read can hold more than one, per classify_and_split), or a
        RawLine for anything else. Every RX line is also logged through
        logging_hook in nano_rx.py's own format; non-RX lines are logged as given.

        Drains _pending_events first, if any: _send_tx queues here any event it saw
        while scanning for its own TX confirmation that was not that confirmation
        (PROTOCOL.md section 6 -- a late ack from a previous retry, or unrelated
        ambient traffic, must reach the caller instead of being lost)."""
        events = list(self._pending_events)
        self._pending_events.clear()
        events.extend(self._read_and_classify_one())
        return events

    def _read_and_classify_one(self):
        """The actual transport read + classify + log, with no _pending_events check.
        _send_tx calls this directly (not read_events()) so that queuing a non-TX
        event into _pending_events can never make it read that same event straight
        back out again instead of advancing to the next line on the wire.

        Returns [] both when nothing at all arrived and when only a still-
        incomplete line is buffered (_readline() returns None either way);
        self._last_stick_output_at, updated inside _readline() itself,
        distinguishes those two cases for _drain()."""
        text = self._readline()
        if not text:
            return []
        stamp = _timestamp(self._clock)
        parsed = parse_rx_line(text)
        if parsed is None:
            self._log(f"{stamp} {text}")
            return [RawLine(text=text, stamp=stamp)]

        pieces = classify_and_split(
            parsed["bytes"], micros=parsed["micros"],
            rssi_dbm=parsed["rssi_dbm"], lqi=parsed["lqi"],
        ) or [
            Piece(None, "n/a", parsed["bytes"], None, None, parsed["micros"],
                  parsed["rssi_dbm"], parsed["lqi"])
        ]
        events = []
        for i, piece in enumerate(pieces):
            self._log(format_rx_log_line(stamp, parsed, piece, is_split=i > 0))
            events.append(piece)
        return events

    def send_raw_command(self, line):
        """Send one raw command line (Q, A0, A1, I<hex>, or a bare T<hex>) with no
        framing or ack handling of any kind; for firmware 3.2's new commands.

        Always goes through _prepare_write() first (drain, then the minimum
        inter-command gap): every command line this client sends, whichever
        method builds it, funnels through here, so that is the one place that
        needs to enforce both."""
        self._prepare_write()
        self._transport.write((line.strip() + "\n").encode())

    def _prepare_write(self):
        """Drain pending input, then wait out whatever is left of
        MIN_COMMAND_GAP_S since the end of the last line the stick printed,
        before this client writes a new command line (2026-09-06 fix; see the
        module-level comment by DRAIN_QUIET_S)."""
        self._drain()
        self._enforce_min_gap()

    def _drain(self):
        """Read and classify whatever the stick has queued up, queuing every
        event via _pending_events, until the port has produced nothing for
        DRAIN_QUIET_S -- or DRAIN_MAX_S total has passed, whichever comes
        first. Temporarily shortens the transport's own read timeout to
        DRAIN_QUIET_S so "silent for DRAIN_QUIET_S" can actually be observed
        one poll at a time, restoring it afterwards; a transport with no
        settable `timeout` (real pyserial and FakeSerialTransport both have
        one) is drained at whatever its own configured timeout already is.

        "Silent" is judged by self._last_stick_output_at (updated inside
        _readline() the instant any bytes arrive, complete line or not), not
        by "did _read_and_classify_one() return an event": a long RX line cut
        mid-hex by a ser2net chunk gap (2026-09-06 firmware 3.4 live defect)
        produces no event on the read that receives its first, partial
        piece, but that read plainly was not silence -- treating it as such
        used to make the drain give up right as the stick was still in the
        middle of printing that very line.

        Returns straight away when the stick has already been silent for
        DRAIN_QUIET_S: the port is then quiet by exactly the criterion the
        loop below waits to observe, so spending another poll interval
        watching it stay quiet only delays the write, which on the pairing
        and registration paths has to be on the air inside the heater's own
        window (PROTOCOL.md 5.9)."""
        if (
            self._last_stick_output_at is not None
            and self._clock() - self._last_stick_output_at >= DRAIN_QUIET_S
        ):
            return  # already quiet by this method's own definition
        original_timeout = getattr(self._transport, "timeout", None)
        if original_timeout is not None:
            self._transport.timeout = DRAIN_QUIET_S
        try:
            deadline = self._clock() + DRAIN_MAX_S
            quiet_since = self._clock()
            while self._clock() < deadline:
                before = self._last_stick_output_at
                events = self._read_and_classify_one()
                self._pending_events.extend(events)
                if self._last_stick_output_at != before:
                    quiet_since = self._clock()
                elif self._clock() - quiet_since >= DRAIN_QUIET_S:
                    return
        finally:
            if original_timeout is not None:
                self._transport.timeout = original_timeout

    @contextlib.contextmanager
    def _read_timeout_at_most(self, seconds):
        """Shorten the transport's own read timeout to `seconds` for the
        duration, the way _drain() does, so a wait that has less time left
        than one full port timeout is not overshot by up to that timeout:
        an ack wait bounded by retry_interval (160 ms) used to cost a whole
        DEFAULT_READ_TIMEOUT_S (200 ms) per attempt, and a scan probe
        bounded by SCAN_REPLY_TIMEOUT_S (150 ms) the same. A transport with
        no settable `timeout` is left alone."""
        original = getattr(self._transport, "timeout", None)
        if original is None or seconds >= original:
            yield
            return
        self._transport.timeout = max(seconds, 0.0)
        try:
            yield
        finally:
            self._transport.timeout = original

    def _enforce_min_gap(self):
        """Sleep, if needed, so at least MIN_COMMAND_GAP_S has passed since
        the end of the last line the stick printed (firmware/README.md,
        firmware/termoweb_rx/main.c: uart_getc_nonblock has no interrupt ring
        buffer, so a byte that lands while the stick is still busy printing a
        long RX line can be lost). A no-op before this client's very first
        write, when nothing has been read yet."""
        if self._last_stick_output_at is None:
            return
        remaining = MIN_COMMAND_GAP_S - (self._clock() - self._last_stick_output_at)
        if remaining > 0:
            self._sleep(remaining)

    def send_raw_frame(self, air_bytes):
        """Send `air_bytes` and consume the stick's own TX confirmation line
        for it -- unlike send_raw_command("T" + air_bytes.hex()), which
        writes and returns immediately, leaving that TX confirmation line
        sitting unread in the stick's own output; the very next unrelated
        read (a background reader's read_events(), or another caller's own
        _send_tx) would otherwise find and consume that stale line instead
        of its own, and mistake it for its own TX confirmation. For a frame
        that is itself an ack (a software ack sent when the stick's own
        hardware auto-ack is off; PROTOCOL.md section 5.5/5.9: "an ack is
        never itself acked or answered"), so send_frame's own ack-wait-and-
        retry, which would wait out a full retry_interval for an ack that
        never comes, is never the right tool for it."""
        return self._send_tx(air_bytes)

    def _send_tx(self, air_bytes):
        """Send `air_bytes` and wait for the stick's own TX confirmation line.

        2026-09-06 fix: a "TXERR empty or bad hex" reply means our own `T` line
        arrived mangled (firmware/termoweb_rx/main.c: uart_getc_nonblock has no
        interrupt ring buffer, so a byte can be lost while the stick is still
        busy printing a long RX line) rather than anything wrong with
        `air_bytes` itself, so it is worth one immediate resend of the
        identical bytes -- after waiting TXERR_RETRY_WAIT_S and draining again,
        in case the stick is still catching up -- before giving up. Any other
        TXERR (underflow, a bad marcstate) is not this failure mode and still
        raises straight away. self._last_send_tx_retries records whether that
        resend fired (0 or 1), for send_frame to fold into AckResult.attempts.

        Reads through _read_and_classify_one(), not a raw readline loop: on a retry,
        a late ack from the previous attempt (its retry_interval window already
        closed) can still be sitting on the wire, arriving interleaved with this
        attempt's own TX confirmation. A raw loop that only recognised "TX "/"TXERR"
        discarded that ack silently and unclassified; this classifies it into a
        Piece and queues it via _pending_events for the _await_ack call right after
        this one to pick up, instead of losing it."""
        command = "T" + air_bytes.hex().upper()
        self.send_raw_command(command)
        result = self._await_tx_confirmation()
        self._last_send_tx_retries = 0
        if result is _TXERR_BAD_HEX:
            self._sleep(TXERR_RETRY_WAIT_S)
            self.send_raw_command(command)  # _prepare_write() drains again
            result = self._await_tx_confirmation()
            self._last_send_tx_retries = 1
            if result is _TXERR_BAD_HEX:
                raise RuntimeError(f"nanoCUL transmit error: {TXERR_BAD_HEX_TEXT}")
        return result

    def _await_tx_confirmation(self):
        """Scan for the TX confirmation or TXERR line for one `T` write already
        sent. Returns the confirmation's own micros (int), or the sentinel
        _TXERR_BAD_HEX for a "TXERR empty or bad hex" line -- the one case
        _send_tx knows to retry -- or raises for any other TXERR."""
        deadline = self._clock() + 3.0
        while self._clock() < deadline:
            for event in self._read_and_classify_one():
                if isinstance(event, RawLine):
                    if event.text.startswith("TX "):
                        return int(event.text.split()[1])
                    if event.text.startswith("TXERR"):
                        if event.text == TXERR_BAD_HEX_TEXT:
                            return _TXERR_BAD_HEX
                        raise RuntimeError(f"nanoCUL transmit error: {event.text}")
                    continue  # a Q line, banner, etc.: already logged, nothing to do
                self._pending_events.append(event)
        raise TimeoutError("no TX confirmation from the stick")

    def send_frame(self, dst, air_bytes, wait_ack=True, retries=None):
        """Send on-air bytes to `dst`, waiting for its ack and retrying up to
        `self.retries` times (or `retries`, given), `self.retry_interval` seconds
        apart (PROTOCOL.md section 6). Returns an AckResult; ok is False (not an
        exception) when every retry is exhausted with no ack, since a caller may
        still want to know how many attempts were made. tx_micros is always set
        (the stick's own TX confirmation timestamp for the attempt that produced
        this result), so a caller that waits for its own ack afterward (e.g. one
        addressed to a spoofed source id) can still compute the stick's own ack
        delta the same way stick_delay_ms does here.

        `retries` is for a frame whose own value expires before the retries
        would finish: the pairing assignment is only useful while the
        announcing heater still dwells on the destination that produced it
        (PROTOCOL.md 5.9), so the next announcement is a better retry than
        resending into a destination it has already left."""
        attempt_limit = self.retries if retries is None else retries
        attempts = 0
        tx_micros = None
        for attempt in range(1, attempt_limit + 1):
            tx_micros = self._send_tx(air_bytes)
            attempts = attempt + self._last_send_tx_retries
            if not wait_ack:
                return AckResult(ok=True, attempts=attempts, tx_micros=tx_micros)
            ack_piece = self._await_ack(dst, self.retry_interval)
            if ack_piece is not None:
                stick_delay_ms = None
                if ack_piece.micros is not None:
                    stick_delay_ms = (ack_piece.micros - tx_micros) / 1000.0
                return AckResult(ok=True, attempts=attempts, piece=ack_piece,
                                  tx_micros=tx_micros, stick_delay_ms=stick_delay_ms)
        return AckResult(ok=False, attempts=attempts, tx_micros=tx_micros)

    def wait_for_reply(self, src, predicate, timeout):
        """Wait up to `timeout` seconds for a Piece from `src` addressed to us
        whose parsed payload satisfies `predicate(parsed_frame)` (a
        termoweb_local.frame.Frame, as tf.parse_frame returns). Used by
        Network.request_status/read_program/request/wait_processed to capture a
        reply that follows the ack send_frame already waited for.

        Reuses read_events()/_pending_events exactly like _await_ack: every
        event this call sees but does not match (unrelated traffic, a report,
        someone else's ack) is queued back via _pending_events instead of being
        dropped, so the next read_events() call (the background reader, or
        another wait_for_reply/_await_ack) still sees it. Returns the matching
        Frame, or None on timeout."""
        deadline = self._clock() + timeout
        found = None
        kept = []
        while self._clock() < deadline and found is None:
            with self._read_timeout_at_most(deadline - self._clock()):
                events = self.read_events()
            for event in events:
                if (
                    found is None
                    and isinstance(event, Piece)
                    and event.verdict == "ok"
                    and event.src == src
                    and event.dst == self.source_id
                ):
                    parsed = tf.parse_frame(bytes(int(b, 16) for b in event.bytes))
                    if predicate(parsed):
                        found = parsed
                        continue
                kept.append(event)
        self._pending_events.extendleft(reversed(kept))
        return found

    def _await_ack(self, dst, timeout):
        """2026-09-06 fix: a batch read_events() call can hold more than one event
        (e.g. this ack and an application-layer reply that arrived right behind
        it, both already sitting in the input buffer -- see
        wait_for_reply()); the old version returned on the first ack match and
        silently dropped everything else in that same batch. Now it queues
        every non-matching event via _pending_events, same as wait_for_reply and
        _send_tx, so nothing behind the ack in one read is lost."""
        deadline = self._clock() + timeout
        found = None
        kept = []
        while self._clock() < deadline and found is None:
            with self._read_timeout_at_most(deadline - self._clock()):
                events = self.read_events()
            for event in events:
                if (
                    found is None
                    and isinstance(event, Piece)
                    and event.cls == ACK_ON_AIR_CLASS
                    and event.verdict == "ok"
                    and event.src == dst
                    and event.dst == self.source_id
                ):
                    found = event
                    continue
                kept.append(event)
        self._pending_events.extendleft(reversed(kept))
        return found


def _timestamp(clock):
    import datetime as dt

    return dt.datetime.fromtimestamp(clock()).isoformat(timespec="milliseconds")

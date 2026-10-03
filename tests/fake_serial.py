"""An in-memory stand-in for a pyserial Serial object, for exercising NanoCul with no
serial port opened. It answers readline() from a queue of pre-scripted lines, records
everything written to it, and advances a shared fake clock by the configured read
timeout on every call that finds nothing to return -- reproducing a real port's
blocking-with-timeout behaviour without an actual sleep, so NanoCul's ack-wait and
retry loops terminate deterministically in a test."""


class FakeClock:
    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds

    def sleep(self, seconds):
        self.advance(seconds)


class FakeSerialTransport:
    def __init__(self, clock, read_timeout=0.2, auto_confirm_tx=True):
        self.clock = clock
        # `timeout` (not `read_timeout`) is the name real pyserial exposes as a
        # settable property; NanoCul._drain() temporarily shortens it to poll
        # for silence, so it must live under this name for that to have any
        # effect here. `read_timeout` is kept as the constructor's own
        # parameter name (existing tests and callers already use it).
        self.timeout = read_timeout
        self.auto_confirm_tx = auto_confirm_tx
        self.rx_queue = []  # list of (due_at, line_bytes, busy_for)
        self.written = []
        self.closed = False
        self._next_tx_micros = 1
        # Set (to clock() + busy_for) when a line scheduled with busy_for=...
        # is actually read; see schedule_rx()'s own docstring and write()
        # below -- the "stick still busy printing a long RX line" simulation
        # for the drain/min-gap fix (2026-09-06).
        self.busy_until = None
        # Chunked delivery: a queue of (due_at, raw_bytes) pieces of one or
        # more lines, plus whatever has already arrived but has no newline in
        # it yet. Separate from rx_queue (whole-line, always available at
        # once) so every existing test that never calls schedule_rx_chunks()
        # keeps going through the untouched whole-line path below; readline()
        # only switches to the chunked path once something has been scheduled
        # that way. See schedule_rx_chunks()'s own docstring for what this
        # models and why.
        self._chunk_queue = []
        self._chunk_buffer = b""

    @property
    def read_timeout(self):
        return self.timeout

    def schedule_rx(self, line, at=None, busy_for=None):
        """Queue a line (str, no trailing newline) to be returned by a future
        readline() once the fake clock reaches `at` (default: right now).

        `busy_for`, given, simulates firmware/termoweb_rx/main.c's
        uart_getc_nonblock having no interrupt ring buffer: once this
        particular line is actually read (not merely queued -- see
        readline()), the transport treats itself as "busy printing" for
        `busy_for` more seconds, rejecting any `T` write attempted before
        that with a TXERR line, exactly like a real stick whose CPU is still
        busy pushing this line's own bytes out and not polling its UART RX
        register."""
        due = self.clock() if at is None else at
        self.rx_queue.append((due, line.encode() + b"\n", busy_for))
        self.rx_queue.sort(key=lambda item: item[0])

    def schedule_rx_chunks(self, parts, gap, at=None):
        """Queue one line's raw bytes to arrive in separate pieces, `gap`
        seconds apart, reproducing what a real pyserial `Serial.readline()`
        sees over a ser2net TCP bridge that forwards the stick's blocking-putc
        UART output in irregular network chunks (2026-09-06 firmware 3.4 live
        defect): each `parts[i]` becomes available at `at + i * gap` (default
        `at`: now), with a trailing `\\n` appended to the last part only --
        the earlier parts are deliberately not newline-terminated, since that
        is exactly what a real mid-line gap looks like on the wire. Once
        anything is queued this way, readline() serves it through
        _readline_chunked() below instead of the whole-line rx_queue path,
        so this never has to be mixed with schedule_rx() in one test."""
        due = self.clock() if at is None else at
        for i, part in enumerate(parts):
            data = part.encode() if isinstance(part, str) else part
            if i == len(parts) - 1:
                data += b"\n"
            self._chunk_queue.append((due, data))
            due += gap
        self._chunk_queue.sort(key=lambda item: item[0])

    def readline(self):
        if self._chunk_queue or self._chunk_buffer:
            return self._readline_chunked()
        for i, (due, line, busy_for) in enumerate(self.rx_queue):
            if due <= self.clock():
                del self.rx_queue[i]
                if busy_for is not None:
                    self.busy_until = self.clock() + busy_for
                return line
        self.clock.advance(self.timeout)
        return b""

    def _readline_chunked(self):
        """Reproduce real pyserial semantics for a line delivered in pieces:
        `Serial.readline()` (io.RawIOBase's generic implementation) reads one
        byte at a time, each such read blocking up to the port's own
        `timeout`; it returns as soon as it has seen `\\n`, or as soon as one
        of those per-byte reads comes back empty (nothing arrived within
        `timeout`) -- in which case whatever was accumulated so far, with no
        trailing `\\n`, is what it returns. This mirrors that: pull whatever
        chunk is already due into the buffer and look for `\\n` again; if the
        next chunk is due within one `timeout` from now, advance the clock to
        it and keep going (bytes kept arriving before the port gave up); only
        once nothing more is due soon does it give up, spend one full
        `timeout`, and return the partial buffer."""
        while True:
            idx = self._chunk_buffer.find(b"\n")
            if idx != -1:
                line = self._chunk_buffer[: idx + 1]
                self._chunk_buffer = self._chunk_buffer[idx + 1 :]
                return line
            if self._chunk_queue and self._chunk_queue[0][0] <= self.clock():
                _, chunk = self._chunk_queue.pop(0)
                self._chunk_buffer += chunk
                continue
            if self._chunk_queue and self._chunk_queue[0][0] <= self.clock() + self.timeout:
                due, chunk = self._chunk_queue.pop(0)
                self.clock.advance(due - self.clock())
                self._chunk_buffer += chunk
                continue
            self.clock.advance(self.timeout)
            partial = self._chunk_buffer
            self._chunk_buffer = b""
            return partial

    def write(self, data):
        self.written.append(data)
        text = data.decode(errors="replace")
        stripped = text.strip()
        if stripped.startswith("T") and len(stripped) > 1:
            if self.busy_until is not None and self.clock() < self.busy_until:
                # The stick is still "busy printing" a prior long line (see
                # schedule_rx's own docstring): this write's bytes arrive
                # while uart_getc_nonblock isn't being polled and come back
                # mangled, whatever they actually were.
                self.schedule_rx("TXERR empty or bad hex", at=self.clock())
                return len(data)
            if self.auto_confirm_tx:
                hexpart = stripped[1:]
                micros = self._next_tx_micros
                self._next_tx_micros += 1000
                # The firmware prints its TX confirmation the moment this write is
                # processed, but that is not necessarily the next byte the host reads:
                # a real UART is a strict FIFO, and a line already due (e.g. a late ack
                # from a previous retry, still in flight when this write happens) was
                # queued earlier and so is ahead of it on the wire. Scheduling this line
                # `due=now` and re-sorting (rather than inserting at position 0)
                # reproduces that ordering instead of always jumping the new
                # confirmation to the front.
                self.schedule_rx(f"TX {micros} {len(hexpart) // 2} {hexpart}", at=self.clock())
        return len(data)

    def reset_input_buffer(self):
        self.rx_queue.clear()

    def close(self):
        self.closed = True

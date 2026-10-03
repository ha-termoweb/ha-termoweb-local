"""Shared fixtures for the Home Assistant integration test suite.

FakeCulTransport stands in for a real pyserial transport, the same role
tests/fake_serial.py's FakeSerialTransport plays for the lower-level NanoCul suite,
but using real (short) wall-clock waits instead of a FakeClock: these tests exercise
a NanoCul instance driven from a background asyncio task via the executor (the
coordinator's own reader loop, docs/90-phase3-plan.md P4), and a shared FakeClock
mutated from two different executor threads (the reader loop and a poll/command
call) has no synchronisation story once threads are actually involved -- real time
with millisecond-scale sleeps avoids that hazard entirely and keeps each test under
a second.

Every T<hex> write is auto-acked: the transport parses the outgoing frame with
termoweb_frame.parse_frame to build the correct ack (acker = the frame's own
destination, per docs/PROTOCOL.md 5.5), so NanoCul.send_frame always succeeds
without a test having to hand-encode acks for whichever heater a test addresses.
"""
from __future__ import annotations

import pathlib
import time

import pytest

# This whole test package needs the .venv-hacs environment (pytest-homeassistant-
# custom-component pinned to core 2026.9.1; see custom_components/termoweb_local/
# README.md). Skip the package cleanly under the plain termoweb_local environment
# (.venv-termoweb-local) instead of failing collection for "pytest tests" there.
pytest.importorskip("pytest_homeassistant_custom_component")

from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from termoweb_local import frame as tf
from termoweb_local.heater import FLAG_BOOST, FLAG_EASY, FLAG_LOCKED, FLAG_RUNBACK
from termoweb_local.nanocul import ACK_ON_AIR_CLASS
from termoweb_local.network import (
    ADVANCED_SETUP_ACCEPTED_REPLY,
    ADVANCED_SETUP_OPCODE,
    ADVANCED_SETUP_READ_OPCODE,
    TOGGLE_BOOST_OPCODE,
    TOGGLE_EASY_OPCODE,
    TOGGLE_LOCK_OPCODE,
    TOGGLE_RUNBACK_OPCODE,
)

from custom_components.termoweb_local.const import (
    CONF_HEATER_ID,
    CONF_HEATER_NAME,
    CONF_HEATERS,
    CONF_SERIAL_URL,
    CONF_STATION_ID,
    DOMAIN,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def hass_config_dir() -> str:
    """Point HA's own config dir at this repo's root instead of the plugin's default
    `pytest_homeassistant_custom_component/testing_config` (which ships its own,
    unrelated `custom_components` regular package).

    `homeassistant.loader._async_mount_config_dir` does a plain `import
    custom_components` after inserting `hass.config.config_dir` onto `sys.path`; a
    plain import is only ever resolved once per process (cached in `sys.modules`),
    so whichever config dir wins that first import decides where every later
    `async_get_custom_components()` call looks, for the whole pytest run, not just
    one test. The plugin's own testing_config directory has an `__init__.py`
    (a regular package, not a namespace package), so if it is ever consulted first
    it wins outright over our repo root, permanently for the process, even though
    our own `custom_components/termoweb_local` also sits on sys.path. Overriding
    this fixture for every test in this suite makes the repo root win instead,
    every time, matching how a real HA OS install resolves this integration from
    its own config dir's `custom_components/`.
    """
    return str(REPO_ROOT)


TEST_STATION_ID = "01"
TEST_SERIAL_URL = "fake://bench"
TEST_HEATERS = [
    {CONF_HEATER_ID: 0x02, CONF_HEATER_NAME: "Living room"},
    {CONF_HEATER_ID: 0x04, CONF_HEATER_NAME: "Master bedroom"},
]
# The full three-heater default (docs/91-p4-parity-plan.md P4a: "heater names
# from the config... for ids 2, 3, 4 defaults"), for tests that need all
# three default slugs (entity_ids.py's own slug derivation).
THREE_HEATERS = [
    {CONF_HEATER_ID: 0x02, CONF_HEATER_NAME: "Living room"},
    {CONF_HEATER_ID: 0x03, CONF_HEATER_NAME: "Bedroom"},
    {CONF_HEATER_ID: 0x04, CONF_HEATER_NAME: "Master bedroom"},
]

# Real captured E6 status reply (docs/captures/2026-09-06-proof/f3-and-edges-results.md
# section 1: heater 04, off, setpoint 25.0), reused here for whichever heater a test
# addresses since FakeCulTransport is generic and node id is not part of this payload.
_STATUS_REPLY_PAYLOAD = bytes.fromhex("B90E2E2F0400F7321D4200002300")
_PROCESSED_COMMAND_REPLY = bytes([0xB5, 0x55])
_PROCESSED_PROGRAM_WRITE_REPLY = bytes([0xB3, 0x55])
_STATUS_REQUEST_PAYLOAD = bytes([0xB8])
_ENERGY_REQUEST_PAYLOAD = bytes([0xBC])
# The real EF replies, per node, from docs/captures/2026-09-06-phase3/
# nano-rx-162127.log's 18:00 sweep: `BD` then a 32-bit big-endian watt-hour
# counter (1620009, 2210563 and 1049657 Wh). Keyed by node id so an integration
# test can assert a different number per heater, and so the gateway total is a
# sum of three distinct values rather than one value counted three times.
_ENERGY_REPLY_PAYLOADS = {
    0x02: bytes.fromhex("bd0018b829"),
    0x03: bytes.fromhex("bd0021bb03"),
    0x04: bytes.fromhex("bd00100439"),
}
_ENERGY_REPLY_FALLBACK = bytes.fromhex("bd00000000")
_PROGRAM_READ_REQUEST_PAYLOAD = bytes([0xB0])
_PROGRAM_READ_REPLY_MARKER = 0xB1
_COMMAND_MARKER = 0xB4
_PROGRAM_WRITE_MARKER = 0xB2
_PRESET_WRITE_MARKER = 0xB6  # network.PRESET_WRITE_OPCODE
_PROCESSED_PRESET_WRITE_REPLY = bytes([0xB7, 0x55])
_FLASH_DISPLAY_REQUEST_PAYLOAD = bytes([0x5E, 0x01])
_FLASH_DISPLAY_REPLY_PAYLOAD = bytes([0x5F, 0x55])
# Network.startup_sequence's own burst (2026-09-06 16:53:30Z capture,
# docs/PROTOCOL.md 5.6's opcode table for the reply payloads): auto-answered
# here too, so a coordinator setup test does not have to wait out every
# step's 300 ms reply timeout for no reason.
_BURST_TERMINATOR_REQUEST_PAYLOAD = bytes([0xCA, 0x00])
_BURST_TERMINATOR_REPLY_PAYLOAD = bytes([0xCB, 0x55])
_STARTUP_C2_REQUEST_PAYLOAD = bytes([0xC2])
_STARTUP_C2_REPLY_PAYLOAD = bytes([0xC3, 0x55])
_STARTUP_IDENTITY_REQUEST_PAYLOAD = bytes([0x5A])
# Heater 04's whole captured E0 payload (docs/PROTOCOL.md 5.9, f3-and-edges-
# results.md): 5B, the E7 identity marker 77, 01 01 01 <id>, the 12-byte
# identity tail, then 0A 1A. The node id byte is filled in per request so each
# heater answers with its own id and a distinct identity, which is what the
# coordinator matches an E7 announcement against.
_STARTUP_IDENTITY_REPLY_TEMPLATE = bytes.fromhex(
    "5b7701010104a1b2c3d4e5f6071829304a5b0a1a"
)


def identity_reply_payload(node_id: int) -> bytes:
    payload = bytearray(_STARTUP_IDENTITY_REPLY_TEMPLATE)
    payload[5] = node_id
    payload[6] = node_id
    return bytes(payload)
_STARTUP_CAPABILITY_REQUEST_PAYLOAD = bytes([0xD0])
_STARTUP_CAPABILITY_REPLY_PAYLOAD = bytes([0xD1, 0x00, 0x05, 0x01, 0x01, 0x00, 0x27, 0x10])
_STARTUP_C6_REQUEST_PAYLOAD = bytes([0xC6])
_STARTUP_C6_REPLY_PAYLOAD = bytes([0xC7, 0x04, 0x00, 0x00, 0x00, 0x14, 0x00, 0x00, 0x00])
# All-zero nibbles (-> program slot 0, "cold", every hour) until a B2 write
# for that node has actually been seen; matches write_program()'s own
# PROGRAM_SLOT_NIBBLE[0] == 0x0.
_DEFAULT_PROGRAM_NIBBLES = bytes(84)

# D2/D6/D4/BA, the four one-bit toggles (2026-09-13 X-19 proof, PROTOCOL.md
# 5.6): each flips its own byte-24 flag bit in FakeCulTransport._toggle_flags
# and replies with its own opcode+1 (D3/D7/D5/BB) 55, never the generic
# B5 55 these heaters do not send for a toggle.
_TOGGLE_FLAG_BITS = {
    TOGGLE_BOOST_OPCODE: FLAG_BOOST,
    TOGGLE_EASY_OPCODE: FLAG_EASY,
    TOGGLE_RUNBACK_OPCODE: FLAG_RUNBACK,
    TOGGLE_LOCK_OPCODE: FLAG_LOCKED,
}
_TOGGLE_REQUEST_PAYLOAD_PREFIXES = tuple(bytes([opcode]) for opcode in _TOGGLE_FLAG_BITS)

# C4, the advanced-setup record: always accepted on this heater
# (2026-09-13 X-19 proof, docs/captures/2026-09-13-x19/notes.md), whatever
# the eight field values are -- this fake never rejects one, since the
# nine-byte max_stemp_limit form (the only rejection this session ever saw)
# is out of scope and never built by this project's own write_advanced_setup.
_ADVANCED_SETUP_REQUEST_PREFIX = bytes([ADVANCED_SETUP_OPCODE])

# F3 DA: heater 04's own worked DA reply (2026-09-13 X-19 proof,
# docs/captures/2026-09-13-x19/notes.md), fixed regardless of which node or
# what was last written to it via C4 -- this fake does not model per-heater
# eco/comfort/boost-temperature values, only the reply shape itself.
_ADVANCED_SETUP_READ_REQUEST_PAYLOAD = bytes([ADVANCED_SETUP_READ_OPCODE])
_ADVANCED_SETUP_READ_REPLY_PAYLOAD = bytes.fromhex("DB00242A032A0100000300000000002A06")


class FakeCulTransport:
    """Auto-acking in-memory transport; see module docstring.

    `status_reply_ids` (owner direction 2026-09-06 task 5, "make
    FakeCulTransport answer F3 B8 with an E6 for a configurable set of ids
    and stay silent for the rest"): None (the default) answers a status
    request for any dst, matching every pre-discovery test's own direct
    calls (climate commands, poll_now, force_refresh, ...), which only ever
    address an already-configured heater anyway. `setup_entry` below
    defaults this to the entry's own configured heater ids whenever a test
    leaves it unset, so the coordinator's own ids-2-to-65 setup scan does
    not spuriously "discover" every other id in that range; a discovery
    test overrides it explicitly (before calling setup_entry) to include
    ids beyond the configured heaters."""

    def __init__(
        self,
        read_timeout: float = 0.02,
        auto_reply_q: bool = True,
        status_reply_ids: set[int] | None = None,
    ) -> None:
        self.read_timeout = read_timeout
        self.auto_reply_q = auto_reply_q
        self.status_reply_ids = status_reply_ids
        self.rx_queue: list[bytes] = []
        self.written: list[bytes] = []
        self.closed = False
        self._next_micros = 1
        # dst node id -> the 84 raw nibble bytes from its last B2 program
        # write, so a later F3 B0 read for that same node reads back what was
        # actually written (docs/91-p4-parity-plan.md P4a set_schedule round
        # trip), not just the fixed default.
        self._programs: dict[int, bytes] = {}
        # dst node id -> the 3 preset half-degree bytes from its last B6
        # write, so a later F3 B8 status request for that same node reads
        # back what was actually written, the same round-trip role
        # self._programs plays for B2/B0.
        self._presets: dict[int, bytes] = {}
        # dst node id -> its own byte-24 flag word, one bit per D2/D6/D4/BA
        # toggle it has been sent (2026-09-13 X-19 proof), ORed into the
        # F3 B8 status reply's own index 11 (E6's flags byte) so a test can
        # read a toggle back the same way a real heater's status report
        # would show it.
        self._toggle_flags: dict[int, int] = {}
        # dst node id -> the 8 raw bytes from its last C4 write, for a test
        # to assert what was actually sent (the same introspection role
        # self._presets/self._programs play for B6/B2); not fed back into
        # any reply, since F3 DA's own reply is fixed regardless of what was
        # last written (see _ADVANCED_SETUP_READ_REPLY_PAYLOAD's own comment).
        self._advanced_setup_writes: dict[int, bytes] = {}

    def queue_line(self, line: str) -> None:
        """Queue an arbitrary line (e.g. an unsolicited E5 report) to be read next."""
        self.rx_queue.append(line.encode() + b"\n")

    def readline(self) -> bytes:
        if self.rx_queue:
            return self.rx_queue.pop(0)
        time.sleep(self.read_timeout)
        return b""

    def write(self, data: bytes) -> int:
        self.written.append(data)
        text = data.decode(errors="replace").strip()
        if text == "Q" and self.auto_reply_q:
            # firmware/README.md "Serial protocol": the Q reply, not the boot
            # banner (those are two distinctly-formatted lines; only this one is
            # what a real stick sends in response to Q).
            self.rx_queue.append(
                b"# Q termoweb_rx 3.2 freq=869.525 pa=0xC0 sync=2DE5 "
                b"mode=dynamic autoack=off id=01\n"
            )
        elif text.startswith("T") and len(text) > 1:
            hexpart = text[1:]
            micros = self._next_micros
            self._next_micros += 1000
            self.rx_queue.append(
                f"TX {micros} {len(hexpart) // 2} {hexpart}\n".encode()
            )
            air = bytes.fromhex(hexpart)
            # An ack is never itself acked or answered (docs/PROTOCOL.md
            # 5.5/5.9; coordinator.py's own send_raw_frame docstring): the
            # station's software ack to a pairing heater's E7 announcement
            # is itself an on-air ack-class frame, and generating a further
            # bogus "ack of the ack" here (as every earlier version of this
            # fake did, unconditionally) would sit unread in the queue and
            # desync whatever real TX-confirmation/ack matching the very
            # next operation on this same transport does.
            if int(hexpart[:2], 16) == ACK_ON_AIR_CLASS:
                return len(data)
            parsed = tf.parse_frame(air)
            ack_air = tf.build_ack(parsed.dst, parsed.src)
            self.rx_queue.append(
                f"RX {micros + 1} -44.0 60 0 {ack_air.hex().upper()}\n".encode()
            )
            application_reply = self._application_reply_payload(parsed)
            if application_reply is not None:
                # A heater's own reply (E6 status, B5 55/B3 55 processed), src
                # and dst swapped from the command frame, hops 01 01 01 (a
                # heater's own outgoing hops, docs/PROTOCOL.md section 4),
                # arriving right behind its ack (2026-09-06 proof,
                # f3-and-edges-results.md section 1).
                reply_air = tf.build_frame(
                    parsed.dst, parsed.src, application_reply, hops=(1, 1, 1)
                )
                self.rx_queue.append(
                    f"RX {micros + 2} -44.0 60 0 {reply_air.hex().upper()}\n".encode()
                )
        return len(data)

    def _application_reply_payload(self, parsed) -> bytes | None:
        """The heater-side application reply this transport auto-generates for
        a command frame, mirroring the 2026-09-06 proof session
        (f3-and-edges-results.md section 1, filtering-results.md "The B5 55
        reply as a processed indicator"; B6/B7 55, 2026-09-12 X-18 proof,
        docs/captures/2026-09-12-x18-s7/notes.md; D2/D6/D4/BA/C4/DA, 2026-09-13
        X-19 proof, docs/captures/2026-09-13-x19/notes.md): F3 B8 -> E6 status
        (its own anti-frost/eco/comfort bytes substituted by whatever that
        node's own last B6 write sent, or the fixed default if it never wrote
        one, and its own flags byte ORed with whatever D2/D6/D4/BA toggles
        that node has been sent), F3 BC -> EF energy counter (docs/PROTOCOL.md
        5.6), F3 B0 -> 9F program read-back (of whatever that node's own last
        B2 write sent, or all-zero nibbles if it never wrote one), F3 DA -> the
        fixed DB advanced-setup-record reply (2026-09-13 X-19 proof), any B4 xx
        command -> F2 B5 55, a B2 program write -> F2 B3 55 (and records the
        write for a later B0 read-back), a B6 preset write -> F2 B7 55 (and
        records the write for a later B8 read-back), a D2/D6/D4/BA toggle ->
        that opcode's own D3/D7/D5/BB 55 (and flips its own byte-24 flag bit,
        read back by a later F3 B8), a C4 advanced-setup write -> F2 C5 55
        (and records the write for a test to assert against, never fed back
        into any reply). None for anything else (no reply, matching the real
        "57 55 elicits nothing" proof for the report confirmation this
        transport also has to answer, which the coordinator sends but never
        expects anything back for)."""
        request_payload = parsed.payload
        if request_payload == _STATUS_REQUEST_PAYLOAD:
            if self.status_reply_ids is not None and parsed.dst not in self.status_reply_ids:
                return None
            payload = bytearray(_STATUS_REPLY_PAYLOAD)
            presets = self._presets.get(parsed.dst)
            if presets is not None:
                payload[1:4] = presets
            payload[11] |= self._toggle_flags.get(parsed.dst, 0)
            return bytes(payload)
        if request_payload == _ADVANCED_SETUP_READ_REQUEST_PAYLOAD:
            return _ADVANCED_SETUP_READ_REPLY_PAYLOAD
        if request_payload[:1] in _TOGGLE_REQUEST_PAYLOAD_PREFIXES:
            opcode = request_payload[0]
            bit = _TOGGLE_FLAG_BITS[opcode]
            on = request_payload[1:2] == b"\x01"
            flags = self._toggle_flags.get(parsed.dst, 0)
            self._toggle_flags[parsed.dst] = (flags | bit) if on else (flags & ~bit)
            return bytes([opcode + 1, 0x55])
        if request_payload[:1] == _ADVANCED_SETUP_REQUEST_PREFIX:
            self._advanced_setup_writes[parsed.dst] = bytes(request_payload[1:])
            return bytes(ADVANCED_SETUP_ACCEPTED_REPLY)
        if request_payload == _ENERGY_REQUEST_PAYLOAD:
            if self.status_reply_ids is not None and parsed.dst not in self.status_reply_ids:
                return None
            return _ENERGY_REPLY_PAYLOADS.get(parsed.dst, _ENERGY_REPLY_FALLBACK)
        if request_payload == _PROGRAM_READ_REQUEST_PAYLOAD:
            nibbles = self._programs.get(parsed.dst, _DEFAULT_PROGRAM_NIBBLES)
            return bytes([_PROGRAM_READ_REPLY_MARKER]) + nibbles
        if request_payload[:1] == bytes([_COMMAND_MARKER]):
            return _PROCESSED_COMMAND_REPLY
        if request_payload[:1] == bytes([_PROGRAM_WRITE_MARKER]):
            self._programs[parsed.dst] = bytes(request_payload[1:])
            return _PROCESSED_PROGRAM_WRITE_REPLY
        if request_payload[:1] == bytes([_PRESET_WRITE_MARKER]):
            self._presets[parsed.dst] = bytes(request_payload[1:])
            return _PROCESSED_PRESET_WRITE_REPLY
        if request_payload == _FLASH_DISPLAY_REQUEST_PAYLOAD:
            return _FLASH_DISPLAY_REPLY_PAYLOAD
        if request_payload == _BURST_TERMINATOR_REQUEST_PAYLOAD:
            return _BURST_TERMINATOR_REPLY_PAYLOAD
        if request_payload == _STARTUP_C2_REQUEST_PAYLOAD:
            return _STARTUP_C2_REPLY_PAYLOAD
        if request_payload == _STARTUP_IDENTITY_REQUEST_PAYLOAD:
            return identity_reply_payload(parsed.dst)
        if request_payload == _STARTUP_CAPABILITY_REQUEST_PAYLOAD:
            return _STARTUP_CAPABILITY_REPLY_PAYLOAD
        if request_payload == _STARTUP_C6_REQUEST_PAYLOAD:
            return _STARTUP_C6_REPLY_PAYLOAD
        return None

    def reset_input_buffer(self) -> None:
        self.rx_queue.clear()

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_transport() -> FakeCulTransport:
    return FakeCulTransport()


def patch_coordinator_nanocul(monkeypatch, transport: FakeCulTransport) -> None:
    """Make the coordinator's NanoCul open against `transport` instead of a real
    port, with no reset delay and fast retries."""
    from termoweb_local.nanocul import NanoCul

    def _factory(url, source_id):
        return NanoCul(
            url=url,
            source_id=source_id,
            transport=transport,
            reset_wait=0,
            retries=2,
            retry_interval=0.02,
        )

    monkeypatch.setattr(
        "custom_components.termoweb_local.coordinator.NanoCul", _factory
    )


def patch_config_flow_nanocul(monkeypatch, transport: FakeCulTransport) -> None:
    """Make config_flow's port-validation NanoCul (a lazy `from termoweb_local.nanocul
    import NanoCul`) open against `transport` instead of a real port."""
    from termoweb_local import nanocul as nanocul_module

    real_nanocul_cls = nanocul_module.NanoCul

    def _factory(url, source_id):
        return real_nanocul_cls(
            url=url, source_id=source_id, transport=transport, reset_wait=0
        )

    monkeypatch.setattr(nanocul_module, "NanoCul", _factory)


def shrink_scan_timeout(monkeypatch, timeout: float = 0.01) -> None:
    """Every coordinator setup now runs a 2-65 discovery scan
    (TermowebLocalCoordinator.async_setup); Network.scan_for_heaters reads
    its own per-id timeout from the module global SCAN_REPLY_TIMEOUT_S at
    call time (not as a bound default argument) specifically so a test can
    shrink it like this, keeping a full 64-id sweep of a FakeCulTransport
    fast (0.01s * 64 ~= 0.6s) without changing the 150 ms production
    default or passing a timeout through every caller."""
    from termoweb_local import network as network_module

    monkeypatch.setattr(network_module, "SCAN_REPLY_TIMEOUT_S", timeout)


async def setup_entry(hass, monkeypatch, transport: FakeCulTransport, **entry_kwargs):
    """make_config_entry + patch_coordinator_nanocul + async_setup, the common
    setup path for climate/sensor/binary_sensor/service/coordinator tests.

    Defaults `transport.status_reply_ids` to the entry's own configured
    heater ids when a test has not already set it explicitly (see
    FakeCulTransport's own docstring), and shrinks the discovery scan's
    reply timeout so the setup-time 2-65 sweep this now always runs stays
    fast."""
    patch_coordinator_nanocul(monkeypatch, transport)
    shrink_scan_timeout(monkeypatch)
    if transport.status_reply_ids is None:
        heaters = entry_kwargs.get("heaters", TEST_HEATERS)
        transport.status_reply_ids = {h[CONF_HEATER_ID] for h in heaters}
    entry = make_config_entry(hass, **entry_kwargs)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def make_config_entry(
    hass,
    *,
    serial_url: str = TEST_SERIAL_URL,
    station_id: str = TEST_STATION_ID,
    heaters: list[dict] | None = None,
    options: dict | None = None,
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SERIAL_URL: serial_url,
            CONF_STATION_ID: station_id,
            CONF_HEATERS: heaters if heaters is not None else list(TEST_HEATERS),
        },
        options=options or {},
        unique_id=serial_url,
    )
    entry.add_to_hass(hass)
    return entry

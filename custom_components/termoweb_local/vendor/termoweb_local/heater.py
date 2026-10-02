"""Heater state: a flat, immutable snapshot per E5 frame, plus a small mutable holder
for the last snapshot, last report time, last energy counter and retry count that link
status is derived from.

Byte layout (PROTOCOL.md section 5.4, and the 2026-09-06 phase3 facts confirmed
against docs/captures/2026-09-06-phase3/nano-rx-101909.log and notes.md, which this
module's own test suite re-derives from that log rather than trusting by assertion):
E5 payload bytes 14, 15 and 16 are the anti-frost, eco and comfort presets in half
degrees (PROTOCOL.md 5.4; proven 2026-09-09 by single-variable preset changes at the
panel, corrected from an earlier reading of bytes 12-16 as identifier-like per-heater
constants -- they looked constant only because no capture before then had changed a
preset), byte 17 is a mode code (01 auto, 02 manual heat, 03 temporary override,
04 off), bytes 18-19 are room temperature in tenths of a degree big-endian, byte 20
is the setpoint in half degrees, bytes 21-22 are the heater's own measured full-load
power in deciwatts big-endian, and bytes 23-26 are carried through as raw values
(byte 23 a duty-percent candidate and byte 24 a heating-flag candidate, per the
phase3 notes, neither proven; bytes 25-26 unknown).

No RSSI or LQI anywhere in this module: link status is derived only from report
cadence and retry count (PROTOCOL.md section 6), per the plan's ground rules.
"""
import dataclasses
import enum
import time

E5_PAYLOAD_LEN = 15
E6_PAYLOAD_LEN = E5_PAYLOAD_LEN - 1  # the E5 payload with its leading marker byte dropped
E3_PAYLOAD_LEN = 17  # PROTOCOL.md 5.10: E5's fields to byte 22, then 2 extra tail bytes
E4_PAYLOAD_LEN = E6_PAYLOAD_LEN + 2  # E6's fields plus the same 2-byte boost tail E3
# adds to E5 (docs/captures/2026-09-12-schedule/notes.md, "Max Temp found, and a new
# status reply class E4 under Runback"): the on-demand F3 B8 status reply a heater
# gives while its own Runback boost is running, in place of the ordinary 14-byte E6.
E6_DROPPED_MARKER_BYTE = 0x56  # constant envelope byte E5/E2 payloads carry at byte 0
# (2026-09-06 proof, notes.md 13:18:42Z-adjacent entries: "E5 payload starts 56 B9,
# E2 starts 56 DB"); prepending it back onto an E6 payload re-aligns every other
# field to the same offsets from_frame() already uses.

# Logical bytes 21-22 (E5 and E3) / 20-21 (E6 and E4), big-endian deciwatts: the
# heater's own measured full-load power, per PROTOCOL.md 5.4. Keyed by payload length
# because that is also the frame class (the on-air class byte is the logical length
# XOR 0xFF), so one table covers all four classes without a second class dispatch.
MEASURED_POWER_OFFSETS = {
    E5_PAYLOAD_LEN: 9,
    E6_PAYLOAD_LEN: 8,
    E3_PAYLOAD_LEN: 9,
    E4_PAYLOAD_LEN: 8,
}

# The optional 2-byte boost-expiry tail E3 and E4 each add after their E5/E6-aligned
# fields (PROTOCOL.md 5.10): big-endian, high 4 bits the day (Sunday-first, the same
# indexing as the schedule's own day order), low 12 bits the minute of day. It always
# sits at the payload's own last 2 bytes, whether or not the payload carries E5's
# leading `56` marker, so one offset (-2) covers both classes.
BOOST_TAIL_PAYLOAD_LENS = (E3_PAYLOAD_LEN, E4_PAYLOAD_LEN)


def decode_boost_tail(payload):
    """The (day, minute_of_day) an E3 or E4 payload's boost tail carries, or
    (None, None) for a payload of any other length (E5 and E6 have no tail)."""
    if len(payload) not in BOOST_TAIL_PAYLOAD_LENS:
        return None, None
    raw = (payload[-2] << 8) | payload[-1]
    return raw >> 12, raw & 0x0FFF


def decode_measured_power_w(payload):
    """The measured full-load power in watts carried by an E5, E6 or E3 payload,
    or None for a payload of any other length (E2 has no such field)."""
    offset = MEASURED_POWER_OFFSETS.get(len(payload))
    if offset is None:
        return None
    return ((payload[offset] << 8) | payload[offset + 1]) / 10.0


# The cumulative watt-hour counter an EF frame carries (PROTOCOL.md 5.6): payload
# `BD` then 4 big-endian bytes, the reply to an F3 BC request. Keyed by payload
# length for the same reason MEASURED_POWER_OFFSETS is: payload length is the frame
# class (the on-air class byte is the logical length XOR 0xFF), so 5 bytes is EF and
# nothing else. Wire unit is watt-hours; no scaling, no duty term and no elapsed-time
# term is applied anywhere between here and the sensor.
ENERGY_PAYLOAD_LEN = 5
ENERGY_COUNTER_OFFSETS = {ENERGY_PAYLOAD_LEN: 1}


def decode_energy_wh(payload):
    """The cumulative energy counter in watt-hours carried by an EF payload, or
    None for a payload of any other length."""
    offset = ENERGY_COUNTER_OFFSETS.get(len(payload))
    if offset is None:
        return None
    return int.from_bytes(bytes(payload[offset:offset + 4]), "big")


# E5 byte 17 / F2 and F1 mode-write payloads (PROTOCOL.md 5.1, 5.2; phase3 facts for
# auto and override). Data, not literals baked into the decode logic below.
MODE_AUTO = 0x01
MODE_MANUAL = 0x02
MODE_OVERRIDE = 0x03
MODE_OFF = 0x04

MODE_NAMES = {
    MODE_AUTO: "auto",
    MODE_MANUAL: "manual",
    MODE_OVERRIDE: "override",
    MODE_OFF: "off",
}

IDLE_REPORT_PERIOD_S = 300.0
HEATING_REPORT_PERIOD_S = 104.0
STALE_FACTOR = 2.0  # how many report periods without a report before "stale"

# Byte 24 flag bits (PROTOCOL.md line 409: "an eight-bit flag word, not a
# second duty byte... the gateway splits every bit separately and gives each
# its own cloud key"). Bits 1 (locked), 6 (easy) and 7 (runback) were driven
# on air by BA/D6/D4 respectively, and bit 5 (boost) by D2, all 2026-09-13
# (X-19, PROTOCOL.md 5.6); bits 3 (window_open) and 4 (true_radiant_active)
# have never been seen set on these three heaters, which is a statement
# about what they have done, not about what the field can hold.
FLAG_ACTIVE = 0x01
FLAG_LOCKED = 0x02
FLAG_PRESENCE = 0x04
FLAG_WINDOW_OPEN = 0x08
FLAG_TRUE_RADIANT_ACTIVE = 0x10
FLAG_BOOST = 0x20
FLAG_EASY = 0x40
FLAG_RUNBACK = 0x80


class LinkState(enum.Enum):
    OK = "ok"
    STALE = "stale"
    LOST = "lost"


@dataclasses.dataclass(frozen=True)
class HeaterSnapshot:
    """One E5 report, decoded only as far as PROTOCOL.md and the phase3 facts allow.
    Never mutated or patched; a new report is a new snapshot."""

    node_id: int
    mode_code: int
    room_temp_c: float
    setpoint_c: float
    # payload bytes 0-4 (logical bytes 12-16): the `56` report marker and the `B9`
    # status record type (constant), then the anti-frost, eco and comfort presets
    # (payload bytes 2-4), which are per-heater but NOT constant -- a panel change
    # moves them (PROTOCOL.md 5.4). Despite the name, only bytes 0-1 are a true
    # per-heater constant; the preset properties below are the field this snapshot
    # actually offers callers for bytes 2-4, so nothing else re-derives the offsets.
    identifier: bytes
    measured_power_w: float  # payload bytes 9-10 (logical bytes 21-22), full load
    duty_candidate: int  # payload byte 11 (logical byte 23), unproven
    heating_flag_candidate: int  # payload byte 12 (logical byte 24), unproven
    raw_byte25: int
    raw_byte26: int
    received_at: float
    # Set from an E3 or E4 payload's own boost tail (PROTOCOL.md 5.10); None on
    # every plain E5/E6 snapshot, which carries no such tail at all.
    boost_end_day: int | None = None
    boost_end_min: int | None = None

    @property
    def mode(self):
        """The mode name for `mode_code`, or None for a code not yet in MODE_NAMES."""
        return MODE_NAMES.get(self.mode_code)

    @property
    def anti_frost_c(self) -> float:
        """Anti-frost preset, logical byte 14, half-degree Celsius plain
        (PROTOCOL.md 5.4). This is the target a schedule's cold slot (code 0)
        governs (`docs/80-handover.md` list C item 2: byte 20, the governing
        setpoint, reads this value while auto mode is in a cold slot)."""
        return self.identifier[2] / 2.0

    @property
    def eco_c(self) -> float:
        """Eco preset, logical byte 15, half-degree Celsius plain (PROTOCOL.md 5.4).
        The target a schedule's night slot (code 1) governs."""
        return self.identifier[3] / 2.0

    @property
    def comfort_c(self) -> float:
        """Comfort preset, logical byte 16, half-degree Celsius plain
        (PROTOCOL.md 5.4). The target a schedule's day slot (code 2) governs."""
        return self.identifier[4] / 2.0

    def _flag(self, bit: int) -> bool | None:
        """One byte-24 flag bit, or None when heating_flag_candidate itself is
        None -- a guard consistent with the field's own type, since a real
        decode (from_frame/from_e6) always populates it today."""
        if self.heating_flag_candidate is None:
            return None
        return bool(self.heating_flag_candidate & bit)

    @property
    def active(self) -> bool | None:
        """Byte 24 bit 0: the heating flag (PROTOCOL.md line 409)."""
        return self._flag(FLAG_ACTIVE)

    @property
    def locked(self) -> bool | None:
        """Byte 24 bit 1: the keypad lock (BA, 2026-09-13 X-19 proof)."""
        return self._flag(FLAG_LOCKED)

    @property
    def presence(self) -> bool | None:
        """Byte 24 bit 2 (PROTOCOL.md line 409)."""
        return self._flag(FLAG_PRESENCE)

    @property
    def window_open(self) -> bool | None:
        """Byte 24 bit 3: open-window detection, never observed set on these
        three heaters (PROTOCOL.md line 409)."""
        return self._flag(FLAG_WINDOW_OPEN)

    @property
    def true_radiant_active(self) -> bool | None:
        """Byte 24 bit 4, never observed set on these three heaters
        (PROTOCOL.md line 409)."""
        return self._flag(FLAG_TRUE_RADIANT_ACTIVE)

    @property
    def boost(self) -> bool | None:
        """Byte 24 bit 5: Boost (D2, 2026-09-13 X-19 proof)."""
        return self._flag(FLAG_BOOST)

    @property
    def easy(self) -> bool | None:
        """Byte 24 bit 6: EASY mode (D6, 2026-09-13 X-19 proof)."""
        return self._flag(FLAG_EASY)

    @property
    def runback(self) -> bool | None:
        """Byte 24 bit 7: Runback Config (D4, 2026-09-13 X-19 proof)."""
        return self._flag(FLAG_RUNBACK)

    # Schedule slot code (network.py's own normalised 0/1/2/None, the same value
    # every encoding -- the 9E/9F nibble family and C9's 2-bit family alike --
    # is already reduced to before it ever reaches a snapshot; see
    # network._decode_program_nibbles/_decode_program_bits) -> which preset
    # property answers it. Cloud order confirmed independently, docs/
    # 20-cloud-api-summary.md: "ptemp[3] preset temps for cold/night/day".
    _SLOT_PRESET_ATTR = {0: "anti_frost_c", 1: "eco_c", 2: "comfort_c"}

    def preset_target_c(self, slot_code):
        """The preset target temperature this snapshot's own anti-frost/eco/comfort
        presets give for a normalised schedule slot code (0 cold, 1 night, 2 day).
        None for any other `slot_code`, including None itself: that covers an hour
        whose raw nibble or 2-bit code was not one of the three recognised values
        (already turned into None upstream, before this ever sees it) and any other
        out-of-range input, so this never returns a confidently wrong temperature
        for a code it does not recognise."""
        attr = self._SLOT_PRESET_ATTR.get(slot_code)
        return None if attr is None else getattr(self, attr)

    @classmethod
    def _from_e5_aligned_payload(cls, node_id, payload, received_at, boost_end_day=None, boost_end_min=None):
        """Shared field decoding for from_frame() and from_e6(): both hand this a
        15-byte-or-longer payload aligned the same way an E5 payload is (from_e6()
        prepends the marker byte E6 drops before calling this), so the byte offsets
        below are the only place either mode/room/setpoint/etc. field position is
        written down. An E3 or E4 payload is longer than 15 (its 2-byte boost tail
        trails these same offsets), which callers decode separately and pass in
        here rather than this method re-deriving it from a length it does not
        otherwise need to know."""
        return cls(
            node_id=node_id,
            mode_code=payload[5],
            room_temp_c=((payload[6] << 8) | payload[7]) / 10.0,
            setpoint_c=payload[8] / 2.0,
            identifier=bytes(payload[0:5]),
            measured_power_w=decode_measured_power_w(payload),
            duty_candidate=payload[11],
            heating_flag_candidate=payload[12],
            raw_byte25=payload[13],
            raw_byte26=payload[14],
            received_at=time.time() if received_at is None else received_at,
            boost_end_day=boost_end_day,
            boost_end_min=boost_end_min,
        )

    @classmethod
    def from_frame(cls, parsed_frame, received_at=None):
        """Build a snapshot from a termoweb_local.frame.parse_frame() result for a
        29-byte E5 frame, or a 31-byte E3 frame (E5 plus PROTOCOL.md 5.10's 2-byte
        boost tail, decoded into boost_end_day/boost_end_min) -- src is the
        reporting heater's node id either way."""
        payload = parsed_frame.payload
        if len(payload) not in (E5_PAYLOAD_LEN, E3_PAYLOAD_LEN):
            raise ValueError(
                f"expected a {E5_PAYLOAD_LEN}-byte E5 payload or a "
                f"{E3_PAYLOAD_LEN}-byte E3 payload, got {len(payload)}"
            )
        boost_end_day, boost_end_min = decode_boost_tail(payload)
        return cls._from_e5_aligned_payload(
            parsed_frame.src, payload, received_at, boost_end_day, boost_end_min
        )

    @classmethod
    def from_e6(cls, parsed_frame, received_at=None):
        """Build a snapshot from a parse_frame() result for a 28-byte E6 frame, the
        on-demand status reply to an F3 B8 request (2026-09-06 proof,
        f3-and-edges-results.md section 1), or a 30-byte E4 frame (the same reply
        while the heater's own Runback boost is running, E6 plus the same 2-byte
        boost tail E3 adds to E5, docs/captures/2026-09-12-schedule/notes.md). Either
        payload is the corresponding E5/E3 payload with byte 0 (the constant 0x56
        marker) dropped, so mode/room/setpoint/etc. each sit one byte earlier than
        in an E5/E3. Prepending that marker byte back and reusing from_frame()'s own
        field decoding keeps the offsets in one place."""
        payload = parsed_frame.payload
        if len(payload) not in (E6_PAYLOAD_LEN, E4_PAYLOAD_LEN):
            raise ValueError(
                f"expected a {E6_PAYLOAD_LEN}-byte E6 payload or a "
                f"{E4_PAYLOAD_LEN}-byte E4 payload, got {len(payload)}"
            )
        boost_end_day, boost_end_min = decode_boost_tail(payload)
        aligned = bytes([E6_DROPPED_MARKER_BYTE]) + bytes(payload)
        return cls._from_e5_aligned_payload(
            parsed_frame.src, aligned, received_at, boost_end_day, boost_end_min
        )


class Heater:
    """Mutable per-node holder: the last snapshot, when it arrived, the last energy
    counter read off this heater, and how many unacked retries are outstanding right
    now. Link state is a property computed from the report time and retry count and
    nothing else.

    The energy counter lives here rather than on HeaterSnapshot because it is not an
    E5 field at all: it only ever arrives in an EF frame answering an F3 BC request
    (PROTOCOL.md 5.6), on its own cadence, so a snapshot has nothing to say about it
    and would carry None on every report."""

    def __init__(self, node_id):
        self.node_id = node_id
        self.last_snapshot = None
        self.last_report_time = None
        self.last_energy_wh = None
        self.retry_count = 0
        # The last F3 DA reply decoded for this node (network.AdvancedRecord),
        # or None before any read: like last_energy_wh, this is not an E5
        # field, it only ever arrives answering its own on-demand request
        # (2026-09-13 X-19 proof, PROTOCOL.md 5.6), read once at
        # startup/registration and once after every boost start/cancel.
        self.last_advanced_record = None

    def record_snapshot(self, snapshot):
        """A fresh report arrived: replace the snapshot wholesale, reset retries."""
        self.last_snapshot = snapshot
        self.last_report_time = snapshot.received_at
        self.retry_count = 0

    def record_energy(self, energy_wh):
        """A fresh EF energy reply arrived. The counter is the heater's own meter, so
        it is stored verbatim and never reset by this station: it survives a station
        restart, a re-pairing, and a factory reset of the station side."""
        self.last_energy_wh = energy_wh

    def record_retry(self):
        self.retry_count += 1

    def record_ack(self):
        self.retry_count = 0

    def expected_report_period(self):
        """Idle or heating cadence (PROTOCOL.md section 6), picked from the last
        snapshot's heating-flag candidate; falls back to idle when unknown or off."""
        snap = self.last_snapshot
        if snap is None or snap.mode_code == MODE_OFF:
            return IDLE_REPORT_PERIOD_S
        if snap.heating_flag_candidate:
            return HEATING_REPORT_PERIOD_S
        return IDLE_REPORT_PERIOD_S

    def link_state(self, now=None, max_retries=3, stale_factor=STALE_FACTOR):
        """OK: reporting on cadence. STALE: no report in over stale_factor report
        periods. LOST: retries exhausted with no ack. Never uses RSSI or LQI."""
        if self.retry_count >= max_retries:
            return LinkState.LOST
        if self.last_report_time is None:
            return LinkState.STALE
        now = time.time() if now is None else now
        if now - self.last_report_time > self.expected_report_period() * stale_factor:
            return LinkState.STALE
        return LinkState.OK

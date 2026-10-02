"""Network: ids, poll cadence, and the per-node command/encode helpers for one
termoweb_local station.

Ground rule (docs/90-phase3-plan.md P2 scope): no node id or opcode is hardcoded in the
logic here unless its meaning has actually been proven. The EB per-device payload
table and any F3 opcode besides the two below remain opaque configuration data a
caller loads and passes in at construction; this module stores and forwards them, it
never switches on their values. The command payloads it does build (setpoint, mode,
override, poll/confirm, clock sync, program write, status request, program read) are
not opaque: they are given, proven formats from PROTOCOL.md sections 5.1/5.2 and the
2026-09-06 phase3 and proof-session facts, re-derived and checked against
docs/captures/2026-09-06-phase3/nano-rx-101909.log,
docs/captures/2026-09-06-proof/notes.md, filtering-results.md and
f3-and-edges-results.md in this package's own tests rather than trusted by assertion.

Three of the previously-opaque F3 opcodes are resolved (B8 status and B0 program read
from the 2026-09-06 proof session, BC energy from the EF frames in the capture
corpus), so those three get named methods here (request_status, read_program,
read_energy) instead of going through the generic request() opcode-table path; every
other F3 opcode is still sent through request(node, opcode) with no meaning attached
by this module. flash_display (F2 5E 01 -> F2 5F 55) is likewise a proven, named
command (2026-09-06 P4b proof). startup_sequence sends the
gateway's own captured power-up burst for a heater (EB association, F3 B0, F2 CA 00,
F3 C2/5A/D0/C6); it names the burst's shape and order, not the C2/5A/D0/C6 opcodes'
own meaning, which stays opaque per the ground rule above -- StartupResult carries
each one's raw reply payload for a caller to store, not a decoded field.

request_status, read_program, read_energy, request, wait_processed, flash_display and
startup_sequence are round trips: unlike every other method here (a pure frame
builder returning bytes), they need a NanoCul bound via bind_nanocul() or the
constructor's nanocul= argument to actually send and wait for the reply.

Discovery/pairing (2026-09-06 17:12:17Z capture, 2026-09-09 bench attempts): a heater
with no assigned node id announces itself with an E7 frame from the broadcast id
`FF`, sweeping the destination id across the whole range about 200 ms at a time,
and every already-paired heater relays a copy of that announcement to the station
under its own source id (PROTOCOL.md 5.9), so an E7 carrying the identity marker is
a pairing announcement whatever its src and dst are. start_discovery() opens a
window during which handle_discovery_frame() matches the announcement against
known_identities (reusing a previously assigned id for the same identity) or hands
out the lowest free id, and builds the F3 <id> assignment frame a caller sends back
to `FF`. This module only builds that frame and tracks the identity/id table handed
to it; a caller (the coordinator) decides when discovery is on and what receiving a
Piece off the wire means.
"""
import dataclasses
import time

from . import frame as tf

GATEWAY_ID = 0x01
DEFAULT_STATION_ID = 0x01
IDLE_POLL_PERIOD_S = 300.0  # mirrors the gateway (PROTOCOL.md section 6)

# F1/F2 payload markers (PROTOCOL.md 5.1, 5.2; phase3 facts for auto/override).
WRITE_MARKER = 0xB4
MODE_HEAT = 0x02  # F2 B4 02: mode manual/heat
MODE_OFF = 0x04  # F2 B4 04: mode off
MODE_AUTO = 0x01  # F2 B4 01: mode auto (phase3 fact)
OVERRIDE_WRITE = 0x03  # F1 B4 03 <half-degrees>: temporary override setpoint (phase3 fact)
SETPOINT_WRITE = 0x02  # F1 B4 02 <half-degrees>: manual setpoint (PROTOCOL.md 5.1)

# B6 <anti-frost> <eco> <comfort>: preset temperature write, three half-degree
# bytes in the same order and encoding as E5 bytes 14-16 (2026-09-12 X-18 proof,
# docs/captures/2026-09-12-x18-s7/notes.md: heater 04 acked `F0 9C 88 58 B3 A1
# CD 20 57 5E 4B 9C B8 E7 CF 7A 32 4D`, logical payload `B6 0E 25 2A`, then
# answered `F2 B7 55`). Unlike SETPOINT_WRITE/OVERRIDE_WRITE, this opcode is
# the payload's own first byte, not a WRITE_MARKER-prefixed sub-code -- the
# same shape PROGRAM_OPCODE (B2) already uses.
PRESET_WRITE_OPCODE = 0xB6

# D2/D6/D4/BA, the four one-bit toggles proven on air 2026-09-13 (X-19,
# PROTOCOL.md 5.6): `<opcode> 01`/`<opcode> 00`, each replied
# `<opcode + 1> 55` (D3/D7/D5/BB), the same request-plus-one rule every other
# opcode in that section follows. D2 is boost start/cancel (the payload byte
# is masked to a single bit: D2 02 and D2 05 produce the same boost as D2 01),
# D6 is EASY mode (forces mode heat, setpoint unchanged), D4 is Runback
# Config (forces mode heat and the setpoint to the anti-frost preset; turning
# it off does not restore the displaced setpoint), BA is the keypad lock.
TOGGLE_BOOST_OPCODE = 0xD2
TOGGLE_EASY_OPCODE = 0xD6
TOGGLE_RUNBACK_OPCODE = 0xD4
TOGGLE_LOCK_OPCODE = 0xBA
TOGGLE_REPLY_PAYLOADS = {
    opcode: bytes([opcode + 1, 0x55])
    for opcode in (TOGGLE_BOOST_OPCODE, TOGGLE_EASY_OPCODE, TOGGLE_RUNBACK_OPCODE, TOGGLE_LOCK_OPCODE)
}

# C4, the advanced-setup record, proven on air 2026-09-13 (X-19, PROTOCOL.md
# 5.6; docs/captures/2026-09-05-gateway-dump/analysis13.md pass D-18): eight
# bytes, control_mode/units/offset/away_mode/away_offset/modified_auto_span/
# window_mode/true_radiant, in that order, replied F2 C5 55 (accepted) or
# F2 C5 56 (rejected -- only ever seen on this heater's own nine-byte
# max_stemp_limit form, which this module never builds). F3 DA, no payload,
# reads the E2 record back on demand (docs/captures/2026-09-13-x19/notes.md):
# answered by a 17-byte DB payload, the E2 record without its own leading 56
# marker.
ADVANCED_SETUP_OPCODE = 0xC4
ADVANCED_SETUP_ACCEPTED_REPLY = bytes([0xC5, 0x55])
ADVANCED_SETUP_REJECTED_REPLY = bytes([0xC5, 0x56])
ADVANCED_SETUP_READ_OPCODE = 0xDA
ADVANCED_SETUP_READ_REPLY_LEN = 17
# Indices into the DA reply's own data bytes (payload[0] is DB, the
# opcode + 1 marker _request_and_wait already checks): heater 04's own
# worked example, `DB 00 24 2A 03 2A 01 00 00 03 00 00 00 00 00 2A 06`
# (docs/captures/2026-09-13-x19/notes.md), reads eco 18.0C at index 2,
# comfort 21.0C at index 5, and the boost temperature 21.0C at index 15 --
# the one field in the record that varies per heater with no wattage
# correlation (heater 02 26.0C, heater 03 20.5C, same source).
ADVANCED_SETUP_ECO_INDEX = 2
ADVANCED_SETUP_COMFORT_INDEX = 5
ADVANCED_SETUP_BOOST_TEMP_INDEX = 15

_MODE_PAYLOAD_BYTE = {"auto": MODE_AUTO, "heat": MODE_HEAT, "off": MODE_OFF}

# EB clock-sync payload (phase3 fact): `52 YY MM DD DOW HH MM SS 03`, local time, two
# digit year; `51` instead of `52` at registration. The DOW byte is 0 = Sunday, 6 =
# Saturday, not ISO weekday (2026-09-06 phase3 capture, notes.md 16:53:30Z: 2026-09-06
# 17:52:14 local, a Sunday, encoded as `00`; the previous day, a Saturday, as `06`).
# Python's isoweekday() (1 Monday .. 7 Sunday) modulo 7 gives exactly this: Sunday's
# isoweekday 7 -> 0, Monday's 1 -> 1, ..., Saturday's 6 -> 6.
EB_CLOCK_STEADY_PREFIX = 0x52
EB_CLOCK_REGISTRATION_PREFIX = 0x51
EB_CLOCK_SUFFIX = 0x03

# Program record payload (phase3 fact, re-read against the gateway's own decoder in
# docs/captures/2026-09-05-gateway-dump/analysis9.md section 8): the opcode byte
# (`B2` written, `B1` read) then a run of bytes carrying four 2-bit slot codes each,
# MSB first, day-major, Sunday first on the wire (PROTOCOL.md 5.6/5.7, corrected
# 2026-09-12 from the earlier Monday-first reading by the owner's panel read of
# heater 04's PROGRAM screen). ProgramRecord.slots and .hourly below stay in this
# wire order; this project's own HA surface (Coordinator.get_prog, climate.py's
# `prog` attribute, the schedule sensors, and the week_slots write_program()
# takes) is Monday first instead, like the cloud's own `prog` array, with
# rotate_week() doing the one rotation at that boundary (X-17 day-order fix).
#
# The gateway decodes every program record -- the 43-byte C9 form, the 85-byte 9E/9F
# form and its own B2 write -- through one record handler (FUN_400eda30) and one
# 2-bit expander (FUN_40162c74) into one 344-byte per-node structure. So there is one
# schedule store, and the expander itself carries no resolution at all: four slots per
# byte, whatever the record length. What the record length selects is how many of
# those flat slots make a day, `record length * 4 / 7`, which the handler writes to
# offset 0x154 of that structure and the gateway publishes to its cloud as
# `prog_resolution`; its inbound `prog` parser (FUN_400eb5f8) accepts a day array of
# exactly 24 or 48 values and nothing else.
PROGRAM_OPCODE = 0xB2
DAYS_PER_WEEK = 7
SLOTS_PER_PROGRAM_BYTE = 4
SLOTS_PER_DAY_HOURLY = 24  # the 43-byte C9 record
SLOTS_PER_DAY_HALF_HOURLY = 48  # the 85-byte 9E/9F record, and every B2 write
PROGRAM_RESOLUTIONS = (SLOTS_PER_DAY_HOURLY, SLOTS_PER_DAY_HALF_HOURLY)
# The hourly resolution under its older name. A decoded `program` list, and the input
# write_program() takes by default, are hourly whatever the record's own resolution
# is (see _fold_to_hourly and ProgramRecord below); custom_components/ and tools/
# import this name for that shape.
HOURS_PER_DAY = SLOTS_PER_DAY_HOURLY
# One hour of a half-hourly record is one nibble whose two 2-bit halves are that
# hour's two half hours. The only three nibble values the whole capture corpus
# contains, `0`, `5` and `A`, are `00 00`, `01 01` and `10 10`: the slot codes 0, 1
# and 2 repeated across both halves, which is all a heater scheduled on the hour can
# produce. A slot value and its 2-bit code are the same number; the nibble here is
# that code written into both halves of an hour.
PROGRAM_SLOT_NIBBLE = {0: 0x0, 1: 0x5, 2: 0xA}
PROGRAM_SLOT_CODES = tuple(PROGRAM_SLOT_NIBBLE)

# F3 on-demand request opcodes proven in the 2026-09-06 proof session
# (f3-and-edges-results.md section 1): every reply's first payload byte is the
# request opcode plus one.
STATUS_OPCODE = 0xB8  # -> E6, the E5 payload with its leading marker byte dropped
PROGRAM_READ_OPCODE = 0xB0  # -> 9F or C9, both payload B1 + program data (see below)
ENERGY_OPCODE = 0xBC  # -> EF, payload BD + a 32-bit big-endian watt-hour counter

# A reply's first payload byte alone does not identify it: a frame's on-air
# class byte is its logical length XOR 0xFF and its payload length is that
# logical length minus 11, so payload length and class byte are the same fact,
# and matching a reply on payload length is the class check. Without it, F3 B0's
# own two reply classes (below) are indistinguishable, since both start `B1`.
# Every length here is counted off the CRC-valid frames under docs/captures/,
# not off PROTOCOL.md's prose (5.6's E6 line says 15 payload bytes; all 12
# CRC-valid E6 frames in the corpus carry 14).
STATUS_REPLY_PAYLOAD_LEN = 14  # E6
STATUS_REPLY_PAYLOAD_LEN_E4 = 16  # E4: E6 plus the 2-byte boost tail (PROTOCOL.md
# 5.10-style relation E3 has to E5; docs/captures/2026-09-12-schedule/notes.md,
# "Max Temp found, and a new status reply class E4 under Runback") -- a heater
# with Runback boost running answers F3 B8 with this instead of the plain E6.
ENERGY_REPLY_PAYLOAD_LEN = 5  # EF: the BD byte plus the 4 counter bytes
C2_REPLY_PAYLOAD_LEN = 2  # F2 C3 55
IDENTITY_REPLY_PAYLOAD_LEN = 20  # E0
CAPABILITY_REPLY_PAYLOAD_LEN = 8  # EC
C6_REPLY_PAYLOAD_LEN = 9  # EB C7 ...

# F3 B0 is answered in one of two frame classes, both opening with the same
# `B1` byte and each carrying a different encoding of the same weekly program.
# Every F3 B0 in the whole capture corpus is answered one of these two ways,
# consistently per heater: heaters 02 and 03 answer C9 (56 to 90 ms later, on
# both capture days), heater 04 answers 9F.
#   9F, 85-byte payload: `B1` then 84 bytes, the same format write_program()
#     sends (PROTOCOL.md 5.7), 48 slots a day.
#   C9, 43-byte payload: `B1` then 42 bytes (PROTOCOL.md 5.6), 24 slots a day.
# Matching on the `B1` byte alone fed a 43-byte C9 to the half-hourly decoder,
# which read its 42 bytes as 42 hours and produced 84 entries with None wherever
# a byte's two nibbles differed.
PROGRAM_NIBBLE_PAYLOAD_LEN = 85
PROGRAM_BITS_PAYLOAD_LEN = 43
PROGRAM_REPLY_PAYLOAD_LENS = (PROGRAM_NIBBLE_PAYLOAD_LEN, PROGRAM_BITS_PAYLOAD_LEN)
# The payload length is the gateway's own record length (FUN_400eda30 takes the
# record length and rejects anything from 0x56 up), so its `record length * 4 / 7`
# is applied to exactly these two lengths and to nothing else. Gating on the pair
# first keeps a payload of some other length that happens to round to 24 or 48
# (42 and 84 both do) out of the decoders, the same way the two class lengths
# always have.
_PROGRAM_RESOLUTION_BY_PAYLOAD_LEN = {
    payload_len: payload_len * SLOTS_PER_PROGRAM_BYTE // DAYS_PER_WEEK
    for payload_len in PROGRAM_REPLY_PAYLOAD_LENS
}
PROGRAM_REPLY_FIRST_BYTE = (PROGRAM_READ_OPCODE + 1) & 0xFF
ENERGY_REPLY_FIRST_BYTE = (ENERGY_OPCODE + 1) & 0xFF

# flags 0x80 (PROTOCOL.md section 4's flags byte, 2026-09-06 proof,
# filtering-results.md "i-flags-80-setpoint"): the heater still processes and
# applies the command but never sends an ack, so a caller using this must send
# with wait_ack=False.
FLAGS_FIRE_AND_FORGET = 0x80

# Accepted setpoint range, inclusive. The lower bound (2026-09-06 proof,
# f3-and-edges-results.md section 2 and notes.md's setpoint-bisect entry) is the
# heater's own applied floor; the upper bound was widened to 35.0 (owner
# direction 2026-09-06) to match the range the Tevolve app itself offers,
# rather than the 26.0 the same proof session found the heaters on this bench
# actually applying -- a value above 26.0 is still acked and replies B5 55
# exactly as an accepted one does, then silently ignored by the heater itself,
# so the entity's displayed target always follows the heater's own reported
# setpoint (docs/PROTOCOL.md 5.1), not the value last requested. Anything
# outside 7.0-35.0 is rejected here, before it is ever sent, the one place a
# caller can find out at all.
MIN_SETPOINT_C = 7.0
MAX_SETPOINT_C = 35.0

# Preset ordering rule (heater 02 live failure, 2026-09-13 00:xx local: owner
# set anti-frost to 10.0 with presets 7.0/23.0/23.5 and got "eco preset 23.0C
# is outside the accepted range 7.5-20.5C", from the fixed per-preset ranges
# below that this replaces). Those fixed ranges came from a single panel
# (heater 04, docs/captures/2026-09-12-schedule/notes.md "Max Temp found":
# "COMFORT (range 18.5 to 35), ECO (7.5 to 20.5), ANTI-FROST (7 to 17.5)")
# whose own presets were 7.0/18.0/21.0 -- each menu's displayed bound is
# relative to the other two presets' current values, half a degree past the
# neighbour, not an absolute limit. The real rule write_presets() enforces is
# ordering: MIN_SETPOINT_C <= anti-frost < eco < comfort <= MAX_SETPOINT_C,
# every value a multiple of 0.5C (so "strictly increasing" already means by
# at least 0.5C).

# Program cache "source" labels (custom_components/termoweb_local sensor.py
# schedule sensor): which of the three ways a heater's program cache entry
# was last populated. Network only tags "read"/"report" here, where it
# already has the node id and payload in hand; "written" is not a decode
# outcome at all (a set_schedule write's own read-back tags "read" like any
# other F3 B0), so a caller (climate.py's async_set_schedule, the only
# caller of Coordinator.async_set_schedule) marks that one explicitly via
# mark_program_written() right after the write completes.
PROGRAM_SOURCE_READ = "read"
PROGRAM_SOURCE_REPORT = "report"
PROGRAM_SOURCE_WRITTEN = "written"

# F2 B5 55 after a processed B4 xx command, F2 B3 55 after a processed program
# write (2026-09-06 proof, filtering-results.md "The B5 55 reply as a processed
# indicator"), F2 B7 55 after a processed B6 preset write (2026-09-12 X-18
# proof, docs/captures/2026-09-12-x18-s7/notes.md): the signal that a frame
# was actually applied, not just acked. Reply-code table, extended 2026-09-06
# (17:14Z+ post-pairing enrolment capture, notes.md): F2 53 55 is the heater's
# own reply to an EB clock sync (the previously-uncorrelated `53` code from
# PROTOCOL.md 5.6's heater-F2-reply-code table), not covered by
# wait_processed() below -- that method is specifically the B4 xx/B2/B6 "was
# it applied" signal, not a general reply-code wait -- so this is a
# documentation note, not a fourth entry in PROCESSED_REPLY_PAYLOADS.
CLOCK_SYNC_REPLY_PAYLOAD = bytes([0x53, 0x55])
PROCESSED_REPLY_PAYLOADS = (
    bytes([0xB5, 0x55]), bytes([0xB3, 0x55]), bytes([0xB7, 0x55]),
)

# B5 55/B3 55 lands 22.6-88.3 ms after the ack, and an E6/9F reply lands within
# roughly 130 ms, across every timing measured in the proof session
# (f3-and-edges-results.md section 1, filtering-results.md "Delays"); 200 ms
# leaves comfortable margin without making a caller wait needlessly long on a
# frame that was rejected before the CPU-side checks (no reply at all).
DEFAULT_REPLY_TIMEOUT_S = 0.2

# F2 5E 01 -> F2 5F 55 within about 100 ms (2026-09-06 P4b flash proof, notes.md
# 16:56:59Z: HA's flash_display button press produced F2 5E 01 to heater 04, the
# heater's own F2 5F 55 reply 96 ms later).
FLASH_DISPLAY_PAYLOAD = bytes([0x5E, 0x01])
FLASH_DISPLAY_REPLY_PAYLOAD = bytes([0x5F, 0x55])

# F1 BE <power hi> <power lo>: a heater's own request to the gateway's power
# manager, sent whenever its element switches on (PROTOCOL.md 5.1); the two
# payload bytes repeat that heater's own measured full-load power in deciwatts
# big-endian, the same encoding E5/E6/E3 use. Left unanswered, heater 04's
# panel raises its lost-gateway LINK indication a few minutes later
# (docs/captures/2026-09-13-x19/link-test.md).
POWER_REQUEST_PAYLOAD_LEN = 3
POWER_REQUEST_FIRST_BYTE = 0xBE

# F2 BF <verdict>: the gateway's only reply to a BE push (PROTOCOL.md 5.1,
# "What the push is for, and the BF reply it draws" -- FUN_400ef0bc is the only
# place in the whole gateway image that builds a BF frame, and FUN_400ef0d8,
# the BE handler, is its only caller). The corpus's own gateway always grants
# it: five BF frames, all `BF 01`, none unprompted.
POWER_VERDICT_OPCODE = 0xBF

# Gateway power-up sequence, per heater, in order (2026-09-06 phase3 capture,
# docs/captures/2026-09-06-phase3/notes.md 16:53:30Z): an EB association frame,
# F3 B0 (program read), F2 CA 00 (burst terminator), F3 C2, F3 5A (identity),
# F3 D0 (capability), F3 C6. Every F3 opcode here besides B0 is still opaque
# per the module docstring's ground rule -- this module names the sequence and
# forwards each opcode's raw reply, it does not decode C2/5A/D0/C6 meaning.
BURST_TERMINATOR_PAYLOAD = bytes([0xCA, 0x00])
BURST_TERMINATOR_REPLY_PAYLOAD = bytes([0xCB, 0x55])
STARTUP_C2_OPCODE = 0xC2
STARTUP_IDENTITY_OPCODE = 0x5A  # -> E0 identity reply (PROTOCOL.md 5.6)
STARTUP_CAPABILITY_OPCODE = 0xD0  # -> EC capability reply (PROTOCOL.md 5.6)
STARTUP_C6_OPCODE = 0xC6  # -> EB reply, sometimes followed by an unsolicited E2

# E2/E5 report payloads both start with a constant `56` opcode byte
# (PROTOCOL.md 5.4: "E5 payload starts 56 B9, E2 starts 56 DB"); this is the
# E2-specific two-byte marker startup_sequence looks for after F3 C6 (section
# 5.6: "followed 148 ms after the request by an unsolicited E2 report").
E2_REPORT_MARKER = bytes([0x56, 0xDB])

# Registration, as re-captured 2026-09-06 17:08:20Z (heater 04 power-cycled
# with the gateway on, notes.md): a heater's opening frame at power-up is an
# F3-length frame with payload `50` (also documented from the original
# registration-burst capture, PROTOCOL.md 5.6); the required station reply is
# an EB clock sync with the registration prefix (sync_clock's own
# registering=True), sent right after acking that opening frame, not the F3
# query burst startup_sequence sends -- that burst is the gateway's own
# power-up behaviour and did not repeat in this re-registration capture.
# Every report the heater then pushes (E5, E2, EA, and the 100-byte 9E
# program report) starts with the same `56` marker byte E5/E2 always have
# (PROTOCOL.md 5.4), confirmed by the station with `F2 57 55` regardless of
# on-air class; the 9E program report additionally carries `B1` as its second
# byte, then 84 nibble bytes, the same encoding a 9F F3 B0 reply carries.
REGISTRATION_OPEN_PAYLOAD = bytes([0x50])
REPORT_MARKER = 0x56
PROGRAM_REPORT_MARKER = bytes([REPORT_MARKER, 0xB1])

# Pairing (2026-09-06 17:12:17Z capture, notes.md): a heater with no node id
# yet announces itself with an E7 frame from the broadcast id `FF`, payload
# `77` plus its 12-byte identity tail (the same bytes carried inside its own
# E0 identity reply, F3 5A's own reply -- PROTOCOL.md 5.6). The station
# assigns it a node id with an F3 <id> frame: the same id as last time for an
# already-known identity ("the same identity got the same id back"), or the
# lowest id in 2..65 not already in use. That frame goes back to `FF` unless
# the announcement was relayed here, in which case it follows the reversed
# path back through the relay (see Network._assignment_route).
DISCOVERY_BROADCAST_ID = 0xFF
E7_FRAME_CLASS = 0xE7
E7_IDENTITY_MARKER = 0x77
E7_IDENTITY_PAYLOAD_LEN = 13  # the 77 marker plus the 12-byte identity tail

# The assignment carries 04 in header byte 11, where every F1/F2/F3/E5 command
# frame in the corpus carries 00: the gateway's own assignment frame at
# 17:12:17.407 descrambles to `0c 1b 30 01 ff 00 01 ff 00 00 00 04 04`, and
# 04 is one of the routing layer's enumerated per-call-site tags (analysis4.md
# section 4.1). A frame with the wrong byte 11 is acked and then discarded
# unprocessed (PROTOCOL.md section 3, 2026-09-06 filtering proof), which is
# what the 2026-09-09 attempts saw: the heater acked every assignment and
# adopted none.
DISCOVERY_ASSIGNMENT_TAG = 0x04

# Logical bytes 6-10 are the end-to-end routing path: byte 6 the originator,
# bytes 7-10 the successive hops towards the final destination, padded out
# (analysis8.md sections 2 and 7, and frame.build_frame's own `path`).
ROUTING_PATH_SLICE = slice(6, 11)
ROUTING_PATH_LEN = 5

# The announcement is a destination sweep, not a frame addressed to the
# station, and every already-paired heater relays its own copy of it
# (PROTOCOL.md 5.9, 2026-09-09 bench attempts), so one pairing press puts
# dozens of copies of the same identity on the air within a few seconds.
# Answering each one would spend the whole sweep transmitting into
# destinations the heater has already moved past, so one assignment per
# identity per sweep step is the cadence here: the observed dwell is about
# 200 ms per destination, and an assignment sent inside the dwell that
# produced it is the only one the heater is still listening for.
DISCOVERY_SWEEP_DWELL_S = 0.2

# 2-65, not the wider 1-254 the id byte's own range would allow: the gateway
# firmware's own node tables hold ids in that range only (Ghidra pass 5,
# docs/captures/2026-09-05-gateway-dump/analysis5.md).
MIN_PAIRED_HEATER_ID = 0x02
MAX_PAIRED_HEATER_ID = 0x41
DEFAULT_DISCOVERY_WINDOW_S = 120.0

# 300 ms reply window for every startup_sequence step (owner direction,
# 2026-09-06 17:00): comfortably above the largest reply latency measured in
# the proof session (see DEFAULT_REPLY_TIMEOUT_S's own comment).
STARTUP_REPLY_TIMEOUT_S = 0.3

# Discovery scan (owner direction 2026-09-06 task 2): "a short reply window
# (about 150 ms, no retries so the scan takes under 15 s)". 150 ms already
# comfortably covers an E6 reply's own worst measured latency (see
# DEFAULT_REPLY_TIMEOUT_S's own comment, ~130 ms), and an empty network (no
# id in 2-65 answers) scans start to finish in
# (MAX_PAIRED_HEATER_ID - MIN_PAIRED_HEATER_ID + 1) * SCAN_REPLY_TIMEOUT_S =
# 64 * 0.15 s = 9.6 s, under the 15 s ceiling.
SCAN_REPLY_TIMEOUT_S = 0.15


@dataclasses.dataclass
class RegistrationStrategy:
    """Handover item 7 is still open: this default strategy only lets NanoCul's own
    ack-on-receive stand (there is no ack-on-receive built into NanoCul yet either --
    acks are currently only sent by hardware/firmware, never by this client -- so today
    this strategy is a no-op placeholder). Wire up a real reply (to the EB association
    value and/or the F3 query burst) here once P3 answers whether one is needed; until
    then this keeps that decision out of Network's core command/decode logic."""

    def on_registration_frame(self, piece):
        return None


@dataclasses.dataclass(frozen=True)
class ProgramRecord:
    """One decoded program record, at the record's own resolution.

    `resolution` is the gateway's `prog_resolution` for this record: 24 slots a
    day for the 43-byte C9 form, 48 for the 85-byte 9E/9F form, by the record
    length alone (see _PROGRAM_RESOLUTION_BY_PAYLOAD_LEN). `slots` is
    `resolution * 7` slot codes in wire order, day-major, day 0 Sunday
    (days_sunday_first, PROTOCOL.md 5.6/5.7), the schedule as the heater
    actually holds it, so a half-hourly record's own half-hour boundaries
    survive intact. `slots_monday_first` below is the same data rotated to
    this project's Monday-first HA surface order, which is what
    Coordinator.get_prog() and, through it, climate.py's `prog` attribute and
    both schedule sensors read (docs/80-handover.md list C item 4, the X-17
    day-order fix). `hourly` is the 168-value projection of `slots` onto one
    value an hour, in the same wire order as `slots`, which
    _check_hourly_write_keeps_the_schedule below reads to refuse overwriting a
    half-hourly record unless it is itself hourly_only; for a half-hourly
    record `hourly` holds None at any hour whose two half hours disagree,
    because there is no hourly value that hour has. `raw` is the record's own
    data bytes after the opcode, 42 or 84 of them."""

    days_sunday_first = True  # class-level note: slots/hourly are wire order, day 0 Sunday

    resolution: int
    slots: list
    hourly: list
    raw: bytes

    @property
    def slots_monday_first(self):
        """`slots` rotated from wire order (day 0 Sunday) to this project's
        Monday-first HA surface order (day 0 Monday), matching the cloud's
        own `prog` array shape. See rotate_week()."""
        return rotate_week(self.slots, self.resolution, to_wire=False)

    @property
    def hourly_only(self):
        """True when this record draws no boundary inside an hour: either it is
        already hourly, or every hour's two half-hour slots agree. Every program
        in the capture corpus is hourly_only, since all three heaters on this
        bench schedule on the hour.

        This is about half-hour boundaries only. An hour holding the never
        observed code 3 decodes to None in both halves and counts as hourly_only,
        because the two halves do agree; no caller can express that code anyway,
        so there is nothing an hourly write could lose there."""
        if self.resolution == SLOTS_PER_DAY_HOURLY:
            return True
        return all(
            first == second
            for first, second in zip(self.slots[0::2], self.slots[1::2])
        )


@dataclasses.dataclass
class StartupResult:
    """What Network.startup_sequence sent and got back for one heater, in the
    order sent. A missing ack or reply for one step never stops the rest of
    the sequence (the real gateway's own power-up burst does not either), so
    every field here is independently None/False when its own step's ack
    never arrived or its 300 ms reply window timed out."""

    association_sent: bool  # True once an association_value was actually given
    association_ack: bool  # meaningless (False) when association_sent is False
    program: list | None  # 168 hourly values (see Network.read_program), or None
    program_raw: bytes | None  # the reply's own data bytes, 84 or 42, or None
    # The record's own resolution is not a field here: it is per node, not per
    # startup, so it is recorded like every other read and read back with
    # Network.program_resolution(node_id).
    burst_terminator_ok: bool  # F2 CA 00 acked and answered F2 CB 55
    c2_reply_payload: bytes | None  # F3 C2's raw reply payload (F2 C3 55 expected)
    identity_payload: bytes | None  # F3 5A's raw reply payload (E0)
    capability_payload: bytes | None  # F3 D0's raw reply payload (EC)
    c6_reply_payload: bytes | None  # F3 C6's raw reply payload (EB)
    e2_payload: bytes | None  # an unsolicited E2 report seen in the C6 window


@dataclasses.dataclass(frozen=True)
class AdvancedRecord:
    """One decoded F3 DA reply: the E2 record's eco/comfort/boost-temperature
    fields (2026-09-13 X-19 proof, PROTOCOL.md 5.6), half-degree Celsius like
    every other preset field in this module. `raw` is the reply's own 17-byte
    payload (the `DB` marker byte included), for a caller that wants the
    still-undecoded tail this record does not name."""

    eco_c: float
    comfort_c: float
    boost_temp_c: float
    raw: bytes


@dataclasses.dataclass
class DiscoveryResult:
    """What Network.handle_discovery_frame did for one E7 identity
    announcement (2026-09-06 17:12:17Z pairing capture, notes.md).

    node_id and assignment_air are None together, for an identity this
    station has never seen while unidentified_node_ids is non-empty: some
    heater already on this network might be the one announcing, and handing
    it a second id is what the 2026-09-09 bench attempt did before the
    heater refused it (PROTOCOL.md 5.9). The caller learns the missing
    identities (Network.read_identity) and the next announcement in the same
    sweep resolves."""

    node_id: int | None  # reused from a known identity, or the lowest free id 2-65
    identity: bytes  # the 12-byte identity tail, E7's payload with its 77 marker dropped
    is_new: bool  # False when this identity already had an assigned id
    assignment_air: bytes | None  # the F3 <node_id> frame, addressed to assignment_dst
    assignment_dst: int | None  # the id assignment_air's own header byte 4 addresses
    unidentified_node_ids: tuple  # configured ids whose own identity is not known yet
    # True when this announcement did reach this station through a relay but
    # its path held no route back, so the assignment fell back to a broadcast
    # that the relay will not forward. With a station id other than 0x01 that
    # is every relayed announcement (reverse_routing_path); a caller is
    # expected to say so rather than let the fallback pass for a fix.
    relay_reversal_failed: bool = False


def reverse_routing_path(path, station_id):
    """Reverse a received frame's logical bytes 6-10 into the path a reply
    takes back to its sender, exactly as the gateway's own FUN_400ff6c8 does
    (analysis7.md section 6; decompiled in that capture's
    work/pairing_steps.txt).

    The firmware scans indices 1 to 4 for the first byte equal to its own id.
    That index is where the path stops being real hops and starts being
    padding: a heater pads with `01` and the gateway with `00`, and
    FUN_401001d4's route-termination test requires this station's id to appear
    there followed by nothing but `00` or `01`. Everything up to and including
    that index is the real path, so the reversal is `out[k] = path[i - k]` for
    every k up to that index i, and `00` for every k after it. The firmware
    zero-fills that tail whatever the received padding was, which is why the
    captured `FF 01 01 01 01` reverses to `01 FF 00 00 00` and not to
    `01 FF 01 01 01`; it is also why a path shorter than five real hops never
    grows one, the reversal is exactly as long as the path it reverses.

    Returns None for a path that never reaches `station_id`, which is what the
    firmware's own 0xffffffff return means: FUN_400ff880 abandons the
    assignment rather than sending one down a path it cannot reverse.

    The scan cannot tell a heater's `01` padding from a station id of `01`,
    and the gateway firmware cannot either: this is the protocol's own
    addressing, not a defect in the port. It has a consequence this station
    does not share with the gateway, though, because the gateway's id is
    always `01` and this station's is configurable (CONF_STATION_ID): with any
    other station id, a heater still builds its path with `01` in it, nothing
    in the path matches, and every relayed announcement reverses to None. The
    caller has to make that visible; see DiscoveryResult.relay_reversal_failed
    and docs/PROTOCOL.md 5.9."""
    path = bytes(path)
    if len(path) != ROUTING_PATH_LEN:
        raise ValueError("path must be logical bytes 6-10, five bytes")
    for index in range(1, ROUTING_PATH_LEN):
        if path[index] == station_id:
            return bytes(
                path[index - k] if k <= index else 0x00
                for k in range(ROUTING_PATH_LEN)
            )
    return None


def is_relayed_announcement(parsed_frame):
    """True when this announcement reached this station through an
    already-paired heater rather than straight off the announcing heater's own
    radio: its link sender (logical byte 3) is not its routing path's own
    originator (logical byte 6).

    That is the whole test. A direct announcement and a swept copy addressed
    at some other id both carry the announcer in both places, since bytes 3-4
    are per-hop and bytes 6-10 are end to end (analysis8.md sections 2 and 3);
    only a relay rewrites byte 3 and leaves byte 6 alone, which is what all
    three real relay frames in docs/captures/2026-09-06-proof/ do
    (analysis8.md section 4).

    A frame too short to hold a five-byte path reads as not relayed, so every
    caller falls back to what it did before the path existed."""
    path = bytes(parsed_frame.logical)[ROUTING_PATH_SLICE]
    return len(path) == ROUTING_PATH_LEN and parsed_frame.src != path[0]


class Network:
    """Owns this station's id, the heaters' ids, and the poll cadence; builds outgoing
    command frames and decodes incoming reports. Holds the F3/EB tables as data handed
    in by the caller (see module docstring) -- it never invents an opcode meaning."""

    def __init__(
        self,
        node_ids,
        station_id=DEFAULT_STATION_ID,
        gateway_id=GATEWAY_ID,
        poll_period_s=IDLE_POLL_PERIOD_S,
        registration_strategy=None,
        f3_opcode_table=(),
        eb_payload_table=None,
        nanocul=None,
        known_identities=None,
    ):
        self.node_ids = tuple(node_ids)
        self.station_id = station_id
        self.gateway_id = gateway_id
        self.poll_period_s = poll_period_s
        self.registration_strategy = registration_strategy or RegistrationStrategy()
        self.f3_opcode_table = tuple(f3_opcode_table)
        self.eb_payload_table = dict(eb_payload_table or {})
        # Only request_status/read_program/request/wait_processed use this; every
        # other method here just builds and returns bytes, as before.
        self.nanocul = nanocul
        # Pairing: identity bytes -> previously assigned node id, handed in by
        # the caller (the coordinator persists this across restarts); mutated
        # in place by handle_discovery_frame() when it assigns a new one.
        self.known_identities: dict = dict(known_identities or {})
        # Node ids no heater is configured on but which are not free either:
        # the caller's own quarantine for a heater whose device was just
        # deleted and which is still out there answering on that id (see
        # _lowest_free_heater_id).
        self.reserved_node_ids: set = set()
        self._discovery_deadline = None  # time.time() the window closes, or None (off)
        # identity -> time.time() this station last acted on an announcement
        # of it, for DISCOVERY_SWEEP_DWELL_S's own rate limit.
        self._last_handled_at: dict = {}
        # node_id -> PROGRAM_SOURCE_READ/REPORT/WRITTEN, for the schedule sensor's
        # own `source` attribute; see the constants' own comment above.
        self.last_program_source: dict[int, str] = {}
        # node_id -> the ProgramRecord last decoded for it, carrying that node's
        # own prog_resolution and its slots at that resolution. The gateway keeps
        # the same thing the same way, one per-node structure whose offset 0x154
        # holds the resolution, so this is per node and not part of the decoded
        # hourly list any caller passes around.
        self.last_program: dict[int, ProgramRecord] = {}
        # node_id -> the raw E6/E4 reply payload last received for it (or None
        # for a request_status() that got no reply), kept alongside the decoded
        # HeaterSnapshot so a caller that wants the exact wire bytes for logging
        # (coordinator.py's own status-reply debug line) doesn't have to
        # re-derive them from the snapshot's decoded fields.
        self.last_status_reply_payload: dict[int, bytes | None] = {}

    def bind_nanocul(self, nanocul):
        """Bind the NanoCul request_status/read_program/request/wait_processed use
        to actually send and wait for a reply. Separate from the constructor
        because a caller (the HA coordinator) builds its Network before it opens
        its NanoCul."""
        self.nanocul = nanocul

    # ---- outgoing: station -> heater, on-air bytes ----

    def set_setpoint(self, node_id, celsius, mode=None, fire_and_forget=False):
        """F1 B4 <mode> <half-degrees>: manual setpoint (PROTOCOL.md section
        5.1). Raises ValueError outside the accepted MIN_SETPOINT_C-
        MAX_SETPOINT_C range: the heater itself acks and replies B5 55 to an
        out-of-range value exactly as it does to an accepted one, then
        silently ignores it, so rejecting it here is the only place the
        caller finds out.
        `mode` ("off"/"heat"/"auto") picks the frame's own mode byte via
        _MODE_PAYLOAD_BYTE; the default None keeps tf.setpoint_payload's
        fixed manual/heat byte (PROTOCOL.md 5.1's own worked example: a plain
        setpoint switched an off heater to heat), for every caller that
        wants the heater to land in heat. Passing a mode lets a setpoint
        write leave the heater in whatever mode it is already in (off/auto)
        instead, in the same single frame, since the radio already accepts
        that.
        fire_and_forget sets flags 0x80 (FLAGS_FIRE_AND_FORGET); the heater still
        applies the command but never acks it, so send the result with
        wait_ack=False."""
        _check_setpoint_range(celsius)
        flags = FLAGS_FIRE_AND_FORGET if fire_and_forget else 0x00
        if mode is None:
            payload = tf.setpoint_payload(celsius)
        else:
            if mode not in _MODE_PAYLOAD_BYTE:
                raise ValueError(
                    f"unknown mode {mode!r}; expected one of {sorted(_MODE_PAYLOAD_BYTE)}"
                )
            payload = bytes([WRITE_MARKER, _MODE_PAYLOAD_BYTE[mode], _half_degrees(celsius)])
        return tf.build_frame(self.station_id, node_id, payload, flags=flags)

    def set_mode(self, node_id, mode, fire_and_forget=False):
        """F2 B4 <code>: mode auto/heat/off. Use set_override() for the temporary
        override preset, which also carries a setpoint. fire_and_forget: see
        set_setpoint()."""
        if mode not in _MODE_PAYLOAD_BYTE:
            raise ValueError(f"unknown mode {mode!r}; expected one of {sorted(_MODE_PAYLOAD_BYTE)}")
        flags = FLAGS_FIRE_AND_FORGET if fire_and_forget else 0x00
        payload = bytes([WRITE_MARKER, _MODE_PAYLOAD_BYTE[mode]])
        return tf.build_frame(self.station_id, node_id, payload, flags=flags)

    def set_toggle(self, node_id, opcode, on):
        """`<opcode> 01`/`<opcode> 00`: one-bit toggle write for D2 (boost)/D6
        (EASY)/D4 (Runback Config)/BA (keypad lock), proven on air 2026-09-13
        (X-19, PROTOCOL.md 5.6). Pure frame builder, the same shape as
        set_mode/write_presets; the caller waits for that opcode's own
        TOGGLE_REPLY_PAYLOADS entry (wait_processed's expected_payloads=)
        rather than the generic B5 55, since D3/D7/D5/BB is each toggle's own
        processed reply, not B5 55."""
        payload = bytes([opcode, 0x01 if on else 0x00])
        return tf.build_frame(self.station_id, node_id, payload)

    def set_override(self, node_id, celsius, fire_and_forget=False):
        """F1 B4 03 <half-degrees>: temporary override setpoint (phase3 fact).
        Same accepted range and fire_and_forget behaviour as set_setpoint()."""
        _check_setpoint_range(celsius)
        half_degrees = _half_degrees(celsius)
        flags = FLAGS_FIRE_AND_FORGET if fire_and_forget else 0x00
        payload = bytes([WRITE_MARKER, OVERRIDE_WRITE, half_degrees])
        return tf.build_frame(self.station_id, node_id, payload, flags=flags)

    def write_presets(self, node_id, antifrost_c, eco_c, comfort_c):
        """B6 <anti-frost> <eco> <comfort>: preset temperature write (2026-09-12
        X-18 proof, docs/captures/2026-09-12-x18-s7/notes.md), three half-degree
        bytes in the same order and encoding E5 bytes 14-16 already use
        (heater.HeaterSnapshot.anti_frost_c/eco_c/comfort_c). The heater acks the
        18-byte frame then answers F2 B7 55 (PROCESSED_REPLY_PAYLOADS); its next
        E5/E6 carries the new values.

        Raises ValueError, before anything is sent, for a value not a multiple
        of 0.5C or for the three not strictly increasing within
        MIN_SETPOINT_C-MAX_SETPOINT_C (see the ordering rule comment above
        MIN_SETPOINT_C's own block) -- the same reasoning _check_setpoint_range's
        own comment gives for set_setpoint()."""
        _check_preset_order(antifrost_c, eco_c, comfort_c)
        payload = bytes(
            [
                PRESET_WRITE_OPCODE,
                _half_degrees(antifrost_c),
                _half_degrees(eco_c),
                _half_degrees(comfort_c),
            ]
        )
        return tf.build_frame(self.station_id, node_id, payload)

    def write_advanced_setup(
        self,
        node_id,
        control_mode,
        units,
        offset_byte,
        away_mode,
        away_offset,
        modified_auto_span,
        window_mode,
        true_radiant,
    ):
        """C4 <control_mode> <units> <offset> <away_mode> <away_offset>
        <modified_auto_span> <window_mode> <true_radiant>: the advanced-setup
        record, eight bytes (2026-09-13 X-19 proof, PROTOCOL.md 5.6;
        docs/captures/2026-09-05-gateway-dump/analysis13.md pass D-18).
        Replied F2 C5 55 (accepted) or F2 C5 56 (rejected -- this module never
        builds the nine-byte max_stemp_limit form that draws the rejection).
        Pure frame builder like write_presets; the caller computes
        `offset_byte` from a signed Celsius-tenths value (the wire's sign is
        the opposite of the panel's own offset wording,
        docs/captures/2026-09-13-x19/notes.md) and waits for the reply with
        wait_advanced_setup_reply()."""
        payload = bytes(
            [
                ADVANCED_SETUP_OPCODE,
                control_mode,
                units,
                offset_byte & 0xFF,
                away_mode,
                away_offset,
                modified_auto_span,
                window_mode,
                true_radiant,
            ]
        )
        return tf.build_frame(self.station_id, node_id, payload)

    def confirm_report(self, node_id):
        """F2 57 55: the report confirmation the gateway sends right after each
        E5/E2 report it receives from a heater (2026-09-06 proof, notes.md
        13:18:42Z and f3-and-edges-results.md section 1); a client must do the
        same after every report it receives. Sent with nothing to confirm, it
        elicits no reply at all, so this is a pure frame builder like every
        other method above it -- there is no round trip to wait for. `poll` is
        this method's pre-2026-09-06 name, kept as an alias for the tests
        already written against it."""
        return tf.build_frame(self.station_id, node_id, tf.poll_payload())

    def poll(self, node_id):
        """Alias for confirm_report(); see its docstring."""
        return self.confirm_report(node_id)

    def sync_clock(self, node_id, when, registering=False):
        """EB clock-sync payload (phase3 fact): `52 YY MM DD DOW HH MM SS 03`, `51` at
        registration. `when` is a local naive/aware datetime; only its calendar fields
        are used. DOW is 0 = Sunday .. 6 = Saturday (see EB_CLOCK_STEADY_PREFIX's own
        comment), not ISO weekday."""
        prefix = EB_CLOCK_REGISTRATION_PREFIX if registering else EB_CLOCK_STEADY_PREFIX
        payload = bytes(
            [
                prefix,
                when.year % 100,
                when.month,
                when.day,
                when.isoweekday() % 7,
                when.hour,
                when.minute,
                when.second,
                EB_CLOCK_SUFFIX,
            ]
        )
        return tf.build_frame(self.station_id, node_id, payload)

    def write_program(self, node_id, week_slots):
        """Program write payload (phase3 fact): `B2` then 84 bytes, 12 per day for 7
        days, day 0 Sunday on the wire (PROTOCOL.md 5.6/5.7). The record is 85 bytes
        long, so it is a half-hourly one, 48 slots a day, four 2-bit slot codes per
        byte MSB first: a B2 write is the only length this station sends, whichever
        length that node's own read replies come back in.

        `week_slots` is 7 sequences, Monday first (this project's HA surface order,
        matching Coordinator.get_prog() and the cloud's own `prog` array), of slot
        values in {0, 1, 2}, either 24 of them (hourly, each hour written into both
        of its half hours, which is byte for byte the nibble encoding this method
        has always sent) or 48 (this record's own resolution, one value per half
        hour). rotate_week() below does the one rotation from that Monday-first
        input to the wire's Sunday-first day order before encoding (X-17 day-order
        fix): every caller, including write_program's own tests, passes and reasons
        about Monday-first days, and only this method's own packing step ever sees
        wire order.

        An hourly write to a node whose own last program came back half-hourly with
        an hour its two halves disagree on is refused with a ValueError rather than
        sent: expanding each hour into two equal halves would overwrite that node's
        half-hour boundaries with a schedule it does not hold, and the write path is
        where that becomes real data loss rather than a lossy read. Pass 48 slots a
        day to write such a node."""
        if len(week_slots) != DAYS_PER_WEEK:
            raise ValueError(f"expected {DAYS_PER_WEEK} days (Monday first), got {len(week_slots)}")
        resolution = len(week_slots[0])
        if resolution not in PROGRAM_RESOLUTIONS or any(
            len(day) != resolution for day in week_slots
        ):
            raise ValueError(
                f"expected {DAYS_PER_WEEK} days of {SLOTS_PER_DAY_HOURLY} or "
                f"{SLOTS_PER_DAY_HALF_HOURLY} slots each, got "
                f"{[len(day) for day in week_slots]}"
            )
        if resolution == SLOTS_PER_DAY_HOURLY:
            self._check_hourly_write_keeps_the_schedule(node_id)
            week_slots = [[slot for slot in day for _ in range(2)] for day in week_slots]
            resolution = SLOTS_PER_DAY_HALF_HOURLY
        flat_monday_first = [slot for day in week_slots for slot in day]
        flat_wire = rotate_week(flat_monday_first, resolution, to_wire=True)
        wire_days = [
            flat_wire[day * resolution : (day + 1) * resolution]
            for day in range(DAYS_PER_WEEK)
        ]
        body = bytes([PROGRAM_OPCODE]) + _encode_program_slots(wire_days)
        return tf.build_frame(self.station_id, node_id, body)

    def _check_hourly_write_keeps_the_schedule(self, node_id):
        record = self.last_program.get(node_id)
        if record is None or record.hourly_only:
            return
        raise ValueError(
            f"node {node_id:02x} holds a half-hourly schedule (prog_resolution "
            f"{record.resolution}) with at least one hour whose two half hours "
            f"differ; a {SLOTS_PER_DAY_HOURLY}-slot-a-day write cannot express "
            f"those boundaries and would replace them with a schedule the heater "
            f"does not hold. Write {DAYS_PER_WEEK} days of "
            f"{SLOTS_PER_DAY_HALF_HOURLY} slots instead"
        )

    # ---- round trips: send an F3 request and wait for the node's own reply.
    # Unlike every method above, these need a NanoCul bound (constructor's
    # nanocul= argument, or bind_nanocul()) since there is nothing to decode
    # until a reply actually arrives.

    def request_status(self, node_id, timeout=DEFAULT_REPLY_TIMEOUT_S):
        """F3 B8: on-demand status request (2026-09-06 proof,
        f3-and-edges-results.md section 1). Answered by an E6 whose payload is
        the E5 payload with its leading marker byte dropped, or by an E4 (the
        same reply while the heater's own Runback boost is running,
        STATUS_REPLY_PAYLOAD_LEN_E4, docs/captures/2026-09-12-schedule/notes.md);
        HeaterSnapshot.from_e6 accepts either length and shares from_frame()'s own
        field decoding via that byte offset. Returns the decoded HeaterSnapshot,
        or None if the frame was not acked or no E6/E4 reply arrived within
        `timeout`."""
        from .heater import HeaterSnapshot

        parsed = self._request_and_wait(
            node_id, STATUS_OPCODE, timeout,
            STATUS_REPLY_PAYLOAD_LEN, STATUS_REPLY_PAYLOAD_LEN_E4,
        )
        self.last_status_reply_payload[node_id] = None if parsed is None else parsed.payload
        return None if parsed is None else HeaterSnapshot.from_e6(parsed)

    def read_program(self, node_id, timeout=DEFAULT_REPLY_TIMEOUT_S):
        """F3 B0: program read (2026-09-06 proof, f3-and-edges-results.md section
        1). Answered by a 9F frame (payload `B1` then 84 nibble bytes) or a C9
        frame (payload `B1` then 42 two-bits-per-hour bytes), whichever encoding
        this heater uses; see PROGRAM_REPLY_PAYLOAD_LENS. Returns
        (hourly_values, raw_program): hourly_values is 168 ints, wire order
        (day 0 Sunday, ProgramRecord.hourly), with None for any code outside
        the three known slot values
        {0, 1, 2} and, for a half-hourly reply, for any hour whose two half hours
        disagree; raw_program is the reply's own data bytes after the `B1`,
        84 of them for a 9F reply and 42 for a C9 one. The reply's own resolution
        and its slots at that resolution are kept per node, not returned here:
        read them back with program_resolution()/program_record(). Returns None
        if the frame was not acked or no reply arrived within `timeout`."""
        parsed = self._request_and_wait(
            node_id, PROGRAM_READ_OPCODE, timeout, *PROGRAM_REPLY_PAYLOAD_LENS
        )
        if parsed is None:
            return None
        record = decode_program_record(parsed.payload)
        if record is None:
            return None
        self._record_program(node_id, record, PROGRAM_SOURCE_READ)
        return record.hourly, record.raw

    def read_energy(self, node_id, timeout=DEFAULT_REPLY_TIMEOUT_S):
        """F3 BC: read the heater's own cumulative energy meter (PROTOCOL.md 5.6).
        Answered by an EF frame whose payload is `BD` then a 32-bit big-endian
        watt-hour counter, 41 to 58 ms later across all 12 EF frames in the capture
        corpus. Returns the counter in watt-hours, or None if the frame was not
        acked, no reply arrived within `timeout`, or the reply was not an EF."""
        from .heater import decode_energy_wh

        parsed = self._request_and_wait(
            node_id, ENERGY_OPCODE, timeout, ENERGY_REPLY_PAYLOAD_LEN
        )
        return None if parsed is None else decode_energy_wh(parsed.payload)

    def read_advanced_record(self, node_id, timeout=DEFAULT_REPLY_TIMEOUT_S):
        """F3 DA: on-demand read of the E2 record (2026-09-13 X-19 proof,
        PROTOCOL.md 5.6), answered by a 17-byte `DB` payload, the E2 record
        without its own leading `56` marker. This can never be confused with
        an E3 status reply of the same 17-byte length: _request_and_wait
        already gates on `payload[0] == opcode + 1` (0xDB here), and an E3's
        own byte 0 is 0x56 (the E5/E2 report marker), never 0xDB (see
        _request_and_wait's own payload[0] check above). Returns an
        AdvancedRecord, or None if the frame was not acked or no DB reply
        arrived within `timeout`."""
        parsed = self._request_and_wait(
            node_id, ADVANCED_SETUP_READ_OPCODE, timeout, ADVANCED_SETUP_READ_REPLY_LEN
        )
        if parsed is None:
            return None
        payload = parsed.payload
        return AdvancedRecord(
            eco_c=payload[ADVANCED_SETUP_ECO_INDEX] / 2.0,
            comfort_c=payload[ADVANCED_SETUP_COMFORT_INDEX] / 2.0,
            boost_temp_c=payload[ADVANCED_SETUP_BOOST_TEMP_INDEX] / 2.0,
            raw=bytes(payload),
        )

    def request(self, node_id, opcode, timeout=DEFAULT_REPLY_TIMEOUT_S):
        """Generic F3 <opcode> request for any opcode besides B8/B0 above
        (2026-09-06 proof, f3-and-edges-results.md section 1: every reply's
        first payload byte is opcode + 1, whatever the opcode). Returns the raw
        reply payload (including that leading opcode+1 byte), or None if the
        frame was not acked or no reply arrived within `timeout`. This module
        attaches no meaning to `opcode` or the reply; see the module docstring."""
        parsed = self._request_and_wait(node_id, opcode, timeout)
        return None if parsed is None else bytes(parsed.payload)

    def wait_processed(
        self, node_id, timeout=DEFAULT_REPLY_TIMEOUT_S, expected_payloads=PROCESSED_REPLY_PAYLOADS
    ):
        """Wait up to `timeout` for the F2 `B5 55` (any B4 xx command), `B3 55`
        (write_program) or `B7 55` (write_presets) reply that means `node_id`
        actually processed the frame just sent, as opposed to merely acking
        receipt of it (2026-09-06 proof, filtering-results.md "The B5 55 reply
        as a processed indicator"; B7 55, 2026-09-12 X-18 proof,
        docs/captures/2026-09-12-x18-s7/notes.md). Call this right after
        sending a set_setpoint/set_mode/set_override/write_program/
        write_presets frame (and, unless fire_and_forget, its own ack).

        `expected_payloads` defaults to PROCESSED_REPLY_PAYLOADS (B5 55/B3 55/
        B7 55), unchanged behaviour for every existing caller; a set_toggle
        caller passes TOGGLE_REPLY_PAYLOADS[opcode] instead, since D2/D6/D4/BA
        each have their own processed reply (D3/D7/D5/BB 55) and never answer
        with the generic B5 55 (2026-09-13 X-19 proof, PROTOCOL.md 5.6).
        Returns True/False, never raises on timeout."""
        if self.nanocul is None:
            raise RuntimeError("Network.wait_processed needs a bound NanoCul; see bind_nanocul()")
        parsed = self.nanocul.wait_for_reply(
            node_id,
            lambda frame: bytes(frame.payload) in expected_payloads,
            timeout,
        )
        return parsed is not None

    def wait_advanced_setup_reply(self, node_id, timeout=DEFAULT_REPLY_TIMEOUT_S):
        """Wait for C4's own F2 `C5 55` (accepted)/`C5 56` (rejected) reply
        (2026-09-13 X-19 proof, PROTOCOL.md 5.6): three-way, unlike
        wait_processed's plain bool, because a caller
        (Coordinator.async_write_advanced_setup) has to tell "the heater
        rejected this record" apart from "no reply arrived in time" -- both
        look like "not accepted" from a bool alone, but only one means the
        record was actually refused. Returns True (accepted), False
        (rejected), or None (no reply within `timeout`)."""
        if self.nanocul is None:
            raise RuntimeError(
                "Network.wait_advanced_setup_reply needs a bound NanoCul; see bind_nanocul()"
            )
        parsed = self.nanocul.wait_for_reply(
            node_id,
            lambda frame: bytes(frame.payload)
            in (ADVANCED_SETUP_ACCEPTED_REPLY, ADVANCED_SETUP_REJECTED_REPLY),
            timeout,
        )
        if parsed is None:
            return None
        return bytes(parsed.payload) == ADVANCED_SETUP_ACCEPTED_REPLY

    def flash_display(self, node_id, timeout=DEFAULT_REPLY_TIMEOUT_S):
        """F2 5E 01: flash the heater's own display (2026-09-06 P4b proof,
        notes.md 16:56:59Z). Sends the frame, waits for its ack, then for the
        heater's own F2 5F 55 reply (96 ms after the ack in the proof
        capture). Returns True once that reply arrives, False if the frame
        was not acked or no reply came within `timeout`; never raises on a
        timeout."""
        if self.nanocul is None:
            raise RuntimeError("Network.flash_display needs a bound NanoCul; see bind_nanocul()")
        air = tf.build_frame(self.station_id, node_id, FLASH_DISPLAY_PAYLOAD)
        ack = self.nanocul.send_frame(node_id, air)
        if not ack.ok:
            return False
        reply = self.nanocul.wait_for_reply(
            node_id,
            lambda frame: bytes(frame.payload) == FLASH_DISPLAY_REPLY_PAYLOAD,
            timeout,
        )
        return reply is not None

    def power_verdict(self, node_id, granted=True):
        """F2 BF <verdict>: the gateway's only reply to a heater's own BE
        power-allocation request (PROTOCOL.md 5.1). A pure frame builder, not
        a request/reply pair like flash_display -- the corpus never shows a
        heater replying to its own BF, so the caller sends this and moves on,
        the same send-and-forget shape as a fire_and_forget setpoint. `granted`
        defaults to True: `BF 01` is the only verdict ever observed on the
        wire; `BF 00` is built the same way should a caller ever need it, with
        no known trigger for it here."""
        payload = bytes([POWER_VERDICT_OPCODE, 0x01 if granted else 0x00])
        return tf.build_frame(self.station_id, node_id, payload)

    def startup_sequence(self, node_id, association_value=None, timeout=STARTUP_REPLY_TIMEOUT_S):
        """Gateway power-up sequence for one heater, in the order captured
        2026-09-06 (docs/captures/2026-09-06-phase3/notes.md 16:53:30Z): the
        EB association frame (only when `association_value` is given -- an
        opaque 9-byte payload, per-heater configuration this module never
        interprets), F3 B0 (program read), F2 CA 00 (burst terminator), F3
        C2, F3 5A (identity), F3 D0 (capability), F3 C6 (also checked for an
        unsolicited E2 report in its own reply window, per PROTOCOL.md 5.6).
        Every step still gets NanoCul.send_frame's own normal ack-with-retries;
        each step's own missing ack or reply never stops the rest of the
        sequence from being sent. Returns a StartupResult with whatever came
        back."""
        if self.nanocul is None:
            raise RuntimeError("Network.startup_sequence needs a bound NanoCul; see bind_nanocul()")

        association_sent = association_value is not None
        association_ack = False
        if association_sent:
            air = tf.build_frame(self.station_id, node_id, bytes(association_value))
            association_ack = self.nanocul.send_frame(node_id, air).ok

        program_parsed = self._request_and_wait(
            node_id, PROGRAM_READ_OPCODE, timeout, *PROGRAM_REPLY_PAYLOAD_LENS
        )
        program = None
        program_raw = None
        if program_parsed is not None:
            record = decode_program_record(program_parsed.payload)
            if record is not None:
                program, program_raw = record.hourly, record.raw
                self._record_program(node_id, record, PROGRAM_SOURCE_READ)

        burst_air = tf.build_frame(self.station_id, node_id, BURST_TERMINATOR_PAYLOAD)
        burst_terminator_ok = False
        if self.nanocul.send_frame(node_id, burst_air).ok:
            reply = self.nanocul.wait_for_reply(
                node_id,
                lambda frame: bytes(frame.payload) == BURST_TERMINATOR_REPLY_PAYLOAD,
                timeout,
            )
            burst_terminator_ok = reply is not None

        c2_parsed = self._request_and_wait(
            node_id, STARTUP_C2_OPCODE, timeout, C2_REPLY_PAYLOAD_LEN
        )
        identity_parsed = self._request_and_wait(
            node_id, STARTUP_IDENTITY_OPCODE, timeout, IDENTITY_REPLY_PAYLOAD_LEN
        )
        capability_parsed = self._request_and_wait(
            node_id, STARTUP_CAPABILITY_OPCODE, timeout, CAPABILITY_REPLY_PAYLOAD_LEN
        )
        c6_parsed = self._request_and_wait(
            node_id, STARTUP_C6_OPCODE, timeout, C6_REPLY_PAYLOAD_LEN
        )
        e2_frame = self.nanocul.wait_for_reply(
            node_id,
            lambda frame: bytes(frame.payload[:2]) == E2_REPORT_MARKER,
            timeout,
        )

        return StartupResult(
            association_sent=association_sent,
            association_ack=association_ack,
            program=program,
            program_raw=program_raw,
            burst_terminator_ok=burst_terminator_ok,
            c2_reply_payload=None if c2_parsed is None else bytes(c2_parsed.payload),
            identity_payload=None if identity_parsed is None else bytes(identity_parsed.payload),
            capability_payload=None if capability_parsed is None else bytes(capability_parsed.payload),
            c6_reply_payload=None if c6_parsed is None else bytes(c6_parsed.payload),
            e2_payload=None if e2_frame is None else bytes(e2_frame.payload),
        )

    def scan_for_heaters(self, min_id=MIN_PAIRED_HEATER_ID, max_id=MAX_PAIRED_HEATER_ID, timeout=None):
        """Blocking: probe every id in [min_id, max_id] with a single F3 B8
        status request each -- one attempt, no ack retries, `timeout`
        seconds to answer (owner direction 2026-09-06 task 2). Returns
        {node_id: HeaterSnapshot} for every id that answered within
        `timeout`; an id no heater holds is simply absent from the result,
        never an error. `timeout` defaults to None, resolved to the module
        global SCAN_REPLY_TIMEOUT_S *inside* this call rather than bound as
        an ordinary default argument, so a test can still shrink that
        global to keep a full 64-id scan fast without passing timeout=
        through every caller. Needs a bound NanoCul; see bind_nanocul().
        Used by the coordinator's own setup-time scan and its "Scan for
        heaters" button."""
        if self.nanocul is None:
            raise RuntimeError("Network.scan_for_heaters needs a bound NanoCul; see bind_nanocul()")
        if timeout is None:
            timeout = SCAN_REPLY_TIMEOUT_S
        found = {}
        saved_retries = self.nanocul.retries
        saved_retry_interval = self.nanocul.retry_interval
        self.nanocul.retries = 1
        self.nanocul.retry_interval = timeout
        try:
            for node_id in range(min_id, max_id + 1):
                snapshot = self.request_status(node_id, timeout=timeout)
                if snapshot is not None:
                    found[node_id] = snapshot
        finally:
            self.nanocul.retries = saved_retries
            self.nanocul.retry_interval = saved_retry_interval
        return found

    # ---- pairing/discovery: no NanoCul needed, these are pure frame builders
    # plus the identity/id table, like every non-round-trip method above.

    @staticmethod
    def identity_from_identity_reply(payload):
        """The (node_id, 12-byte identity tail) an E0 identity reply carries,
        or None when `payload` is not one. PROTOCOL.md 5.9 gives the exact
        relation between the two forms of the same identity: E0's payload is
        `5B`, the E7 marker `77`, `01 01 01 <id>`, the same 12 bytes an E7
        announcement carries after its own marker, then `0A 1A`. That is the
        only way to learn the identity of a heater that never announced
        itself to this station.

        The node id byte comes back for completeness, not as an identifier:
        it reads `04` in node 3's own E0 frame as well as node 4's
        (PROTOCOL.md section 9, registration-burst.md), so the id a caller
        should record an identity under is the id it addressed, not this
        one. The 12-byte tail itself does differ per heater in the same
        capture, which is what makes it usable as an identity at all."""
        payload = bytes(payload)
        if len(payload) != IDENTITY_REPLY_PAYLOAD_LEN:
            return None
        if payload[0] != 0x5B or payload[1] != E7_IDENTITY_MARKER:
            return None
        return payload[5], payload[6:18]

    def read_identity(self, node_id, timeout=STARTUP_REPLY_TIMEOUT_S):
        """F3 5A against one configured heater, decoded to its 12-byte
        identity tail (identity_from_identity_reply). Returns the identity,
        or None when the heater did not answer or answered something else.
        Needs a bound NanoCul; see bind_nanocul()."""
        parsed = self._request_and_wait(
            node_id, STARTUP_IDENTITY_OPCODE, timeout, IDENTITY_REPLY_PAYLOAD_LEN
        )
        if parsed is None:
            return None
        decoded = self.identity_from_identity_reply(parsed.payload)
        return None if decoded is None else decoded[1]

    def start_discovery(self, window_s=DEFAULT_DISCOVERY_WINDOW_S, now=None):
        """Open a discovery window `window_s` seconds long (default 120 s,
        owner direction): handle_discovery_frame() only assigns an id while
        discovery_active() is True."""
        now = time.time() if now is None else now
        self._discovery_deadline = now + window_s
        self._last_handled_at.clear()

    def stop_discovery(self):
        self._discovery_deadline = None
        self._last_handled_at.clear()

    def discovery_active(self, now=None):
        if self._discovery_deadline is None:
            return False
        now = time.time() if now is None else now
        return now < self._discovery_deadline

    def handle_discovery_frame(self, parsed_frame, now=None):
        """Handle one E7 identity announcement (2026-09-06 17:12:17Z pairing
        capture, notes.md; PROTOCOL.md 5.9). Returns a DiscoveryResult, or
        None when no discovery window is open, the payload is not an
        identity announcement, or this identity was already answered less
        than DISCOVERY_SWEEP_DWELL_S ago.

        src and dst are deliberately not checked: the announcement sweeps
        its destination across the whole id range and reaches this station
        just as often relayed by an already-paired heater, under that
        heater's own source id, as it does addressed here directly.

        The assignment's own destination and routing path come from the
        announcement's path rather than from a constant, so a relayed
        announcement is answered back through its relay; see
        _assignment_route(). A caller sending assignment_air has to address it
        at the returned assignment_dst, which is `FF` only for the direct case.

        Does not send anything itself -- a caller (the coordinator) sends
        the returned assignment_air frame."""
        now = time.time() if now is None else now
        if not self.discovery_active(now):
            return None
        payload = bytes(parsed_frame.payload)
        if len(payload) != E7_IDENTITY_PAYLOAD_LEN or payload[0] != E7_IDENTITY_MARKER:
            return None
        identity = payload[1:]
        last_at = self._last_handled_at.get(identity)
        if last_at is not None and now - last_at < DISCOVERY_SWEEP_DWELL_S:
            return None
        self._last_handled_at[identity] = now

        node_id = self.known_identities.get(identity)
        is_new = node_id is None
        unidentified = self.unidentified_node_ids()
        if is_new and unidentified:
            return DiscoveryResult(
                node_id=None,
                identity=identity,
                is_new=True,
                assignment_air=None,
                assignment_dst=None,
                unidentified_node_ids=unidentified,
            )
        if is_new:
            node_id = self._lowest_free_heater_id()
            self.known_identities[identity] = node_id
        assignment_dst, assignment_path, reversal_failed = self._assignment_route(
            parsed_frame
        )
        return DiscoveryResult(
            node_id=node_id,
            identity=identity,
            is_new=is_new,
            assignment_air=tf.build_frame(
                self.station_id,
                assignment_dst,
                bytes([node_id]),
                tag=DISCOVERY_ASSIGNMENT_TAG,
                path=assignment_path,
            ),
            assignment_dst=assignment_dst,
            unidentified_node_ids=unidentified,
            relay_reversal_failed=reversal_failed,
        )

    def _assignment_route(self, parsed_frame):
        """The assignment's link destination (header byte 4) and its own
        logical bytes 6-10, derived from the announcement's path the way the
        gateway's FUN_400ff6c8 derives them (analysis7.md section 6, divergence
        2). Returns (destination, path, reversal_failed), where a path of None leaves
        frame.build_frame at its default, this station originating a one-hop
        frame to that destination.

        The reversal is only applied to an announcement that reached this
        station through a relay, that is one whose link sender (byte 3) is not
        the path's own originator (byte 6). Two cases fall outside that, and
        both keep the broadcast frame this method's caller built before the
        reversal existed:

        - A direct announcement, where reversing its path produces that exact
          frame anyway: the captured path is `FF 01 01 01 01`, the reversal is
          `01 FF 00 00 00` addressed to `FF`, and that is byte for byte the
          gateway's own assignment (PROTOCOL.md 5.9).
        - A copy of the sweep addressed at some other id, which the firmware
          drops in FUN_401004e4 before it reverses anything, so the firmware is
          no precedent for what to answer it with. Answering it at all is this
          station's own deliberate divergence 1, and reversing that copy's path
          would address the assignment at the swept id rather than at the
          announcer, which is not what has ever paired a heater here."""
        if not is_relayed_announcement(parsed_frame):
            return DISCOVERY_BROADCAST_ID, None, False
        path = bytes(parsed_frame.logical)[ROUTING_PATH_SLICE]
        reversed_path = reverse_routing_path(path, self.station_id)
        if reversed_path is None:
            # A relayed announcement this station cannot answer through its
            # relay. The broadcast fallback is deliberate (4b85149) and it is
            # fail-safe, but it is also inert: the relay is what has to carry
            # the assignment and a broadcast is not addressed to it. The flag
            # is the third return value rather than something the caller
            # infers from a broadcast destination, because the direct and
            # swept cases broadcast too and are not failures.
            return DISCOVERY_BROADCAST_ID, None, True
        return reversed_path[1], reversed_path, False

    def unidentified_node_ids(self):
        """Configured node ids whose own 12-byte identity this station does
        not know. An announcement from an unknown identity cannot be told
        apart from a re-announcement by one of these, so it is not safe to
        hand out a fresh id while any of them is outstanding: identity is
        only ever learned from an E7 or an E0 identity reply, so a heater
        registered by the scan or by its own report has none recorded, which
        is how the 2026-09-09 attempt gave heater 04's identity a second id
        (PROTOCOL.md 5.9). read_identity() closes the gap for one heater."""
        identified = set(self.known_identities.values())
        return tuple(node_id for node_id in self.node_ids if node_id not in identified)

    def _lowest_free_heater_id(self):
        """The lowest id in MIN_PAIRED_HEATER_ID..MAX_PAIRED_HEATER_ID not
        already used by a known identity, a currently configured heater, or
        an id the caller has reserved."""
        used = set(self.known_identities.values()) | set(self.node_ids) | set(self.reserved_node_ids)
        for candidate in range(MIN_PAIRED_HEATER_ID, MAX_PAIRED_HEATER_ID + 1):
            if candidate not in used:
                return candidate
        raise RuntimeError("no free heater id left in 2..65")

    def _request_and_wait(self, node_id, opcode, timeout, *payload_lens):
        """Send F3 <opcode> to `node_id` and wait for its reply. `payload_lens`
        is the reply's accepted payload length, or the several lengths an opcode
        has more than one reply class for; each length is the class byte itself
        (see PROGRAM_REPLY_PAYLOAD_LENS' own comment), so passing one narrows
        the match from "first payload byte is opcode + 1" to that byte in the
        right frame class. Pass none for an opcode whose reply class the corpus
        does not pin down: an unconstrained match is what this module had
        everywhere, and it is still better than a guessed constraint that
        rejects a real reply."""
        if self.nanocul is None:
            raise RuntimeError("Network.request needs a bound NanoCul; see bind_nanocul()")
        air = tf.build_frame(self.station_id, node_id, bytes([opcode]))
        ack = self.nanocul.send_frame(node_id, air)
        if not ack.ok:
            return None
        expected_first_byte = (opcode + 1) & 0xFF
        return self.nanocul.wait_for_reply(
            node_id,
            lambda frame: (
                len(frame.payload) > 0
                and frame.payload[0] == expected_first_byte
                and (not payload_lens or len(frame.payload) in payload_lens)
            ),
            timeout,
        )

    # ---- incoming: heater -> station ----

    def decode_report(self, parsed_frame, received_at=None):
        """Decode a parsed E5 frame into a HeaterSnapshot. Imported lazily to avoid a
        hard import cycle (heater.py does not need to know about Network)."""
        from .heater import HeaterSnapshot

        return HeaterSnapshot.from_frame(parsed_frame, received_at=received_at)

    def decode_program_report(self, parsed_frame):
        """Decode a 9E program report (payload `56 B1` + 84 nibble bytes),
        pushed unsolicited by a heater as part of its registration reply
        (2026-09-06 17:08:20Z capture, notes.md: "9E 100-byte program report
        56 B1 plus 84 nibble bytes"): the same nibble format a 9F F3 B0
        reply carries, just wrapped in the `56` report marker instead of a
        query reply. Returns (hourly_values, raw_nibbles), the same
        shape read_program() returns, or None for a payload of any other
        length."""
        record = decode_program_record(parsed_frame.payload[1:])  # payload[0] is 56
        if record is None:
            return None
        self._record_program(parsed_frame.src, record, PROGRAM_SOURCE_REPORT)
        return record.hourly, record.raw

    def decode_program_reply(self, parsed_frame):
        """Decode a 9F or C9 program frame that arrived on the reader loop
        rather than inside read_program()'s own reply window (the coordinator's
        _handle_event). C9 is heaters 02 and 03's own F3 B0 reply class, so one
        landing here is a real program, not junk; both classes decode through
        the same length dispatch read_program() uses. Returns
        (hourly_values, raw_program), or None when the payload is neither
        class."""
        record = decode_program_record(parsed_frame.payload)
        if record is None:
            return None
        self._record_program(parsed_frame.src, record, PROGRAM_SOURCE_READ)
        return record.hourly, record.raw

    def _record_program(self, node_id, record, source):
        self.last_program[node_id] = record
        self.last_program_source[node_id] = source

    def program_record(self, node_id):
        """The whole ProgramRecord last decoded for `node_id`, or None before
        any read or report for it: resolution, native slots, the 168-value
        hourly projection and the raw reply bytes together, of which
        Coordinator.get_prog() reads only `.slots` back out (the record
        itself carries strictly more than any one of its own fields)."""
        return self.last_program.get(node_id)

    def program_resolution(self, node_id):
        """`node_id`'s own prog_resolution, 24 or 48 slots a day, or None before
        any program has been decoded for it. This is per node and set by the
        record length alone, exactly as the gateway sets its own structure's
        offset 0x154 and publishes it under this name."""
        record = self.last_program.get(node_id)
        return None if record is None else record.resolution

    def program_source(self, node_id):
        """The PROGRAM_SOURCE_* label for `node_id`'s last program cache
        update, or None before any read/report/write for it (schedule
        sensor's own `source` attribute)."""
        return self.last_program_source.get(node_id)

    def mark_program_written(self, node_id):
        """Called by the only caller of a set_schedule write (climate.py's
        async_set_schedule) right after Coordinator.async_set_schedule
        returns, so program_source() reports "written" for the write that
        just happened rather than "read" for its own read-back (see
        PROGRAM_SOURCE_WRITTEN's own comment)."""
        self.last_program_source[node_id] = PROGRAM_SOURCE_WRITTEN

    def forget_program(self, node_id):
        """Drop node_id's cached ProgramRecord and source (the coordinator's
        own device-removal bookkeeping, Coordinator.async_remove_heater):
        without this a deleted heater's last schedule would keep answering
        get_prog() for the whole quarantine window and, if a different
        identity is later paired onto the same freed id, would hand that new
        heater's schedule sensor a stale program it never reported."""
        self.last_program.pop(node_id, None)
        self.last_program_source.pop(node_id, None)


def is_report_payload(payload):
    """True when `payload` starts with the constant report marker `56`
    (PROTOCOL.md 5.4, 2026-09-06 17:08:20Z capture): E5, E2, EA and the 9E
    program report all start this way, whatever their own on-air class byte
    is. A caller (the coordinator's reader loop) uses this to decide whether
    an incoming frame needs a `F2 57 55` confirmation, instead of gating on a
    fixed set of on-air classes."""
    return len(payload) > 0 and payload[0] == REPORT_MARKER


def is_program_reply_payload(payload):
    """True for a 9F or C9 program frame's own payload: the `B1` opcode byte in
    one of the two payload lengths that carry a program (see
    PROGRAM_REPLY_PAYLOAD_LENS). A caller (the coordinator's reader loop) uses
    this to route a program frame that arrived outside a read_program() window;
    neither class carries the `56` report marker, so is_report_payload() above
    is False for both and they take no F2 57 55 confirmation."""
    return (
        len(payload) in PROGRAM_REPLY_PAYLOAD_LENS
        and payload[0] == PROGRAM_REPLY_FIRST_BYTE
    )


def is_energy_reply_payload(payload):
    """True for an EF energy reply's own payload: the `BD` opcode byte in the one
    payload length that class has (PROTOCOL.md 5.6). A caller (the coordinator's
    reader loop) uses this to route an EF that arrived outside a read_energy()
    window; `BD` is not the `56` report marker, so is_report_payload() above is
    False for it and it takes no F2 57 55 confirmation."""
    return (
        len(payload) == ENERGY_REPLY_PAYLOAD_LEN
        and payload[0] == ENERGY_REPLY_FIRST_BYTE
    )


def is_power_request_payload(payload):
    """True for a heater's own BE power-allocation request (PROTOCOL.md 5.1):
    the `BE` byte in the one payload length that carries it. A caller (the
    coordinator's reader loop) uses this to route it to a `BF` verdict reply;
    `BE` is not the `56` report marker, so is_report_payload() above is False
    for it and it takes no F2 57 55 confirmation."""
    return (
        len(payload) == POWER_REQUEST_PAYLOAD_LEN
        and payload[0] == POWER_REQUEST_FIRST_BYTE
    )


def is_registration_open_payload(payload):
    """True for a heater's own opening frame at power-up (PROTOCOL.md 5.6;
    payload `50`, an F3-length frame, heater to gateway)."""
    return bytes(payload) == REGISTRATION_OPEN_PAYLOAD


def _half_degrees(celsius):
    return int(round(celsius * 2))


def _check_setpoint_range(celsius):
    if not (MIN_SETPOINT_C <= celsius <= MAX_SETPOINT_C):
        raise ValueError(
            f"setpoint {celsius}C is outside the accepted range "
            f"{MIN_SETPOINT_C}-{MAX_SETPOINT_C}C inclusive (2026-09-06 proof, "
            "f3-and-edges-results.md section 2)"
        )


def _check_preset_order(antifrost_c, eco_c, comfort_c):
    for celsius, label in (
        (antifrost_c, "anti-frost"),
        (eco_c, "eco"),
        (comfort_c, "comfort"),
    ):
        half = celsius * 2
        if abs(half - round(half)) > 1e-9:
            raise ValueError(f"{label} preset {celsius}C is not a multiple of 0.5C")
    if not (MIN_SETPOINT_C <= antifrost_c < eco_c < comfort_c <= MAX_SETPOINT_C):
        raise ValueError(
            "preset temperatures must satisfy "
            f"{MIN_SETPOINT_C}C <= anti-frost < eco < comfort <= {MAX_SETPOINT_C}C "
            "(strictly increasing, each a multiple of 0.5C, so at least 0.5C "
            f"apart); got anti-frost {antifrost_c}C, eco {eco_c}C, "
            f"comfort {comfort_c}C"
        )


def program_resolution_for_payload_len(payload_len):
    """`payload_len`'s own prog_resolution in slots a day, or None when that
    length is not a program record at all.

    The gateway derives this from the record length and nothing else
    (FUN_400eda30 at 0x400eda51: shift left 2, divide by 7, store at structure
    offset 0x154), giving 24 for the 43-byte C9 record and 48 for the 85-byte
    9E/9F one; those two lengths are the two program frame classes, since a
    frame's class byte is its logical length XOR 0xFF."""
    return _PROGRAM_RESOLUTION_BY_PAYLOAD_LEN.get(payload_len)


def decode_program_record(payload):
    """One program payload (`B1` then the record's data bytes) -> a
    ProgramRecord at that record's own resolution, or None for a payload of any
    other length, so a frame that merely starts `B1` is never read as a
    program."""
    resolution = program_resolution_for_payload_len(len(payload))
    if resolution is None:
        return None
    raw_program = bytes(payload[1:])
    slots = _decode_program_slots(raw_program)
    return ProgramRecord(
        resolution=resolution,
        slots=slots,
        hourly=_fold_to_hourly(slots, resolution),
        raw=raw_program,
    )


def _decode_program_slots(raw_program):
    """The gateway's own 2-bit slot expander (FUN_40162c74), which is the whole
    of both program encodings: four slot codes per byte, MSB first, in wire
    order, day-major, day 0 Sunday (PROTOCOL.md 5.6/5.7). It takes no
    resolution, because there is none to take -- the record length decides how
    many of these slots make a day, not how they are unpacked.

    Re-verified byte for byte in this suite against the one program reference
    the repository holds, heater 04's `prog` array
    (docs/captures/2026-09-06-phase3/notes.md 10:28:21.265), over the 112 hours
    its truncated 24-slot-a-day C9 covers: 0 mismatches MSB-first, 26 of 112
    LSB-first. Code 3 has never been observed and is not one of the three slot
    values, so it decodes to None."""
    slots = []
    for byte in raw_program:
        for shift in (6, 4, 2, 0):
            code = (byte >> shift) & 0x3
            slots.append(code if code in PROGRAM_SLOT_NIBBLE else None)
    return slots


def _fold_to_hourly(slots, resolution):
    """`slots` at `resolution` projected onto one value an hour, the 168-value
    shape this station's program cache, `prog` attribute and set_schedule
    service all use. An hourly record is already that. A half-hourly one folds
    each hour's two half hours together, and an hour whose halves disagree folds
    to None: it has no hourly value, and None is what this station already
    reported for such an hour, when it read that hour's nibble as an
    unrecognised one rather than as two half hours."""
    if resolution == SLOTS_PER_DAY_HOURLY:
        return list(slots)
    return [
        first if first == second else None
        for first, second in zip(slots[0::2], slots[1::2])
    ]


def _decode_program_nibbles(raw_nibbles):
    """The half-hourly (85-byte 9E/9F/B2) record's data bytes read as one value
    an hour, high nibble first."""
    return _fold_to_hourly(_decode_program_slots(raw_nibbles), SLOTS_PER_DAY_HALF_HOURLY)


def _decode_program_bits(raw_bits):
    """The hourly (43-byte C9) record's data bytes read as one value an hour,
    which for that resolution is _decode_program_slots() itself."""
    return _decode_program_slots(raw_bits)


def _encode_program_slots(week_slots):
    """Inverse of _decode_program_slots(): 7 days of slot values, day-major,
    day 0 Sunday, packed four 2-bit codes to a byte, MSB first. A slot value is
    its own 2-bit code, so PROGRAM_SLOT_NIBBLE is consulted here only for which
    values exist. Callers pass wire-order days here (write_program() rotates
    its own Monday-first input first); this function itself does no rotation."""
    codes = [slot for day in week_slots for slot in day]
    body = bytearray()
    for index in range(0, len(codes), SLOTS_PER_PROGRAM_BYTE):
        byte = 0
        for offset, slot in enumerate(codes[index : index + SLOTS_PER_PROGRAM_BYTE]):
            if slot not in PROGRAM_SLOT_CODES:
                raise ValueError(f"unknown program slot value {slot!r}")
            byte |= slot << (6 - 2 * offset)
        body.append(byte)
    return bytes(body)


def rotate_week(slots, resolution, to_wire):
    """Rotate a flat, day-major week of `resolution` slots a day between the
    wire's own day order (day 0 Sunday, PROTOCOL.md 5.6/5.7) and this
    project's Monday-first HA surface order (day 0 Monday, matching the
    cloud's own `prog` array) -- the one rotation point the X-17 day-order fix
    puts at the boundary between the two, done here and nowhere else.

    Wire day 0 (Sunday) is HA day 6 (Monday-first), and every other day slides
    forward by one from wire to HA, so wire index i holds HA day (i - 1) % 7
    and HA index j holds wire day (j + 1) % 7. `to_wire=True` rotates a
    Monday-first week (write_program()'s own input) into wire order for
    encoding; `to_wire=False` rotates a decoded wire-order week
    (ProgramRecord.slots) into Monday-first for the HA surface
    (ProgramRecord.slots_monday_first, Coordinator.get_prog()). Both
    directions are the same day permutation run forwards or backwards, since
    shifting by one day seven times is the identity."""
    if len(slots) != resolution * DAYS_PER_WEEK:
        raise ValueError(
            f"expected {DAYS_PER_WEEK} days of {resolution} slots "
            f"({resolution * DAYS_PER_WEEK} total), got {len(slots)}"
        )
    days = [slots[day * resolution : (day + 1) * resolution] for day in range(DAYS_PER_WEEK)]
    shift = 1 if to_wire else -1
    rotated_days = [None] * DAYS_PER_WEEK
    for index, day in enumerate(days):
        rotated_days[(index + shift) % DAYS_PER_WEEK] = day
    return [slot for day in rotated_days for slot in day]


def _program_byte(first_hour_slot, second_hour_slot):
    """Two hours of a half-hourly record as one byte, each hour written into
    both of its half hours. write_program() reaches the same bytes through
    _encode_program_slots(); this stays for tools/proof_batch.py, which builds
    its own program frames against it."""
    try:
        hi = PROGRAM_SLOT_NIBBLE[first_hour_slot]
        lo = PROGRAM_SLOT_NIBBLE[second_hour_slot]
    except KeyError as exc:
        raise ValueError(f"unknown program slot value {exc}") from exc
    return (hi << 4) | lo

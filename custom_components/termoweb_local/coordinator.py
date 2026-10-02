"""DataUpdateCoordinator for termoweb_local.

Owns the single NanoCul client and Network for one config entry (docs/90-phase3-plan.md
P4: "a single DataUpdateCoordinator owning the one NanoCul client and polling
cadence"). A background task independently feeds every frame the stick receives
(reports, acks) into the Heater holders as soon as it arrives; the coordinator's own
timed refresh only drives the poll-each-heater cadence (idle default 300 s,
docs/PROTOCOL.md section 6), the daily clock sync and the hourly energy read
(ENERGY_POLL_INTERVAL_S). All blocking serial I/O
(open, read, send/ack-wait) runs in the executor; nothing here opens or touches the
port directly on the event loop.

Heater discovery (owner direction 2026-09-06: "the heaters should be autodiscovered
or there should be a workflow to add them" -- no heater list typed by hand). Three
ways a heater joins this coordinator, all converging on _register_heater_bookkeeping
plus a startup_sequence run: the ids-2-to-65 scan (async_scan_for_heaters, run once
at setup after the clock sync, and again on demand from the "Scan for heaters"
button), a runtime report or registration-opening from an id this coordinator has
never seen (_handle_event), and E7 pairing (_async_handle_discovery_frame /
_register_paired_heater, "Pair heater" button). Every path persists the new
heater to entry.options[CONF_HEATERS] (so a restart keeps its name) without
triggering the usual options-changed reload -- consume_skip_reload() -- and creates
its entities immediately through each platform's own stored async_add_entities
callback (register_platform), never a reload: a full reload is what a user-driven
options change (poll interval, dev id, ...) still uses, but re-running platform
setup for the entities that already exist just to add one more is needless churn a
brand-new heater's entities do not need.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Any, Callable

import serial
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import _vendor_compat  # noqa: F401 -- resolves termoweb_local before the imports below
from termoweb_local import frame as tf
from termoweb_local import network as network_module
from termoweb_local.heater import (
    E3_PAYLOAD_LEN,
    E4_PAYLOAD_LEN,
    E5_PAYLOAD_LEN,
    Heater,
    HeaterSnapshot,
    decode_energy_wh,
)
from termoweb_local.nanocul import RESET_WAIT_S, AckResult, NanoCul, Piece
from termoweb_local.network import (
    DISCOVERY_BROADCAST_ID,
    E7_FRAME_CLASS,
    GATEWAY_ID,
    MAX_PAIRED_HEATER_ID,
    MIN_PAIRED_HEATER_ID,
    PROGRAM_REPORT_MARKER,
    ROUTING_PATH_SLICE,
    TOGGLE_BOOST_OPCODE,
    TOGGLE_EASY_OPCODE,
    TOGGLE_LOCK_OPCODE,
    TOGGLE_REPLY_PAYLOADS,
    TOGGLE_RUNBACK_OPCODE,
    Network,
    is_energy_reply_payload,
    is_power_request_payload,
    is_program_reply_payload,
    is_registration_open_payload,
    is_relayed_announcement,
    is_report_payload,
)

from .const import (
    CLOCK_SYNC_INTERVAL_S,
    CONF_DEV_ID,
    CONF_HEATER_ADVANCED_SETUP,
    CONF_HEATER_ASSOCIATION,
    CONF_HEATER_ID,
    CONF_HEATER_IDENTITIES,
    CONF_HEATER_NAME,
    CONF_HEATERS,
    CONF_PAIR_HEATER_SECONDS,
    CONF_POLL_INTERVAL,
    CONF_SERIAL_URL,
    CONF_STATION_ID,
    DEFAULT_DEV_ID,
    DEFAULT_PAIR_HEATER_SECONDS,
    DEFAULT_POLL_INTERVAL_S,
    DOMAIN,
    GATEWAY_DEVICE_NAME,
    GATEWAY_MODEL,
    LINK_KEEPALIVE_INTERVAL_S,
)

# Gateway online (docs/91-p4-parity-plan.md P4a): "on when the serial link is
# open and at least one heater has reported within 2 report periods" -- the
# same stale_factor Heater.link_state() itself uses for STALE, but computed
# gateway-wide from report cadence only, never RSSI/LQI.
GATEWAY_STALE_FACTOR = 2.0

# 2026-09-06 live stall postmortem: heaters 03/04 never registered, the scan
# button found nothing, and nothing in the HA log said why -- because there
# was nothing to say why. Every executor call that touches the port is now
# wrapped in asyncio.wait_for with one of these ceilings (generous margins
# above network.py's own DEFAULT_REPLY_TIMEOUT_S/STARTUP_REPLY_TIMEOUT_S/
# SCAN_REPLY_TIMEOUT_S, which bound one on-air reply, not the executor call
# around it) so a wedged transport surfaces as a logged ERROR within a bounded
# time instead of a silently stuck _io_lock.
READ_TIMEOUT_S = 5.0
COMMAND_TIMEOUT_S = 3.0
SCAN_TIMEOUT_S = 30.0
STARTUP_SEQUENCE_TIMEOUT_S = 15.0
CONNECT_TIMEOUT_S = 10.0

# Reconnects: never fire two within this many seconds of the last successful
# connect (the watchdog and a timed-out command can both decide to reconnect
# within the same second; _reconnecting already stops them stacking, this
# stops a second one starting moments after the first one just finished).
RECONNECT_DEBOUNCE_S = 5.0

# Reopen backoff (2026-09-09 live postmortem): a ser2net bridge that is down
# refuses the socket straight away, so a failed reopen left the old, closed
# NanoCul bound and the reader loop asked for another reconnect on its very
# next read -- 3578 reconnects during a roughly 6 minute outage, about 600 a
# minute, every one of them logging "reader read failed: Attempting to use a
# port that is not open". Each consecutive failed reopen now doubles the wait
# from RECONNECT_BACKOFF_MIN_S up to RECONNECT_BACKOFF_MAX_S, with the reader
# loop held off the transport for it (_async_wait_before_read), which costs
# that same outage about ten attempts instead. The cap stays well inside one
# report period (docs/PROTOCOL.md section 6: 300 s idle), so a bridge that
# comes back is still picked up on its own, and a successful connect clears
# the backoff outright.
RECONNECT_BACKOFF_MIN_S = 5.0
RECONNECT_BACKOFF_MAX_S = 60.0

# Stall watchdog (2026-09-06 owner direction, task 3; thresholds revised the
# same day after the live postmortem below): a periodic check, independent of
# every other executor-call timeout above, that reconnects when the link
# looks dead -- the backstop for exactly the failure mode above, where the
# reader loop's own per-call timeout either has not fired yet or (peer
# review, same day) never will because the call it is waiting on is not one
# of the wrapped ones.
#
# Which threshold applies depends on whether a heater is known, never on how
# long ago this coordinator connected: WATCHDOG_STALL_S (600 s, 2 report
# periods at the 300 s idle default, docs/PROTOCOL.md section 6) once at
# least one heater is known, since an idle heater simply does not transmit
# more often than that; WATCHDOG_STARTUP_STALL_S (90 s) only while none is
# known at all, since the setup-time scan (and its own retry, see
# async_setup) should already have found one well within that time. The live
# postmortem this replaces used the 90 s threshold for a time window after
# connect regardless of whether heaters were already known, which is exactly
# why it fired on perfectly healthy, merely idle heaters.
#
# Either threshold still only leads to an actual reconnect when the last
# command attempt (any send) itself failed or timed out (_last_command_ok);
# a stall with a last command that succeeded means the link is fine and the
# heaters are simply quiet, logged at INFO instead of reconnecting.
WATCHDOG_INTERVAL_S = 60.0
WATCHDOG_STALL_S = 600.0
WATCHDOG_STARTUP_STALL_S = 90.0

# Watchdog-triggered reconnects specifically (as opposed to the immediate,
# short RECONNECT_DEBOUNCE_S above, shared by every reconnect trigger) are
# capped to one per this many seconds: a stall that outlives one reconnect
# attempt would otherwise refire every WATCHDOG_INTERVAL_S forever.
WATCHDOG_RECONNECT_COOLDOWN_S = 600.0

# First connection only (task 2, same postmortem): after the connect and the
# Q line, before the clock sync and the scan, actively read and discard
# whatever the stick already printed during its own reset -- NanoCul's own
# reset_input_buffer() call at construction time only clears a real
# pyserial port's local buffer, which is a documented no-op for a socket://
# URL (this integration's typical serial_url, e.g. ser2net), so a boot
# banner or stray RX bytes from that 2.5 s reset window can otherwise sit
# unread in the socket and desync the very first scan's own on-air replies.
# If that first scan still finds nothing while the link itself is alive (Q
# was answered), it is retried once after this delay before declaring none.
INITIAL_SCAN_RETRY_DELAY_S = 5.0

# INFO stick-line logging (task 1; this integration cannot be switched to
# DEBUG through the API on the affected system): every line the stick sends
# is logged at INFO for the first this-many lines after each connect, DEBUG
# after that, so a long healthy run does not flood the log forever.
FRAME_LOG_INFO_COUNT = 200

# Pairing logging (analysis7.md section 11 item 1): the whole logical frame of
# every E7 announcement is logged, but only the first sighting of each distinct
# header goes to INFO, and only this many distinct headers per window do.
# The announcement sweeps its destination across ids 02 to 21 about 200 ms at a
# time (docs/PROTOCOL.md 5.9) and every already-paired heater relays its own
# copy, so a default 120 s window carries on the order of a thousand E7 frames
# and perhaps thirty-five distinct headers: the sweep's own destinations, plus
# one per relay. Logging every copy at INFO would push the rest of the pairing
# evidence out of the rolling `ha core logs` buffer the acceptance capture
# reads, which would defeat the purpose of logging it at all; logging only the
# novel ones keeps the INFO volume bounded by the sweep's width instead of by
# the window's length. The cap bounds it even if something on the air makes
# every copy look novel.
DISCOVERY_HEADER_LOG_LIMIT = 160

# Pairing (2026-09-09 bench attempts, docs/PROTOCOL.md 5.9). The heater keeps
# sweeping its E7 announcement across the id range for as long as its own
# pairing mode is open, and only adopts an id it hears while it is still on
# the destination that produced the reply, so the port has to stay free for
# the next announcement rather than be spent enrolling an id the heater has
# not adopted yet. The real gateway waits about 60 s after the assignment
# before its clock-and-query-burst enrolment, and the 2026-09-09 attempt that
# ran it immediately instead held the port for 5.6 s of timeouts and missed
# the announcements that followed.
PAIRED_ENROLMENT_DELAY_S = 60.0

# Device deletion (2026-09-09 bench attempts): a heater whose device the
# owner deletes is still out there on its id and re-registers itself from
# its own next report, about 300 s later at the idle cadence
# (docs/PROTOCOL.md section 6). Its id is therefore neither in use nor free
# for this long: _handle_event does not re-register it, and pairing does not
# hand its id to a different identity. Three report periods, so a heater
# that really has gone is released after one missed report rather than on
# the deletion alone.
REMOVED_HEATER_QUARANTINE_S = 900.0

# Energy poll cadence (F3 BC -> EF, docs/PROTOCOL.md 5.6). The real gateway polls
# every node hourly on the top of the hour, 250 ms apart, and buckets the answers
# into its own hourly flash records; an hour is kept here, but deliberately not the
# top-of-hour alignment, which exists to fill those flash buckets and has no meaning
# for a station whose history is Home Assistant's own recorder.
#
# The poll is gated inside _async_update_data on elapsed time, exactly the way the
# daily clock sync above it is, rather than run from a clock-aligned callback of its
# own: that keeps every radio call on the single path that already serialises through
# _run_locked/_io_lock, so an energy read can never contend with the reader loop or
# with a status request the way an independently-scheduled task could. The cost is
# that a poll lands wherever the refresh cadence puts it (one per 3600 to 3600 +
# update_interval seconds) instead of on the hour, which the counter itself does not
# care about: it is cumulative and heater-authored, so a reading is worth the same
# whenever it is taken.
ENERGY_POLL_INTERVAL_S = 3600.0

_LOGGER = logging.getLogger(__name__)

# A heater's own opening frame at power-up (PROTOCOL.md 5.6; 2026-09-06
# 17:08:20Z re-registration capture, notes.md): an F3-length frame carrying
# Network.REGISTRATION_OPEN_PAYLOAD. Local to this module like NanoCul's own
# ACK_ON_AIR_CLASS, since it is only ever used to classify a raw Piece here.
F3_FRAME_CLASS = 0xF3

# C4 advanced-setup defaults for a heater with no persisted record yet
# (2026-09-13 X-19 proof, docs/captures/2026-09-13-x19/notes.md): PID control
# mode, Celsius, zero offset, everything else off -- the manual's own
# factory defaults, matching what a twice-reset heater 04 actually held
# during that session.
_DEFAULT_ADVANCED_SETUP: dict[str, int] = {
    "control_mode": 4,
    "units": 0,
    "offset_tenths": 0,
    "away_mode": 0,
    "away_offset": 0,
    "modified_auto_span": 0,
    "window_mode": 0,
    "true_radiant": 0,
}


def _dedupe_identity_hex(
    identity_hex: dict[str, str], node_ids: tuple[int, ...]
) -> dict[str, str]:
    """Resolve CONF_HEATER_IDENTITIES entries that map the same identity to
    more than one node id (2026-09-15 x07: options held the announcing
    heater's identity under both '4' and '6', and the raw dict comprehension
    that used to build Network's known_identities picked whichever key
    iterates last -- insertion order, not node id -- so a stale leftover
    entry from an earlier pairing/removal silently outranked the heater's
    own configured id on every fresh load). For each duplicated identity,
    keep the entry whose node id is a currently configured heater; if none
    of them is (or more than one is, which should not happen but is not
    this function's job to prevent), keep the lowest node id and warn either
    way so a stale entry does not linger unnoticed."""
    by_identity: dict[str, list[str]] = {}
    for node_id, hex_value in identity_hex.items():
        by_identity.setdefault(hex_value, []).append(node_id)
    resolved: dict[str, str] = {}
    for hex_value, node_id_strs in by_identity.items():
        if len(node_id_strs) == 1:
            resolved[node_id_strs[0]] = hex_value
            continue
        configured = [nid for nid in node_id_strs if int(nid) in node_ids]
        keep = min(configured or node_id_strs, key=int)
        dropped = [nid for nid in node_id_strs if nid != keep]
        _LOGGER.warning(
            "identity %s is mapped to more than one node id in stored options "
            "(%s); keeping %s%s and dropping %s",
            hex_value, ", ".join(node_id_strs), keep,
            " (a configured heater)" if int(keep) in node_ids else "",
            ", ".join(dropped),
        )
        resolved[keep] = hex_value
    return resolved


def _prune_other_ids_for_identity(
    identity_hex: dict[str, str], identity_hex_value: str, keep_node_id: int
) -> dict[str, str]:
    """Drop every entry that maps `identity_hex_value` to a node id other
    than `keep_node_id`, so writing a fresh mapping for one id never leaves a
    stale duplicate under another (the accumulation that produced the
    2026-09-15 x07 bug: a heater relearns or gets re-paired onto a different
    id and the id it used to hold is never cleared out of options)."""
    keep_node_id_str = str(keep_node_id)
    return {
        node_id: hex_value
        for node_id, hex_value in identity_hex.items()
        if hex_value != identity_hex_value or node_id == keep_node_id_str
    }


class TermowebLocalCoordinator(DataUpdateCoordinator[dict[int, Heater]]):
    """Poll cycle plus a background frame reader for one nanoCUL/station."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        # entry.data has no CONF_HEATERS key at all for an entry created by
        # the current config flow (owner direction 2026-09-06: no heater
        # list typed by hand); heaters live only in entry.options, written
        # by the discovery scan, a runtime unknown-id report, or pairing.
        heaters_conf: list[dict[str, Any]] = entry.options.get(
            CONF_HEATERS, entry.data.get(CONF_HEATERS, [])
        )
        poll_interval = entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL_S)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=dt.timedelta(seconds=poll_interval),
        )
        self.entry = entry
        self.heater_names: dict[int, str] = {
            h[CONF_HEATER_ID]: h[CONF_HEATER_NAME] for h in heaters_conf
        }
        node_ids = tuple(self.heater_names)
        self._station_id = int(entry.data[CONF_STATION_ID], 16)
        self._serial_url = entry.data[CONF_SERIAL_URL]
        # Pairing (2026-09-06 17:12:17Z capture): identity -> node id, so a
        # re-announcing heater gets its own id back across restarts too, not
        # just within one coordinator's lifetime. Deduped first: identity_hex
        # is keyed by node id, so a stale entry left over from an earlier
        # pairing/removal can hold the same identity as the heater's current
        # id, and building known_identities straight off it would pick
        # whichever key happens to iterate last.
        stored_identity_hex = entry.options.get(CONF_HEATER_IDENTITIES, {})
        identity_hex: dict[str, str] = _dedupe_identity_hex(stored_identity_hex, node_ids)
        known_identities = {
            bytes.fromhex(hex_value): int(node_id) for node_id, hex_value in identity_hex.items()
        }
        self.network = Network(
            node_ids=node_ids, station_id=self._station_id, known_identities=known_identities
        )
        if self._station_id != GATEWAY_ID:
            # CONF_STATION_ID is a free hex field in the config flow, so this
            # is reachable, and it makes the relayed half of pairing inert
            # rather than broken-loudly: a heater builds its routing path with
            # the gateway's own `01` in it, so a relayed announcement's path
            # holds nothing matching this station and reverse_routing_path
            # returns None for every one of them (docs/PROTOCOL.md 5.9). The
            # broadcast fallback still pairs a heater that announces to this
            # station directly, which is why this is a warning and not a
            # setup failure.
            _LOGGER.warning(
                "station id %02x is not the gateway's own %02x: an announcement relayed "
                "by an already-paired heater cannot be answered through that relay, "
                "because the path a heater builds names the gateway as %02x and the "
                "reversal has nothing to match. Pairing still works for a heater whose "
                "announcement sweep reaches this station directly (docs/PROTOCOL.md 5.9)",
                self._station_id, GATEWAY_ID, GATEWAY_ID,
            )
        # Distinct E7 headers already logged at INFO, reset when a pairing
        # window opens (_log_discovery_header, DISCOVERY_HEADER_LOG_LIMIT).
        self._discovery_headers_seen: set[bytes] = set()
        # One warning per window for a relayed announcement whose path could
        # not be reversed; the sweep would otherwise repeat it every 200 ms.
        self._relay_reversal_warned = False
        self.heaters: dict[int, Heater] = {
            node_id: Heater(node_id) for node_id in node_ids
        }
        self._nanocul: NanoCul | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._stop_reader = asyncio.Event()
        self._last_clock_sync: dt.datetime | None = None
        self._last_energy_poll: dt.datetime | None = None
        # node_id -> when this coordinator last sent that heater an EB frame
        # (clock sync or keepalive), for the link keepalive check in
        # _async_update_data (LINK_KEEPALIVE_INTERVAL_S). Populated by
        # _async_sync_clock_all/async_sync_clock_one, whatever the reason for
        # that EB was, so a keepalive never follows one of those too soon.
        self._last_eb_sent: dict[int, dt.datetime] = {}

        # Reconnect/diagnostic state (task 1-3): _connected_at anchors the
        # INFO stick-line budget; _last_command_at/_last_command_ok are
        # surfaced in the watchdog's own log lines and gate whether a stall
        # actually reconnects (WATCHDOG_STALL_S/WATCHDOG_STARTUP_STALL_S's
        # own docstring above); _reconnecting stops the watchdog, a timed-out
        # read, and a timed-out command all stacking a second reconnect on
        # top of one already running, parks the reader loop off the transport
        # for as long as the reconnect owns it, and tells that loop that an
        # exception from its in-flight read is this reconnect closing the
        # transport out from under it, not a real fault (_reader_loop);
        # _reconnect_backoff_s/_reconnect_retry_at pace the reopen attempts
        # after a failure (RECONNECT_BACKOFF_MIN_S's own docstring above);
        # _last_watchdog_reconnect_at is WATCHDOG_RECONNECT_COOLDOWN_S's own
        # anchor; reconnect_count is a gateway_online attribute.
        self._connected_at: float | None = None
        self._last_command_at: float | None = None
        self._last_command_ok: bool | None = None
        self._last_watchdog_reconnect_at: float | None = None
        self._frames_since_connect = 0
        self._reconnect_idle = asyncio.Event()
        self._reconnecting = False
        self._reconnect_backoff_s = 0.0
        self._reconnect_retry_at = 0.0
        self.reconnect_count = 0
        # Set by _log_stick_status (task 2): whether the stick answered Q at
        # all on this connect, i.e. the link itself is alive even if the
        # scan right after it finds no heater.
        self._q_answered = False

        # docs/91-p4-parity-plan.md P4a owner decision, an options-flow field
        # with the plan's own stated default.
        self.dev_id: str = entry.options.get(CONF_DEV_ID, DEFAULT_DEV_ID)

        # node_id -> the 9-byte EB association-family value to send that
        # heater at station start (Network.startup_sequence), for a heater
        # whose value the owner has captured and entered in the options flow
        # (docs/PROTOCOL.md 5.6's association family; per-heater, optional,
        # default skip). Stored in options as a hex string (JSON options
        # storage needs a string, not bytes), decoded to bytes here.
        association_hex: dict[str, str] = entry.options.get(CONF_HEATER_ASSOCIATION, {})
        self._association_values: dict[int, bytes] = {
            int(node_id): bytes.fromhex(hex_value) for node_id, hex_value in association_hex.items()
        }

        # C4 advanced-setup record, per node (2026-09-13 X-19 proof,
        # PROTOCOL.md 5.6): the record last accepted for that heater, or
        # _DEFAULT_ADVANCED_SETUP for one never written this way. Persisted
        # the same way association_hex/identity_hex are, decoded here from
        # entry.options's own JSON-safe string keys.
        advanced_setup_options: dict[str, dict] = entry.options.get(CONF_HEATER_ADVANCED_SETUP, {})
        self._advanced_setup: dict[int, dict] = {
            int(node_id): dict(fields) for node_id, fields in advanced_setup_options.items()
        }

        # Runback's own pre-toggle mode/setpoint cache (2026-09-13 X-19
        # proof: Runback off does not restore the setpoint it displaced, so
        # this coordinator restores it itself). None means "turned off with
        # no cache", the panel-originated case switch.py's own
        # extra_state_attributes reports as "not restored"; in-memory only
        # (Risks: a coordinator restart between Runback on and off loses it,
        # by design -- see async_set_runback's own docstring).
        self._runback_cache: dict[int, tuple[str, float] | None] = {}
        # Whether the last Runback-off actually restored the cached
        # mode/setpoint (see async_set_runback): None means this coordinator
        # has never turned Runback off for this node (switch.py's own
        # "restored" attribute starting point), True/False once it has.
        self._runback_restored: dict[int, bool | None] = {}

        # F3 B0 program-read cache (climate.py's `prog` attribute): no cache
        # lives here any more. Network.last_program already holds one
        # ProgramRecord per node -- at that node's own prog_resolution, 24 or
        # 48 slots a day (Network.program_record/program_resolution) -- kept
        # current by Network.startup_sequence's own program read, the
        # set_schedule service's read-back, and a 9E program report pushed
        # during a heater's registration reply, all of which already call
        # Network._record_program(). get_prog() below reads that record's
        # own native slots directly; keeping a second, coordinator-local copy
        # duplicated the cache and was the reason it only ever held the
        # 168-value hourly projection, silently folding away a half-hourly
        # schedule's own half-hour boundaries before any sensor saw them
        # (docs/80-handover.md "Owed" list, item 7).

        # Gateway online (docs/91-p4-parity-plan.md P4a binary_sensor):
        # `_port_open` is the serial-link half of "connected"; the report-
        # cadence half comes from each Heater's own last_report_time.
        self._port_open = False
        self._link_healthy_since: float | None = None
        # NanoCul.send_frame does its own blocking read_events() loop while it waits
        # for an ack; without this lock that loop and the background reader's own
        # read_events() call would both call the transport's readline() from two
        # different executor threads at once, and whichever one wins can steal the
        # ack the other was waiting for. Every read or send goes through this lock
        # so exactly one thread ever touches the transport at a time.
        self._io_lock = asyncio.Lock()
        # Set by async_setup, before any platform builds a heater DeviceInfo
        # (devices.py's via_device_id needs the gateway device's registry id,
        # not just its identifiers).
        self.gateway_device_id: str | None = None

        # "Pair heater" button window length (CONF_PAIR_HEATER_SECONDS,
        # default 120; owner direction 2026-09-06 task 3): a persisted
        # option now, not the one-shot options-flow trigger this used to be.
        self.pair_heater_seconds: float = float(
            entry.options.get(CONF_PAIR_HEATER_SECONDS, DEFAULT_PAIR_HEATER_SECONDS)
        )

        # Per-platform entity builders/adders, registered by each platform's
        # own async_setup_entry (register_platform), so a heater discovered
        # after setup (scan, an unknown id's own report, or pairing) gets
        # its entities created immediately -- no reload, no restart.
        self._platform_adders: dict[str, Callable[[list], None]] = {}
        self._platform_builders: dict[str, Callable[[int], list]] = {}

        # node_id -> time.monotonic() when its deletion stops being treated as
        # unconfirmed (REMOVED_HEATER_QUARANTINE_S), and the scheduled
        # enrolments a pairing assignment has outstanding, cancelled at
        # shutdown (PAIRED_ENROLMENT_DELAY_S).
        self._removed_heaters: dict[int, float] = {}
        self._enrolment_cancels: list[Callable[[], None]] = []

        # The open "Pair heater" window's own expiry callback, and whether an
        # id was assigned while it was open (start_pairing).
        self._pairing_window_cancel: Callable[[], None] | None = None
        self._pairing_window_paired = False

        # See consume_skip_reload(): set right before an options update this
        # coordinator itself makes to persist a newly discovered/removed
        # heater, so __init__.py's update listener does not reload the
        # entry for it (a reload would just redo, by a slower path, what
        # register_platform's live entity creation already did).
        self._skip_next_reload = False
        self._self_written_options: dict[str, Any] | None = None

        # _dedupe_identity_hex above only fixes known_identities in memory;
        # without this, the stored map kept a resolved duplicate forever
        # (2026-09-16 residual of commit 5c15b7b's own fix): the in-memory
        # resolution never reached entry.options, so any unrelated options
        # write -- an advanced-setup C4 write from number.py/select.py, a
        # heater rename, ... -- just carried the still-duplicated stored
        # dict forward untouched, and the "mapped to more than one node id"
        # warning above kept firing at every restart. Comparing against
        # stored_identity_hex (read once, before _dedupe_identity_hex ran)
        # rather than re-deduping again here keeps this to one persist per
        # load, only when there was actually a duplicate to resolve; placed
        # after the reset above (not beside identity_hex's own construction)
        # so _persist_options_no_reload's skip-next-reload bookkeeping is not
        # immediately overwritten by it.
        if identity_hex != stored_identity_hex:
            _LOGGER.info(
                "stored heater identity map held a duplicate; rewriting it to %s",
                identity_hex,
            )
            new_options = dict(entry.options)
            new_options[CONF_HEATER_IDENTITIES] = identity_hex
            self._persist_options_no_reload(new_options)

    @property
    def _reconnecting(self) -> bool:
        """Whether the reconnect path currently owns the transport.

        Backed by _reconnect_idle rather than a plain attribute so the reader
        loop can park on exactly the state it tests, with no polling interval
        and no second flag that could drift out of step with this one.
        """
        return not self._reconnect_idle.is_set()

    @_reconnecting.setter
    def _reconnecting(self, value: bool) -> None:
        if value:
            self._reconnect_idle.clear()
        else:
            self._reconnect_idle.set()

    async def async_setup(self) -> None:
        """Open the port and start the background reader; call once, before the
        coordinator's first refresh."""
        self._register_gateway_device()
        await self._connect_nanocul()
        self._stop_reader.clear()
        self._reader_task = self.config_entry.async_create_background_task(
            self.hass,
            self._reader_loop(),
            name=f"{DOMAIN}-reader-{self.entry.entry_id}",
        )
        self._watchdog_task = self.config_entry.async_create_background_task(
            self.hass,
            self._watchdog_loop(),
            name=f"{DOMAIN}-watchdog-{self.entry.entry_id}",
        )
        await self._async_sync_clock_all()
        for node_id in list(self.heaters):
            await self._async_run_startup_sequence(node_id)
        # Discovery scan (owner direction 2026-09-06 task 2): after the
        # clock sync and every already-configured heater's own startup
        # burst above, sweep ids 2-65 for anything not yet configured. Runs
        # on every setup (not just the first), so a heater that appeared
        # while Home Assistant was stopped is not missed; an id already in
        # self.heaters is left alone (async_scan_for_heaters only registers
        # ids it does not already know).
        #
        # First-connection retry (2026-09-06 postmortem, task 2): a scan that
        # finds nothing at all while the link itself is alive (Q was
        # answered) is retried once after INITIAL_SCAN_RETRY_DELAY_S before
        # this setup gives up on it -- only here, not on the button's or a
        # reconnect's own scan, since only the very first connection can
        # still be settling from the stick's own reset regardless of the
        # drain above.
        found, _ = await self._async_scan_once()
        if not found and self._q_answered:
            _LOGGER.info(
                "initial scan found no heaters (link alive); retrying once after %.0fs",
                INITIAL_SCAN_RETRY_DELAY_S,
            )
            await asyncio.sleep(INITIAL_SCAN_RETRY_DELAY_S)
            found, _ = await self._async_scan_once()
            if not found:
                _LOGGER.warning("second scan also found no heaters")

    def register_platform(
        self, platform: str, async_add_entities, builder: Callable[[int], list]
    ) -> None:
        """Called once by each platform's own async_setup_entry so a heater
        discovered later (scan, an unknown id's own report, or pairing) gets
        this platform's entities created immediately, with no reload and no
        restart. `builder(node_id)` returns that platform's list of
        entities for one heater; `async_add_entities` is the callback HA
        handed that platform's async_setup_entry."""
        self._platform_adders[platform] = async_add_entities
        self._platform_builders[platform] = builder

    def _create_entities_for_heater(self, node_id: int) -> None:
        for platform, builder in self._platform_builders.items():
            self._platform_adders[platform](builder(node_id))

    def consume_skip_reload(self) -> bool:
        """__init__.py's update listener calls this once per options update;
        True means this coordinator itself made that change (a newly
        discovered, removed or identified heater) and already applied it
        live, so the listener must not also reload the entry for it.

        The armed flag is matched against the options this coordinator
        actually wrote, not consumed on sight: an update it makes while the
        entry is still setting up (the setup scan's own registrations and
        identity reads) has no listener to consume the flag at all, and a
        flag left armed used to swallow the next real options change
        instead."""
        if self._skip_next_reload and self.entry.options == self._self_written_options:
            return True
        self._skip_next_reload = False
        self._self_written_options = None
        return False

    def _persist_options_no_reload(self, new_options: dict[str, Any]) -> None:
        self._skip_next_reload = True
        self._self_written_options = dict(new_options)
        self.hass.config_entries.async_update_entry(self.entry, options=new_options)

    def _register_heater_bookkeeping(
        self, node_id: int, *, name: str | None = None, identity: bytes | None = None
    ) -> str:
        """The non-blocking part of registering a brand-new heater: default
        name, this coordinator's live tables, persisted options (no
        reload), and its entities (created immediately through each
        platform's stored adder). Returns the name used. No radio I/O here,
        so this never delays a caller's own time-bound reply (e.g. the
        registration-open frame's required EB clock reply) -- run the
        slower startup_sequence, and any pairing-only follow-up like the
        persistent notification, after calling this, not before."""
        heater_name = name or f"Heater {node_id:02X}"
        self.heater_names[node_id] = heater_name
        self.heaters[node_id] = Heater(node_id)
        self.network.node_ids = self.network.node_ids + (node_id,)

        current_heaters = self.entry.options.get(CONF_HEATERS, self.entry.data.get(CONF_HEATERS, []))
        new_options = dict(self.entry.options)
        new_options[CONF_HEATERS] = list(current_heaters) + [
            {CONF_HEATER_ID: node_id, CONF_HEATER_NAME: heater_name}
        ]
        if identity is not None:
            current_identity_hex = self.entry.options.get(CONF_HEATER_IDENTITIES, {})
            new_options[CONF_HEATER_IDENTITIES] = {
                **_prune_other_ids_for_identity(current_identity_hex, identity.hex(), node_id),
                str(node_id): identity.hex(),
            }
            self.network.known_identities[identity] = node_id
        self._persist_options_no_reload(new_options)
        self._create_entities_for_heater(node_id)
        _LOGGER.info(
            "registered heater %02x as %r%s",
            node_id, heater_name, " (paired identity)" if identity is not None else "",
        )
        return heater_name

    def _scan_for_heaters_sync(self) -> dict[int, HeaterSnapshot]:
        """Blocking: probe every id in MIN_PAIRED_HEATER_ID..MAX_PAIRED_HEATER_ID
        with a single F3 B8 status request each, logging each id's own ack/reply
        result and timing at INFO (task 1) -- the one thing an INFO-only log of
        the 2026-09-06 stall could not show: whether the scan's own frames were
        even going out and getting acked, versus simply getting no reply back.
        Reimplemented here rather than calling Network.scan_for_heaters (which
        only returns the final {node_id: HeaterSnapshot} dict, with no per-id
        ack/reply distinction to log) since that method is owned by a change in
        flight elsewhere and has no hook for this; the request/decode shape
        mirrors Network._request_and_wait exactly, including its reply-payload
        length constraint, and uses Network's own public STATUS_OPCODE,
        STATUS_REPLY_PAYLOAD_LEN and station_id, not a value invented here."""
        assert self._nanocul is not None
        timeout = network_module.SCAN_REPLY_TIMEOUT_S
        found: dict[int, HeaterSnapshot] = {}
        saved_retries = self._nanocul.retries
        saved_retry_interval = self._nanocul.retry_interval
        self._nanocul.retries = 1
        self._nanocul.retry_interval = timeout
        try:
            for node_id in range(MIN_PAIRED_HEATER_ID, MAX_PAIRED_HEATER_ID + 1):
                start = time.monotonic()
                air = tf.build_frame(
                    self.network.station_id, node_id, bytes([network_module.STATUS_OPCODE])
                )
                ack_result = self._nanocul.send_frame(node_id, air)
                if not ack_result.ok:
                    _LOGGER.info(
                        "scan %02x: ack=no reply=no (%.0f ms)",
                        node_id, (time.monotonic() - start) * 1000,
                    )
                    continue
                expected_first_byte = (network_module.STATUS_OPCODE + 1) & 0xFF
                # Accept E4 (Runback boost running) alongside the plain E6 -- same
                # match Network.request_status itself now takes.
                expected_lens = (
                    network_module.STATUS_REPLY_PAYLOAD_LEN,
                    network_module.STATUS_REPLY_PAYLOAD_LEN_E4,
                )
                reply = self._nanocul.wait_for_reply(
                    node_id,
                    lambda frame, expected=expected_first_byte, lengths=expected_lens: (
                        len(frame.payload) in lengths and frame.payload[0] == expected
                    ),
                    timeout,
                )
                elapsed_ms = (time.monotonic() - start) * 1000
                if reply is None:
                    _LOGGER.info("scan %02x: ack=yes reply=no (%.0f ms)", node_id, elapsed_ms)
                    continue
                _LOGGER.info("scan %02x: ack=yes reply=yes (%.0f ms)", node_id, elapsed_ms)
                found[node_id] = HeaterSnapshot.from_e6(reply)
        finally:
            self._nanocul.retries = saved_retries
            self._nanocul.retry_interval = saved_retry_interval
        return found

    async def async_scan_for_heaters(self) -> list[int]:
        """Probe ids 2-65 for any answering heater and register every id not
        already configured, and not still in the deletion quarantine, as a
        new heater. Returns the list of newly registered ids. Bound to the
        "Scan for heaters" button, to
        async_setup's own scan, and to the watchdog's own no-heater-known
        rescan (_check_stall_no_heaters)."""
        _, new_ids = await self._async_scan_once()
        return new_ids

    async def _async_scan_once(self) -> tuple[dict[int, HeaterSnapshot], list[int]]:
        """The full body of a scan: probe, register every newly-answering id,
        and run each one's startup_sequence. Returns both the raw per-id
        results (every id that answered, new or already known -- async_setup's
        own first-connection retry, task 2, needs to tell "found nothing at
        all" apart from "found nothing new") and the list of newly registered
        ids (async_scan_for_heaters's own public return value).

        Sequences run only after every id has been probed and every new
        heater's bookkeeping/entities are already in place (task 5): a slow
        startup_sequence must never be interleaved with -- and so starve --
        the scan of ids it has not reached yet."""
        _LOGGER.info(
            "scan for heaters: starting ids %02x-%02x", MIN_PAIRED_HEATER_ID, MAX_PAIRED_HEATER_ID
        )
        scan_start = time.monotonic()
        try:
            found = await self._run_locked(
                self._scan_for_heaters_sync, timeout=SCAN_TIMEOUT_S, description="scan for heaters"
            )
        except asyncio.TimeoutError:
            await self._async_reconnect("scan for heaters timed out")
            return {}, []
        elapsed = time.monotonic() - scan_start
        found_list = ", ".join(f"{nid:02x}" for nid in sorted(found)) or "none"
        _LOGGER.info("scan for heaters: done in %.1fs, found: %s", elapsed, found_list)

        new_ids: list[int] = []
        for node_id in sorted(found):
            if node_id in self.heaters:
                _LOGGER.info("scan %02x: already a known heater, not re-registering", node_id)
            elif self._is_quarantined(node_id):
                # The deletion quarantine has to hold on this path too
                # (2026-09-09 18:26:36 live run: a scan pressed two minutes
                # after the owner deleted heater 04 re-registered it and
                # recreated every entity -- the same failure _handle_event's
                # own check prevents, reached by the other door). The id is
                # still probed and still counted in `found`: its per-id
                # ack/reply line is the log's only evidence that a deleted
                # heater is still on the air, and async_setup's
                # first-connection retry and the watchdog's no-heater rescan
                # both read `found` to tell "found nothing at all" apart from
                # "found nothing new" -- skipping the probe would make a
                # station whose only heater was just deleted look like a dead
                # radio.
                _LOGGER.info(
                    "scan %02x: answered after its device was deleted; not re-registering "
                    "until the deletion has held for %.0fs",
                    node_id, REMOVED_HEATER_QUARANTINE_S,
                )
            else:
                new_ids.append(node_id)
        for node_id in new_ids:
            self._register_heater_bookkeeping(node_id)
            heater = self.heaters.get(node_id)
            if heater is not None:
                heater.record_snapshot(found[node_id])
        for node_id in new_ids:
            await self._async_run_startup_sequence(node_id)
        return found, new_ids

    async def async_remove_heater(self, node_id: int) -> None:
        """Drop a heater from persisted options/identities and this
        coordinator's live tables, with no reload -- used by
        async_remove_config_entry_device (__init__.py) when a user deletes
        a heater's device from the HA UI; HA removes the device (and, with
        it, every entity registered under it) itself once that callback
        returns True, so no entity teardown is needed here.

        The deletion is not confirmed by the heater in any way: it keeps its
        id and re-registers itself from its own next report, about 300 s
        later at the idle cadence, which is how the 2026-09-09 attempt found
        the heater it had just deleted back on the network in time to be
        given a second id (docs/PROTOCOL.md 5.9). The id is therefore held in
        quarantine for REMOVED_HEATER_QUARANTINE_S -- neither re-registered
        from a report (_handle_event) nor handed to another identity
        (Network.reserved_node_ids) -- and its identity mapping is kept, so
        re-pairing the same heater gives it back the id it still holds."""
        current_heaters = self.entry.options.get(CONF_HEATERS, self.entry.data.get(CONF_HEATERS, []))
        new_heaters = [h for h in current_heaters if h[CONF_HEATER_ID] != node_id]
        new_options = dict(self.entry.options)
        new_options[CONF_HEATERS] = new_heaters
        self._persist_options_no_reload(new_options)

        self.heaters.pop(node_id, None)
        self.heater_names.pop(node_id, None)
        self.network.node_ids = tuple(nid for nid in self.network.node_ids if nid != node_id)
        self.network.forget_program(node_id)
        self._association_values.pop(node_id, None)
        self._removed_heaters[node_id] = time.monotonic() + REMOVED_HEATER_QUARANTINE_S
        self.network.reserved_node_ids.add(node_id)

    def _register_gateway_device(self) -> None:
        """Register the gateway device up front so heater devices can link
        `via_device_id` to it (docs/91-p4-parity-plan.md "Naming scheme": one
        gateway device, identifiers (DOMAIN, dev_id))."""
        device_registry = dr.async_get(self.hass)
        gateway_device = device_registry.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(DOMAIN, self.dev_id)},
            name=GATEWAY_DEVICE_NAME,
            manufacturer="ATC",
            model=GATEWAY_MODEL,
        )
        self.gateway_device_id = gateway_device.id

    def _open_nanocul(self) -> NanoCul:
        return NanoCul(url=self._serial_url, source_id=self._station_id)

    async def _connect_nanocul(self) -> None:
        """Open (or reopen) the NanoCul, bind it to Network, and log the
        connect/reconnect Q status line at INFO (task 1). Shared by
        async_setup and _async_reconnect so both go through the exact same
        connect sequence and logging, and so a reconnect's own "reopen" step
        is not a second, subtly different code path from the first connect."""
        try:
            self._nanocul = await asyncio.wait_for(
                self.hass.async_add_executor_job(self._open_nanocul),
                timeout=CONNECT_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            _LOGGER.error(
                "opening nanoCUL %s did not return within %.0fs", self._serial_url, CONNECT_TIMEOUT_S
            )
            raise
        self.network.bind_nanocul(self._nanocul)
        self._port_open = True
        self._connected_at = time.time()
        self._frames_since_connect = 0
        _LOGGER.info("nanoCUL connected: %s", self._serial_url)
        await self._configure_stick()
        await self._log_stick_status()
        await self._drain_reset_window()

    def _configure_stick_sync(self) -> None:
        """Set the stick's own station id and turn its hardware auto-ack on
        (2026-09-09 fix, read-only ser2net tap postmortem): this integration
        never sent `I<hex>`/`A1` anywhere -- grepping coordinator.py and
        config_flow.py for them turns up nothing, and NanoCul's own
        constructor only sleeps out the reset and clears the local input
        buffer, never touches stick state. Firmware defaults auto-ack OFF
        (firmware/termoweb_rx/main.c top comment: "'A0'/'A1' -> auto-ack
        off/on (default off)"), and deploy/ser2net.yaml's `local` connector
        flag means a TCP reconnect never toggles DTR, so the stick is never
        actually reset by anything this integration does -- whatever
        auto_ack/our_id it booted or was last left with (by a bench tool, or
        firmware's own RAM default after a real power cycle) persists across
        every reconnect. Without auto-ack, a heater's report gets no
        link-layer ack at all and is retransmitted up to 3 times, about
        160-190 ms apart, byte-identical (PROTOCOL.md section 5.5 "Frames
        that receive no ack are retransmitted 3 times", section 3 "no
        sequence counter or nonce anywhere in the frame") -- exactly the
        fast, byte-identical, different-RSSI duplicates a read-only tap on
        the ser2net stream showed live (two full receptions, not one frame
        parsed twice). `I<hex>` before `A1` so auto-ack's own destination
        check (firmware main.c: `dst == our_id`) matches this station's real
        id from the moment it turns on; both are plain state sets, not
        toggles, so sending them again on every reconnect is harmless
        whatever state the stick was already in."""
        assert self._nanocul is not None
        self._nanocul.send_raw_command(f"I{self._station_id:02X}")
        self._nanocul.send_raw_command("A1")

    async def _configure_stick(self) -> None:
        """Executor wrapper for _configure_stick_sync, run right after every
        connect and reconnect, before the Q status query (task 1's own log
        line) so that query's own `autoack=`/`id=` fields reflect what this
        method just set rather than whatever the stick happened to boot
        with. Goes through _run_locked like every other executor call that
        touches the port, so no command path can have a second thread on the
        brand-new transport while this handshake runs (2026-09-09 fix, the
        same corruption the post-reconnect Q banner showed). Best-effort: the
        timeout _run_locked logs is swallowed here rather than raised, since
        a stick that cannot take two more command lines right after a
        successful open will already surface as a failure somewhere else (the
        Q query right after this, or the first scan)."""
        try:
            await self._run_locked(
                self._configure_stick_sync,
                timeout=COMMAND_TIMEOUT_S,
                description="setting the nanoCUL station id and auto-ack",
            )
        except asyncio.TimeoutError:
            pass

    async def _log_stick_status(self) -> None:
        """Send Q and log whatever the stick sends back (task 1: "connect and
        reconnect with the banner and Q line"), through the same
        _log_stick_event path the reader loop itself uses -- so a boot
        banner line or Q's own status line counts toward, and is logged
        under, the same first-FRAME_LOG_INFO_COUNT INFO budget as everything
        else the stick sends after this connect. Also sets _q_answered (task
        2), so async_setup's own first-connection scan retry can tell "the
        link is alive but nothing answered the scan" apart from "the link
        itself never came up". Reads the reply under _io_lock (_run_locked)
        for the same reason _configure_stick does: this query is the one
        handshake step that consumes bytes off the wire, so nothing else may
        be reading the transport while it waits for its own line."""
        def _query() -> list[object]:
            events: list[object] = []
            self._nanocul.send_raw_command("Q")
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and not events:
                events.extend(self._nanocul.read_events())
            return events

        try:
            events = await self._run_locked(
                _query, timeout=COMMAND_TIMEOUT_S, description="nanoCUL Q status query"
            )
        except asyncio.TimeoutError:
            self._q_answered = False
            return
        self._q_answered = bool(events)
        if not events:
            _LOGGER.info("nanoCUL status: no reply to Q")
            return
        for event in events:
            self._log_stick_event(event)

    async def _drain_reset_window(self) -> None:
        """First-connection cleanup (2026-09-06 postmortem, task 2): right
        after the connect and the Q line, before the clock sync and the scan,
        actively read and discard whatever the stick already printed during
        its own reset -- the boot banner and any stray RX bytes from the 2.5 s
        reset window (termoweb_local.nanocul.RESET_WAIT_S) -- since NanoCul's
        own reset_input_buffer() call at construction time only clears a real
        pyserial port's local buffer, a documented no-op for a socket:// URL
        (this integration's typical serial_url, e.g. ser2net): whatever the
        stick already sent during its reset otherwise sits unread in the
        socket, ready to desync the very first scan's own on-air replies.

        By the time this runs, NanoCul's own constructor has already slept
        the full reset_wait, so anything from that window has already fully
        arrived; a plain "read until genuinely empty" loop is enough, no need
        to keep waiting for more to trickle in. Bounded by RESET_WAIT_S total
        purely as a safety net against a pathological transport that never
        reports empty."""
        deadline = time.monotonic() + RESET_WAIT_S
        discarded = 0
        while time.monotonic() < deadline:
            try:
                events = await self._run_locked(
                    self._nanocul.read_events, timeout=READ_TIMEOUT_S, description="startup drain"
                )
            except asyncio.TimeoutError:
                break
            if not events:
                break
            for event in events:
                self._log_stick_event(event)
            discarded += len(events)
        if discarded:
            _LOGGER.info("startup drain: discarded %d line(s) from the reset window", discarded)

    def _log_stick_event(self, event: object) -> None:
        """Log one event from the stick (task 1, plus the 2026-09-06 peer
        review of the live stall): a decoded summary for a received frame,
        or -- verbatim, whatever the counter's own level is -- any line
        starting with `#` (the boot banner, the Q status line, and firmware
        3.4's own `# uart overrun`/`# autoack` lines). `# uart overrun` is
        the one line that tells a lost byte on the wire apart from a blocked
        host, so it must never be silently dropped to DEBUG-only logging a
        caller cannot see; every other non-frame line (TX confirmations,
        TXERR, ...) stays at DEBUG, already covered by this integration's own
        logging of every confirmation/command it sends. Counts every event
        (frame or raw line) toward the first FRAME_LOG_INFO_COUNT budget.

        rssi and lqi are the radio's own measurement of the receive event a
        frame was recovered from, carried through from the stick's RX line by
        NanoCul (nanocul.parse_rx_line, Piece). They are logged because
        per-link signal margin is the leading explanation for the P6 run's
        missed report windows, two heaters missing 20 percent of their 300 s
        windows and a third 10 percent, with the silences independent across
        heaters, no time-of-day pattern and CRC-bad frames uncorrelated with
        them. Production had never recorded either field, so a whole 24 h run
        could not weigh that explanation without a parallel radio tap;
        historical tap logs put node 04 at a median -44 dBm, node 03 at -56.5
        and node 02 at -68, a 24 dB spread across one installation.

        Two short fields in this line's own key=value style, and nothing
        richer: it is emitted once per frame and the acceptance capture reads
        a rolling `ha core logs` buffer, so every character costs history.
        `rssi=-44.0 lqi=60` is about seventeen characters against a journald
        record of roughly two hundred, a tenth more per line, and it adds no
        lines at all. rssi keeps the stick's own single decimal rather than
        being rounded, since the per-node medians that matter here are half a
        dB apart. A raw line (a banner, a Q status line) never reaches this
        branch, and a Piece carrying no measurement prints `--`, the
        placeholder this line already uses for an absent src or dst."""
        self._frames_since_connect += 1
        level = logging.INFO if self._frames_since_connect <= FRAME_LOG_INFO_COUNT else logging.DEBUG

        if isinstance(event, Piece):
            if event.verdict == "ok" and event.cls is not None:
                try:
                    parsed = tf.parse_frame(bytes(int(b, 16) for b in event.bytes))
                    payload_hex = parsed.payload.hex()
                except Exception:  # noqa: BLE001 - logging must never break the reader loop
                    payload_hex = "".join(event.bytes[:16])
            else:
                payload_hex = "".join(event.bytes[:16])
            _LOGGER.log(
                level,
                "rx #%d: class=%02x src=%s dst=%s rssi=%s lqi=%s verdict=%s payload=%s",
                self._frames_since_connect,
                event.cls if event.cls is not None else 0,
                f"{event.src:02x}" if event.src is not None else "--",
                f"{event.dst:02x}" if event.dst is not None else "--",
                f"{event.rssi_dbm:.1f}" if event.rssi_dbm is not None else "--",
                event.lqi if event.lqi is not None else "--",
                event.verdict,
                payload_hex,
            )
            return

        text = getattr(event, "text", None)
        if text is None:
            return
        if text.startswith("#"):
            _LOGGER.log(level, "stick: %s", text)
        else:
            _LOGGER.debug("stick: %s", text)

    async def _watchdog_loop(self) -> None:
        """Periodic stall check (task 3), independent of _io_lock in both
        directions: it never takes the lock to decide (last_frame_at,
        last_command_at, and whether the lock is currently held are all read,
        never awaited), and its own reconnect action (_async_reconnect) never
        waits for the lock either -- see that method's own docstring for why
        that matters specifically when the lock is the thing stuck."""
        while not self._stop_reader.is_set():
            try:
                await asyncio.wait_for(self._stop_reader.wait(), timeout=WATCHDOG_INTERVAL_S)
                return  # _stop_reader was set: shutting down
            except asyncio.TimeoutError:
                pass
            try:
                await self._check_stall()
            except Exception:  # noqa: BLE001 - the watchdog itself must never die
                _LOGGER.exception("watchdog stall check failed")

    def _note_command_outcome(self, ok: bool) -> None:
        """Record whether the most recent "send a command and expect a
        reply" attempt got through (task 1): the watchdog's own stall check
        uses this, not just elapsed time, to tell a link that has actually
        died apart from one that is merely quiet because its heaters are
        genuinely idle (docs/PROTOCOL.md section 6: up to 300 s between
        reports is normal, not a fault)."""
        self._last_command_ok = ok

    async def _check_stall(self) -> None:
        """Watchdog stall check (task 1, thresholds revised 2026-09-06 after
        the live postmortem): which threshold applies depends on whether a
        heater is known, not on how long ago this coordinator connected --
        see WATCHDOG_STALL_S/WATCHDOG_STARTUP_STALL_S's own module-level
        docstring for why."""
        if not self.heaters:
            await self._check_stall_no_heaters()
            return

        now = time.time()
        last_frame_at = self.gateway_last_frame_at()
        reference = last_frame_at if last_frame_at is not None else self._connected_at
        if reference is None:
            return
        stalled_for = now - reference
        if stalled_for <= WATCHDOG_STALL_S:
            return

        if self._last_command_ok is False:
            command_elapsed = now - self._last_command_at if self._last_command_at is not None else None
            await self._watchdog_reconnect(
                "watchdog stall: no frame in %.0fs (threshold %.0fs), last command "
                "attempt failed %.0fs ago" % (
                    stalled_for, WATCHDOG_STALL_S, command_elapsed if command_elapsed is not None else -1,
                )
            )
        else:
            _LOGGER.info(
                "quiet link, heaters idle (no frame in %.0fs, threshold %.0fs, last "
                "command attempt ok)",
                stalled_for, WATCHDOG_STALL_S,
            )

    async def _check_stall_no_heaters(self) -> None:
        """No heater known yet (task 1): the shorter WATCHDOG_STARTUP_STALL_S
        threshold applies, measured from connect since there is no report to
        measure from, and the action is a rescan first -- a reconnect is not
        yet warranted just because nothing has answered, since the scan
        itself is what is supposed to find a heater. Only if that rescan
        also finds nothing (every one of its own probes having itself failed
        to get an ack, satisfying the same "last command attempt failed"
        condition _check_stall's own heaters-known branch checks explicitly)
        does this reconnect."""
        if self._connected_at is None:
            return
        now = time.time()
        stalled_for = now - self._connected_at
        if stalled_for <= WATCHDOG_STARTUP_STALL_S:
            return
        _LOGGER.warning(
            "watchdog: no heater known %.0fs after connect (threshold %.0fs); rescanning",
            stalled_for, WATCHDOG_STARTUP_STALL_S,
        )
        await self.async_scan_for_heaters()
        if self.heaters:
            return  # the rescan found at least one heater; link is fine
        await self._watchdog_reconnect(
            "watchdog: still no heater known %.0fs after connect, "
            "rescan also found none" % stalled_for
        )

    async def _watchdog_reconnect(self, reason: str) -> None:
        """Cap watchdog-triggered reconnects to one per
        WATCHDOG_RECONNECT_COOLDOWN_S (task 1): a stall that outlives one
        reconnect attempt would otherwise refire every WATCHDOG_INTERVAL_S
        forever. Logged here (not just inside _async_reconnect) so the
        cooldown itself, and the reason with its own elapsed times, are both
        visible even when the reconnect is suppressed."""
        now = time.time()
        if (
            self._last_watchdog_reconnect_at is not None
            and now - self._last_watchdog_reconnect_at < WATCHDOG_RECONNECT_COOLDOWN_S
        ):
            _LOGGER.debug(
                "watchdog reconnect suppressed (%s): last one was %.0fs ago, cooldown %.0fs",
                reason, now - self._last_watchdog_reconnect_at, WATCHDOG_RECONNECT_COOLDOWN_S,
            )
            return
        self._last_watchdog_reconnect_at = now
        _LOGGER.warning("%s -- reconnecting", reason)
        await self._async_reconnect(reason)

    async def _async_reconnect(self, reason: str) -> None:
        """Close the current NanoCul and open a new one, then redo the clock
        sync and discovery scan -- the same sequence async_setup runs once,
        minus each already-configured heater's own startup_sequence burst (a
        stall is not a fresh power-up: a heater that is still there does not
        need re-registering, and rerunning its EB association/F3 query burst
        would only make the reconnect slower for no benefit).

        Deliberately never waits for _io_lock (2026-09-06 peer review of the
        live stall): a wedged executor call -- e.g. a pyserial socket:// read
        that never returns -- can hold that lock forever, and this is exactly
        the case the watchdog exists to recover from, so it cannot itself
        depend on the lock ever being released. Closing the old NanoCul out
        from under a stuck call is what actually unblocks it: closing the
        transport makes its own blocked read raise, and every caller that
        could be holding the lock at that moment (the reader loop, or a
        command's own _run_locked) already treats any exception from its
        executor call as something to log and recover from, not crash on --
        so this method does not need to wait for that caller to notice
        before it reopens; that caller's own `async with self._io_lock`
        releases it as its exception propagates, whenever that turns out to
        be.

        The reader loop is parked for the whole of this (_reconnecting, which
        _async_wait_before_read waits on), so between the close below and the
        end of _connect_nanocul's own I/A1/Q handshake this is the only
        consumer of the transport; a reopen that fails hands the reader a
        backoff instead, rather than the immediate retry that made a bridge
        outage cost hundreds of attempts a minute (RECONNECT_BACKOFF_MIN_S)."""
        if self._reconnecting:
            _LOGGER.debug("reconnect already in progress (%s); not stacking another", reason)
            return
        if self._stop_reader.is_set():
            return
        backoff_left = self._reconnect_retry_at - time.monotonic()
        if backoff_left > 0:
            _LOGGER.debug(
                "reconnect requested (%s) with %.0fs of reopen backoff left; skipping",
                reason, backoff_left,
            )
            return
        if self._connected_at is not None and time.time() - self._connected_at < RECONNECT_DEBOUNCE_S:
            # Hold the reader off the transport for the rest of the debounce
            # window too, so a link that dies again immediately after a
            # successful reopen cannot spin this same skip at read speed.
            self._reconnect_retry_at = time.monotonic() + (
                RECONNECT_DEBOUNCE_S - (time.time() - self._connected_at)
            )
            _LOGGER.debug(
                "reconnect requested (%s) within %.0fs of the last one; skipping",
                reason, RECONNECT_DEBOUNCE_S,
            )
            return
        self._reconnecting = True
        try:
            self.reconnect_count += 1
            _LOGGER.warning("reconnecting nanoCUL (%s); reconnect #%d", reason, self.reconnect_count)
            self._port_open = False
            old_nanocul = self._nanocul
            if old_nanocul is not None:
                try:
                    await asyncio.wait_for(
                        self.hass.async_add_executor_job(old_nanocul.close),
                        timeout=COMMAND_TIMEOUT_S,
                    )
                except Exception:  # noqa: BLE001 - a stuck call's own read raising on close must not abort the reconnect
                    _LOGGER.exception("error closing the old nanoCUL connection during reconnect")
            try:
                await self._connect_nanocul()
            except Exception:
                self._reconnect_backoff_s = min(
                    max(self._reconnect_backoff_s * 2.0, RECONNECT_BACKOFF_MIN_S),
                    RECONNECT_BACKOFF_MAX_S,
                )
                self._reconnect_retry_at = time.monotonic() + self._reconnect_backoff_s
                _LOGGER.exception(
                    "failed to reopen nanoCUL during reconnect; next attempt in %.0fs",
                    self._reconnect_backoff_s,
                )
                return
            self._reconnect_backoff_s = 0.0
            self._reconnect_retry_at = 0.0
            try:
                await self._async_sync_clock_all()
            except Exception:
                _LOGGER.exception("clock sync after reconnect failed")
            try:
                await self.async_scan_for_heaters()
            except Exception:
                _LOGGER.exception("scan after reconnect failed")
        finally:
            self._reconnecting = False

    async def _run_locked(self, func: Callable[..., Any], *args: Any, timeout: float, description: str) -> Any:
        """Run `func(*args)` in the executor under `_io_lock`, bounded by
        `timeout` (task 2). On expiry: log an ERROR naming `description` and
        re-raise asyncio.TimeoutError -- releasing the lock as the exception
        propagates out of the `async with` below, before the caller's own
        except block ever runs, so a caller that reacts to this by calling
        _async_reconnect never has to wait for it. The executor thread itself
        keeps running against the old transport; see _async_reconnect's own
        docstring for why that is fine."""
        async with self._io_lock:
            self._last_command_at = time.time()
            try:
                return await asyncio.wait_for(
                    self.hass.async_add_executor_job(func, *args), timeout=timeout
                )
            except asyncio.TimeoutError:
                _LOGGER.error(
                    "%s: no response within %.0fs; the executor thread may still be "
                    "blocked on the old transport",
                    description, timeout,
                )
                raise

    async def _async_wait_before_read(self) -> bool:
        """Hold the reader off the transport until it may read again; False
        means the coordinator is shutting down and the loop should end.

        Two things make reading regardless harmful (2026-09-09 live
        postmortem). A reconnect owns the transport from the moment it closes
        the old one until the new one's handshake is done, so a read taken in
        that window is either a second consumer racing _connect_nanocul for
        the Q reply's own bytes (the post-reconnect banner arriving with
        characters missing, 2026-09-06) or an instant raise against the
        transport that reconnect just closed. And a reopen that failed leaves
        the old, closed NanoCul bound, so the next read raises immediately and
        asks for yet another reconnect: unpaced, that is the 600-a-minute loop
        the 2026-09-09 outage logged, and _reconnect_retry_at is what spaces
        it out instead.
        """
        while not self._stop_reader.is_set():
            if self._reconnecting:
                await self._reconnect_idle.wait()
                continue
            delay = self._reconnect_retry_at - time.monotonic()
            if delay <= 0:
                return True
            try:
                await asyncio.wait_for(self._stop_reader.wait(), timeout=delay)
            except asyncio.TimeoutError:
                continue
            return False  # _stop_reader was set: shutting down
        return False

    async def _reader_loop(self) -> None:
        """Background frame reader.

        Ordering rule (task 4): a pending command (a confirmation send, a
        startup_sequence step, a scan probe, ...) and this loop's own
        read_events() call must never be able to wait on each other. This
        holds because every command path (_send_frame, _async_confirm_report,
        _async_run_startup_sequence, async_scan_for_heaters, ...) goes
        through _run_locked, which holds _io_lock only for its own single
        send-then-wait-for-reply executor call, bounded by its own timeout;
        and this loop only ever holds _io_lock for one single read_events()
        call at a time (never across the whole while loop), also bounded by
        READ_TIMEOUT_S. So a command in flight simply delays the next
        read_events() call until it releases the lock (at most its own
        timeout later), and a read in flight simply delays the next
        command's own send the same way -- neither side is ever left
        waiting for something that is itself waiting on the other.
        tests/integration/test_coordinator.py's
        test_report_arrives_while_startup_sequence_is_running exercises this
        directly: a report queued for a different heater while a
        startup_sequence holds the lock is not lost, just delayed until the
        lock frees up.

        Every pass starts at _async_wait_before_read, which is what keeps this
        loop off the transport entirely while a reconnect owns it and while a
        failed reopen is backing off.
        """
        assert self._nanocul is not None
        while not self._stop_reader.is_set():
            if not await self._async_wait_before_read():
                return
            try:
                events = await self._run_locked(
                    self._nanocul.read_events, timeout=READ_TIMEOUT_S, description="reader read_events"
                )
            except asyncio.TimeoutError:
                await self._async_reconnect("reader read_events timed out")
                continue
            except serial.SerialException as err:
                # A reconnect in progress closes the old transport out from
                # under this in-flight read on purpose (_async_reconnect's own
                # docstring): that raises here as a serial exception (e.g.
                # PortNotOpenError), which is the expected, clean way that
                # read unblocks, not a fault -- log one INFO line and park on
                # _async_wait_before_read until the reconnect has finished with
                # the transport, instead of a full traceback for every single
                # reconnect. Outside of a reconnect already in
                # progress, the same exception means the port itself just
                # died (unplugged, ser2net dropped the socket, ...): still no
                # traceback (the caller does not need this client's own stack,
                # just what happened), but this is new information the
                # watchdog has not already reacted to, so it reconnects too.
                if self._reconnecting:
                    _LOGGER.info("reader stopped for reconnect")
                else:
                    _LOGGER.error("nanoCUL read failed for %s: %s", self._serial_url, err)
                    await self._async_reconnect(f"reader read failed: {err}")
                continue
            except Exception:  # noqa: BLE001 - keep the reader alive across I/O hiccups
                _LOGGER.exception("nanoCUL read failed for %s", self._serial_url)
                await asyncio.sleep(1.0)
                continue
            for event in events:
                self._log_stick_event(event)
                try:
                    await self._handle_event(event)
                except Exception:  # noqa: BLE001 - one bad event must not kill the reader loop or the registration path behind it
                    _LOGGER.exception("error handling event %r", event)

    async def _send_frame(self, node_id: int, air: bytes) -> Any:
        try:
            result = await self._run_locked(
                self._nanocul.send_frame, node_id, air,
                timeout=COMMAND_TIMEOUT_S, description=f"send_frame to node {node_id:02x}",
            )
        except asyncio.TimeoutError:
            await self._async_reconnect(f"send_frame to node {node_id:02x} timed out")
            self._note_command_outcome(False)
            return AckResult(ok=False, attempts=0)
        self._note_command_outcome(result.ok)
        return result

    async def _handle_event(self, event: object) -> None:
        if not isinstance(event, Piece):
            return
        if event.verdict != "ok":
            return

        if event.cls == E7_FRAME_CLASS:
            # A not-yet-assigned heater's own identity announcement
            # (2026-09-06 17:12:17Z pairing capture, PROTOCOL.md 5.9). src is
            # the broadcast id `FF` when the sweep reaches this station
            # directly and an already-paired heater's own id when that heater
            # relays it, so neither src nor dst is checked here or in
            # Network.handle_discovery_frame; the class and the identity
            # marker are what identify it. Checked before the self.heaters
            # lookup below, since a relayed copy does come from a configured
            # heater.
            await self._async_handle_discovery_frame(event)
            return

        parsed = tf.parse_frame(bytes(int(b, 16) for b in event.bytes))

        is_new_heater = False
        if event.src not in self.heaters:
            if not (MIN_PAIRED_HEATER_ID <= event.src <= MAX_PAIRED_HEATER_ID):
                return
            if self._is_quarantined(event.src):
                _LOGGER.info(
                    "node %02x reported after its device was deleted; not re-registering "
                    "until the deletion has held for %.0fs",
                    event.src, REMOVED_HEATER_QUARANTINE_S,
                )
                return
            is_registration_open = (
                event.cls == F3_FRAME_CLASS and is_registration_open_payload(parsed.payload)
            )
            if not (is_registration_open or is_report_payload(parsed.payload)):
                return
            # A CRC-valid report or registration-opening from an id this
            # coordinator has never seen (owner direction 2026-09-06 task
            # 2): register it the same way the scan does, entities included,
            # with no restart. Bookkeeping only touches in-memory tables,
            # persisted options and entity creation -- no radio I/O -- so it
            # never delays the time-bound replies below.
            self._register_heater_bookkeeping(event.src)
            is_new_heater = True

        heater = self.heaters[event.src]

        if event.cls == F3_FRAME_CLASS and is_registration_open_payload(parsed.payload):
            # A heater's own opening frame at power-up (PROTOCOL.md 5.6;
            # 2026-09-06 17:08:20Z re-registration capture, notes.md): the
            # required station reply is an EB clock sync with the
            # registration prefix, not the gateway's own F3 query burst --
            # that burst only runs once, at station start (async_setup /
            # _async_run_startup_sequence), and did not repeat in this
            # re-registration capture. Sent before the new heater's own
            # startup_sequence below, which is not time-bound the way this
            # reply is.
            await self._async_register_heater(event.src)
            if is_new_heater:
                await self._async_run_startup_sequence(event.src)
            return

        if is_program_reply_payload(parsed.payload):
            # A 9F or C9 program frame that reached the reader loop instead of
            # a read_program() reply window (C9 is heaters 02 and 03's own F3
            # B0 reply class, PROTOCOL.md 5.6): cache it like any read-back.
            # decode_program_reply() records it (at its own native
            # resolution) in Network.last_program itself; get_prog() reads
            # that record straight back, so there is nothing further to do
            # with its return value here. Neither class carries the `56`
            # report marker, and the gateway is never seen confirming one, so
            # it skips the confirmation below.
            self.network.decode_program_reply(parsed)
            return

        if is_energy_reply_payload(parsed.payload):
            # An EF energy reply that reached the reader loop instead of a
            # read_energy() reply window. No EF in the capture corpus is ever
            # pushed unsolicited -- all 12 answer an F3 BC within 58 ms -- but one
            # arriving here is still this heater's own meter, so it is recorded
            # rather than dropped as junk. Its payload starts `BD`, not the `56`
            # report marker, so it takes no F2 57 55 confirmation and returns
            # before the one below.
            energy_wh = decode_energy_wh(parsed.payload)
            if energy_wh is not None:
                heater.record_energy(energy_wh)
                # async_update_listeners, not async_set_updated_data: the energy
                # sensors read the Heater holders directly and only need waking,
                # and an EF is not a refresh -- rescheduling the poll cadence
                # around one would let stray frames drag the whole cycle.
                self.async_update_listeners()
            return

        if is_power_request_payload(parsed.payload):
            # F1 BE <power hi> <power lo>: the heater's own request to the
            # gateway's power manager, sent whenever its element switches on
            # (PROTOCOL.md 5.1). Left unanswered, heater 04's panel raised its
            # lost-gateway LINK indication about 12 minutes after its last EB
            # (docs/captures/2026-09-13-x19/link-test.md); the corpus's own
            # gateway always grants it and never waits for a reply to its own
            # reply, so this replies F2 BF 01 and returns without awaiting
            # anything back. `BE` is not the `56` report marker, so it takes
            # no F2 57 55 confirmation either.
            requested_w = ((parsed.payload[1] << 8) | parsed.payload[2]) / 10.0
            _LOGGER.debug(
                "power request (BE) from node %02x: %.1f W, replying BF 01",
                event.src, requested_w,
            )
            air = self.network.power_verdict(event.src, granted=True)
            await self._send_frame(event.src, air)
            return

        if not is_report_payload(parsed.payload):
            if (
                len(parsed.payload) == E4_PAYLOAD_LEN
                and parsed.payload[0] == (network_module.STATUS_OPCODE + 1) & 0xFF
            ):
                # An E4 never carries the `56` report marker (it is E6's own
                # shape, PROTOCOL.md 5.10-style, so is_report_payload() above
                # cannot see it), but one arriving unsolicited here rather than
                # inside request_status()'s own reply window is still a status
                # report, the same way an unsolicited E3 is
                # (docs/captures/2026-09-12-schedule/notes.md).
                snapshot = HeaterSnapshot.from_e6(parsed)
                heater.record_snapshot(snapshot)
                self._update_link_health()
                self.data = self.heaters
                self.last_update_success = True
                _LOGGER.debug(
                    "status report (E4) from node %02x updated coordinator data",
                    event.src,
                )
                self.async_update_listeners()
                await self._async_confirm_report(event.src)
            return

        if len(parsed.payload) in (E5_PAYLOAD_LEN, E3_PAYLOAD_LEN):
            snapshot = self.network.decode_report(parsed)
            heater.record_snapshot(snapshot)
            self._update_link_health()
            # The same rule as the EF branch above, and for the same reason: a
            # heater's own unsolicited report is not a refresh, and
            # rescheduling the poll cadence around one lets stray frames drag
            # the whole cycle. DataUpdateCoordinator.async_set_updated_data
            # does exactly that -- "manually update data, notify listeners and
            # reset refresh interval", and in HA 2026.9.1 it really does
            # _async_unsub_refresh() then _schedule_refresh() for a fresh,
            # whole update_interval rather than the remainder. With the poll
            # interval at DEFAULT_POLL_INTERVAL_S (300 s) and the heaters'
            # documented idle report period also 300 s (PROTOCOL.md section
            # 6), a single reporting heater reset that timer before it could
            # ever expire and three gave a median gap of about 50 s: over
            # 17.16 h of the acceptance capture the scheduled refresh ran 9
            # times against 205 due, which starved the hourly energy sweep,
            # the per-heater F3 B8 status refresh and the daily clock sync
            # alike (docs/captures/2026-09-10-energy/notes.md;
            # docs/captures/2026-09-10-cadence/notes.md conclusion C5 reached
            # the same mechanism independently).
            #
            # Everything else that call does is done here: data, the success
            # flag an entity's availability reads, and the listener
            # notification. Only the timer reset and its debouncer cancel are
            # dropped, which is the whole point.
            self.data = self.heaters
            self.last_update_success = True
            _LOGGER.debug("report from node %02x updated coordinator data", event.src)
            self.async_update_listeners()
        elif parsed.payload[:2] == PROGRAM_REPORT_MARKER:
            # The 9E program report pushed as part of a registration reply
            # (2026-09-06 17:08:20Z capture): store it exactly like a fresh
            # F3 B0 read-back, so `prog` reflects it without waiting for the
            # next set_schedule or coordinator restart. decode_program_report()
            # records it in Network.last_program itself, at the 9E frame's own
            # native (half-hourly) resolution.
            self.network.decode_program_report(parsed)
        # E2, EA, and any other `56`-prefixed payload: still undecoded
        # (PROTOCOL.md section 9), just confirmed below like every report.

        # F2 57 55: required after every report a heater sends (2026-09-06
        # proof, notes.md 13:18:42Z and f3-and-edges-results.md section 1;
        # also the 2026-09-06 17:08:20Z capture, for EA and the 9E program
        # report specifically), not a poll -- the real gateway sends it right
        # after each report it receives, and this coordinator must do the
        # same, regardless of the report's own on-air class.
        await self._async_confirm_report(event.src)
        if is_new_heater:
            await self._async_run_startup_sequence(event.src)

    async def _async_register_heater(self, node_id: int) -> None:
        """Registration reply required from a station (2026-09-06 17:08:20Z
        capture, notes.md): an EB clock sync with the registration prefix,
        sent right after the heater's own opening F3 frame (already acked by
        the nanoCUL's own hardware auto-ack by the time this runs)."""
        when = dt_util.now()
        air = self.network.sync_clock(node_id, when, registering=True)
        result = await self._send_frame(node_id, air)
        self._record_ack_result(node_id, result)

    def _expire_quarantines(self) -> None:
        """Release every deleted heater's id whose quarantine has run out, in
        network.reserved_node_ids as well, so a quarantine never outlives its
        own deadline just because that heater never reported again."""
        now = time.monotonic()
        for node_id, until in list(self._removed_heaters.items()):
            if now >= until:
                del self._removed_heaters[node_id]
                self.network.reserved_node_ids.discard(node_id)

    def _is_quarantined(self, node_id: int) -> bool:
        """Whether this id's deletion is still unconfirmed
        (REMOVED_HEATER_QUARANTINE_S)."""
        self._expire_quarantines()
        return node_id in self._removed_heaters

    def _learn_identity(self, node_id: int, identity: bytes) -> None:
        """Record one heater's 12-byte identity (from its E0 identity reply,
        Network.identity_from_identity_reply) in the live table and in
        persisted options, so an E7 announcement from that heater is matched
        to the id it already holds instead of being handed a second one."""
        if self.network.known_identities.get(identity) == node_id:
            return
        self.network.known_identities[identity] = node_id
        current_identity_hex = self.entry.options.get(CONF_HEATER_IDENTITIES, {})
        self._persist_options_no_reload(
            {
                **self.entry.options,
                CONF_HEATER_IDENTITIES: {
                    **_prune_other_ids_for_identity(current_identity_hex, identity.hex(), node_id),
                    str(node_id): identity.hex(),
                },
            }
        )
        _LOGGER.info("learned identity %s for node %02x", identity.hex(), node_id)

    async def _async_learn_missing_identities(self) -> None:
        """F3 5A against every configured heater whose identity is not
        recorded yet (Network.unidentified_node_ids). Identity is otherwise
        only ever learned from an E7 announcement, so a heater the scan or
        its own report registered has none -- which is what let the
        2026-09-09 attempt assign heater 04's identity a second id
        (PROTOCOL.md 5.9)."""
        for node_id in self.network.unidentified_node_ids():
            try:
                identity = await self._run_locked(
                    self.network.read_identity, node_id,
                    timeout=COMMAND_TIMEOUT_S,
                    description=f"identity read for node {node_id:02x}",
                )
            except asyncio.TimeoutError:
                continue
            if identity is not None:
                self._learn_identity(node_id, identity)

    def start_pairing(self, window_s: float | None = None) -> None:
        """Open a pairing window (the "Pair heater" button, 2026-09-06
        17:12:17Z capture): a not-yet-configured heater's E7 identity
        announcement, seen by the reader loop while the window is open, gets
        assigned a node id and registered as a new heater. `window_s`
        defaults to this entry's configured CONF_PAIR_HEATER_SECONDS
        (self.pair_heater_seconds, itself defaulting to
        DEFAULT_DISCOVERY_WINDOW_S/DEFAULT_PAIR_HEATER_SECONDS's own 120 s).

        Reads any configured heater's still-unknown identity first, in the
        background: a heater that announces itself during the window can only
        be matched against the heaters already here if their identities are
        known, and the window is long enough that this costs the pairing
        itself nothing.

        The window opening is logged and notified (2026-09-09 live run: the
        owner pressed the button, nothing appeared in the UI or the log, and
        they reasonably read that as a broken button -- the press was in fact
        arming a 120 s window silently)."""
        window = self.pair_heater_seconds if window_s is None else window_s
        self.network.start_discovery(window)
        _LOGGER.info(
            "pairing: discovery window open for %.0fs -- put the heater into its own "
            "pairing mode now (see its manual)",
            window,
        )
        persistent_notification.async_create(
            self.hass,
            f"Pairing window open for {window:.0f} seconds. Put the heater into its "
            "own pairing mode now (see its manual). This notification is replaced "
            "when the window closes.",
            title="Termoweb Local: pairing window open",
            notification_id=self._pairing_notification_id,
        )
        self._pairing_window_paired = False
        self._discovery_headers_seen.clear()
        self._relay_reversal_warned = False
        self._schedule_pairing_window_close(window)
        self.async_update_listeners()
        self.entry.async_create_background_task(
            self.hass,
            self._async_learn_missing_identities(),
            name=f"{DOMAIN}_learn_identities",
        )

    @property
    def _pairing_notification_id(self) -> str:
        return f"{DOMAIN}_{self.entry.entry_id}_pairing_window"

    def _schedule_pairing_window_close(self, window_s: float) -> None:
        """Say in the log and in the UI that the window has closed, and
        whether anything paired: nothing else ever reads the deadline again
        once it has passed (discovery_active() is only consulted when a frame
        arrives), so without this the owner's log ends at "window open"
        either way."""
        if self._pairing_window_cancel is not None:
            self._pairing_window_cancel()

        async def _close(_now) -> None:
            self._pairing_window_cancel = None
            # Close the window here rather than leaving discovery_active()
            # to notice the deadline on its own: this callback is the only
            # thing that runs at the deadline, so it is also the only moment
            # the `pairing_active` attribute can be pushed out truthfully.
            self.network.stop_discovery()
            _LOGGER.info(
                "pairing: discovery window closed after %.0fs, %s",
                window_s,
                "a heater was paired" if self._pairing_window_paired else "nothing paired",
            )
            if self._pairing_window_paired:
                # _register_paired_heater's own notification names the id and
                # stands on its own; this one has nothing left to add.
                persistent_notification.async_dismiss(
                    self.hass, self._pairing_notification_id
                )
            else:
                persistent_notification.async_create(
                    self.hass,
                    f"The {window_s:.0f} second pairing window closed and no heater "
                    "announced itself. Put the heater into its own pairing mode first, "
                    "then press Pair heater while it is still announcing.",
                    title="Termoweb Local: pairing window closed",
                    notification_id=self._pairing_notification_id,
                )
            self.async_update_listeners()

        self._pairing_window_cancel = async_call_later(self.hass, window_s, _close)

    def _send_discovery_assignment_sync(
        self, assignment_dst: int, assignment_air: bytes
    ) -> AckResult:
        # send_frame's first argument is not part of the frame: the on-air
        # destination is header byte 4, which Network.handle_discovery_frame
        # already wrote into assignment_air. It is only the source id whose
        # ack this call waits for, so it has to be the node that receives this
        # hop -- `FF` for a direct announcement, where the heater acks while
        # still addressed as `FF` (PROTOCOL.md 5.9), and the relay for an
        # announcement that reached this station through one. Passing the
        # broadcast id for a relayed assignment would wait out the whole
        # retry interval for an ack from `FF` that the relay is sending under
        # its own id, and report a delivered frame as unacked.
        #
        # One attempt, no ack retries: the assignment is only worth anything
        # while the announcing heater is still on the destination that
        # produced it (about 200 ms, PROTOCOL.md 5.9), and the next
        # announcement is a better retry than resending into a destination it
        # has already left. No software ack either -- the stick's own
        # hardware auto-ack has covered every frame addressed to this station
        # since _configure_stick started sending `A1` on each connect, and an
        # announcement swept at some other destination is not this station's
        # to ack.
        return self._nanocul.send_frame(assignment_dst, assignment_air, retries=1)

    def _log_discovery_header(self, parsed: tf.Frame) -> None:
        """Log one E7 announcement's whole logical frame, whether or not this
        station goes on to answer it (analysis7.md section 11 item 1: what a
        heater puts in a relayed announcement's bytes 6 to 11 is the open
        question, and the `rx #N` line carries no header bytes at all, so a
        pairing run answers it with the copy it happens to reply to and
        nothing else).

        Bytes 6 to 11 are broken out alongside the raw frame because they are
        the answer being looked for: byte 6 the originator, 7 to 10 the hops,
        11 the tag. `relayed` is the same test the assignment route is
        decided on (network.is_relayed_announcement), so the log says which
        way this station read each copy, not only what arrived.

        Level is graded by evidential novelty rather than fixed, for the
        buffer reasons DISCOVERY_HEADER_LOG_LIMIT records: the first copy of
        each distinct header at INFO, every repeat of one already logged at
        DEBUG. A repeat carries no header the log does not already hold, and
        the `rx #N` line above still counts and times every frame."""
        logical = bytes(parsed.logical)
        path = logical[ROUTING_PATH_SLICE]
        header = logical[3:5] + logical[ROUTING_PATH_SLICE] + logical[11:12]
        novel = (
            header not in self._discovery_headers_seen
            and len(self._discovery_headers_seen) < DISCOVERY_HEADER_LOG_LIMIT
        )
        if novel:
            self._discovery_headers_seen.add(header)
        _LOGGER.log(
            logging.INFO if novel else logging.DEBUG,
            "pairing: e7 seen src=%s dst=%s relayed=%s originator=%s hops=%s "
            "tag=%s logical=%s",
            f"{parsed.src:02x}", f"{parsed.dst:02x}",
            is_relayed_announcement(parsed),
            f"{path[0]:02x}" if path else "--",
            path[1:].hex() if len(path) > 1 else "--",
            f"{logical[11]:02x}" if len(logical) > 11 else "--",
            logical.hex(),
        )

    async def _async_handle_discovery_frame(self, event: Piece) -> None:
        parsed = tf.parse_frame(bytes(int(b, 16) for b in event.bytes))
        self._log_discovery_header(parsed)
        self._expire_quarantines()
        result = self.network.handle_discovery_frame(parsed)
        if result is None:
            return
        if result.node_id is None:
            await self._async_defer_unidentified_pairing(result.unidentified_node_ids)
            return
        # Naming the destination in both the timeout and the result line is
        # what makes `acked` readable: on the broadcast route it is the
        # announcing heater itself acking, and through a relay it is only the
        # relay accepting the hop, which says nothing about whether the
        # announcer adopted the id. The relayed route is the firmware's rule
        # (analysis7.md section 6) and is not proven on air.
        if result.relay_reversal_failed:
            # Once per window: the sweep repeats the same relayed copy about
            # every 200 ms, and a warning per copy would bury the pairing
            # evidence it is meant to point at.
            if not self._relay_reversal_warned:
                self._relay_reversal_warned = True
                _LOGGER.warning(
                    "pairing: an announcement relayed by node %02x carries a routing "
                    "path with no route back to station id %02x, so its assignment "
                    "falls back to a broadcast the relay will not forward. With a "
                    "station id other than %02x this is every relayed announcement "
                    "(docs/PROTOCOL.md 5.9)",
                    parsed.src, self.network.station_id, GATEWAY_ID,
                )
            route = "broadcast, relay path not reversed"
        elif result.assignment_dst == DISCOVERY_BROADCAST_ID:
            route = "broadcast"
        else:
            route = "relayed"
        description = f"discovery assignment to {result.assignment_dst:02x}"
        try:
            ack = await self._run_locked(
                self._send_discovery_assignment_sync,
                result.assignment_dst, result.assignment_air,
                timeout=COMMAND_TIMEOUT_S, description=description,
            )
        except asyncio.TimeoutError:
            await self._async_reconnect(f"{description} timed out")
            return
        _LOGGER.info(
            "pairing: assigned node id %02x to identity %s, addressed to %02x (%s), "
            "acked=%s, announcement=%s, assignment=%s",
            result.node_id, result.identity.hex(), result.assignment_dst, route,
            ack.ok, parsed.logical.hex(), result.assignment_air.hex(),
        )
        self._pairing_window_paired = True
        self._removed_heaters.pop(result.node_id, None)
        self.network.reserved_node_ids.discard(result.node_id)
        if result.is_new or result.node_id not in self.heaters:
            self._register_paired_heater(
                result.node_id,
                result.identity,
                relay=(
                    None
                    if result.assignment_dst == DISCOVERY_BROADCAST_ID
                    else result.assignment_dst
                ),
            )

    async def _async_defer_unidentified_pairing(self, unidentified: tuple[int, ...]) -> None:
        """An announcement from an identity this station has never seen,
        while some configured heater's own identity is still unknown: the
        announcing heater could be that one, and giving it a second id is
        what the 2026-09-09 attempt did before the heater refused it
        (PROTOCOL.md 5.9). Learn the missing identities instead; the heater
        keeps announcing for as long as its own pairing mode is open, so the
        next announcement resolves either way."""
        _LOGGER.info(
            "pairing: unknown identity while node(s) %s have no identity recorded; "
            "reading their identity before assigning an id",
            ", ".join(f"{node_id:02x}" for node_id in unidentified),
        )
        await self._async_learn_missing_identities()
        still_unknown = self.network.unidentified_node_ids()
        if not still_unknown:
            return
        names = ", ".join(
            f"{self.heater_names.get(node_id, f'Heater {node_id:02X}')} ({node_id:02X})"
            for node_id in still_unknown
        )
        persistent_notification.async_create(
            self.hass,
            f"A heater is asking to pair, but {names} did not answer the identity "
            "request, so the two cannot be told apart and no id has been assigned. "
            "Make sure every heater already added is powered on and in range, then "
            "press Pair heater again.",
            title="Termoweb Local: pairing needs every heater reachable",
            notification_id=f"{DOMAIN}_{self.entry.entry_id}_pairing_unidentified",
        )

    def _register_paired_heater(
        self, node_id: int, identity: bytes, relay: int | None = None
    ) -> None:
        """A brand-new E7 pairing (owner direction 2026-09-06 task 3):
        register the heater exactly like a scan/runtime discovery
        (bookkeeping + live entities, no reload), plus its identity so a
        restart still recognises it, then tell the user which id was
        assigned via a persistent notification. The enrolment sequence is
        scheduled for PAIRED_ENROLMENT_DELAY_S later rather than run here,
        so the port stays free for the heater's own announcements and for
        its first frames under the new id."""
        heater_name = self._register_heater_bookkeeping(node_id, identity=identity)
        self._schedule_enrolment(node_id, relay=relay)
        persistent_notification.async_create(
            self.hass,
            f'Paired a new heater as node id {node_id:02X} ("{heater_name}"). '
            "Rename it from its device page in Settings -> Devices & services "
            "if you like.",
            title="Termoweb Local: heater paired",
            notification_id=f"{DOMAIN}_{self.entry.entry_id}_paired_{node_id:02x}",
        )

    def _schedule_enrolment(self, node_id: int, relay: int | None = None) -> None:
        """The gateway's own clock-and-query-burst enrolment, PAIRED_ENROLMENT_DELAY_S
        after the id assignment (2026-09-06 17:12:17Z capture: about 60 s).

        `relay` is the node the pairing assignment was addressed through, or
        None when it went to the broadcast id. The burst itself is sent one hop
        direct to `node_id` whatever that is, and cannot currently be anything
        else, so a heater reachable only through a relay is assigned an id and
        then fails at enrolment. That is not a route this station can invent:

        - Network.startup_sequence builds every step with build_frame's default
          one-hop path and waits for each reply with
          NanoCul.wait_for_reply(node_id, ...), which matches on the reply's own
          source id. Through a relay both directions are wrong, not just the
          outbound one, and the ack send_frame waits for would be the relay's,
          which says nothing about whether the heater received anything.
        - Reusing the assignment's reversed path would mean putting the
          heater's newly assigned id where the announcement's path held `FF`,
          which is an inference about what the heater now answers to, not
          something any capture or the gateway dump shows.
        - The relay also has to forward the frame, and what a heater does with
          a path that names a further node after its own entry is unknown: the
          one captured heater forward (analysis8.md section 4) carries the
          heater-side `01 01 01` padding after the relay, not a node id, and no
          heater firmware has been dumped.
        - The gateway does not derive this route from an announcement at all.
          It keeps a next-hop table (`DAT_400d1c78`) that `FUN_400ffba0` fills
          from the F4 route advertisements a heater sends with byte 11 `06`
          (analysis8.md section 4). This station has no such table and does not
          read those advertisements, and building one is the actual fix.

        Every later frame to that heater is one hop direct too, so routing the
        burst alone would produce a heater that enrols and is then unreachable,
        which reads as success. Logging the case is what this does instead:
        docs/PROTOCOL.md 5.9 records the limitation."""
        if relay is not None:
            _LOGGER.warning(
                "pairing: node %02x was assigned its id through relay %02x, but its "
                "enrolment burst and every later command are sent one hop direct. If "
                "the heater is not in direct range of this station, enrolment will "
                "fail and the heater will not report (docs/PROTOCOL.md 5.9)",
                node_id, relay,
            )

        async def _run(_now) -> None:
            self._enrolment_cancels.remove(cancel)
            await self._async_run_startup_sequence(node_id)

        cancel = async_call_later(self.hass, PAIRED_ENROLMENT_DELAY_S, _run)
        self._enrolment_cancels.append(cancel)

    async def _async_confirm_report(self, node_id: int) -> None:
        air = self.network.confirm_report(node_id)
        _LOGGER.info("confirming report from node %02x", node_id)
        try:
            result = await self._run_locked(
                self._nanocul.send_frame, node_id, air,
                timeout=COMMAND_TIMEOUT_S, description=f"confirm report to node {node_id:02x}",
            )
        except asyncio.TimeoutError:
            await self._async_reconnect(f"confirm report to node {node_id:02x} timed out")
            self._note_command_outcome(False)
            return
        except Exception:  # noqa: BLE001 - a confirmation failure must not kill the reader loop
            _LOGGER.exception(
                "failed to send report confirmation to node %02x", node_id
            )
            self._note_command_outcome(False)
            return
        self._note_command_outcome(result.ok)

    async def _async_update_data(self) -> dict[int, Heater]:
        now = dt_util.utcnow()
        if (
            self._last_clock_sync is None
            or (now - self._last_clock_sync).total_seconds() >= CLOCK_SYNC_INTERVAL_S
        ):
            await self._async_sync_clock_all()
        else:
            # The daily sync above already sent every heater a fresh EB when
            # it ran, so this only ever has anything to do on the refreshes
            # in between (LINK_KEEPALIVE_INTERVAL_S).
            await self._async_send_link_keepalives()

        for node_id in self.heaters:
            await self._async_request_status(node_id)
        # Re-read the clock rather than reuse the one taken above: the status
        # sweep between them is a radio round trip per heater, and measuring
        # the energy period from before it makes the period ENERGY_POLL_INTERVAL_S
        # plus the sweep instead of ENERGY_POLL_INTERVAL_S.
        now = dt_util.utcnow()
        if (
            self._last_energy_poll is None
            or (now - self._last_energy_poll).total_seconds() >= ENERGY_POLL_INTERVAL_S
        ):
            await self._async_poll_energy_all()
        self._update_link_health()
        return self.heaters

    async def _async_request_status(self, node_id: int) -> None:
        """F3 B8 on-demand status request (2026-09-06 proof,
        f3-and-edges-results.md section 1), the periodic refresh's own cadence
        step, the poll_now service's target, and the post-command status
        request _async_send_confirm_and_refresh sends after a setpoint/mode/
        override command: this replaces the old F2 57 55 poll, which the same
        proof session showed elicits no reply at all.

        Logs one DEBUG line either way (docs/80-handover.md list C item 6):
        without it, a setpoint-latency acceptance run could only infer the E6
        confirmation from the absence of a reconciliation warning, never see
        the reply or its latency directly."""
        sent_at = time.monotonic()
        try:
            snapshot = await self._run_locked(
                self.network.request_status, node_id,
                timeout=COMMAND_TIMEOUT_S, description=f"status request to node {node_id:02x}",
            )
        except asyncio.TimeoutError:
            await self._async_reconnect(f"status request to node {node_id:02x} timed out")
            self._note_command_outcome(False)
            return
        except serial.SerialException as err:
            # A reconnect in progress closes the transport out from under this
            # request the same way it does the reader loop's own in-flight
            # read: the expected, clean way a request unblocks while a
            # reconnect owns the port, not a fault. Left uncaught this would
            # propagate out of _async_update_data as DataUpdateCoordinator's
            # own "Unexpected error fetching" failure even though the
            # reconnect itself is already handling the outage; the next
            # refresh retries this heater.
            _LOGGER.debug(
                "status request to node %02x stopped by a closed port: %s",
                node_id, err,
            )
            self._note_command_outcome(False)
            return
        elapsed_ms = (time.monotonic() - sent_at) * 1000
        self._note_command_outcome(snapshot is not None)
        heater = self.heaters.get(node_id)
        if heater is None:
            return
        if snapshot is not None:
            heater.record_snapshot(snapshot)
            heater.record_ack()
            # boost_end_day is only ever set from an E4's own tail (E6 has none,
            # HeaterSnapshot.from_e6); getattr covers a caller-supplied fake
            # snapshot in a unit test, which carries neither attribute.
            reply_class = "E4" if getattr(snapshot, "boost_end_day", None) is not None else "E6"
            reply_len = (
                network_module.STATUS_REPLY_PAYLOAD_LEN_E4
                if reply_class == "E4"
                else network_module.STATUS_REPLY_PAYLOAD_LEN
            )
            reply_payload = self.network.last_status_reply_payload.get(node_id)
            reply_payload_hex = "" if reply_payload is None else reply_payload.hex()
            _LOGGER.debug(
                "status reply (%s) from node %02x: request B8, payload %d "
                "bytes, %.0f ms, payload %s",
                reply_class, node_id, reply_len, elapsed_ms, reply_payload_hex,
            )
        else:
            heater.record_retry()
            _LOGGER.debug(
                "no status reply (E6) from node %02x within timeout (request "
                "B8, %.0f ms)",
                node_id, elapsed_ms,
            )

    async def _async_poll_energy_all(self) -> None:
        """One F3 BC energy read per heater, in id order (ENERGY_POLL_INTERVAL_S).
        Each read is its own _run_locked call, so the reader loop and any command in
        flight get the lock back between heaters instead of waiting out the whole
        sweep -- which is also what spaces the reads apart on air, the way the real
        gateway spaces its own about 250 ms apart."""
        for node_id in list(self.heaters):
            await self._async_request_energy(node_id)
        self._last_energy_poll = dt_util.utcnow()

    async def _async_request_energy(self, node_id: int) -> None:
        """F3 BC energy read for one heater (docs/PROTOCOL.md 5.6). A heater that
        does not answer keeps whatever counter it last reported: the value is
        cumulative, so a missed read is a gap in the sampling, never a reason to
        drop back to unknown."""
        sent_at = time.monotonic()
        try:
            energy_wh = await self._run_locked(
                self.network.read_energy, node_id,
                timeout=COMMAND_TIMEOUT_S, description=f"energy read for node {node_id:02x}",
            )
        except asyncio.TimeoutError:
            await self._async_reconnect(f"energy read for node {node_id:02x} timed out")
            self._note_command_outcome(False)
            return
        elapsed_ms = (time.monotonic() - sent_at) * 1000
        self._note_command_outcome(energy_wh is not None)
        if energy_wh is None:
            _LOGGER.debug(
                "no energy reply (EF) from node %02x within timeout (request "
                "BC, %.0f ms)",
                node_id, elapsed_ms,
            )
            return
        heater = self.heaters.get(node_id)
        if heater is not None:
            previous_wh = heater.last_energy_wh
            heater.record_energy(energy_wh)
            if previous_wh is None:
                _LOGGER.debug(
                    "energy reply (EF) from node %02x: %d Wh (request BC, "
                    "payload %d bytes, %.0f ms)",
                    node_id, energy_wh, network_module.ENERGY_REPLY_PAYLOAD_LEN, elapsed_ms,
                )
            else:
                _LOGGER.debug(
                    "energy reply (EF) from node %02x: %d Wh (delta %+d Wh) "
                    "(request BC, payload %d bytes, %.0f ms)",
                    node_id, energy_wh, energy_wh - previous_wh,
                    network_module.ENERGY_REPLY_PAYLOAD_LEN, elapsed_ms,
                )

    async def _async_sync_clock_all(self) -> None:
        when = dt_util.now()  # local wall clock; EB payload is local time (PROTOCOL.md 5.6)
        sent_at = dt_util.utcnow()
        try:
            for node_id in self.heaters:
                air = self.network.sync_clock(node_id, when)
                await self._send_frame(node_id, air)
                self._last_eb_sent[node_id] = sent_at
        except serial.SerialException as err:
            # Same closed-port race as _async_send_link_keepalives, the other
            # branch of this same daily-sync/keepalive choice in
            # _async_update_data: a reconnect in progress can close the
            # transport out from under this send too, and left uncaught it
            # would propagate out as DataUpdateCoordinator's own "Unexpected
            # error fetching" failure. The next refresh retries whichever
            # heaters this pass did not reach, and _last_clock_sync is only
            # updated below on a full pass, so a partial one is retried as a
            # full daily sync rather than falling back to keepalive cadence.
            _LOGGER.debug("daily clock sync sweep stopped by a closed port: %s", err)
            return
        self._last_clock_sync = sent_at

    async def _async_send_link_keepalives(self) -> None:
        """One EB clock-sync frame to every heater whose last EB (from this or
        the daily full sync) is older than LINK_KEEPALIVE_INTERVAL_S: heater
        04's panel raised the lost-gateway LINK indication after a few
        minutes with no EB at all from the station, cleared by a single EB,
        and the Tevolve gateway itself sent one about every 157s
        (docs/captures/2026-09-13-x19/link-test.md). Runs from the periodic
        refresh's own 300 s tick alongside the daily clock sync rather than on
        its own schedule, so the effective cadence is one EB per heater per
        refresh -- above 157s but well under the panel's own LINK timeout,
        and not worth a separate timer for."""
        when = dt_util.now()
        sent_at = dt_util.utcnow()
        try:
            for node_id in self.heaters:
                last_sent = self._last_eb_sent.get(node_id)
                if last_sent is not None and (sent_at - last_sent).total_seconds() < LINK_KEEPALIVE_INTERVAL_S:
                    continue
                air = self.network.sync_clock(node_id, when)
                await self._send_frame(node_id, air)
                self._last_eb_sent[node_id] = sent_at
        except serial.SerialException as err:
            # A reconnect in progress closes the transport out from under
            # this send the same way it does the reader loop's own in-flight
            # read (_reader_loop's own comment): the expected, clean way a
            # send unblocks while a reconnect owns the port, not a fault.
            # Uncaught here this propagated out of _async_update_data as
            # DataUpdateCoordinator's own "Unexpected error fetching
            # termoweb_local data" (2026-09-15 20:09:04 local outage), which
            # the reconnect itself was already handling; the next refresh
            # retries whichever heaters this pass did not reach.
            _LOGGER.debug("link keepalive sweep stopped by a closed port: %s", err)

    async def _async_run_startup_sequence(self, node_id: int) -> None:
        """Gateway power-up sequence for one heater (Network.startup_sequence,
        2026-09-06 16:53:30Z capture): runs once per heater at station start,
        after the clock sync above, never on a re-registration (see
        _async_register_heater)."""
        association_value = self._association_values.get(node_id)
        try:
            result = await self._run_locked(
                self.network.startup_sequence, node_id, association_value,
                timeout=STARTUP_SEQUENCE_TIMEOUT_S, description=f"startup sequence for node {node_id:02x}",
            )
        except asyncio.TimeoutError:
            await self._async_reconnect(f"startup sequence for node {node_id:02x} timed out")
            return
        _LOGGER.info(
            "startup sequence for %02x: association=%s/%s program=%s burst=%s "
            "c2=%s identity=%s capability=%s c6=%s e2=%s",
            node_id, result.association_sent, result.association_ack,
            result.program is not None, result.burst_terminator_ok,
            result.c2_reply_payload is not None, result.identity_payload is not None,
            result.capability_payload is not None, result.c6_reply_payload is not None,
            result.e2_payload is not None,
        )
        # No cache write needed here: Network.startup_sequence already calls
        # _record_program() itself when its own F3 B0 read decodes, so
        # get_prog(node_id) already reflects it by the time this returns.
        if result.identity_payload is not None:
            # Keyed on the id that was addressed, not the reply's own node id
            # byte, which reads 04 for node 3 as well (PROTOCOL.md section 9).
            decoded = self.network.identity_from_identity_reply(result.identity_payload)
            if decoded is not None:
                self._learn_identity(node_id, decoded[1])
        if association_value is not None and not result.association_ack:
            _LOGGER.debug(
                "node %02x did not ack its configured EB association frame at startup",
                node_id,
            )
        # F3 DA, once per heater at the end of its own startup/registration
        # sequence (2026-09-13 X-19 proof): every caller of this method (setup,
        # a scan, pairing's enrolment, a runtime registration) converges here,
        # so this is the one place that covers all of them.
        await self.async_read_advanced_record(node_id)

    async def async_shutdown(self) -> None:
        self._port_open = False
        self._stop_reader.set()
        for cancel in self._enrolment_cancels:
            cancel()
        self._enrolment_cancels.clear()
        if self._pairing_window_cancel is not None:
            self._pairing_window_cancel()
            self._pairing_window_cancel = None
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
        if self._nanocul is not None:
            try:
                await asyncio.wait_for(
                    self.hass.async_add_executor_job(self._nanocul.close), timeout=COMMAND_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                _LOGGER.error(
                    "closing nanoCUL did not return within %.0fs during shutdown", COMMAND_TIMEOUT_S
                )
        await super().async_shutdown()

    # ---- command methods used by climate.py and services.py ----

    async def async_set_setpoint(
        self, node_id: int, celsius: float, mode: str | None = None
    ) -> bool:
        """F1 plain setpoint write. `mode` ("off"/"heat"/"auto") lets a
        caller carry that mode byte in the same frame instead of
        network.set_setpoint's own default manual/heat byte (PROTOCOL.md
        5.1's own worked example: an F1 setpoint alone switched an off
        heater to heat), keeping the heater in whatever mode it should
        actually end up in without a second F2 mode frame -- the radio
        already accepts a setpoint write that leaves mode alone, in the same
        single frame. `mode=None` (the default) keeps every existing caller
        landing the heater in heat, unchanged."""
        air = self.network.set_setpoint(node_id, celsius, mode=mode)
        return await self._async_send_confirm_and_refresh(node_id, air, "setpoint")

    async def async_set_mode(self, node_id: int, mode: str) -> bool:
        air = self.network.set_mode(node_id, mode)
        return await self._async_send_confirm_and_refresh(node_id, air, "mode")

    async def async_set_override(self, node_id: int, celsius: float) -> bool:
        air = self.network.set_override(node_id, celsius)
        return await self._async_send_confirm_and_refresh(node_id, air, "override")

    async def async_set_preset_temperatures(
        self, node_id: int, cold: float, night: float, day: float
    ) -> bool:
        air = self.network.write_presets(node_id, cold, night, day)
        return await self._async_send_confirm_and_refresh(node_id, air, "preset_temperatures")

    async def async_set_toggle(self, node_id: int, opcode: int, on: bool, command_name: str) -> bool:
        """D2/D6/D4/BA one-bit toggle write (2026-09-13 X-19 proof,
        PROTOCOL.md 5.6): mirrors async_set_mode, except the processed reply
        it waits for is that opcode's own entry in TOGGLE_REPLY_PAYLOADS
        (D3/D7/D5/BB 55), never the generic B5 55 these heaters do not send
        for a toggle."""
        air = self.network.set_toggle(node_id, opcode, on)
        return await self._async_send_confirm_and_refresh(
            node_id, air, command_name, expected_payloads=TOGGLE_REPLY_PAYLOADS[opcode]
        )

    async def async_start_boost(self, node_id: int) -> bool:
        """D2 01 (2026-09-13 X-19 proof): boost sets E5 byte 24 bit 5 and the
        heater pushes an unprompted E3 carrying the boost-end tail; the F3 DA
        read right after picks up the boost temperature for climate.py's own
        attribute, regardless of whether the toggle itself was confirmed (a
        failed toggle makes the read a cheap no-op, not a harmful one)."""
        ok = await self.async_set_toggle(node_id, TOGGLE_BOOST_OPCODE, True, "boost")
        await self.async_read_advanced_record(node_id)
        return ok

    async def async_cancel_boost(self, node_id: int) -> bool:
        """D2 00 (2026-09-13 X-19 proof): boost cancel, same DA follow-up read
        as async_start_boost."""
        ok = await self.async_set_toggle(node_id, TOGGLE_BOOST_OPCODE, False, "boost")
        await self.async_read_advanced_record(node_id)
        return ok

    async def async_set_easy_mode(self, node_id: int, on: bool) -> bool:
        """D6 01/D6 00 (2026-09-13 X-19 proof): forces mode heat, setpoint
        unchanged; EASY off restores the previous mode."""
        return await self.async_set_toggle(node_id, TOGGLE_EASY_OPCODE, on, "easy_mode")

    async def async_set_child_lock(self, node_id: int, on: bool) -> bool:
        """BA 01/BA 00 (2026-09-13 X-19 proof): the keypad lock."""
        return await self.async_set_toggle(node_id, TOGGLE_LOCK_OPCODE, on, "child_lock")

    async def async_set_runback(self, node_id: int, on: bool) -> bool:
        """D4 01/D4 00 (2026-09-13 X-19 proof): Runback Config forces mode
        heat and the setpoint to the anti-frost preset; turning it off does
        not itself restore the setpoint it displaced (docs/captures/
        2026-09-13-x19/notes.md and verify-04.log: a heater off beforehand
        stayed at the anti-frost setpoint after Runback off), so this
        coordinator caches the pre-toggle mode/setpoint on the way on and
        restores both, unconditionally, on the way off.

        The cache is in-memory only, not persisted to entry.options (Risks:
        a coordinator restart between "Runback on" and "Runback off" loses
        it, silently degrading to "not restored" -- deliberately not fixed by
        persisting, since the write churn on every toggle is worse than the
        narrow restart window it would close). `switch.py`'s own
        extra_state_attributes reads get_runback_restored(node_id) to say
        whether the last "off" restored anything: None before this
        coordinator has ever turned Runback off for this node, True/False
        once it has (_runback_restored, set below) -- kept separate from
        _runback_cache itself, which this method clears to None on every
        successful "off" so a stale cache can never leak into the next "on".

        Restoration always runs when a cache exists, for every cached mode
        (HeaterSnapshot.mode's own strings, heater.py's MODE_NAMES): a cached
        "manual" is restored with async_set_setpoint alone (which puts the
        heater back in heat); a cached "override" is restored with
        async_set_override, since the network layer's own mode payload has
        no override code of its own (network.py's _MODE_PAYLOAD_BYTE covers
        only auto/heat/off); a cached "off" or "auto" is restored with
        async_set_setpoint first -- which flips the heater to heat, the only
        way to move the setpoint off the anti-frost preset Runback left it
        at -- then async_set_mode back to the cached mode, off or auto.
        `restored` is True once every restore command for the cached mode
        has succeeded, False if any of them failed."""
        if on:
            heater = self.heaters.get(node_id)
            snap = heater.last_snapshot if heater is not None else None
            self._runback_cache[node_id] = (
                (snap.mode, snap.setpoint_c) if snap is not None else None
            )
            ok = await self.async_set_toggle(node_id, TOGGLE_RUNBACK_OPCODE, True, "runback")
            if not ok:
                self._runback_cache[node_id] = None
            return ok

        ok = await self.async_set_toggle(node_id, TOGGLE_RUNBACK_OPCODE, False, "runback")
        if ok:
            cached = self._runback_cache.get(node_id)
            if cached is None:
                restored = None
            else:
                mode, setpoint_c = cached
                if mode == "manual":
                    restored = await self.async_set_setpoint(node_id, setpoint_c)
                elif mode == "override":
                    restored = await self.async_set_override(node_id, setpoint_c)
                elif mode in ("auto", "off"):
                    setpoint_ok = await self.async_set_setpoint(node_id, setpoint_c)
                    mode_ok = await self.async_set_mode(node_id, mode)
                    restored = setpoint_ok and mode_ok
                else:
                    # snap.mode returned None (a mode_code not yet in
                    # MODE_NAMES): nothing recognised to restore against.
                    restored = False
            self._runback_cache[node_id] = None
            self._runback_restored[node_id] = restored
            # async_set_toggle's own listener push (inside async_set_toggle's
            # _async_send_confirm_and_refresh) already happened before
            # _runback_restored was set above; push again so the switch's own
            # "restored" attribute reflects it without waiting for the next
            # unrelated update (the same follow-up push climate.py's own
            # async_set_schedule does for the schedule sensor's `source`).
            self.async_update_listeners()
        return ok

    def get_runback_restored(self, node_id: int) -> bool | None:
        """Whether this coordinator's last Runback-off for `node_id` restored
        the cached pre-toggle mode/setpoint: None before it has ever turned
        Runback off for this node (switch.py's own starting attribute
        value), True/False once it has (async_set_runback)."""
        return self._runback_restored.get(node_id)

    def get_advanced_setup(self, node_id: int) -> dict:
        """The persisted C4 record for `node_id` (2026-09-13 X-19 proof): the
        record last accepted for that heater, or the manual's own defaults
        (_DEFAULT_ADVANCED_SETUP) for one never written this way -- the same
        "return a copy" convention get_prog() uses."""
        return dict(self._advanced_setup.get(node_id, _DEFAULT_ADVANCED_SETUP))

    async def async_write_advanced_setup(self, node_id: int, **changed_fields) -> bool:
        """C4, the eight-field advanced-setup record (2026-09-13 X-19 proof,
        PROTOCOL.md 5.6): every write sends the whole record, the persisted
        other seven fields plus whichever ones `changed_fields` overrides, so
        one entity's write can never silently drop another's last-known
        field (Risks: this is what routes every C4 write through this one
        method rather than letting each entity build its own eight-byte
        payload). Waits for C4's own three-way reply
        (Network.wait_advanced_setup_reply) under the same _run_locked hold
        _async_send_and_confirm_processed uses, so the reader loop cannot
        steal it between two separate lock acquisitions either.

        Persists the merged record and re-reads status (the existing
        post-command F3 B8 refresh) only once the heater actually accepted
        it; a rejected or unconfirmed write is neither persisted nor
        refreshed, and returns False."""
        current = dict(self._advanced_setup.get(node_id, _DEFAULT_ADVANCED_SETUP))
        merged = {**current, **changed_fields}
        air = self.network.write_advanced_setup(
            node_id,
            merged["control_mode"],
            merged["units"],
            merged["offset_tenths"],
            merged["away_mode"],
            merged["away_offset"],
            merged["modified_auto_span"],
            merged["window_mode"],
            merged["true_radiant"],
        )
        try:
            result, accepted = await self._run_locked(
                self._send_and_wait_advanced_setup_sync, node_id, air,
                timeout=COMMAND_TIMEOUT_S, description=f"advanced setup write to node {node_id:02x}",
            )
        except asyncio.TimeoutError:
            await self._async_reconnect(f"advanced setup write to node {node_id:02x} timed out")
            self._note_command_outcome(False)
            return False
        self._note_command_outcome(result.ok)
        self._record_ack_result(node_id, result)
        if not result.ok:
            _LOGGER.warning("node %02x did not ack the advanced-setup write", node_id)
            return False
        if accepted is False:
            _LOGGER.warning("node %02x rejected the advanced-setup write (F2 C5 56)", node_id)
            return False
        if accepted is None:
            _LOGGER.warning(
                "node %02x did not confirm the advanced-setup write (no C5 55/56 "
                "reply within timeout)",
                node_id,
            )
            return False

        self._advanced_setup[node_id] = merged
        new_options = dict(self.entry.options)
        new_options[CONF_HEATER_ADVANCED_SETUP] = {
            **self.entry.options.get(CONF_HEATER_ADVANCED_SETUP, {}),
            str(node_id): merged,
        }
        self._persist_options_no_reload(new_options)
        await self._async_request_status(node_id)
        self.async_update_listeners()
        return True

    def _send_and_wait_advanced_setup_sync(self, node_id: int, air: bytes):
        result = self._nanocul.send_frame(node_id, air)
        accepted = self.network.wait_advanced_setup_reply(node_id) if result.ok else False
        return result, accepted

    async def async_read_advanced_record(self, node_id: int) -> None:
        """F3 DA (2026-09-13 X-19 proof, PROTOCOL.md 5.6): read once per
        heater at the end of _async_run_startup_sequence and once after every
        async_start_boost/async_cancel_boost call; never on the poll cadence.
        Stashes the result on Heater.last_advanced_record for climate.py's own
        boost_temperature attribute; a missing reply leaves that field
        unchanged rather than clearing it, the same "a gap in the sampling,
        not a reason to drop back to unknown" rule _async_request_energy
        already applies to the energy counter."""
        try:
            record = await self._run_locked(
                self.network.read_advanced_record, node_id,
                timeout=COMMAND_TIMEOUT_S, description=f"advanced setup read for node {node_id:02x}",
            )
        except asyncio.TimeoutError:
            await self._async_reconnect(f"advanced setup read for node {node_id:02x} timed out")
            self._note_command_outcome(False)
            return
        self._note_command_outcome(record is not None)
        if record is None:
            _LOGGER.debug(
                "no advanced-setup reply (DB) from node %02x within timeout (request DA)",
                node_id,
            )
            return
        heater = self.heaters.get(node_id)
        if heater is not None:
            heater.last_advanced_record = record
            self.async_update_listeners()

    async def _async_send_confirm_and_refresh(
        self, node_id: int, air: bytes, command_name: str, expected_payloads=None
    ) -> bool:
        """async_set_setpoint/async_set_mode/async_set_override/
        async_set_preset_temperatures's shared tail (docs/80-handover.md list
        C item 10, step 1): a command the heater actually accepted
        (_async_send_and_confirm_processed's own `ok`, i.e. the frame was
        acked -- not gated on `processed` too, since an acked command with a
        missing/late B5 55/B3 55/B7 55 is exactly the more uncertain case a
        fresh status read is most useful for, not less) is followed by its
        own F3 B8/E6 status request, so the snapshot -- and the climate
        entity's target_temperature/hvac_mode/preset_mode/ptemp -- reflect it
        immediately instead of waiting for the heater's next unsolicited E5 on
        its own cadence (up to DEFAULT_POLL_INTERVAL_S).

        This does not live inside _async_send_and_confirm_processed itself,
        even though that is the only caller of async_set_schedule too:
        async_set_schedule already does its own reconciliation (a B0
        read-back right after), so folding a bonus status request in there
        would be redundant on-air traffic the handover's fix list never asked
        for -- it names async_set_setpoint/async_set_mode/async_set_override/
        async_set_preset_temperatures only.

        The status request below is deliberately its own separate
        _run_locked hold, not folded into the one _async_send_and_confirm_processed
        already took for the send-and-wait-for-processed above. Read together,
        _run_locked, _async_send_and_confirm_processed and _async_request_status
        settle this: the existing single hold in
        _async_send_and_confirm_processed exists to stop the reader loop's own
        read_events() from stealing the B5 55/B3 55 reply in the gap between
        two separate lock acquisitions, because send and wait_processed used to
        be (and without that hold, could again be) two separate operations
        racing the reader loop for the same bytes. The status request has no
        such gap to protect: Network.request_status (via _request_and_wait)
        already sends the F3 B8 and waits for its own E6 inside one single
        executor call, exactly like wait_processed's own protection, so
        nothing is left in flight for the reader loop to steal between this
        call and the one before it -- the send-and-wait-for-processed hold
        already consumed (or timed out on) its own reply before releasing the
        lock. The gap between the two _run_locked calls only exposes ordinary
        ambient traffic to the reader loop, the same exposure that already
        exists between any two commands or polls today. That is exactly the
        shape _async_poll_energy_all already chooses on purpose, chaining a
        separate _run_locked call per heater rather than stretching one hold
        across all of them, so the reader loop and any other in-flight command
        get the lock back between one atomic request/reply exchange and the
        next instead of being starved across two unrelated ones back to back.
        Extending the single hold to cover the status request too would win
        nothing (no reply is being protected) and would cost the reader loop
        up to another whole COMMAND_TIMEOUT_S of starvation if the status
        request is itself slow, on top of the send-and-confirm hold's own
        budget.

        Gated on `ok`: a command the heater never acked has nothing to read
        back, and reading its status anyway would neither confirm nor deny
        anything the command itself did not already establish.

        `expected_payloads` is threaded straight through to
        _async_send_and_confirm_processed/wait_processed: a D2/D6/D4/BA
        toggle's own processed reply (TOGGLE_REPLY_PAYLOADS) is never the
        generic B5 55 this method's other callers wait for (2026-09-13 X-19
        proof, PROTOCOL.md 5.6), so async_set_toggle passes its own opcode's
        entry here rather than this method guessing at one."""
        ok = await self._async_send_and_confirm_processed(
            node_id, air, command_name, expected_payloads=expected_payloads
        )
        if ok:
            await self._async_request_status(node_id)
            self.async_update_listeners()
        return ok

    async def async_flash_display(self, node_id: int) -> bool:
        """F2 5E 01 (2026-09-06 P4b proof, notes.md 16:56:59Z): the
        button.<heater>_flash_display press, backed now that a radio frame
        has been captured for it."""
        try:
            ok = await self._run_locked(
                self.network.flash_display, node_id,
                timeout=COMMAND_TIMEOUT_S, description=f"flash_display for node {node_id:02x}",
            )
        except asyncio.TimeoutError:
            await self._async_reconnect(f"flash_display for node {node_id:02x} timed out")
            self._note_command_outcome(False)
            return False
        self._note_command_outcome(ok)
        if not ok:
            _LOGGER.warning(
                "node %02x did not confirm flash_display (no F2 5F 55 reply within timeout)",
                node_id,
            )
        return ok

    async def _async_send_and_confirm_processed(
        self, node_id: int, air: bytes, command_name: str, expected_payloads=None
    ) -> bool:
        """Send `air`, then wait for the F2 B5 55/B3 55/B7 55 reply that means
        `node_id` actually processed it (2026-09-06 proof, filtering-results.md
        "The B5 55 reply as a processed indicator"; B7 55, 2026-09-12 X-18
        proof, docs/captures/2026-09-12-x18-s7/notes.md), logging when it does
        not arrive. Both the send and the wait run in the same executor job,
        under one _io_lock hold, so the background reader loop cannot steal
        the reply out from under wait_processed in the gap between two
        separate lock acquisitions.

        `expected_payloads`, passed straight to `_send_and_wait_processed_sync`
        and from there to `Network.wait_processed`, defaults to None (that
        method's own PROCESSED_REPLY_PAYLOADS default): async_set_toggle is the
        one caller that overrides it, since D2/D6/D4/BA each answer with their
        own processed reply (TOGGLE_REPLY_PAYLOADS), not B5 55/B3 55/B7 55
        (2026-09-13 X-19 proof, PROTOCOL.md 5.6)."""
        try:
            result, processed = await self._run_locked(
                self._send_and_wait_processed_sync, node_id, air, expected_payloads,
                timeout=COMMAND_TIMEOUT_S, description=f"{command_name} to node {node_id:02x}",
            )
        except asyncio.TimeoutError:
            await self._async_reconnect(f"{command_name} to node {node_id:02x} timed out")
            self._note_command_outcome(False)
            return False
        self._note_command_outcome(result.ok)
        self._record_ack_result(node_id, result)
        if result.ok and not processed:
            _LOGGER.warning(
                "node %02x did not confirm processing the %s command "
                "(no B5 55/B3 55 reply within timeout)",
                node_id,
                command_name,
            )
        return result.ok

    def _send_and_wait_processed_sync(self, node_id: int, air: bytes, expected_payloads=None):
        result = self._nanocul.send_frame(node_id, air)
        if not result.ok:
            return result, False
        wait_kwargs = {} if expected_payloads is None else {"expected_payloads": expected_payloads}
        processed = self.network.wait_processed(node_id, **wait_kwargs)
        return result, processed

    async def async_poll_now(self, node_id: int | None = None) -> None:
        node_ids = [node_id] if node_id is not None else list(self.heaters)
        for nid in node_ids:
            await self._async_request_status(nid)
        self.async_update_listeners()

    async def async_sync_clock_now(self) -> None:
        await self._async_sync_clock_all()

    async def async_sync_clock_one(self, node_id: int) -> bool:
        when = dt_util.now()
        air = self.network.sync_clock(node_id, when)
        result = await self._send_frame(node_id, air)
        self._record_ack_result(node_id, result)
        # Counts for the link keepalive check too (_async_send_link_keepalives):
        # whatever the reason for this EB, it just refreshed the panel's own
        # LINK indication the same way a keepalive would.
        self._last_eb_sent[node_id] = dt_util.utcnow()
        return result.ok

    def _record_ack_result(self, node_id: int, result: Any) -> None:
        heater = self.heaters.get(node_id)
        if heater is None:
            return
        if result.ok:
            heater.record_ack()
        else:
            heater.record_retry()

    # ---- program (prog attribute / set_schedule service) ----

    async def async_read_program(self, node_id: int) -> list[int | None] | None:
        """F3 B0 program read; Network.read_program() records the reply in
        Network.last_program itself, at that node's own native resolution
        (24 or 48 slots a day). Returns the node's own slots at that
        resolution (get_prog()'s own return shape), or None (leaving any
        previous cache entry alone) if the heater does not reply within the
        usual timeout.

        Logs one DEBUG line either way (docs/80-handover.md list C item 6),
        matching the request-path logging every other F3 opcode now has."""
        sent_at = time.monotonic()
        try:
            result = await self._run_locked(
                self.network.read_program, node_id,
                timeout=COMMAND_TIMEOUT_S, description=f"read program for node {node_id:02x}",
            )
        except asyncio.TimeoutError:
            await self._async_reconnect(f"read program for node {node_id:02x} timed out")
            self._note_command_outcome(False)
            return None
        elapsed_ms = (time.monotonic() - sent_at) * 1000
        self._note_command_outcome(result is not None)
        if result is None:
            _LOGGER.debug(
                "no program reply (9F/C9) from node %02x within timeout "
                "(request B0, %.0f ms)",
                node_id, elapsed_ms,
            )
            return None
        record = self.network.program_record(node_id)
        if record is not None:
            reply_class = "9F" if record.resolution == network_module.SLOTS_PER_DAY_HALF_HOURLY else "C9"
            _LOGGER.debug(
                "program reply (%s) from node %02x: request B0, payload %d "
                "bytes, %.0f ms",
                reply_class, node_id, len(record.raw) + 1, elapsed_ms,
            )
        return self.get_prog(node_id)

    def get_prog(self, node_id: int) -> list[int | None] | None:
        """`node_id`'s own program, at its own native resolution (24 hourly
        C9 slots a day or 48 half-hourly 9E/9F slots a day,
        Network.program_resolution) rather than projected onto one hourly
        value: a half-hourly schedule's own half-hour boundaries survive
        here instead of being folded away before climate.py's `prog`
        attribute or the schedule sensors ever see them (docs/80-handover.md
        "Owed" list, item 7).

        Monday first, this project's HA surface order matching the cloud's
        own `prog` array: ProgramRecord.slots itself is wire order (day 0
        Sunday, PROTOCOL.md 5.6/5.7), and slots_monday_first is where that
        one rotation happens (X-17 day-order fix; network.rotate_week()).
        None before any read, report or write for this node."""
        record = self.network.program_record(node_id)
        return None if record is None else record.slots_monday_first

    async def async_set_schedule(self, node_id: int, week_slots) -> bool:
        """set_schedule service: B2 program write, then a B0 read-back so the
        `prog` attribute reflects what the heater actually stored rather than
        merely what was sent (docs/91-p4-parity-plan.md P4a)."""
        air = self.network.write_program(node_id, week_slots)
        ok = await self._async_send_and_confirm_processed(node_id, air, "set_schedule")
        await self.async_read_program(node_id)
        # async_read_program only updates the cache; push it out so the
        # climate entity's `prog` attribute re-renders now, the same way
        # async_poll_now does for its own coordinator-mutating calls.
        self.async_update_listeners()
        return ok

    # ---- gateway online (binary_sensor.py) ----

    def _update_link_health(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if self.gateway_connected(now):
            if self._link_healthy_since is None:
                self._link_healthy_since = now
        else:
            self._link_healthy_since = None

    def gateway_connected(self, now: float | None = None) -> bool:
        """Serial link open AND at least one heater has reported within 2 of
        its own expected report periods (docs/91-p4-parity-plan.md P4a;
        report cadence only, never RSSI/LQI, per PROTOCOL.md section 6)."""
        if not self._port_open:
            return False
        now = time.time() if now is None else now
        for heater in self.heaters.values():
            if heater.last_report_time is None:
                continue
            if now - heater.last_report_time <= heater.expected_report_period() * GATEWAY_STALE_FACTOR:
                return True
        return False

    def gateway_last_frame_at(self) -> float | None:
        times = [h.last_report_time for h in self.heaters.values() if h.last_report_time is not None]
        return max(times) if times else None

    def gateway_link_healthy_minutes(self, now: float | None = None) -> float | None:
        if self._link_healthy_since is None:
            return None
        now = time.time() if now is None else now
        return (now - self._link_healthy_since) / 60.0


type TermowebLocalConfigEntry = ConfigEntry[TermowebLocalCoordinator]

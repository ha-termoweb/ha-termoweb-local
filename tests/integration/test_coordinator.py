"""Coordinator poll cycle and background frame delivery, against a fake NanoCul
transport: docs/90-phase3-plan.md P4 verification line ("the coordinator's poll
cycle... a simulated incoming poll-response frame updating the entity state"), and
the discovery/pairing/removal workflow (owner direction 2026-09-06).

Also the 2026-09-06 live stall postmortem (heaters 03/04 never registered, the
scan button found nothing, nothing in the HA log said why): the timeout/
reconnect/watchdog/logging coverage lives here too, against
custom_components.termoweb_local.coordinator's own module-level timeout
constants, shrunk per test via monkeypatch the same way shrink_scan_timeout
already shrinks Network's own SCAN_REPLY_TIMEOUT_S."""
import asyncio
import datetime as dt
import logging
import threading
import time
import types

import serial
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from termoweb_local import frame as tf

from termoweb_local import network as network_module
from termoweb_local.nanocul import Piece

import custom_components.termoweb_local.coordinator as coordinator_module
from custom_components.termoweb_local.coordinator import PAIRED_ENROLMENT_DELAY_S
from custom_components.termoweb_local.const import (
    CONF_HEATER_ID,
    CONF_HEATER_IDENTITIES,
    CONF_HEATER_NAME,
    CONF_HEATERS,
    DOMAIN,
)

from .conftest import (
    FakeCulTransport,
    THREE_HEATERS,
    identity_reply_payload,
    make_config_entry,
    setup_entry,
)

# Master bedroom heater (node 4), off, room 23.1C, setpoint 25.0C -- the same worked
# frame tests/test_heater.py::test_snapshot_from_initial_off_report decodes.
E5_OFF_REPORT_HEX = "E59C885DB6A1C825565F4A9C5850E47E05BAB4FC84AE07F1E686E38616"


async def _wait_until(predicate, timeout=2.0, step=0.02):
    async def _sleep():
        await asyncio.sleep(step)

    elapsed = 0.0
    while not predicate():
        if elapsed >= timeout:
            raise AssertionError("condition not met before timeout")
        await _sleep()
        elapsed += step


def _sent_frames(transport: FakeCulTransport):
    return [
        tf.parse_frame(bytes.fromhex(w.strip()[1:].decode()))
        for w in transport.written
        if w.startswith(b"T")
    ]


def _last_non_status_hex(transport: FakeCulTransport) -> str:
    """The last command frame's own on-air hex, skipping the F3 B8 status
    request that now follows every successful setpoint/mode/override command
    (docs/80-handover.md list C item 10 step 1) -- that status request is the
    actual last T write, not the command under test."""
    for w in reversed(transport.written):
        if not w.startswith(b"T"):
            continue
        hexpart = w.strip()[1:].decode()
        if tf.parse_frame(bytes.fromhex(hexpart)).payload != bytes([0xB8]):
            return hexpart
    raise AssertionError("no non-status-request frame found in transport.written")


async def test_coordinator_setup_opens_port_and_polls_each_heater(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)

    coordinator = entry.runtime_data
    assert set(coordinator.heaters) == {0x02, 0x04}

    sent = _sent_frames(transport)
    # Clock sync (EB, class 0x14 -- test_services.py's own worked check)
    # once per already-configured heater.
    clock_frames = [f for f in sent if f.logical[0] == 0x14 and f.dst in (0x02, 0x04)]
    assert len(clock_frames) == 2
    # startup_sequence's own F3 B0 program read, once per already-configured
    # heater (no EB association configured in this test, so that frame is
    # skipped for both, per Network.startup_sequence).
    program_read_frames = [f for f in sent if f.payload == bytes([0xB0]) and f.dst in (0x02, 0x04)]
    assert len(program_read_frames) == 2
    # The discovery scan (owner direction 2026-09-06 task 2) sends an F3 B8
    # status request to every id 2-65 after the clock sync and each
    # already-configured heater's own startup burst, plus the coordinator's
    # first refresh sends one more F3 B8 to each of the 2 already-configured
    # heaters: 64 + 2 = 66.
    status_request_frames = [f for f in sent if f.payload == bytes([0xB8])]
    assert len(status_request_frames) == 66
    assert {f.dst for f in status_request_frames} == set(range(2, 66))

    for node_id in (0x02, 0x04):
        assert coordinator.heaters[node_id].retry_count == 0
        assert coordinator.heaters[node_id].last_snapshot is not None


async def test_connect_sets_station_id_and_enables_auto_ack(
    hass, monkeypatch, enable_custom_integrations
):
    """2026-09-09 fix: a read-only tap on the live ser2net stream caught fast
    (about 100-200 ms apart), byte-identical, different-RSSI report
    duplicates -- two genuine receptions of the same retransmitted report,
    not one frame parsed twice (PROTOCOL.md 5.5: "frames that receive no ack
    are retransmitted 3 times"). Grepping this integration for `A1`/`I<hex>`
    turned up nothing: the stick's hardware auto-ack (default off,
    firmware/termoweb_rx/main.c) was never turned on and its station id
    never explicitly set, so it only ever worked by the firmware default
    (`our_id = 0x01`) happening to match. This must now be sent, and before
    the Q status query so that query's own reply already reflects it."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    assert transport.written.count(b"I01\n") == 1
    assert transport.written.count(b"A1\n") == 1
    assert transport.written.index(b"I01\n") < transport.written.index(b"A1\n")
    assert transport.written.index(b"A1\n") < transport.written.index(b"Q\n")


async def test_reconnect_resends_station_id_and_auto_ack(
    hass, monkeypatch, enable_custom_integrations
):
    """The stick's own RAM state (auto_ack, our_id) survives a TCP reconnect
    unconditionally: deploy/ser2net.yaml's `local` connector flag means a
    reconnect never toggles DTR over the bridge, so nothing resets the stick
    between connects. I<hex>/A1 must therefore be resent on every reconnect,
    not just the very first connect after Home Assistant starts."""
    monkeypatch.setattr(coordinator_module, "COMMAND_TIMEOUT_S", 0.05)
    monkeypatch.setattr(coordinator_module, "RECONNECT_DEBOUNCE_S", 0.0)

    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    assert transport.written.count(b"A1\n") == 1

    def _hang(*args, **kwargs):
        time.sleep(1.0)

    monkeypatch.setattr(coordinator._nanocul, "send_frame", _hang)
    result = await coordinator._send_frame(0x04, b"\x00")
    assert result.ok is False
    assert coordinator.reconnect_count >= 1

    assert transport.written.count(b"I01\n") == 2
    assert transport.written.count(b"A1\n") == 2


async def test_setup_scan_discovers_a_new_heater(hass, monkeypatch, enable_custom_integrations):
    """Owner direction 2026-09-06 task 2: an id that answers F3 B8 but was
    never configured is registered as a heater ("Heater <id>", two hex
    digits), with entities created and startup_sequence run for it, purely
    from the setup-time scan -- no config-flow heater list, no pairing."""
    transport = FakeCulTransport(status_reply_ids={0x02, 0x04, 0x05})
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    assert set(coordinator.heaters) == {0x02, 0x04, 0x05}
    assert coordinator.heater_names[0x05] == "Heater 05"
    assert coordinator.heaters[0x05].last_snapshot is not None

    # Persisted so a reload does not lose it (and so a later scan only
    # confirms, per the task's own wording).
    persisted = {h[CONF_HEATER_ID] for h in entry.options[CONF_HEATERS]}
    assert 0x05 in persisted

    # Entities created live, with no reload: the climate entity for the
    # scan-discovered heater exists under this same, still-running entry.
    state = hass.states.get("climate.heater_05")
    assert state is not None


async def test_scan_for_heaters_button_repeats_the_scan(hass, monkeypatch, enable_custom_integrations):
    """The "Scan for heaters" button (owner direction 2026-09-06 task 2)
    reruns the same scan on demand; an id that starts answering only after
    setup is picked up the next time it is pressed."""
    transport = FakeCulTransport(status_reply_ids={0x02, 0x04})
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    assert 0x06 not in coordinator.heaters

    transport.status_reply_ids = {0x02, 0x04, 0x06}
    new_ids = await coordinator.async_scan_for_heaters()

    assert new_ids == [0x06]
    assert 0x06 in coordinator.heaters
    assert coordinator.heater_names[0x06] == "Heater 06"


async def test_runtime_report_from_unknown_id_registers_a_new_heater(
    hass, monkeypatch, enable_custom_integrations
):
    """Owner direction 2026-09-06 task 2: after setup, a CRC-valid report
    (payload starting `56`) from an id 2-65 this coordinator has never seen
    registers it the same way, with no restart -- entities created live
    through the platforms' own stored async_add_entities callbacks."""
    transport = FakeCulTransport(status_reply_ids={0x02, 0x04})
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    assert 0x03 not in coordinator.heaters

    # A report from node 3 (bedroom slug in other fixtures, but this entry
    # never configured it): same worked E5 frame as elsewhere in this file,
    # readdressed to node 3.
    report_air = tf.build_frame(
        0x03, 0x01, tf.parse_frame(bytes.fromhex(E5_OFF_REPORT_HEX)).payload, hops=(1, 1, 1)
    )
    transport.queue_line(f"RX 1000 -44.0 60 0 {report_air.hex().upper()}")

    await _wait_until(lambda: 0x03 in coordinator.heaters)
    await _wait_until(lambda: coordinator.heaters[0x03].last_snapshot is not None)
    assert coordinator.heater_names[0x03] == "Heater 03"

    # Confirmed like any other report, and its own startup_sequence ran.
    confirm_air = coordinator.network.confirm_report(0x03)
    expected = b"T" + confirm_air.hex().upper().encode() + b"\n"
    await _wait_until(lambda: expected in transport.written)
    await _wait_until(lambda: coordinator.get_prog(0x03) is not None)

    # No reload happened: this is still the same coordinator/entry.
    assert entry.runtime_data is coordinator
    state = hass.states.get("climate.heater_03")
    assert state is not None


async def test_runtime_registration_open_from_unknown_id_registers_a_new_heater(
    hass, monkeypatch, enable_custom_integrations
):
    """An unknown id's own F3 `50` opening frame (power-up) is registered
    the same way as a report, and still gets the required EB registration
    clock reply (docs/PROTOCOL.md 5.6/5.8), not just bookkeeping."""
    transport = FakeCulTransport(status_reply_ids={0x02, 0x04})
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    transport.written.clear()

    open_air = tf.build_frame(0x05, 0x01, bytes([0x50]), hops=(1, 1, 1))
    transport.queue_line(f"RX 1000 -44.0 60 0 {open_air.hex().upper()}")

    await _wait_until(lambda: 0x05 in coordinator.heaters)
    await _wait_until(
        lambda: any(f.payload[:1] == bytes([0x51]) and f.dst == 0x05 for f in _sent_frames(transport))
    )
    await _wait_until(lambda: coordinator.get_prog(0x05) is not None)
    assert coordinator.heater_names[0x05] == "Heater 05"


async def test_coordinator_background_reader_updates_heater_from_e5_report(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)

    # Queued only now: NanoCul.__init__ clears the transport's input buffer on open
    # (mirrors discarding stale bytes on a real port reset), so anything queued
    # before setup never reaches the reader loop.
    transport.queue_line(f"RX 1000 -44.0 60 0 {E5_OFF_REPORT_HEX}")

    coordinator = entry.runtime_data
    # The initial refresh (triggered by async_setup above) already gave this
    # heater a snapshot from its own F3 B8 status request (FakeCulTransport's
    # fixed E6 reply, room 24.7C); wait for the specific E5 report's own value
    # rather than merely "a snapshot exists".
    await _wait_until(
        lambda: coordinator.heaters[0x04].last_snapshot is not None
        and coordinator.heaters[0x04].last_snapshot.room_temp_c == 23.1
    )

    snap = coordinator.heaters[0x04].last_snapshot
    assert snap.mode == "off"
    assert snap.room_temp_c == 23.1
    assert snap.setpoint_c == 25.0


async def test_coordinator_unload_closes_the_port(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert transport.closed is True


async def test_coordinator_command_methods_send_correct_frames(hass, monkeypatch, enable_custom_integrations):
    """The command frame itself, not the F3 B8 status request that now
    follows it (docs/80-handover.md list C item 10 step 1): picked out by
    payload with _sent_frames rather than assumed to be the last T write."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    transport.written.clear()

    ok = await coordinator.async_set_setpoint(0x04, 25.5)
    assert ok is True
    setpoint_sent = _last_non_status_hex(transport)
    expected = tf.build_frame(0x01, 0x04, tf.setpoint_payload(25.5))
    assert setpoint_sent == expected.hex().upper()

    transport.written.clear()
    ok = await coordinator.async_set_mode(0x04, "auto")
    assert ok is True
    mode_sent = _last_non_status_hex(transport)
    assert mode_sent == coordinator.network.set_mode(0x04, "auto").hex().upper()


async def test_successful_command_issues_status_request_and_updates_snapshot(
    hass, monkeypatch, enable_custom_integrations
):
    """docs/80-handover.md list C item 10 step 1: a successful setpoint/mode/
    override command is followed by its own F3 B8, and the reply (E6) updates
    the heater's snapshot without any separate poll_now -- the whole point
    being that the entity does not have to wait for the heater's next
    unsolicited E5 on its own (up to DEFAULT_POLL_INTERVAL_S) cadence."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    transport.written.clear()
    coordinator.heaters[0x04].last_snapshot = None

    ok = await coordinator.async_set_setpoint(0x04, 25.5)
    assert ok is True

    status_frames = [f for f in _sent_frames(transport) if f.payload == bytes([0xB8]) and f.dst == 0x04]
    assert len(status_frames) == 1
    # FakeCulTransport's fixed E6 reply (conftest.py _STATUS_REPLY_PAYLOAD):
    # off, room 24.7C, setpoint 25.0C -- proof the snapshot came from this
    # status request's own reply, not merely "some snapshot or other".
    snap = coordinator.heaters[0x04].last_snapshot
    assert snap is not None
    assert snap.setpoint_c == 25.0
    assert snap.room_temp_c == 24.7


async def test_status_request_answered_by_e4_reconciles_snapshot_and_climate(
    hass, monkeypatch, enable_custom_integrations
):
    """docs/captures/2026-09-12-schedule/notes.md, "Max Temp found, and a new
    status reply class E4 under Runback": a heater with Runback boost running
    answers F3 B8 with this 16-byte class instead of the ordinary 14-byte E6.
    Before the fix, request_status()'s length constraint rejected it, so the
    post-command status request timed out, the coordinator logged "no status
    reply (E6)" and the climate entity's optimistic setpoint was never
    reconciled against the heater's own (Max-Temp-clamped) value."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    # The captured payload's own setpoint (26.0C) stands in for the Max Temp
    # clamp: commanding 30.0C and getting this E4 back proves the entity
    # settles on the heater's own reported value, not the optimistic one.
    e4_payload = bytes.fromhex("b90e24340200ec341daf00a01d0064a3")
    real_reply = transport._application_reply_payload

    def reply_with_e4_for_node_04_status(parsed):
        if parsed.dst == 0x04 and parsed.payload == bytes([0xB8]):
            return e4_payload
        return real_reply(parsed)

    monkeypatch.setattr(transport, "_application_reply_payload", reply_with_e4_for_node_04_status)
    transport.written.clear()

    ok = await coordinator.async_set_setpoint(0x04, 30.0)
    assert ok is True

    snap = coordinator.heaters[0x04].last_snapshot
    assert snap is not None
    assert snap.setpoint_c == 26.0
    assert snap.mode_code == 2
    assert (snap.boost_end_day, snap.boost_end_min) == (6, 1187)

    state = hass.states.get("climate.master_bedroom_heater")
    assert state.attributes["temperature"] == 26.0


async def test_failed_command_does_not_issue_a_status_request(
    hass, monkeypatch, enable_custom_integrations
):
    """The status request is gated on the command actually being accepted
    (result.ok): a heater that never acked the frame has nothing to read
    back, so no F3 B8 follows and the stale snapshot is left alone."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    original_send_frame = coordinator._nanocul.send_frame

    def _refuse_once(dst, air_bytes, *args, **kwargs):
        if dst == 0x04:
            from termoweb_local.nanocul import AckResult

            return AckResult(ok=False, attempts=1)
        return original_send_frame(dst, air_bytes, *args, **kwargs)

    monkeypatch.setattr(coordinator._nanocul, "send_frame", _refuse_once)
    transport.written.clear()

    ok = await coordinator.async_set_setpoint(0x04, 25.5)
    assert ok is False

    status_frames = [f for f in _sent_frames(transport) if f.payload == bytes([0xB8]) and f.dst == 0x04]
    assert status_frames == []


async def test_command_status_request_is_its_own_locked_hold(
    hass, monkeypatch, enable_custom_integrations
):
    """The locking contract for docs/80-handover.md list C item 10 step 1:
    the status request runs as a SEPARATE _run_locked hold from the
    send-and-wait-for-processed one, not folded into it --
    TermowebLocalCoordinator._async_send_confirm_and_refresh's own docstring
    is where that choice is justified; this asserts it is actually what the
    code does, not just what it says."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    descriptions: list[str] = []
    original_run_locked = coordinator._run_locked

    async def _recording_run_locked(func, *args, timeout, description):
        descriptions.append(description)
        return await original_run_locked(func, *args, timeout=timeout, description=description)

    monkeypatch.setattr(coordinator, "_run_locked", _recording_run_locked)
    descriptions.clear()

    ok = await coordinator.async_set_setpoint(0x04, 25.5)
    assert ok is True

    # Two separate holds, in order: the send-and-wait-for-processed one,
    # then the status request one, each its own _run_locked call rather than
    # one call covering both (a background reader read_events() could land
    # between them; that is by design, see the docstring above).
    relevant = [d for d in descriptions if "node 04" in d]
    assert relevant == ["setpoint to node 04", "status request to node 04"]


def _lift_io_timeouts(monkeypatch):
    """A test that jumps a frozen clock by minutes at a time would expire
    every asyncio.wait_for deadline that happens to be in flight when it
    jumps -- above all the reader loop's own read_events, which is always in
    flight -- and the coordinator would spend the test reconnecting instead of
    scheduling. The two executor timeouts are lifted so the clock jump drives
    only the scheduler under test."""
    monkeypatch.setattr(coordinator_module, "READ_TIMEOUT_S", 1e6)
    monkeypatch.setattr(coordinator_module, "COMMAND_TIMEOUT_S", 1e6)


async def _settle_frozen(hass, predicate, timeout=3.0, step=0.02):
    """_wait_until for a test that has frozen the clock. asyncio.sleep never
    returns while hass.loop.time() is frozen, so the wait runs in an executor
    thread on real time and comes back through call_soon_threadsafe, which
    needs no timer; the frozen clock is left exactly where the test put it."""
    waited = 0.0
    while not predicate():
        if waited >= timeout:
            raise AssertionError("condition not met before timeout")
        await hass.async_add_executor_job(time.sleep, step)
        await hass.async_block_till_done()
        waited += step


async def test_an_unsolicited_report_does_not_postpone_the_scheduled_refresh(
    hass, monkeypatch, enable_custom_integrations, freezer
):
    """DataUpdateCoordinator.async_set_updated_data is documented as "manually
    update data, notify listeners and reset refresh interval", and in HA
    2026.9.1 it really does call _async_unsub_refresh() then _schedule_refresh()
    for a fresh, whole update_interval rather than the remainder. A heater's
    own unsolicited report is not a refresh and must not move that timer: with
    DEFAULT_POLL_INTERVAL_S at 300 s and the documented idle report period also
    300 s (docs/PROTOCOL.md section 6), one reporting heater on its own pushes
    the timer out of reach for good.

    A report lands at t=250 here, 50 s before the refresh is due. If it resets
    the timer the next refresh is at t=550 and nothing runs by t=350."""
    _lift_io_timeouts(monkeypatch)
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    refreshes: list[int] = []

    async def _counting_update():
        refreshes.append(1)
        return coordinator.heaters

    monkeypatch.setattr(coordinator, "_async_update_data", _counting_update)

    freezer.tick(250)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert refreshes == []  # not due yet, at 300 s

    # Setup's own F3 B8 sweep already recorded a snapshot, so the report has
    # to be waited for on something only it produces.
    coordinator.heaters[0x04].last_snapshot = None
    transport.queue_line(f"RX 1000 -44.0 60 0 {E5_OFF_REPORT_HEX}")
    await _settle_frozen(
        hass, lambda: coordinator.heaters[0x04].last_snapshot is not None
    )

    freezer.tick(100)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert refreshes, (
        "the scheduled refresh was pushed past its own interval by an "
        "unsolicited report"
    )


async def test_the_energy_sweep_still_runs_under_a_continuous_report_stream(
    hass, monkeypatch, enable_custom_integrations, freezer
):
    """The consequence of the timer reset above, and the acceptance criterion
    as code. Everything periodic hangs off _async_update_data: the hourly
    energy sweep, the per-heater F3 B8 status refresh and the daily clock
    sync. Two heaters reporting on their own cadence reset the 300 s timer
    often enough that it never expires, so the energy counters stop moving
    even while the heaters are consuming.

    Two hours of one report every 50 s here, counting only the sweeps that
    happen after setup: setup's own sweep runs unconditionally, since its
    _last_energy_poll is None, and it is exactly the one sweep the starved
    timer allows. The two due at t=3600 and t=7200 are the ones that never
    happen, and they are the whole test."""
    _lift_io_timeouts(monkeypatch)
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    reads: list[int] = []

    def _read_energy(node_id, timeout=0):
        reads.append(node_id)
        return 1000 + 100 * len(reads)

    monkeypatch.setattr(coordinator.network, "read_energy", _read_energy)

    for _step in range(144):  # 144 x 50 s = 7200 s
        freezer.tick(50)
        coordinator.heaters[0x04].last_snapshot = None
        transport.queue_line(f"RX 1000 -44.0 60 0 {E5_OFF_REPORT_HEX}")
        await _settle_frozen(
            hass, lambda: coordinator.heaters[0x04].last_snapshot is not None
        )
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        # A refresh this step started runs its own radio round trips, and
        # _unsub_refresh is None for as long as it is in flight: wait for the
        # next one to be scheduled before moving the clock again, or the tick
        # lands mid-refresh and the cadence under test is the test harness's
        # rather than the coordinator's.
        await _settle_frozen(hass, lambda: coordinator._unsub_refresh is not None)

    for node_id in coordinator.heaters:
        assert reads.count(node_id) >= 2, (
            f"node {node_id:02x} was swept for energy {reads.count(node_id)} time(s) "
            "in two hours of reports"
        )


async def test_coordinator_confirms_report_with_5755(hass, monkeypatch, enable_custom_integrations):
    """The coordinator sends F2 57 55 after every E5/E2 report it receives
    (2026-09-06 proof, notes.md 13:18:42Z and f3-and-edges-results.md section 1):
    it is a report confirmation, not a poll, and replaces the old poll-cadence
    use of 57 55."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    transport.written.clear()

    transport.queue_line(f"RX 1000 -44.0 60 0 {E5_OFF_REPORT_HEX}")
    await _wait_until(
        lambda: coordinator.heaters[0x04].last_snapshot is not None
        and coordinator.heaters[0x04].last_snapshot.room_temp_c == 23.1
    )

    confirm_air = coordinator.network.confirm_report(0x04)
    expected = b"T" + confirm_air.hex().upper().encode() + b"\n"
    await _wait_until(lambda: expected in transport.written)


async def test_coordinator_poll_now_sends_f3_b8_status_request(hass, monkeypatch, enable_custom_integrations):
    """poll_now (and the periodic refresh) send an F3 B8 status request, not the
    old F2 57 55 poll (2026-09-06 proof, notes.md 13:18:42Z: 57 55 alone elicits
    no report at all)."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    transport.written.clear()

    await coordinator.async_poll_now(0x04)

    sent = [w for w in transport.written if w.startswith(b"T")]
    assert len(sent) == 1
    parsed = tf.parse_frame(bytes.fromhex(sent[0].strip()[1:].decode()))
    assert parsed.payload == bytes([0xB8])
    assert coordinator.heaters[0x04].last_snapshot is not None


async def test_energy_read_logs_the_counter_at_debug_level(
    hass, monkeypatch, enable_custom_integrations, caplog
):
    """A successful F3 BC energy read (docs/80-handover.md section 3 list C item
    3) must leave a trace in the log: without one, "the hourly energy counters
    returned and non-decreasing per heater" can only be evidenced from HA sensor
    history, not from the capture itself."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    coordinator.heaters[0x04].last_energy_wh = None  # isolate from setup's own initial poll
    monkeypatch.setattr(coordinator.network, "read_energy", lambda node_id, timeout=0: 1000)
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        await coordinator._async_request_energy(0x04)

    assert coordinator.heaters[0x04].last_energy_wh == 1000
    assert "energy reply (EF) from node 04: 1000 Wh" in caplog.text
    assert "delta" not in caplog.text


async def test_energy_read_logs_the_delta_since_the_previous_reading(
    hass, monkeypatch, enable_custom_integrations, caplog
):
    """The acceptance check's own criterion is non-decreasing counters, so the
    delta between consecutive reads is worth logging once a previous reading
    exists (docs/80-handover.md section 3 list C item 3)."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    monkeypatch.setattr(coordinator.network, "read_energy", lambda node_id, timeout=0: 1000)
    await coordinator._async_request_energy(0x04)

    monkeypatch.setattr(coordinator.network, "read_energy", lambda node_id, timeout=0: 1500)
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        await coordinator._async_request_energy(0x04)

    assert coordinator.heaters[0x04].last_energy_wh == 1500
    assert "energy reply (EF) from node 04: 1500 Wh (delta +500 Wh)" in caplog.text


async def test_status_request_logs_the_reply_and_its_latency(
    hass, monkeypatch, enable_custom_integrations, caplog
):
    """docs/80-handover.md list C item 6: the coordinator's request path
    (F3 B8) logged neither the E6 reply nor its latency, so a
    setpoint-latency acceptance run could only infer the confirmation from
    the absence of a reconciliation warning, never see it directly."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    fake_snapshot = types.SimpleNamespace(received_at=time.time())
    monkeypatch.setattr(
        coordinator.network, "request_status", lambda node_id, timeout=0: fake_snapshot
    )
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        await coordinator._async_request_status(0x04)

    assert coordinator.heaters[0x04].last_snapshot is fake_snapshot
    assert "status reply (E6) from node 04: request B8, payload 14 bytes" in caplog.text
    assert " ms" in caplog.text


async def test_status_request_logs_the_full_reply_payload_hex(
    hass, monkeypatch, enable_custom_integrations, caplog
):
    """handover section 3 list C item 10: the status-reply debug line used to
    stop at payload length; it must also carry the reply's own bytes in full
    (not the rx #N line's truncated 16, which cuts E2/E3/E4/9E/9F/C9/DA), so
    a captured log alone tells you what the heater actually reported. Real
    radio round trip through FakeCulTransport's own fixed E6 reply
    (conftest.py _STATUS_REPLY_PAYLOAD), not the monkeypatched snapshot the
    test above uses, so Network.request_status's own payload caching runs."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        await coordinator._async_request_status(0x04)

    assert "b90e2e2f0400f7321d4200002300" in caplog.text


async def test_status_request_logs_a_timeout(
    hass, monkeypatch, enable_custom_integrations, caplog
):
    """The timeout side of the same fix: no reply is a fact worth a log line
    too, not silence."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    monkeypatch.setattr(
        coordinator.network, "request_status", lambda node_id, timeout=0: None
    )
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        await coordinator._async_request_status(0x04)

    assert "no status reply (E6) from node 04 within timeout (request B8," in caplog.text


async def test_link_keepalive_sends_eb_to_each_heater_after_the_interval(
    hass, monkeypatch, enable_custom_integrations, freezer
):
    """docs/captures/2026-09-13-x19/link-test.md: heater 04's panel raised the
    lost-gateway LINK indication after a few minutes with no EB frame at all
    from the station, cleared by a single EB clock-sync frame, and the
    Tevolve gateway itself sent one about every 157s. The periodic refresh
    now resends a steady (0x52) EB to every heater once
    LINK_KEEPALIVE_INTERVAL_S has passed since its last one -- on top of, not
    instead of, the once-a-day full clock sync, whose own interval this does
    not touch."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    def _eb_frames_sent():
        frames = []
        for written in transport.written:
            if not written.startswith(b"T"):
                continue
            payload = tf.parse_frame(bytes.fromhex(written.strip()[1:].decode())).payload
            if payload[:1] == b"\x52":
                frames.append(payload)
        return frames

    # Setup's own startup clock sync just sent every heater a fresh EB, so a
    # refresh straight after it must not send another.
    transport.written.clear()
    await coordinator._async_update_data()
    assert _eb_frames_sent() == []

    freezer.tick(coordinator_module.LINK_KEEPALIVE_INTERVAL_S + 10)
    transport.written.clear()
    await coordinator._async_update_data()
    assert len(_eb_frames_sent()) == len(coordinator.heaters)


async def test_link_keepalive_survives_a_closed_port(
    hass, monkeypatch, enable_custom_integrations, caplog
):
    """docs/80-handover.md list C item 19: a bridge outage during a reconnect
    made _async_send_link_keepalives hit a closed port and raise
    serial.SerialException, which _async_update_data had no guard for, so it
    propagated all the way out as DataUpdateCoordinator's own "Unexpected
    error fetching termoweb_local data" (2026-09-15 20:09:04 local) instead
    of the debug-level, reconnect-owns-the-port handling the reader loop
    already gives the same exception. _async_update_data itself (not just
    the keepalive method directly) is what a live refresh actually calls, so
    that is what this asserts survives.

    _last_eb_sent is backdated directly rather than via the freezer fixture:
    freezer.tick jumps the coordinator's real background watchdog/reader
    tasks forward too, which chased its own reconnect independently of the
    one this test cares about and made the test both slow and flaky. Direct
    backdating only touches the one piece of state
    _async_send_link_keepalives actually reads.

    Only the EB clock-sync frame fails the port; the status request
    _async_update_data also sends this same cycle (_async_request_status)
    goes through untouched, since a closed port there is a different,
    pre-existing gap (_async_request_status catches only
    asyncio.TimeoutError) that list C item 19 does not ask this fix to
    cover -- conflating the two would make this test fail for a reason
    outside this fix's own scope."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    original_send_frame = coordinator._nanocul.send_frame
    eb_prefixes = (
        bytes([network_module.EB_CLOCK_STEADY_PREFIX]),
        bytes([network_module.EB_CLOCK_REGISTRATION_PREFIX]),
    )

    def _closed_port(node_id, air):
        if tf.parse_frame(air).payload[:1] in eb_prefixes:
            raise serial.SerialException("Attempting to use a port that is not open")
        return original_send_frame(node_id, air)

    stale_at = dt_util.utcnow() - dt.timedelta(seconds=coordinator_module.LINK_KEEPALIVE_INTERVAL_S + 10)
    for node_id in coordinator.heaters:
        coordinator._last_eb_sent[node_id] = stale_at

    monkeypatch.setattr(coordinator._nanocul, "send_frame", _closed_port)
    try:
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            await coordinator._async_update_data()  # must not raise
    finally:
        monkeypatch.setattr(coordinator._nanocul, "send_frame", original_send_frame)

    assert "link keepalive sweep stopped by a closed port" in caplog.text
    assert "Unexpected error fetching" not in caplog.text


async def test_status_request_survives_a_closed_port(
    hass, monkeypatch, enable_custom_integrations, caplog
):
    """_async_request_status caught only asyncio.TimeoutError, so the same
    closed-port race the link keepalive sweep already survives (a reconnect
    closing the transport out from under an in-flight send) reached this
    call site uncaught instead, propagating out of _async_update_data as
    DataUpdateCoordinator's own "Unexpected error fetching termoweb_local
    data" failure on every heater in the refresh's status loop, not just the
    one whose EB collided with the reconnect."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    original_send_frame = coordinator._nanocul.send_frame
    status_payload = bytes([network_module.STATUS_OPCODE])

    def _closed_port(node_id, air):
        if tf.parse_frame(air).payload == status_payload:
            raise serial.SerialException("Attempting to use a port that is not open")
        return original_send_frame(node_id, air)

    monkeypatch.setattr(coordinator._nanocul, "send_frame", _closed_port)
    try:
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            await coordinator._async_update_data()  # must not raise
    finally:
        monkeypatch.setattr(coordinator._nanocul, "send_frame", original_send_frame)

    assert "status request to node 04 stopped by a closed port" in caplog.text
    assert "Unexpected error fetching" not in caplog.text


async def test_daily_clock_sync_survives_a_closed_port(
    hass, monkeypatch, enable_custom_integrations, caplog
):
    """_async_sync_clock_all is the other branch of the same daily-sync/
    keepalive choice _async_send_link_keepalives already covers, and had no
    guard at all around its own send: the identical reconnect-closes-the-
    transport race reaches it on whichever refresh lands on the daily
    boundary instead of the keepalive cadence, and was still uncaught."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    coordinator._last_clock_sync = dt_util.utcnow() - dt.timedelta(
        seconds=coordinator_module.CLOCK_SYNC_INTERVAL_S + 10
    )

    original_send_frame = coordinator._nanocul.send_frame
    eb_prefixes = (
        bytes([network_module.EB_CLOCK_STEADY_PREFIX]),
        bytes([network_module.EB_CLOCK_REGISTRATION_PREFIX]),
    )

    def _closed_port(node_id, air):
        if tf.parse_frame(air).payload[:1] in eb_prefixes:
            raise serial.SerialException("Attempting to use a port that is not open")
        return original_send_frame(node_id, air)

    monkeypatch.setattr(coordinator._nanocul, "send_frame", _closed_port)
    try:
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            await coordinator._async_update_data()  # must not raise
    finally:
        monkeypatch.setattr(coordinator._nanocul, "send_frame", original_send_frame)

    assert "daily clock sync sweep stopped by a closed port" in caplog.text
    assert "Unexpected error fetching" not in caplog.text


async def test_power_request_push_draws_a_granted_verdict(
    hass, monkeypatch, enable_custom_integrations
):
    """PROTOCOL.md 5.1: a heater's own `F1 BE <power hi> <power lo>` push asks
    the gateway's power manager for permission to draw, and the gateway's
    only reply is `F2 BF 01` -- unanswered, this is today's live finding
    behind heater 04 flashing LINK about 12 minutes after its last EB
    (docs/captures/2026-09-13-x19/link-test.md). Worked frame from
    PROTOCOL.md 5.1's own example, 790.0 W (`BE 1E DC`)."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    assert entry.runtime_data is not None
    transport.written.clear()

    air = tf.build_frame(0x04, 0x01, bytes([0xBE, 0x1E, 0xDC]), hops=(1, 1, 1))
    transport.queue_line(f"RX 1000 -44.0 60 0 {air.hex().upper()}")

    await _wait_until(
        lambda: any(
            frame.dst == 0x04 and frame.payload == bytes([0xBF, 0x01])
            for frame in _sent_frames(transport)
        )
    )


async def test_program_read_logs_the_reply_class_and_latency(
    hass, monkeypatch, enable_custom_integrations, caplog
):
    """docs/80-handover.md list C item 6, extended to the F3 B0 program read:
    the reply class (9F or C9, PROTOCOL.md 5.6) follows the length of the
    last record written to that heater, so the log line names it rather than
    assuming one."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    fake_record = network_module.ProgramRecord(
        resolution=network_module.SLOTS_PER_DAY_HALF_HOURLY,
        slots=[0] * (network_module.SLOTS_PER_DAY_HALF_HOURLY * network_module.DAYS_PER_WEEK),
        hourly=[0] * 168,
        raw=b"\x00" * 84,
    )
    monkeypatch.setattr(
        coordinator.network, "read_program", lambda node_id, timeout=0: (fake_record.hourly, fake_record.raw)
    )
    monkeypatch.setattr(coordinator.network, "program_record", lambda node_id: fake_record)
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        await coordinator.async_read_program(0x04)

    assert "program reply (9F) from node 04: request B0, payload 85 bytes" in caplog.text
    assert " ms" in caplog.text


async def test_coordinator_replies_to_registration_open_with_registration_clock(
    hass, monkeypatch, enable_custom_integrations
):
    """2026-09-06 17:08:20Z re-registration capture, notes.md: a heater's own
    opening F3 frame (payload `50`) is answered with an EB clock sync using
    the registration prefix (`51`), not the station-start F3 query burst."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    transport.written.clear()

    open_air = tf.build_frame(0x04, 0x01, bytes([0x50]), hops=(1, 1, 1))
    transport.queue_line(f"RX 1000 -44.0 60 0 {open_air.hex().upper()}")

    await _wait_until(
        lambda: any(f.payload[:1] == bytes([0x51]) for f in _sent_frames(transport))
    )
    registration_clock = next(f for f in _sent_frames(transport) if f.payload[:1] == bytes([0x51]))
    assert registration_clock.dst == 0x04


async def test_coordinator_confirms_and_stores_9e_program_report(
    hass, monkeypatch, enable_custom_integrations
):
    """2026-09-06 17:08:20Z capture: the 9E program report pushed during a
    registration reply is stored exactly like a fresh F3 B0 read-back and
    confirmed with F2 57 55, like every other report."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    transport.written.clear()

    raw_nibbles = bytes.fromhex("555555500000000000000055" * 7)  # 84 bytes, every day uniform
    program_report_air = tf.build_frame(
        0x04, 0x01, bytes([0x56, 0xB1]) + raw_nibbles, hops=(1, 1, 1)
    )
    transport.queue_line(f"RX 1000 -44.0 60 0 {program_report_air.hex().upper()}")

    # startup_sequence's own F3 B0 read already populated the cache with all
    # zeros (FakeCulTransport's default nibbles) during setup, so wait for
    # this report's own value specifically rather than merely "is not None".
    await _wait_until(lambda: (coordinator.get_prog(0x04) or [None])[0] == 1)
    assert coordinator.get_prog(0x04)[0:7] == [1, 1, 1, 1, 1, 1, 1]

    confirm_air = coordinator.network.confirm_report(0x04)
    expected = b"T" + confirm_air.hex().upper().encode() + b"\n"
    await _wait_until(lambda: expected in transport.written)


async def test_coordinator_stores_a_c9_program_frame_at_two_bits_per_hour(
    hass, monkeypatch, enable_custom_integrations
):
    """A C9 program frame reaching the reader loop is heaters 02 and 03's own
    F3 B0 reply class (PROTOCOL.md 5.6), so it is cached like any read-back,
    decoded at 2 bits per hour rather than as nibble pairs. It carries no `56`
    report marker and the gateway is never seen confirming one, so no F2 57 55
    goes out for it."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    transport.written.clear()

    # Day-major, day 0 Sunday, day 1 Monday (PROTOCOL.md 5.6/5.7, X-17 day-order
    # fix): Sunday's own 24 hours, then Monday's, then five identical Tue-to-Sat
    # days. Monday's block (the one this test reads back via get_prog(), which
    # rotates wire order to this project's Monday-first HA surface order) is
    # 6h cold, 3h night, 11h day, 2h night, 2h cold.
    c9_payload = bytes.fromhex(
        "b10001aaaaaa5000056aaaaa5000016aaaaa5000016aaaaa"
        "5000016aaaaa5000016aaaaa5000016aaaaa50"
    )
    c9_air = tf.build_frame(0x04, 0x01, c9_payload, hops=(1, 1, 1))
    transport.queue_line(f"RX 1000 -44.0 60 0 {c9_air.hex().upper()}")

    # setup's own F3 B0 read left the cache at all-zero slots, so wait for this
    # frame's own first non-zero hour rather than merely "is not None".
    await _wait_until(lambda: (coordinator.get_prog(0x04) or [0])[6] == 1)
    prog = coordinator.get_prog(0x04)
    assert len(prog) == 168
    assert None not in prog
    assert prog[0:24] == [0] * 6 + [1] * 3 + [2] * 11 + [1] * 2 + [0] * 2

    confirm_air = coordinator.network.confirm_report(0x04)
    assert b"T" + confirm_air.hex().upper().encode() + b"\n" not in transport.written


async def test_coordinator_confirms_ea_report_without_decoding_it(
    hass, monkeypatch, enable_custom_integrations
):
    """2026-09-06 17:08:20Z capture: an EA report (still undecoded,
    PROTOCOL.md section 9) still gets a plain F2 57 55 confirmation."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    transport.written.clear()

    ea_payload = bytes.fromhex("56C7040000001400000000")
    ea_air = tf.build_frame(0x04, 0x01, ea_payload, hops=(1, 1, 1))
    transport.queue_line(f"RX 1000 -44.0 60 0 {ea_air.hex().upper()}")

    confirm_air = coordinator.network.confirm_report(0x04)
    expected = b"T" + confirm_air.hex().upper().encode() + b"\n"
    await _wait_until(lambda: expected in transport.written)


async def test_pair_button_opens_window_and_e7_creates_heater_with_notification(
    hass, monkeypatch, enable_custom_integrations
):
    """Owner direction 2026-09-06 task 3: the "Pair heater" button opens the
    discovery window; an E7 identity announcement seen during that window is
    assigned an id, registered with entities created immediately (no
    reload), and raises a persistent notification naming the assigned id."""
    from homeassistant.components import persistent_notification

    transport = FakeCulTransport()
    entry = await setup_entry(
        hass, monkeypatch, transport, heaters=[{CONF_HEATER_ID: 0x02, CONF_HEATER_NAME: "Living room"}]
    )
    coordinator = entry.runtime_data

    await hass.services.async_call(
        "button", "press", {"entity_id": "button.termoweb_local_pair_heater"}, blocking=True
    )
    assert coordinator.network.discovery_active() is True

    identity = bytes.fromhex("A1B2C3D4E5F6071829304A5B")
    e7_air = tf.build_frame(0xFF, coordinator._station_id, bytes([0x77]) + identity, hops=(1, 1, 1))
    transport.queue_line(f"RX 5000 -44.0 60 0 {e7_air.hex().upper()}")

    # Node 3 is the lowest free id: 2 is already configured, and its own
    # identity is known from its startup sequence, so an announcement from a
    # different identity is safe to give a fresh id.
    await _wait_until(lambda: 0x03 in coordinator.heaters)
    assert entry.options[CONF_HEATER_IDENTITIES]["3"] == identity.hex()
    assert entry.runtime_data is coordinator  # no reload

    # The enrolment burst is scheduled, not run inline: the port stays free
    # for the heater's own announcements (PAIRED_ENROLMENT_DELAY_S).
    assert coordinator.get_prog(0x03) is None
    async_fire_time_changed(
        hass, dt_util.utcnow() + dt.timedelta(seconds=PAIRED_ENROLMENT_DELAY_S + 1)
    )
    await _wait_until(lambda: coordinator.get_prog(0x03) is not None)

    notifications = persistent_notification._async_get_or_create_notifications(hass)
    assert any("03" in n["message"] for n in notifications.values())

    state = hass.states.get("climate.heater_03")
    assert state is not None


async def test_device_removal_drops_heater_from_options_and_live_tables(
    hass, monkeypatch, enable_custom_integrations
):
    """Owner direction 2026-09-06 task 4: deleting a heater's device from the
    HA UI (async_remove_config_entry_device) drops it from persisted options
    and this coordinator's live tables, with no reload."""
    from homeassistant.helpers import device_registry as dr

    from custom_components.termoweb_local import async_remove_config_entry_device

    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, f"{coordinator.dev_id}:4"), entry.entry_id
    )
    assert device is not None

    removable = await async_remove_config_entry_device(hass, entry, device)
    assert removable is True
    await hass.async_block_till_done()

    assert 0x04 not in coordinator.heaters
    assert {h[CONF_HEATER_ID] for h in entry.options[CONF_HEATERS]} == {0x02}
    assert entry.runtime_data is coordinator  # no reload

    gateway_device = device_registry.async_get_device_by_identifier(
        (DOMAIN, coordinator.dev_id), entry.entry_id
    )
    removable_gateway = await async_remove_config_entry_device(hass, entry, gateway_device)
    assert removable_gateway is False


async def test_heater_persists_across_reload(hass, monkeypatch, enable_custom_integrations):
    """Owner direction 2026-09-06 task 2: a heater the scan discovered
    survives a reload (its name kept, and the scan for it just confirms it
    rather than re-adding it)."""
    transport = FakeCulTransport(status_reply_ids={0x02, 0x04, 0x05})
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    assert 0x05 in coordinator.heaters

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    reloaded = entry.runtime_data
    assert reloaded is not coordinator
    assert 0x05 in reloaded.heaters
    assert reloaded.heater_names[0x05] == "Heater 05"
    # A restart's own scan only confirms an already-known id, it does not
    # add a second copy of it.
    assert list(entry.options[CONF_HEATERS]).count({CONF_HEATER_ID: 0x05, CONF_HEATER_NAME: "Heater 05"}) == 1


# ---- 2026-09-06 live stall postmortem: logging, timeouts, watchdog, reconnect ----


class _BlockedReadTransport:
    """A transport whose readline() blocks forever until close() is called,
    then raises -- simulating a wedged pyserial socket:// read that only
    unblocks when the socket itself is closed out from under it (2026-09-06
    peer review of the live stall: "a pyserial socket:// read that never
    returns keeps the lock forever... closing the socket unblocks the stuck
    read, which then raises inside the executor and must be swallowed and
    logged")."""

    def __init__(self) -> None:
        self._closed_event = threading.Event()
        self.written: list[bytes] = []
        self.closed = False

    def readline(self) -> bytes:
        self._closed_event.wait()
        raise OSError("socket closed")

    def write(self, data: bytes) -> int:
        self.written.append(data)
        return len(data)

    def reset_input_buffer(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True
        self._closed_event.set()


async def test_scan_registers_three_heaters_from_ids_2_3_4(
    hass, monkeypatch, enable_custom_integrations
):
    """Owner direction 2026-09-06 task 2, exercised at the width the live
    failure needed (heaters 03 and 04, which report every 300 s and answer
    F3 B8, were never registered): a fresh entry with no configured heaters
    at all still discovers and registers all three of 2, 3 and 4 from the
    setup-time scan alone."""
    transport = FakeCulTransport(status_reply_ids={0x02, 0x03, 0x04})
    entry = await setup_entry(hass, monkeypatch, transport, heaters=[])
    coordinator = entry.runtime_data

    assert set(coordinator.heaters) == {0x02, 0x03, 0x04}
    for node_id, hex_id in ((0x02, "02"), (0x03, "03"), (0x04, "04")):
        assert coordinator.heater_names[node_id] == f"Heater {hex_id.upper()}"
        assert coordinator.heaters[node_id].last_snapshot is not None
        assert hass.states.get(f"climate.heater_{hex_id}") is not None


async def test_info_level_frame_log_lines_appear(
    hass, monkeypatch, enable_custom_integrations, caplog
):
    """Task 1: every received frame is summarised at INFO (class/src/dst/
    payload) so this integration's own log -- the only visibility available
    on the affected system, which cannot be switched to DEBUG through the
    API -- actually shows traffic arriving."""
    caplog.set_level(logging.INFO)
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    transport.queue_line(f"RX 1000 -44.0 60 0 {E5_OFF_REPORT_HEX}")
    await _wait_until(
        lambda: coordinator.heaters[0x04].last_snapshot is not None
        and coordinator.heaters[0x04].last_snapshot.room_temp_c == 23.1
    )

    assert "rx #" in caplog.text
    assert "class=" in caplog.text and "src=" in caplog.text and "dst=" in caplog.text


async def test_the_frame_log_line_carries_rssi_and_lqi(
    hass, monkeypatch, enable_custom_integrations, caplog
):
    """Per-link signal margin is the leading explanation for the P6 run's missed
    report windows, and production had never recorded either field, so a whole 24 h
    run could not weigh it without a parallel radio tap. The stick measures both per
    receive event and NanoCul has always parsed them; they now reach the one line the
    acceptance capture reads."""
    caplog.set_level(logging.INFO)
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    transport.queue_line(f"RX 1000 -56.5 41 0 {E5_OFF_REPORT_HEX}")
    await _wait_until(
        lambda: coordinator.heaters[0x04].last_snapshot is not None
        and coordinator.heaters[0x04].last_snapshot.room_temp_c == 23.1
    )

    line = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("rx #") and "class=e5" in record.getMessage()
    )
    assert "rssi=-56.5 lqi=41" in line
    # rssi keeps the stick's own single decimal: the per-node medians this is for are
    # half a dB apart (-44, -56.5, -68 across the three heaters).
    assert "src=04 dst=01 rssi=-56.5 lqi=41 verdict=ok" in line


async def test_the_frame_log_line_prints_dashes_when_there_is_no_measurement(
    hass, monkeypatch, enable_custom_integrations, caplog
):
    """A Piece that carries no measurement prints `--`, the placeholder this line
    already uses for an absent src or dst, rather than an empty field or a zero that
    would read as a real reading of 0 dBm."""
    caplog.set_level(logging.INFO)
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    caplog.clear()

    coordinator._log_stick_event(Piece(0xE5, "ok", ["E5", "9C"], 0x04, 0x01, 1234))

    line = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("rx #")
    )
    assert "rssi=-- lqi=--" in line


async def test_exception_in_event_handling_does_not_stop_the_reader(
    hass, monkeypatch, enable_custom_integrations
):
    """Task 1/4: an exception in _handle_event (which covers the
    registration path too, since registration runs inside it) is logged with
    _LOGGER.exception and swallowed, so the reader loop keeps serving the
    next event instead of dying silently."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    real_handle_event = coordinator._handle_event
    calls = {"n": 0}

    async def _flaky_handle_event(event):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        await real_handle_event(event)

    monkeypatch.setattr(coordinator, "_handle_event", _flaky_handle_event)

    transport.queue_line(f"RX 1000 -44.0 60 0 {E5_OFF_REPORT_HEX}")
    transport.queue_line(f"RX 2000 -44.0 60 0 {E5_OFF_REPORT_HEX}")

    await _wait_until(
        lambda: coordinator.heaters[0x04].last_snapshot is not None
        and coordinator.heaters[0x04].last_snapshot.room_temp_c == 23.1
    )
    assert calls["n"] >= 2


async def test_command_timeout_logs_error_and_reconnects(
    hass, monkeypatch, enable_custom_integrations, caplog
):
    """Task 2: a wait_for expiry around an executor call logs an ERROR naming
    the call and triggers a reconnect, instead of hanging _io_lock forever."""
    monkeypatch.setattr(coordinator_module, "COMMAND_TIMEOUT_S", 0.05)
    monkeypatch.setattr(coordinator_module, "RECONNECT_DEBOUNCE_S", 0.0)

    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    def _hang(*args, **kwargs):
        time.sleep(1.0)

    monkeypatch.setattr(coordinator._nanocul, "send_frame", _hang)

    with caplog.at_level(logging.ERROR):
        result = await coordinator._send_frame(0x04, b"\x00")

    assert result.ok is False
    assert "no response" in caplog.text
    assert coordinator.reconnect_count >= 1
    # The reconnect reopened against the same fake transport with a fresh
    # NanoCul, so a normal command now succeeds again.
    ok = await coordinator.async_sync_clock_one(0x04)
    assert ok is True


async def test_watchdog_does_not_reconnect_at_91s_with_idle_heaters(
    hass, monkeypatch, enable_custom_integrations
):
    """Task 1 (thresholds revised 2026-09-06 after the live postmortem): once
    at least one heater is known, the shorter 90 s threshold no longer
    applies at all -- only the 600 s one does, and even that only reconnects
    when the last command attempt also failed. 91 s of silence right after a
    successful setup-time scan, with the last status poll having succeeded,
    must not reconnect (the exact failure mode of the old time-window-based
    90 s threshold, which fired on perfectly healthy, merely idle heaters)."""
    monkeypatch.setattr(coordinator_module, "WATCHDOG_INTERVAL_S", 0.05)

    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    assert coordinator._last_command_ok is True

    monkeypatch.setattr(coordinator, "gateway_last_frame_at", lambda: time.time() - 91)
    await asyncio.sleep(0.3)

    assert coordinator.reconnect_count == 0


async def test_watchdog_reconnects_after_a_stall_when_a_command_also_failed(
    hass, monkeypatch, enable_custom_integrations
):
    """Task 1: with at least one heater known, a stall past the 600 s
    threshold only reconnects when the last command attempt (any send)
    itself failed or timed out."""
    monkeypatch.setattr(coordinator_module, "WATCHDOG_INTERVAL_S", 0.05)
    monkeypatch.setattr(coordinator_module, "RECONNECT_DEBOUNCE_S", 0.0)
    monkeypatch.setattr(coordinator_module, "WATCHDOG_RECONNECT_COOLDOWN_S", 0.0)

    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    # Force every stall check to see a heater that last reported a very long
    # time ago, deterministically, instead of racing the real clock against
    # WATCHDOG_STALL_S; mark the last command attempt as failed, the
    # condition the 600 s threshold now also requires.
    monkeypatch.setattr(coordinator, "gateway_last_frame_at", lambda: time.time() - 10_000)
    coordinator._note_command_outcome(False)

    await _wait_until(lambda: coordinator.reconnect_count >= 1, timeout=2.0)


async def test_watchdog_rescans_then_reconnects_when_no_heater_known(
    hass, monkeypatch, enable_custom_integrations
):
    """Task 1: with no heater known at all, the 90 s threshold's own action
    is a rescan first, not an immediate reconnect; a reconnect only follows
    when that rescan also finds nothing."""
    monkeypatch.setattr(coordinator_module, "RECONNECT_DEBOUNCE_S", 0.0)
    monkeypatch.setattr(coordinator_module, "WATCHDOG_RECONNECT_COOLDOWN_S", 0.0)
    monkeypatch.setattr(coordinator_module, "INITIAL_SCAN_RETRY_DELAY_S", 0.01)

    transport = FakeCulTransport(status_reply_ids=set())
    entry = await setup_entry(hass, monkeypatch, transport, heaters=[])
    coordinator = entry.runtime_data
    assert coordinator.heaters == {}

    coordinator._connected_at = time.time() - 10_000
    await coordinator._check_stall()

    assert coordinator.heaters == {}
    assert coordinator.reconnect_count >= 1


async def test_watchdog_no_heater_rescan_that_finds_one_skips_reconnect(
    hass, monkeypatch, enable_custom_integrations
):
    """Task 1, the other half of the rescan-first path: when the rescan
    finds a heater after all, the link was fine and no reconnect follows."""
    monkeypatch.setattr(coordinator_module, "RECONNECT_DEBOUNCE_S", 0.0)
    monkeypatch.setattr(coordinator_module, "INITIAL_SCAN_RETRY_DELAY_S", 0.01)

    transport = FakeCulTransport(status_reply_ids=set())
    entry = await setup_entry(hass, monkeypatch, transport, heaters=[])
    coordinator = entry.runtime_data
    assert coordinator.heaters == {}

    transport.status_reply_ids = {0x02}
    coordinator._connected_at = time.time() - 10_000
    await coordinator._check_stall()

    assert 0x02 in coordinator.heaters
    assert coordinator.reconnect_count == 0


async def test_watchdog_reconnects_while_a_read_is_permanently_blocked(
    hass, monkeypatch, enable_custom_integrations
):
    """2026-09-06 peer review of the live stall: a pyserial socket:// read
    that never returns holds _io_lock forever from the reader loop's own
    side (READ_TIMEOUT_S never even gets a chance to fire if the transport
    itself never surfaces an exception). The watchdog's own reconnect must
    still be able to act without ever waiting for that lock: closing the old
    transport out from under the stuck read is what unblocks it, and the
    resulting exception is swallowed and logged by the reader loop, not left
    to crash it."""
    monkeypatch.setattr(coordinator_module, "RECONNECT_DEBOUNCE_S", 0.0)
    good_transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, good_transport)
    coordinator = entry.runtime_data

    blocked_transport = _BlockedReadTransport()
    coordinator._nanocul._transport = blocked_transport
    # Give the reader loop's next read_events() call a moment to actually
    # start blocking inside the executor against blocked_transport.
    await asyncio.sleep(0.1)

    await coordinator._async_reconnect("test: read permanently blocked")

    assert coordinator.reconnect_count >= 1
    assert blocked_transport.closed is True
    assert coordinator._nanocul is not None
    assert coordinator._nanocul._transport is not blocked_transport

    # The reader loop's own stuck call, unblocked by that close(), must have
    # raised and been swallowed rather than killing the loop: the reconnected
    # coordinator still processes a fresh report afterwards.
    transport = good_transport
    transport.queue_line(f"RX 5000 -44.0 60 0 {E5_OFF_REPORT_HEX}")
    await _wait_until(
        lambda: coordinator.heaters[0x04].last_snapshot is not None
        and coordinator.heaters[0x04].last_snapshot.room_temp_c == 23.1
    )


async def test_report_arrives_while_startup_sequence_is_running(
    hass, monkeypatch, enable_custom_integrations
):
    """Task 4: a startup_sequence step holds _io_lock for its own multi-step
    round trip; a report for a different, already-configured heater that
    lands on the wire during that window must not be lost or deadlock the
    reader loop -- it is picked up once the sequence releases the lock."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    transport.written.clear()

    report_payload = tf.parse_frame(bytes.fromhex(E5_OFF_REPORT_HEX)).payload
    report_air = tf.build_frame(0x02, 0x01, report_payload, hops=(1, 1, 1))
    transport.queue_line(f"RX 1000 -44.0 60 0 {report_air.hex().upper()}")

    await coordinator._async_run_startup_sequence(0x04)

    await _wait_until(
        lambda: coordinator.heaters[0x02].last_snapshot is not None
        and coordinator.heaters[0x02].last_snapshot.room_temp_c == 23.1
    )


class _NoResetClearTransport(FakeCulTransport):
    """A transport whose reset_input_buffer() does nothing -- the documented
    pyserial socket:// behaviour (this integration's typical serial_url,
    e.g. ser2net) that task 2's own startup drain exists to work around,
    unlike FakeCulTransport's own reset_input_buffer() (which actually clears
    the queue, matching a real local port).

    Queues two boot-banner lines right behind the Q status line's own reply:
    NanoCul's own _prepare_write() already drains whatever is queued before
    it ever writes anything (including "Q" itself), so anything queued
    ahead of time is consumed by that internal drain, not by the new
    startup drain this test means to exercise -- these two lines have to
    arrive appear only once Q's own reply is already on the wire, exactly
    like a stick still finishing its boot banner right after answering Q."""

    def write(self, data: bytes) -> int:
        result = super().write(data)
        if data.decode(errors="replace").strip() == "Q":
            self.rx_queue.append(b"# booting nanoCUL v1.67 freq=869.525\n")
            self.rx_queue.append(b"# reset ok\n")
        return result

    def reset_input_buffer(self) -> None:
        pass


async def test_start_drain_discards_reset_window_lines(
    hass, monkeypatch, enable_custom_integrations, caplog
):
    """Task 2: whatever the stick printed during its own reset (the boot
    banner here, arriving right behind Q's own reply) is read and discarded
    right after the connect and the Q line, before the clock sync and the
    scan -- not left sitting in the socket to desync them -- and setup still
    completes normally against the same transport afterwards."""
    caplog.set_level(logging.INFO)
    transport = _NoResetClearTransport()

    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    assert "startup drain: discarded 2 line(s)" in caplog.text
    assert set(coordinator.heaters) == {0x02, 0x04}
    assert hass.states.get("climate.living_room_heater") is not None


class _SerialExceptionOnCloseTransport:
    """A transport whose readline() blocks until close() and raises
    serial.PortNotOpenError from then on -- the exact exception the live log
    showed (2026-09-06 follow-up finding): the reader loop's own in-flight
    read against a transport a reconnect just closed out from under it.
    `read_started` says a read is genuinely in flight, so a test can close on
    that rather than on a sleep long enough to hope one has begun."""

    def __init__(self) -> None:
        self._closed_event = threading.Event()
        self.read_started = threading.Event()
        self.written: list[bytes] = []

    def readline(self) -> bytes:
        if self._closed_event.is_set():
            raise serial.PortNotOpenError()
        self.read_started.set()
        self._closed_event.wait()
        raise serial.PortNotOpenError()

    def write(self, data: bytes) -> int:
        self.written.append(data)
        return len(data)

    def reset_input_buffer(self) -> None:
        pass

    def close(self) -> None:
        self._closed_event.set()


async def test_reader_loop_clean_stop_for_serial_exception_during_reconnect(
    hass, monkeypatch, enable_custom_integrations, caplog
):
    """2026-09-06 follow-up finding: closing the old transport during a
    reconnect makes the reader loop's own in-flight read raise
    serial.PortNotOpenError; while a reconnect is already in progress that
    is the expected, clean way that read unblocks and must not log a
    traceback or count as a second reconnect -- only the reconnect already
    under way (never started here) does. Outside of a reconnect the same
    exception is new information the watchdog has not already reacted to,
    so the reader loop reconnects on its own, once."""
    good_transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, good_transport)
    coordinator = entry.runtime_data
    assert coordinator.reconnect_count == 0

    exc_transport = _SerialExceptionOnCloseTransport()
    coordinator._nanocul._transport = exc_transport
    await _wait_until(exc_transport.read_started.is_set, timeout=2.0)

    # Phase 1: simulate a reconnect already under way elsewhere (the
    # watchdog, in the real failure) and let it close the transport out from
    # under the reader loop's own in-flight read.
    caplog.clear()
    with caplog.at_level(logging.INFO):
        coordinator._reconnecting = True
        exc_transport.close()
        await _wait_until(lambda: "reader stopped for reconnect" in caplog.text, timeout=1.0)

    assert "Traceback" not in caplog.text
    assert coordinator.reconnect_count == 0  # no reconnect fired from this branch

    # Phase 2: the same exception outside of a reconnect already in progress
    # is new information -- the reader loop reconnects on its own, exactly
    # once, logging a single summary line with no traceback.
    caplog.clear()
    with caplog.at_level(logging.INFO):
        coordinator._reconnecting = False
        await _wait_until(lambda: coordinator.reconnect_count >= 1, timeout=1.0)

    assert coordinator.reconnect_count == 1
    assert "nanoCUL read failed" in caplog.text
    assert "Traceback" not in caplog.text

    # reconnect_count increments right at the start of _async_reconnect, well
    # before its own clock-sync-and-rescan tail (a full 2-65 sweep) finishes;
    # wait for that whole reconnect to actually settle before relying on the
    # transport it leaves behind.
    await _wait_until(lambda: not coordinator._reconnecting, timeout=5.0)

    # The reconnect reopened against the original (still working)
    # good_transport, per patch_coordinator_nanocul's own factory: the
    # coordinator keeps processing frames normally afterwards.
    good_transport.queue_line(f"RX 5000 -44.0 60 0 {E5_OFF_REPORT_HEX}")
    await _wait_until(
        lambda: coordinator.heaters[0x04].last_snapshot is not None
        and coordinator.heaters[0x04].last_snapshot.room_temp_c == 23.1,
        timeout=5.0,
    )


async def test_reader_loop_parks_while_a_reconnect_owns_the_transport(
    hass, monkeypatch, enable_custom_integrations
):
    """2026-09-09 live postmortem: a reconnect that starts while the reader
    loop is mid-read has to stop that loop reading before the handshake, not
    leave it running as a second consumer of the same socket -- which is what
    the post-reconnect Q banner arriving with characters missing came from.
    Every read the reader loop takes while a reconnect owns the transport is
    counted here, and there must be none."""
    monkeypatch.setattr(coordinator_module, "RECONNECT_DEBOUNCE_S", 0.0)

    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    reader_reads = {"during_reconnect": 0}
    original_run_locked = coordinator._run_locked

    async def _counting_run_locked(func, *args, timeout, description):
        if description == "reader read_events" and coordinator._reconnecting:
            reader_reads["during_reconnect"] += 1
        return await original_run_locked(
            func, *args, timeout=timeout, description=description
        )

    monkeypatch.setattr(coordinator, "_run_locked", _counting_run_locked)

    # Stretch the handshake out so an unparked reader would get several reads
    # in against the new transport before the reconnect was done with it.
    original_configure_sync = coordinator._configure_stick_sync

    def _slow_configure_sync() -> None:
        time.sleep(0.3)
        original_configure_sync()

    monkeypatch.setattr(coordinator, "_configure_stick_sync", _slow_configure_sync)

    await asyncio.sleep(0.05)  # let the reader loop's next read start
    await coordinator._async_reconnect("test: reconnect while the reader is mid-read")

    assert coordinator.reconnect_count == 1
    assert reader_reads["during_reconnect"] == 0
    assert coordinator._port_open is True

    # The reader loop resumed against the reopened NanoCul, not left parked.
    transport.queue_line(f"RX 5000 -44.0 60 0 {E5_OFF_REPORT_HEX}")
    await _wait_until(
        lambda: coordinator.heaters[0x04].last_snapshot is not None
        and coordinator.heaters[0x04].last_snapshot.room_temp_c == 23.1,
        timeout=5.0,
    )


async def test_failed_reopen_backs_off_and_still_recovers(
    hass, monkeypatch, enable_custom_integrations
):
    """2026-09-09 live postmortem: a bridge that is down refuses the socket
    immediately, so a failed reopen used to be retried by the reader loop's
    very next read -- 3578 reconnects during a roughly 6 minute ser2net
    outage, about 600 a minute. Each consecutive failure now doubles the wait,
    and the link still comes back on its own once the transport does, with no
    intervention."""
    monkeypatch.setattr(coordinator_module, "RECONNECT_DEBOUNCE_S", 0.0)
    monkeypatch.setattr(coordinator_module, "RECONNECT_BACKOFF_MIN_S", 0.2)
    monkeypatch.setattr(coordinator_module, "RECONNECT_BACKOFF_MAX_S", 0.4)

    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    state = {"attempts": 0, "refuse": True}
    real_open_nanocul = coordinator._open_nanocul

    def _open_nanocul():
        state["attempts"] += 1
        if state["refuse"]:
            raise serial.SerialException("Attempting to use a port that is not open")
        return real_open_nanocul()

    monkeypatch.setattr(coordinator, "_open_nanocul", _open_nanocul)

    # The bridge drops the socket: every read from here on raises the exact
    # exception the live log showed, and every reopen is refused.
    dead_transport = _SerialExceptionOnCloseTransport()
    dead_transport.close()
    coordinator._nanocul._transport = dead_transport

    await _wait_until(lambda: state["attempts"] >= 2, timeout=5.0)
    await asyncio.sleep(1.0)
    # Unpaced this is a reopen per read cycle for as long as the outage lasts;
    # at 0.2 s doubling to a 0.4 s cap it is a handful, and it never gives up.
    assert 3 <= state["attempts"] <= 8
    assert coordinator._port_open is False

    state["refuse"] = False
    await _wait_until(lambda: coordinator._port_open, timeout=5.0)
    await _wait_until(lambda: not coordinator._reconnecting, timeout=10.0)

    transport.queue_line(f"RX 5000 -44.0 60 0 {E5_OFF_REPORT_HEX}")
    await _wait_until(
        lambda: coordinator.heaters[0x04].last_snapshot is not None
        and coordinator.heaters[0x04].last_snapshot.room_temp_c == 23.1,
        timeout=5.0,
    )


def _e7_line(
    identity: bytes, src: int = 0xFF, dst: int = 0x01, path: bytes | None = None
) -> str:
    """One announcement line as the stick prints it. The default is the
    captured direct announcement's own header: source `FF`, and logical bytes
    6-10 `FF 01 01 01 01`, the announcer as originator, the addressed id next
    and the heater-side `01` padding after it (docs/PROTOCOL.md 5.9). `path`
    overrides those five bytes for an announcement that arrived through a
    relay, whose own header is not in the corpus."""
    if path is None:
        air = tf.build_frame(src, dst, bytes([0x77]) + identity, hops=(1, 1, 1))
    else:
        air = tf.build_frame(src, dst, bytes([0x77]) + identity, path=path)
    return f"RX 5000 -44.0 60 0 {air.hex().upper()}"


def _assignments_to_broadcast(transport: FakeCulTransport) -> list[int]:
    """The node id each F3 assignment frame sent to the pairing broadcast id
    carries (docs/PROTOCOL.md 5.9)."""
    return [
        frame.payload[0]
        for frame in _sent_frames(transport)
        if frame.dst == 0xFF and len(frame.payload) == 1
    ]


def _assignment_frames(transport: FakeCulTransport):
    """Every pairing assignment on the air, whatever it is addressed to: a
    one-byte payload under the pairing tag in header byte 11, which no other
    frame this station sends carries (docs/PROTOCOL.md 5.9)."""
    return [
        frame
        for frame in _sent_frames(transport)
        if len(frame.payload) == 1
        and frame.logical[11] == network_module.DISCOVERY_ASSIGNMENT_TAG
    ]


def _sent_air(transport: FakeCulTransport) -> list[bytes]:
    return [
        bytes.fromhex(w.strip()[1:].decode())
        for w in transport.written
        if w.startswith(b"T")
    ]


# The gateway's own id assignment for the direct announcement, on air, from
# docs/captures/2026-09-06-phase3/nano-rx-162127.log at 17:12:17.407; logical
# `0c 1b 30 01 ff 00 01 ff 00 00 00 04 04` (docs/PROTOCOL.md 5.9).
GATEWAY_ASSIGNMENT_AIR = bytes.fromhex("F39C885848A1CDDB575E4B980A03E7")


async def test_relayed_e7_announcement_is_answered(
    hass, monkeypatch, enable_custom_integrations
):
    """docs/PROTOCOL.md 5.9: an already-paired heater relays the announcement
    to the station under its own source id, so src is that heater's id and
    not the broadcast id. The 2026-09-09 attempt discarded every relayed
    copy."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    identity = identity_reply_payload(0x04)[6:18]
    coordinator.start_pairing(60.0)
    await hass.async_block_till_done()

    transport.queue_line(_e7_line(identity, src=0x02, dst=0x01))

    await _wait_until(lambda: _assignments_to_broadcast(transport) == [0x04])


async def test_swept_e7_announcement_addressed_elsewhere_is_answered(
    hass, monkeypatch, enable_custom_integrations
):
    """docs/PROTOCOL.md 5.9: the announcement sweeps its destination across
    the id range about 200 ms at a time, so most copies are addressed to some
    other id, not to this station."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    identity = identity_reply_payload(0x04)[6:18]
    coordinator.start_pairing(60.0)
    await hass.async_block_till_done()

    transport.queue_line(_e7_line(identity, src=0xFF, dst=0x07))

    await _wait_until(lambda: _assignments_to_broadcast(transport) == [0x04])


async def test_discovery_assignment_goes_out_alone_and_once(
    hass, monkeypatch, enable_custom_integrations
):
    """The assignment has to be on the air while the announcing heater is
    still on the destination that produced it (about 200 ms, docs/PROTOCOL.md
    5.9). The station's own software ack used to go first, spending a whole
    drain, minimum command gap and TX confirmation ahead of it, and the
    assignment then retried three times into a destination the heater had
    already left; the stick's hardware auto-ack covers the ack itself."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    identity = identity_reply_payload(0x04)[6:18]
    coordinator.start_pairing(60.0)
    await hass.async_block_till_done()
    before = len(transport.written)

    transport.queue_line(_e7_line(identity))

    await _wait_until(lambda: _assignments_to_broadcast(transport) == [0x04])
    await asyncio.sleep(network_module.DISCOVERY_SWEEP_DWELL_S)
    sent = [
        tf.parse_frame(bytes.fromhex(w.strip()[1:].decode()))
        for w in transport.written[before:]
        if w.startswith(b"T")
    ]
    assert [frame.dst for frame in sent] == [0xFF]


async def test_direct_announcement_assignment_is_the_captured_broadcast_frame(
    hass, monkeypatch, enable_custom_integrations, caplog
):
    """CAPTURED GROUND TRUTH. The announcement here is the one in
    docs/captures/2026-09-06-phase3/nano-rx-162127.log at 17:12:17, header
    bytes 6-10 included, and the assignment this station answers it with is
    byte for byte the frame the real gateway sent (docs/PROTOCOL.md 5.9).

    Addressing send_frame at DiscoveryResult.assignment_dst instead of at the
    DISCOVERY_BROADCAST_ID constant must not move any of it: assignment_dst is
    `FF` for a direct announcement, so the same frame goes out and the same
    ack, the announcing heater's own while it is still addressed as `FF`, is
    the one waited for. `acked=True` in the log is that wait having matched."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    identity = identity_reply_payload(0x04)[6:18]
    coordinator.start_pairing(60.0)
    await hass.async_block_till_done()
    caplog.clear()
    caplog.set_level(logging.INFO)

    transport.queue_line(_e7_line(identity))

    await _wait_until(lambda: _assignments_to_broadcast(transport) == [0x04])
    assert GATEWAY_ASSIGNMENT_AIR in _sent_air(transport)
    assignment = _assignment_frames(transport)[0]
    assert assignment.logical[:13].hex() == "0c1b3001ff00" "01ff000000" "0404"
    # The pairing log line is written after the ack wait, so it can trail the
    # sent frame under load; wait for it rather than racing it.
    await _wait_until(lambda: "acked=" in caplog.text)
    assert "addressed to ff (broadcast)" in caplog.text
    assert "acked=True" in caplog.text


async def test_relayed_announcement_assignment_is_addressed_to_the_relay(
    hass, monkeypatch, enable_custom_integrations, caplog
):
    """FIRMWARE-DERIVED EXPECTATION, NOT A CAPTURE. No relayed `E7`'s own
    logical bytes 6-11 exist in the corpus: they are open item 1 of
    docs/captures/2026-09-05-gateway-dump/analysis7.md section 11, still open
    because the logs that recorded the relays print class, src, dst and a
    truncated payload and never the on-air header. The announcement below is
    therefore synthesised from
    docs/captures/2026-09-05-gateway-dump/analysis8.md section 4's forwarding
    rule (byte 6 held at the originator, byte 7 at the relay) and the
    expectation is what FUN_400ff6c8 plus FUN_400ff728 would produce from it
    (analysis7.md section 6, divergence 2). Only a tap capture of a real
    relayed announcement, and of a real gateway's answer to it, can make this
    ground truth.

    What is tested here is the wiring, not the rule: the assignment must go to
    the destination Network chose, and the ack waited for must be that
    destination's own. The fake transport acks under the id the frame was
    addressed to, so `acked=True` can only be logged if send_frame was asked
    for the relay's ack; asking for `FF`'s, as this coordinator did before,
    times out and logs `acked=False` even though the frame was delivered."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    identity = identity_reply_payload(0x04)[6:18]
    coordinator.start_pairing(60.0)
    await hass.async_block_till_done()
    caplog.clear()
    caplog.set_level(logging.INFO)

    transport.queue_line(
        _e7_line(identity, src=0x02, dst=0x01, path=bytes([0xFF, 0x02, 0x01, 0x01, 0x01]))
    )

    # Wait on _pairing_window_paired, not on transport.written: the assignment frame
    # is appended to transport.written partway through the executor job that then
    # waits for its ack, so a wait keyed on it can observe the frame before
    # "acked=True" is logged and fail on the ack assertions regardless of whether the
    # relay addressing is correct. _pairing_window_paired is the last thing the path
    # sets, so it covers the whole sequence.
    await _wait_until(lambda: coordinator._pairing_window_paired is True, timeout=10.0)
    assignment = _assignment_frames(transport)[0]
    assert assignment.dst == 0x02  # the relay, not the broadcast id
    assert assignment.logical[6:11] == bytes([0x01, 0x02, 0xFF, 0x00, 0x00])
    assert assignment.payload == bytes([0x04])
    assert _assignments_to_broadcast(transport) == []
    assert "addressed to 02 (relayed)" in caplog.text
    assert "acked=True" in caplog.text
    assert coordinator._pairing_window_paired is True


async def test_every_e7_logs_its_whole_logical_header_answered_or_not(
    hass, monkeypatch, enable_custom_integrations, caplog
):
    """analysis7.md section 11 item 1: the open question is what a heater puts
    in a relayed announcement's logical bytes 6 to 11, and the `rx #N` line
    carries no header bytes at all, so the header has to be logged for every
    copy rather than only for the one this station answers.

    The relayed copy here arrives with no discovery window open, so nothing
    answers it and no assignment goes out; its header still has to reach the
    log. Its path is FIRMWARE-SHAPED, NOT CAPTURED: no relayed E7's own bytes
    6-11 exist in the corpus, which is the whole reason for logging them."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    identity = identity_reply_payload(0x04)[6:18]
    caplog.clear()
    caplog.set_level(logging.INFO)

    transport.queue_line(
        _e7_line(identity, src=0x02, dst=0x01, path=bytes([0xFF, 0x02, 0x01, 0x01, 0x01]))
    )
    await _wait_until(lambda: "pairing: e7 seen" in caplog.text)

    assert (
        "pairing: e7 seen src=02 dst=01 relayed=True originator=ff hops=02010101 tag=00"
        in caplog.text
    )
    assert _assignment_frames(transport) == []

    coordinator.start_pairing(60.0)
    await hass.async_block_till_done()
    transport.queue_line(_e7_line(identity))
    await _wait_until(lambda: _assignments_to_broadcast(transport) == [0x04])
    await _wait_until(lambda: caplog.text.count("pairing: e7 seen") == 2)

    assert (
        "pairing: e7 seen src=ff dst=01 relayed=False originator=ff hops=01010101 tag=00"
        in caplog.text
    )


async def test_a_repeated_e7_header_is_logged_once_at_info_and_then_at_debug(
    hass, monkeypatch, enable_custom_integrations, caplog
):
    """The sweep resends the same header about every 200 ms for as long as the
    window is open, so logging every copy at INFO would push the rest of the
    pairing evidence out of the rolling buffer the acceptance capture reads
    (DISCOVERY_HEADER_LOG_LIMIT). A repeat carries no header the log does not
    already hold, so it drops to DEBUG; it is still logged."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    identity = identity_reply_payload(0x04)[6:18]
    coordinator.start_pairing(60.0)
    await hass.async_block_till_done()
    caplog.clear()
    caplog.set_level(logging.DEBUG)

    transport.queue_line(_e7_line(identity))
    transport.queue_line(_e7_line(identity))
    await _wait_until(lambda: caplog.text.count("pairing: e7 seen") == 2)

    levels = [
        record.levelno
        for record in caplog.records
        if record.getMessage().startswith("pairing: e7 seen")
    ]
    assert levels == [logging.INFO, logging.DEBUG]


async def test_a_heater_paired_through_a_relay_warns_that_enrolment_is_direct(
    hass, monkeypatch, enable_custom_integrations, caplog
):
    """FIRMWARE-DERIVED SHAPE, NOT A CAPTURE, like every relayed E7 here. The
    assignment can travel back through the relay it arrived on, but the
    enrolment burst that follows it is a run of ordinary one-hop commands to
    the new id, each waiting for a reply from that id, and this station keeps
    no next-hop table to route them any other way (docs/PROTOCOL.md 5.9). A
    heater reachable only through a relay is therefore assigned an id and then
    fails to enrol, which has to be said rather than left to look like a
    successful pairing."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    coordinator.start_pairing(60.0)
    await hass.async_block_till_done()
    caplog.clear()
    caplog.set_level(logging.WARNING)

    identity = bytes.fromhex("A1B2C3D4E5F6071829304A5B")
    transport.queue_line(
        _e7_line(identity, src=0x02, dst=0x01, path=bytes([0xFF, 0x02, 0x01, 0x01, 0x01]))
    )

    await _wait_until(lambda: 0x03 in coordinator.heaters)
    assert "node 03 was assigned its id through relay 02" in caplog.text
    assert "sent one hop direct" in caplog.text


async def test_relayed_announcement_with_an_unreversible_path_still_broadcasts(
    hass, monkeypatch, enable_custom_integrations, caplog
):
    """A DELIBERATE DIVERGENCE, NOT THE FIRMWARE RULE, and one this wiring
    must not undo: FUN_400ff880 abandons the assignment when the received path
    holds no route back to this station, where commit 4b85149 falls back to
    the broadcast answer that has actually paired a heater on this bench. The
    coordinator has to send that fallback the way it always sent it, so
    assignment_dst being the broadcast id has to keep addressing the frame,
    and the ack wait, at `FF`.

    The announcement is synthesised the same way as the test above and is
    equally FIRMWARE-DERIVED, NOT A CAPTURE."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    identity = identity_reply_payload(0x04)[6:18]
    coordinator.start_pairing(60.0)
    await hass.async_block_till_done()
    caplog.clear()
    caplog.set_level(logging.INFO)

    transport.queue_line(
        _e7_line(identity, src=0x02, dst=0x01, path=bytes([0xFF, 0x02, 0x00, 0x00, 0x00]))
    )

    await _wait_until(lambda: _assignments_to_broadcast(transport) == [0x04])
    assert GATEWAY_ASSIGNMENT_AIR in _sent_air(transport)
    await _wait_until(lambda: "acked=" in caplog.text)
    assert "addressed to ff (broadcast, relay path not reversed)" in caplog.text
    assert "acked=True" in caplog.text
    # The fallback is deliberate, but it is inert on this route and has to say
    # so: a broadcast is not addressed to the relay that has to carry it.
    assert "no route back to station id 01" in caplog.text
    assert any(record.levelno == logging.WARNING for record in caplog.records)


async def test_a_station_id_other_than_01_warns_that_relayed_pairing_is_inert(
    hass, monkeypatch, enable_custom_integrations, caplog
):
    """CONF_STATION_ID is a free hex field in the config flow, so a station id
    other than `01` is reachable, and it makes every relayed announcement
    unreversible: a heater builds its routing path with the gateway's own `01`
    in it and the scan finds nothing to match (docs/PROTOCOL.md 5.9). The
    fallback pairs a heater announcing directly, so this warns rather than
    fails, but it must not be silent.

    The relayed announcement is FIRMWARE-SHAPED, NOT CAPTURED, like every
    relayed E7 in this file."""
    transport = FakeCulTransport()
    caplog.set_level(logging.WARNING)
    entry = await setup_entry(hass, monkeypatch, transport, station_id="05")
    coordinator = entry.runtime_data
    assert "station id 05 is not the gateway's own 01" in caplog.text

    identity = identity_reply_payload(0x04)[6:18]
    coordinator.start_pairing(60.0)
    await hass.async_block_till_done()
    caplog.clear()

    transport.queue_line(
        _e7_line(identity, src=0x02, dst=0x01, path=bytes([0xFF, 0x02, 0x01, 0x01, 0x01]))
    )

    await _wait_until(lambda: _assignments_to_broadcast(transport) == [0x04])
    await _wait_until(lambda: "no route back to station id 05" in caplog.text)


async def test_a_relay_that_rewrote_byte_6_falls_back_to_the_broadcast_assignment(
    hass, monkeypatch, enable_custom_integrations, caplog
):
    """The failure mode the relayed/direct guard is chosen to fail safe on,
    pinned end to end. The guard reads an announcement as relayed only when
    its link sender differs from the path's own originator in byte 6, so a
    heater firmware that rewrote byte 6 to its own id while relaying would be
    read as direct. What the station then sends is the broadcast assignment it
    has always sent: the fix goes inert, and nothing is addressed at a node
    that never carried the announcement.

    The three real relay frames in docs/captures/2026-09-06-proof/ (analysis8
    section 4: `01 04 01 01 01` from heater 04 forwarding a gateway F1, and
    `04 02 01 01 01` and `04 03 01 01 01` from heaters 02 and 03 relaying
    heater 04's F4 probes) all hold byte 6 at the originator, so this is the
    branch the evidence says should not be taken. It is tested because it is
    the one that runs if that evidence does not carry over to an announcement
    a heater relays, which is heater firmware nobody here has dumped."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    identity = identity_reply_payload(0x04)[6:18]
    coordinator.start_pairing(60.0)
    await hass.async_block_till_done()
    caplog.clear()
    caplog.set_level(logging.INFO)

    transport.queue_line(
        _e7_line(identity, src=0x02, dst=0x01, path=bytes([0x02, 0x01, 0x01, 0x01, 0x01]))
    )

    await _wait_until(lambda: _assignments_to_broadcast(transport) == [0x04])
    assert GATEWAY_ASSIGNMENT_AIR in _sent_air(transport)
    # The pairing log line is written after the ack wait, so it can trail the
    # sent frame under load; wait for it rather than racing it.
    await _wait_until(lambda: "acked=" in caplog.text)
    assert "addressed to ff (broadcast)" in caplog.text
    assert "acked=True" in caplog.text


async def test_pairing_reuses_the_id_of_a_heater_already_on_the_network(
    hass, monkeypatch, enable_custom_integrations
):
    """2026-09-09 bench attempt 1 (docs/PROTOCOL.md 5.9): heater 04 had been
    registered by the scan, which records no identity, so its own
    announcement was treated as a new heater and given the lowest free id,
    05, which it refused. An announcement that cannot be told apart from a
    heater already here gets no id until that heater's identity is read."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    identity = identity_reply_payload(0x04)[6:18]
    # As a scan-registered heater looks: on the network, no identity recorded.
    coordinator.network.known_identities.clear()
    coordinator.network.start_discovery(60.0)

    transport.queue_line(_e7_line(identity))
    await _wait_until(lambda: coordinator.network.known_identities.get(identity) == 0x04)
    assert _assignments_to_broadcast(transport) == []
    assert 0x05 not in coordinator.heaters

    # The next sweep step, one dwell later (docs/PROTOCOL.md 5.9).
    await asyncio.sleep(0.2)
    transport.queue_line(_e7_line(identity, dst=0x09))

    await _wait_until(lambda: _assignments_to_broadcast(transport) == [0x04])
    assert 0x05 not in coordinator.heaters


async def test_pairing_reassigns_a_configured_heaters_own_id_despite_a_stale_duplicate(
    hass, monkeypatch, enable_custom_integrations
):
    """2026-09-15 x07 live incident: heater 04's identity was already mapped
    to node 4 in stored options, but a stale leftover entry mapped the same
    identity to node 6 too (options held it under both '4' and '6'). The
    dict comprehension that used to build Network's known_identities picked
    whichever key iterated last -- here '6', the buggy outcome that actually
    happened on air -- and an announcement from that identity was treated as
    unknown and given the fresh id 06, registering a phantom heater rather
    than reassigning heater 04's own id back to it."""
    transport = FakeCulTransport()
    identity = bytes.fromhex("A1B2C3D4E5F6071829304A5B")
    entry = await setup_entry(
        hass, monkeypatch, transport,
        heaters=THREE_HEATERS,
        options={CONF_HEATER_IDENTITIES: {"4": identity.hex(), "6": identity.hex()}},
    )
    coordinator = entry.runtime_data

    # Loading the duplicated options must resolve to node 4, the configured
    # heater, not node 6, the stale leftover.
    assert coordinator.network.known_identities[identity] == 0x04

    coordinator.start_pairing(60.0)
    await hass.async_block_till_done()
    transport.queue_line(_e7_line(identity))

    await _wait_until(lambda: _assignments_to_broadcast(transport) == [0x04])
    assert 0x06 not in coordinator.heaters
    assert hass.states.get("climate.heater_06") is None


async def test_learning_an_identity_drops_a_stale_duplicate_under_another_id(
    hass, monkeypatch, enable_custom_integrations
):
    """The other half of the 2026-09-15 x07 fix: a stale entry mapping an
    identity to node 6 (its device long since deleted, or a leftover from an
    earlier mis-pairing) must not survive the next time that same identity
    is learned for its real, configured node -- otherwise the duplicate that
    caused the incident just reappears on the next reload."""
    transport = FakeCulTransport()
    identity = identity_reply_payload(0x04)[6:18]
    entry = await setup_entry(
        hass, monkeypatch, transport,
        options={CONF_HEATER_IDENTITIES: {"6": identity.hex()}},
    )
    coordinator = entry.runtime_data

    # Node 4 had no identity of its own at load (only the stale '6' entry
    # existed), so the startup sequence's own identity read for it ran and
    # learned it -- which must prune the stale '6' entry, not merely add '4'.
    await _wait_until(lambda: coordinator.network.known_identities.get(identity) == 0x04)
    assert "6" not in entry.options[CONF_HEATER_IDENTITIES]
    assert entry.options[CONF_HEATER_IDENTITIES]["4"] == identity.hex()


def test_duplicate_identity_map_is_rewritten_at_load(hass, caplog):
    """2026-09-16 residual of commit 5c15b7b: _dedupe_identity_hex used to
    fix known_identities in memory only, so the stored map kept the
    duplicate forever and any unrelated options write (an advanced-setup C4
    write, a heater rename, ...) carried it forward untouched, which is what
    a live host saw the same day -- entry.options still held both '4' and
    '6' after an unrelated write, long after commit 5c15b7b was deployed.
    The fix persists the deduped map once, immediately at coordinator
    construction, with no pairing/E7/radio activity needed to trigger it.
    Built with the coordinator constructor directly (not the full
    setup_entry/async_setup flow) so the startup sequence's own separate
    identity-learning pass -- which answers every configured heater's own
    E0 identity request and would rewrite this same option again -- cannot
    mask whether this specific write actually happened."""
    identity = bytes.fromhex("A1B2C3D4E5F6071829304A5B")
    entry = make_config_entry(
        hass,
        heaters=THREE_HEATERS,
        options={CONF_HEATER_IDENTITIES: {"4": identity.hex(), "6": identity.hex()}},
    )

    caplog.clear()
    with caplog.at_level(logging.INFO):
        coordinator_module.TermowebLocalCoordinator(hass, entry)

    assert entry.options[CONF_HEATER_IDENTITIES] == {"4": identity.hex()}
    assert "stored heater identity map held a duplicate" in caplog.text


def test_identity_map_without_a_duplicate_is_not_rewritten(hass, caplog):
    """The common case (no duplicate) must not touch entry.options at all --
    only a genuine duplicate is worth an unsolicited options write at load."""
    identity = bytes.fromhex("A1B2C3D4E5F6071829304A5B")
    clean_options = {CONF_HEATER_IDENTITIES: {"4": identity.hex()}}
    entry = make_config_entry(hass, heaters=THREE_HEATERS, options=clean_options)

    caplog.clear()
    with caplog.at_level(logging.INFO):
        coordinator_module.TermowebLocalCoordinator(hass, entry)

    assert entry.options[CONF_HEATER_IDENTITIES] == {"4": identity.hex()}
    assert "stored heater identity map held a duplicate" not in caplog.text


def test_dedupe_identity_hex_prefers_the_configured_heater(caplog):
    caplog.set_level(logging.WARNING)
    identity_hex = {"2": "e9f0", "3": "c1d2", "4": "a1b2c3", "6": "a1b2c3"}

    resolved = coordinator_module._dedupe_identity_hex(identity_hex, (2, 3, 4))

    assert resolved == {"2": "e9f0", "3": "c1d2", "4": "a1b2c3"}
    assert "keeping 4" in caplog.text
    assert "dropping 6" in caplog.text


def test_dedupe_identity_hex_keeps_lowest_id_when_none_is_configured(caplog):
    caplog.set_level(logging.WARNING)
    identity_hex = {"6": "a1b2c3", "9": "a1b2c3"}

    resolved = coordinator_module._dedupe_identity_hex(identity_hex, (2, 3, 4))

    assert resolved == {"6": "a1b2c3"}


def test_prune_other_ids_for_identity_drops_the_stale_key():
    identity_hex = {"2": "e9f0", "4": "a1b2c3", "6": "a1b2c3"}

    pruned = coordinator_module._prune_other_ids_for_identity(identity_hex, "a1b2c3", 4)

    assert pruned == {"2": "e9f0", "4": "a1b2c3"}


async def test_startup_sequence_records_each_heater_identity(
    hass, monkeypatch, enable_custom_integrations
):
    """Identity is otherwise only ever learned from an E7 announcement; the
    E0 identity reply the startup sequence already asks for carries the same
    12 bytes (docs/PROTOCOL.md 5.9)."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    assert entry.options[CONF_HEATER_IDENTITIES]["4"] == identity_reply_payload(0x04)[6:18].hex()
    assert coordinator.network.unidentified_node_ids() == ()


async def test_report_from_a_just_deleted_heater_does_not_re_register_it(
    hass, monkeypatch, enable_custom_integrations
):
    """2026-09-09 bench attempt 1: the deletion is not confirmed by the
    heater in any way, and its own next report re-registered it about 300 s
    later, in time for pairing to see its id as taken."""
    from custom_components.termoweb_local import async_remove_config_entry_device
    from homeassistant.helpers import device_registry as dr

    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"{coordinator.dev_id}:4"), entry.entry_id
    )
    assert await async_remove_config_entry_device(hass, entry, device) is True
    await hass.async_block_till_done()
    assert 0x04 in coordinator.network.reserved_node_ids

    transport.queue_line(f"RX 6000 -44.0 60 0 {E5_OFF_REPORT_HEX}")
    await asyncio.sleep(0.3)
    assert 0x04 not in coordinator.heaters

    # Once the quarantine has held, the heater is a new heater like any other.
    coordinator._removed_heaters[0x04] = time.monotonic() - 1.0
    transport.queue_line(f"RX 6001 -44.0 60 0 {E5_OFF_REPORT_HEX}")

    await _wait_until(lambda: 0x04 in coordinator.heaters)
    assert 0x04 not in coordinator.network.reserved_node_ids


async def test_a_quarantined_id_is_not_handed_to_another_identity(
    hass, monkeypatch, enable_custom_integrations
):
    """A deleted heater still holds its id until REMOVED_HEATER_QUARANTINE_S
    has passed, so a heater pairing in the meantime gets the next free id."""
    from custom_components.termoweb_local import async_remove_config_entry_device
    from homeassistant.helpers import device_registry as dr

    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"{coordinator.dev_id}:2"), entry.entry_id
    )
    assert await async_remove_config_entry_device(hass, entry, device) is True
    await hass.async_block_till_done()
    coordinator.network.known_identities.pop(identity_reply_payload(0x02)[6:18], None)
    coordinator.start_pairing(60.0)
    await hass.async_block_till_done()

    transport.queue_line(_e7_line(bytes(12)))

    # 02 is quarantined and 04 is configured, so 03 is the lowest free id.
    await _wait_until(lambda: _assignments_to_broadcast(transport) == [0x03])


async def test_scan_does_not_re_register_a_just_deleted_heater(
    hass, monkeypatch, enable_custom_integrations, caplog
):
    """2026-09-09 18:26:36 live run: the owner deleted heater 04's device,
    pressed a button that ran a scan, and the scan re-registered 04 and
    recreated every one of its entities two minutes later -- the same
    failure _handle_event's own quarantine check prevents, reached by the
    other door. The id is still probed, and still answers in the log; only
    the registration is held back."""
    from custom_components.termoweb_local import async_remove_config_entry_device
    from homeassistant.helpers import device_registry as dr

    transport = FakeCulTransport(status_reply_ids={0x02, 0x04})
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"{coordinator.dev_id}:4"), entry.entry_id
    )
    assert await async_remove_config_entry_device(hass, entry, device) is True
    await hass.async_block_till_done()

    caplog.set_level(logging.INFO)
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.termoweb_local_scan_for_heaters"},
        blocking=True,
    )

    assert 0x04 not in coordinator.heaters
    assert [h[CONF_HEATER_ID] for h in entry.options[CONF_HEATERS]] == [0x02]
    assert "scan 04: ack=yes reply=yes" in caplog.text
    assert "scan 04: answered after its device was deleted" in caplog.text

    # Once the quarantine has held, the scan registers it like any other id.
    coordinator._removed_heaters[0x04] = time.monotonic() - 1.0
    assert await coordinator.async_scan_for_heaters() == [0x04]
    assert 0x04 in coordinator.heaters

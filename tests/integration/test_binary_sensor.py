"""Gateway online binary_sensor transitions (docs/91-p4-parity-plan.md P4a):
on once a report has arrived within cadence, off once the serial link closes
or every heater has gone stale.

Also the per-heater heating_while_off sensor (docs/80-handover.md list C
item 11): on when mode off, duty 0 and pcb_temp has risen more than 3C over
the rolling window -- the 2026-09-12 20:28-21:20 incident's own shape,
PROTOCOL.md 5.10's "Incident" paragraph."""
import asyncio

from termoweb_local import frame as tf

from .conftest import FakeCulTransport, setup_entry

MASTER_BEDROOM = 0x04  # matches TEST_HEATERS's second entry
HEATING_WHILE_OFF = "binary_sensor.master_bedroom_heater_heating_while_off"
# Real captured off-mode E5 report (test_coordinator.py's own E5_OFF_REPORT_HEX):
# mode off, duty 0, flags 0, pcb_temp 28C, error 0.
_E5_OFF_REPORT_HEX = "E59C885DB6A1C825565F4A9C5850E47E05BAB4FC84AE07F1E686E38616"


def _e5_off_report_with_pcb_temp(pcb_temp: int) -> str:
    """The same captured off-mode report, its own pcb_temp byte (payload
    index 13) replaced and the CRC recomputed via tf.build_frame, the same
    approach test_climate.py's own hvac_action regression tests use rather
    than a hand-typed hex literal."""
    parsed = tf.parse_frame(bytes.fromhex(_E5_OFF_REPORT_HEX))
    payload = bytearray(parsed.payload)
    payload[13] = pcb_temp
    air = tf.build_frame(parsed.src, parsed.dst, bytes(payload), hops=(1, 1, 1))
    return air.hex().upper()


def _e5_heating_report_with_pcb_temp(pcb_temp: int) -> str:
    """The same captured report with mode switched to manual/heat and duty
    to 100, pcb_temp set the same way -- proves the off/duty-0 gate holds
    even while pcb_temp itself is rising."""
    parsed = tf.parse_frame(bytes.fromhex(_E5_OFF_REPORT_HEX))
    payload = bytearray(parsed.payload)
    payload[5] = 0x02  # mode manual/heat
    payload[11] = 0x64  # duty 100
    payload[13] = pcb_temp
    air = tf.build_frame(parsed.src, parsed.dst, bytes(payload), hops=(1, 1, 1))
    return air.hex().upper()


async def _wait_for_state(hass, entity_id, expected_state, timeout=2.0, step=0.02):
    elapsed = 0.0
    while elapsed < timeout:
        state = hass.states.get(entity_id)
        if state is not None and state.state == expected_state:
            return state
        await asyncio.sleep(step)
        elapsed += step
    raise AssertionError(
        f"{entity_id} did not reach {expected_state!r} (last: {hass.states.get(entity_id)})"
    )


GATEWAY_ONLINE = "binary_sensor.termoweb_local_gateway_online"


async def test_gateway_online_after_setup(hass, monkeypatch, enable_custom_integrations):
    """Setup's own F3 B8 status requests already give every heater a
    snapshot and a last_report_time, so the gateway should read online right
    after setup with no further frame needed."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    state = await _wait_for_state(hass, GATEWAY_ONLINE, "on")
    assert state.attributes["connected"] is True
    assert state.attributes["dev_id"]
    assert state.attributes["model"] == "nanoCUL868 termoweb_rx"
    assert state.attributes["link_status"] == "healthy"
    assert state.attributes["last_frame_at"] is not None
    assert state.attributes["link_healthy_minutes"] is not None


async def test_gateway_offline_when_port_closed(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    await _wait_for_state(hass, GATEWAY_ONLINE, "on")
    coordinator = entry.runtime_data

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    # entry.runtime_data is cleared once unloaded, and the entity itself is
    # removed with the platform; assert the coordinator's own connectivity
    # method directly instead (grabbed before unload), which is what the
    # entity's is_on property read while it existed.
    assert coordinator.gateway_connected() is False


async def test_gateway_offline_when_every_heater_is_stale(hass, monkeypatch, enable_custom_integrations):
    """gateway_connected() is report-cadence only (docs/PROTOCOL.md section 6,
    never RSSI/LQI): forcing every heater's last_report_time far enough into
    the past must flip the gateway off even though the port is still open."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    await _wait_for_state(hass, GATEWAY_ONLINE, "on")

    stale_time = 0.0  # long before "now" regardless of when this test runs
    for heater in coordinator.heaters.values():
        heater.last_report_time = stale_time
    coordinator._update_link_health()
    coordinator.async_update_listeners()

    state = await _wait_for_state(hass, GATEWAY_ONLINE, "off")
    assert state.attributes["connected"] is False
    assert state.attributes["link_status"] == "unhealthy"
    assert state.attributes["link_healthy_minutes"] is None


async def test_heating_while_off_created_and_initially_off(
    hass, monkeypatch, enable_custom_integrations
):
    """Setup's own first refresh already gives every heater a snapshot
    (mode off, duty 0, pcb_temp 35C, conftest.py's own _STATUS_REPLY_PAYLOAD)
    but only one sample, so there is nothing yet to compare a rise against."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    state = await _wait_for_state(hass, HEATING_WHILE_OFF, "off")
    assert state.attributes["addr"] == MASTER_BEDROOM
    assert state.attributes["pcb_temp"] == 35
    assert state.attributes["window_minutes"] == 10


async def test_heating_while_off_turns_on_when_pcb_temp_rises_while_off(
    hass, monkeypatch, enable_custom_integrations
):
    """docs/80-handover.md list C item 11: mode off and duty 0 both hold,
    same as the 2026-09-12 incident, and pcb_temp climbs more than 3C from
    the baseline (35C) this entity sampled right after setup."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    await _wait_for_state(hass, HEATING_WHILE_OFF, "off")

    transport.queue_line(f"RX 1000 -44.0 60 0 {_e5_off_report_with_pcb_temp(39)}")

    state = await _wait_for_state(hass, HEATING_WHILE_OFF, "on")
    assert state.attributes["pcb_temp"] == 39


async def test_heating_while_off_stays_off_at_the_3c_boundary(
    hass, monkeypatch, enable_custom_integrations
):
    """A rise of exactly 3C (35 -> 38) must not trigger: the rule is "more
    than 3C", not "at least 3C"."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    await _wait_for_state(hass, HEATING_WHILE_OFF, "off")

    transport.queue_line(f"RX 1000 -44.0 60 0 {_e5_off_report_with_pcb_temp(38)}")
    await asyncio.sleep(0.2)

    state = hass.states.get(HEATING_WHILE_OFF)
    assert state.state == "off"
    assert state.attributes["pcb_temp"] == 38


async def test_heating_while_off_stays_off_while_actually_heating(
    hass, monkeypatch, enable_custom_integrations
):
    """The same pcb_temp rise while mode is manual/heat and duty is 100 must
    not trigger: a heater that is genuinely heating is not the incident this
    sensor exists to catch."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    await _wait_for_state(hass, HEATING_WHILE_OFF, "off")

    transport.queue_line(f"RX 1000 -44.0 60 0 {_e5_heating_report_with_pcb_temp(39)}")
    await asyncio.sleep(0.2)

    state = hass.states.get(HEATING_WHILE_OFF)
    assert state.state == "off"

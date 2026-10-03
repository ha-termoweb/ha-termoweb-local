"""Per-heater energy/power sensors and the gateway total-energy sensor
(docs/91-p4-parity-plan.md P4a parity matrix): energy is the heater's own
watt-hour meter read with F3 BC, power scales the duty byte by the full-load
power the heater itself reports and needs no owner configuration."""
import asyncio

from termoweb_local import frame as tf

from .conftest import THREE_HEATERS, FakeCulTransport, setup_entry

# Real E5 reports, straight off the wire, one per heater, each carrying that
# heater's own measured full-load power at logical bytes 21-22 (PROTOCOL.md
# 5.4). Node 04's is the 2026-09-06 10:22:49.541 report from
# docs/captures/2026-09-06-phase3/nano-rx-101909.log: duty byte 0x64 (100) at
# 749.0 W full load, so the sensor must read 749.0 W. Nodes 02 and 03 are idle
# (duty 0), so they read 0.0 W while still reporting their own full-load
# ratings, 1846.5 W against an 1800 W nameplate and 786.6 W against 750 W.
E5_HEATING_NODE_04_HEX = "E59C885DB6A1C825565F4A9C5850E47E05BCB4F384AD1F95E786E340EF"
E5_IDLE_NODE_02_HEX = "E59C885BB6A1CE25565F4A9C5850E47E05BCB4F89FF87CF1E684E38282"
E5_IDLE_NODE_03_HEX = "E59C885AB6A1CF25565F4A9C5850E47A05BCB4C29FAEE7F1E68DE3379B"


async def _wait_for_state(hass, entity_id, expected):
    """The fixture's own F3 B8 reply already gives every heater a snapshot at
    setup (duty 0, full load 749.0 W, conftest._STATUS_REPLY_PAYLOAD), so a
    test that queues its own report has to wait for that report's value, not
    merely for the entity to stop being unknown."""
    for _ in range(100):
        state = hass.states.get(entity_id)
        if state is not None and state.state == expected:
            return state
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"{entity_id} never reached {expected!r}; last was "
        f"{None if state is None else state.state!r}"
    )


async def test_energy_sensor_reports_the_heater_own_counter_in_kwh(hass, monkeypatch, enable_custom_integrations):
    """The setup refresh's own F3 BC sweep answers with the real captured EF
    payloads (conftest._ENERGY_REPLY_PAYLOADS), so each heater reads its own
    counter: node 02's 1620009 Wh and node 04's 1049657 Wh, in kWh."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    assert await _wait_for_state(hass, "sensor.living_room_heater_energy", "1620.009")
    assert await _wait_for_state(hass, "sensor.master_bedroom_heater_energy", "1049.657")


async def test_gateway_total_energy_sensor_sums_the_per_heater_counters(hass, monkeypatch, enable_custom_integrations):
    """1620009 + 2210563 + 1049657 Wh over the three real heaters."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport, heaters=THREE_HEATERS)

    state = await _wait_for_state(hass, "sensor.termoweb_local_total_energy", "4880.229")
    assert state.attributes["heaters_reporting"] == 3


async def test_unsolicited_ef_is_recorded_and_never_confirmed(hass, monkeypatch, enable_custom_integrations):
    """An EF that arrives outside a poll window is still this heater's own meter,
    so it updates the sensor -- and it must not draw an F2 57 55 confirmation,
    since its payload starts `BD`, not the `56` report marker."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    # A different counter from the fixture's own poll reply, so the assertion
    # cannot pass on the polled value: node 04's own 13:00 sweep, 1049397 Wh.
    transport.queue_line("RX 5000 -44.0 60 0 EF9C885DB6A1C825565F4A9CB3E9FA531F08A0")
    await _wait_for_state(hass, "sensor.master_bedroom_heater_energy", "1049.397")

    confirmations = [
        payload
        for payload in _sent_payloads(transport, dst=0x04)
        if payload == bytes([0x57, 0x55])
    ]
    assert confirmations == []


def _sent_payloads(transport: FakeCulTransport, dst: int) -> list[bytes]:
    """Every application payload this station actually put on the air towards
    `dst`, decoded back out of the transport's own T<hex> writes."""
    payloads = []
    for written in transport.written:
        text = written.decode(errors="replace").strip()
        if not text.startswith("T") or len(text) <= 1:
            continue
        parsed = tf.parse_frame(bytes.fromhex(text[1:]))
        if parsed.dst == dst:
            payloads.append(bytes(parsed.payload))
    return payloads


async def test_power_sensor_available_with_no_owner_configuration(hass, monkeypatch, enable_custom_integrations):
    """No rated-power option exists any more: the fixture's own F3 B8 status
    reply is enough to make the sensor report a value."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    state = hass.states.get("sensor.master_bedroom_heater_power")
    assert state is not None
    assert state.state == "0.0"  # duty byte 0 in the fixture's own status reply
    assert state.attributes["provisional"] is True
    assert state.attributes["provisional_reason"] == "duty percent byte not proven (E5 byte 23 vs 24)"


async def test_power_sensor_scales_duty_byte_by_the_reported_full_load_power(hass, monkeypatch, enable_custom_integrations):
    """Node 04 heating at duty 100 with a reported full load of 749.0 W."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    transport.queue_line(f"RX 1000 -44.0 60 0 {E5_HEATING_NODE_04_HEX}")
    state = await _wait_for_state(hass, "sensor.master_bedroom_heater_power", "749.0")

    assert state.attributes["full_load_power_w"] == 749.0


async def test_power_sensor_full_load_attribute_is_per_heater(hass, monkeypatch, enable_custom_integrations):
    """The 1800 W and 750 W heaters report their own measured ratings, 2.4
    apart, with no owner-supplied figure anywhere in the path. Needs the
    three-heater config, since node 03 is the 750 W bedroom heater and the
    default two-heater fixture does not have it."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport, heaters=THREE_HEATERS)

    transport.queue_line(f"RX 1000 -44.0 60 0 {E5_IDLE_NODE_02_HEX}")
    transport.queue_line(f"RX 1001 -44.0 60 0 {E5_IDLE_NODE_03_HEX}")

    # Both heaters are idle in these reports, so the state stays 0.0 W and the
    # ratings only show up in the attribute; wait on the attribute itself.
    for entity_id, full_load in (
        ("sensor.living_room_heater_power", 1846.5),
        ("sensor.bedroom_heater_power", 786.6),
    ):
        for _ in range(100):
            state = hass.states.get(entity_id)
            if state is not None and state.attributes.get("full_load_power_w") == full_load:
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError(f"{entity_id} never reported full load {full_load}")
        assert state.state == "0.0"

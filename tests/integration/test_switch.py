"""switch.<heater>_boost/_easy_mode/_runback/_open_window_detection/
_true_radiant (2026-09-13 X-19 proof, PROTOCOL.md 5.6)."""
from termoweb_local import frame as tf

from .conftest import FakeCulTransport, setup_entry

MASTER_BEDROOM = 0x04  # matches TEST_HEATERS's second entry


def _last_command_hex(transport: FakeCulTransport) -> str:
    """The last outgoing frame carrying an actual command payload (more than
    one byte), skipping the F3 B8 status refresh and, for boost, the F3 DA
    advanced-setup read that follow a successful toggle
    (Coordinator._async_send_confirm_and_refresh / async_start_boost's own
    DA follow-up) -- both single-byte payloads, unlike D2/D6/D4/BA (2 bytes)
    or C4 (9 bytes)."""
    sent = [w for w in transport.written if w.startswith(b"T")]
    for raw in reversed(sent):
        hexpart = raw.strip()[1:].decode()
        if len(tf.parse_frame(bytes.fromhex(hexpart)).payload) > 1:
            return hexpart
    raise AssertionError("no multi-byte-payload frame found in transport.written")


async def test_boost_turn_on_sends_d2_01_and_reads_on(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    transport.written.clear()

    await hass.services.async_call(
        "switch", "turn_on",
        {"entity_id": "switch.master_bedroom_heater_boost"},
        blocking=True,
    )

    expected_logical = "0D1B30010400010400000000D201"
    sent_air = bytes.fromhex(_last_command_hex(transport))
    assert tf.descramble(sent_air)[:14].hex().upper() == expected_logical
    assert hass.states.get("switch.master_bedroom_heater_boost").state == "on"


async def test_boost_turn_off_sends_d2_00_and_reads_off(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.master_bedroom_heater_boost"}, blocking=True,
    )
    transport.written.clear()

    await hass.services.async_call(
        "switch", "turn_off",
        {"entity_id": "switch.master_bedroom_heater_boost"},
        blocking=True,
    )

    expected_logical = "0D1B30010400010400000000D200"
    sent_air = bytes.fromhex(_last_command_hex(transport))
    assert tf.descramble(sent_air)[:14].hex().upper() == expected_logical
    assert hass.states.get("switch.master_bedroom_heater_boost").state == "off"


async def test_easy_mode_turn_on_sends_d6_01_and_reads_on(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    transport.written.clear()

    await hass.services.async_call(
        "switch", "turn_on",
        {"entity_id": "switch.master_bedroom_heater_easy_mode"},
        blocking=True,
    )

    expected_logical = "0D1B30010400010400000000D601"
    sent_air = bytes.fromhex(_last_command_hex(transport))
    assert tf.descramble(sent_air)[:14].hex().upper() == expected_logical
    assert hass.states.get("switch.master_bedroom_heater_easy_mode").state == "on"


async def test_runback_turn_on_sends_d4_01_and_reads_on(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    transport.written.clear()

    await hass.services.async_call(
        "switch", "turn_on",
        {"entity_id": "switch.master_bedroom_heater_runback"},
        blocking=True,
    )

    expected_logical = "0D1B30010400010400000000D401"
    sent_air = bytes.fromhex(_last_command_hex(transport))
    assert tf.descramble(sent_air)[:14].hex().upper() == expected_logical
    assert hass.states.get("switch.master_bedroom_heater_runback").state == "on"


async def test_runback_restored_attribute_none_before_any_toggle(
    hass, monkeypatch, enable_custom_integrations
):
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    state = hass.states.get("switch.master_bedroom_heater_runback")
    assert state.attributes["restored"] is None


async def test_runback_restored_true_restores_cached_off_mode_and_setpoint(
    hass, monkeypatch, enable_custom_integrations
):
    """setup_entry's own fixed E6 reply leaves the heater in mode off (mode
    code 04), setpoint 25.0C. Runback on displaces that setpoint to the
    anti-frost preset (2026-09-13 X-19 verify-04.log); off must restore both
    the setpoint and the off mode, not leave the setpoint at anti-frost the
    way the pre-fix coordinator did -- a setpoint write (flipping the heater
    to heat, the only way to move the setpoint) then a mode-off write, in
    that order."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.master_bedroom_heater_runback"}, blocking=True,
    )
    transport.written.clear()
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.master_bedroom_heater_runback"}, blocking=True,
    )

    state = hass.states.get("switch.master_bedroom_heater_runback")
    assert state.attributes["restored"] is True
    sent_payloads = [
        tf.parse_frame(bytes.fromhex(w.strip()[1:].decode())).payload
        for w in transport.written if w.startswith(b"T")
    ]
    setpoint_payload = tf.setpoint_payload(25.0)
    mode_off_payload = tf.mode_payload("off")
    assert setpoint_payload in sent_payloads
    assert mode_off_payload in sent_payloads
    assert sent_payloads.index(setpoint_payload) < sent_payloads.index(mode_off_payload)


async def test_runback_restored_true_restores_cached_manual_mode_and_setpoint(
    hass, monkeypatch, enable_custom_integrations
):
    """Real worked frame (tests/test_heater.py's own suite): master bedroom,
    manual (heat) mode, setpoint 24.0C. Runback on caches that, and off must
    restore it via a setpoint write alone -- the setpoint write itself puts
    the heater back in heat, so a cached "manual" needs no separate mode
    write (unlike a cached "off"/"auto", which does)."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data

    transport.queue_line(
        "RX 1000 -44.0 60 0 E59C885DB6A1C825565F4A9C5850E47E05BCB4F086AE28C3E7BEE3933D"
    )
    await _wait_for_snapshot_mode(coordinator, MASTER_BEDROOM, "manual")

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.master_bedroom_heater_runback"}, blocking=True,
    )
    transport.written.clear()
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.master_bedroom_heater_runback"}, blocking=True,
    )

    state = hass.states.get("switch.master_bedroom_heater_runback")
    assert state.attributes["restored"] is True
    sent_payloads = [
        tf.parse_frame(bytes.fromhex(w.strip()[1:].decode())).payload
        for w in transport.written if w.startswith(b"T")
    ]
    assert tf.setpoint_payload(24.0) in sent_payloads
    assert tf.mode_payload("heat") not in sent_payloads


async def test_open_window_detection_turn_on_sends_c4_and_reads_on(
    hass, monkeypatch, enable_custom_integrations
):
    """C4's own window_mode field, no radio readback (2026-09-13 X-19
    proof): state is read straight from the persisted record, so it is
    correct immediately, not merely optimistic."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    transport.written.clear()

    await hass.services.async_call(
        "switch", "turn_on",
        {"entity_id": "switch.master_bedroom_heater_open_window_detection"},
        blocking=True,
    )

    expected_logical = "141B30010400010400000000C40400000000000100"
    sent_air = bytes.fromhex(_last_command_hex(transport))
    assert tf.descramble(sent_air)[:21].hex().upper() == expected_logical
    assert hass.states.get("switch.master_bedroom_heater_open_window_detection").state == "on"


async def test_true_radiant_turn_on_sends_c4_and_reads_on(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    transport.written.clear()

    await hass.services.async_call(
        "switch", "turn_on",
        {"entity_id": "switch.master_bedroom_heater_true_radiant"},
        blocking=True,
    )

    expected_logical = "141B30010400010400000000C40400000000000001"
    sent_air = bytes.fromhex(_last_command_hex(transport))
    assert tf.descramble(sent_air)[:21].hex().upper() == expected_logical
    assert hass.states.get("switch.master_bedroom_heater_true_radiant").state == "on"


async def _wait_for_snapshot_mode(coordinator, node_id, expected_mode, timeout=2.0, step=0.02):
    import asyncio

    elapsed = 0.0
    while elapsed < timeout:
        heater = coordinator.heaters.get(node_id)
        snap = heater.last_snapshot if heater else None
        if snap is not None and snap.mode == expected_mode:
            return
        await asyncio.sleep(step)
        elapsed += step
    raise AssertionError(f"node {node_id:02x} never reached mode {expected_mode!r}")

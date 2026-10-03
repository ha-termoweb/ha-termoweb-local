"""number.<heater>_priority (placeholder) -- docs/91-p4-parity-plan.md P4a
parity matrix. number.<heater>_temperature_offset (2026-09-13 X-19 proof,
PROTOCOL.md 5.6): backed by C4's own offset field, panel-confirmed to match
the wire byte unchanged (docs/captures/2026-09-13-x19/link-test.md)."""
import pytest
from homeassistant.exceptions import HomeAssistantError

from termoweb_local import frame as tf

from .conftest import FakeCulTransport, setup_entry


def _last_command_hex(transport: FakeCulTransport) -> str:
    sent = [w for w in transport.written if w.startswith(b"T")]
    for raw in reversed(sent):
        hexpart = raw.strip()[1:].decode()
        if len(tf.parse_frame(bytes.fromhex(hexpart)).payload) > 1:
            return hexpart
    raise AssertionError("no multi-byte-payload frame found in transport.written")


async def test_priority_number_is_unknown(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    state = hass.states.get("number.living_room_heater_priority")
    assert state is not None
    assert state.state == "unknown"


async def test_priority_number_set_raises_not_supported(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    with pytest.raises(HomeAssistantError, match="not supported by the radio protocol yet"):
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": "number.living_room_heater_priority", "value": 5},
            blocking=True,
        )


async def test_temperature_offset_reads_zero_by_default(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    state = hass.states.get("number.master_bedroom_heater_temperature_offset")
    assert state is not None
    assert float(state.state) == 0.0


async def test_temperature_offset_plus_one_sends_0a_wire_byte(hass, monkeypatch, enable_custom_integrations):
    """+1.0C sends wire offset +10 (0x0A), the panel's own worked example
    (2026-09-13 X-19 proof, docs/captures/2026-09-13-x19/link-test.md: the
    entity now matches the panel's Temp Offset, wire and HA value share a
    sign, and a positive offset lowers the reported temperature)."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    transport.written.clear()

    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": "number.master_bedroom_heater_temperature_offset", "value": 1.0},
        blocking=True,
    )

    expected_logical = "141B30010400010400000000C404000A0000000000"
    sent_air = bytes.fromhex(_last_command_hex(transport))
    assert tf.descramble(sent_air)[:21].hex().upper() == expected_logical
    state = hass.states.get("number.master_bedroom_heater_temperature_offset")
    assert float(state.state) == 1.0


async def test_temperature_offset_minus_two_sends_ec_wire_byte(hass, monkeypatch, enable_custom_integrations):
    """-2.0C sends wire offset -20 (0xEC), the panel's own worked example
    (docs/captures/2026-09-13-x19/link-test.md: wire and HA value share a
    sign, a negative offset raises the reported temperature)."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    transport.written.clear()

    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": "number.master_bedroom_heater_temperature_offset", "value": -2.0},
        blocking=True,
    )

    expected_logical = "141B30010400010400000000C40400EC0000000000"
    sent_air = bytes.fromhex(_last_command_hex(transport))
    assert tf.descramble(sent_air)[:21].hex().upper() == expected_logical
    state = hass.states.get("number.master_bedroom_heater_temperature_offset")
    assert float(state.state) == -2.0

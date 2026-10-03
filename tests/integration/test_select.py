"""select.<heater>_control_mode/_units (2026-09-13 X-19 proof, PROTOCOL.md
5.6): both C4 fields with no radio readback, so every write sends the whole
eight-byte record, carrying over whatever a prior write (from either select,
or from the temperature_offset number/the two C4-backed switches) last set
for the other seven fields."""
from termoweb_local import frame as tf

from .conftest import FakeCulTransport, setup_entry


def _last_command_hex(transport: FakeCulTransport) -> str:
    sent = [w for w in transport.written if w.startswith(b"T")]
    for raw in reversed(sent):
        hexpart = raw.strip()[1:].decode()
        if len(tf.parse_frame(bytes.fromhex(hexpart)).payload) > 1:
            return hexpart
    raise AssertionError("no multi-byte-payload frame found in transport.written")


async def test_control_mode_select_options_and_default():
    from custom_components.termoweb_local.select import CONTROL_MODE_OPTIONS

    assert list(CONTROL_MODE_OPTIONS) == [
        "PID",
        "Hysteresis 0.25C",
        "Hysteresis 0.35C",
        "Hysteresis 0.5C",
        "Hysteresis 0.75C",
    ]
    assert CONTROL_MODE_OPTIONS["PID"] == 4


async def test_units_select_reads_default_c(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    assert hass.states.get("select.master_bedroom_heater_units").state == "C"
    assert hass.states.get("select.master_bedroom_heater_control_mode").state == "PID"


async def test_units_select_option_f_sends_whole_c4_record(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    transport.written.clear()

    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": "select.master_bedroom_heater_units", "option": "F"},
        blocking=True,
    )

    # C4 <control_mode=4> <units=1> <offset=0> <away_mode=0> <away_offset=0>
    # <modified_auto_span=0> <window_mode=0> <true_radiant=0>: every field but
    # units still the persisted default (decision 4, "every write sends the
    # whole record").
    expected_logical = "141B30010400010400000000C40401000000000000"
    sent_air = bytes.fromhex(_last_command_hex(transport))
    assert tf.descramble(sent_air)[:21].hex().upper() == expected_logical
    assert hass.states.get("select.master_bedroom_heater_units").state == "F"


async def test_control_mode_select_carries_over_a_prior_units_write(
    hass, monkeypatch, enable_custom_integrations
):
    """Setting units to F first, then control_mode to a hysteresis step,
    must not silently drop the units write (decision 4's own race-avoidance
    reasoning, Risks section)."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": "select.master_bedroom_heater_units", "option": "F"},
        blocking=True,
    )
    transport.written.clear()

    await hass.services.async_call(
        "select", "select_option",
        {
            "entity_id": "select.master_bedroom_heater_control_mode",
            "option": "Hysteresis 0.25C",
        },
        blocking=True,
    )

    expected_logical = "141B30010400010400000000C40001000000000000"
    sent_air = bytes.fromhex(_last_command_hex(transport))
    assert tf.descramble(sent_air)[:21].hex().upper() == expected_logical
    assert hass.states.get("select.master_bedroom_heater_units").state == "F"
    assert (
        hass.states.get("select.master_bedroom_heater_control_mode").state
        == "Hysteresis 0.25C"
    )

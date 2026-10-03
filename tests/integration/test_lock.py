"""lock.<heater>_child_lock -- BA, the keypad lock (2026-09-13 X-19 proof,
PROTOCOL.md 5.6), backed now."""
from termoweb_local import frame as tf

from .conftest import FakeCulTransport, setup_entry


def _last_command_hex(transport: FakeCulTransport) -> str:
    sent = [w for w in transport.written if w.startswith(b"T")]
    for raw in reversed(sent):
        hexpart = raw.strip()[1:].decode()
        if len(tf.parse_frame(bytes.fromhex(hexpart)).payload) > 1:
            return hexpart
    raise AssertionError("no multi-byte-payload frame found in transport.written")


async def test_child_lock_is_unlocked_by_default(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    state = hass.states.get("lock.living_room_heater_child_lock")
    assert state is not None
    assert state.state == "unlocked"


async def test_lock_sends_ba_01_and_reads_locked(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    transport.written.clear()

    await hass.services.async_call(
        "lock", "lock",
        {"entity_id": "lock.master_bedroom_heater_child_lock"},
        blocking=True,
    )

    expected_logical = "0D1B30010400010400000000BA01"
    sent_air = bytes.fromhex(_last_command_hex(transport))
    assert tf.descramble(sent_air)[:14].hex().upper() == expected_logical
    assert hass.states.get("lock.master_bedroom_heater_child_lock").state == "locked"


async def test_unlock_sends_ba_00_and_reads_unlocked(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    await hass.services.async_call(
        "lock", "lock", {"entity_id": "lock.master_bedroom_heater_child_lock"}, blocking=True,
    )
    transport.written.clear()

    await hass.services.async_call(
        "lock", "unlock",
        {"entity_id": "lock.master_bedroom_heater_child_lock"},
        blocking=True,
    )

    expected_logical = "0D1B30010400010400000000BA00"
    sent_air = bytes.fromhex(_last_command_hex(transport))
    assert tf.descramble(sent_air)[:14].hex().upper() == expected_logical
    assert hass.states.get("lock.master_bedroom_heater_child_lock").state == "unlocked"

"""button.<heater>_flash_display (backed, F2 5E 01),
button.termoweb_local_force_refresh (backed, F3 B8 to every heater), and the
two discovery buttons on the station device (owner direction 2026-09-06
tasks 2 and 3) -- docs/91-p4-parity-plan.md P4a parity matrix."""
import datetime as dt
import logging

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from termoweb_local import frame as tf

from .conftest import FakeCulTransport, setup_entry


def _sent_hex_frames(transport: FakeCulTransport) -> list[str]:
    return [w.strip()[1:].decode() for w in transport.written if w.startswith(b"T")]


async def test_flash_display_button_sends_f2_5e_01_and_gets_5f_55_reply(
    hass, monkeypatch, enable_custom_integrations
):
    """2026-09-06 P4b proof, notes.md 16:56:59Z: F2 5E 01 to the heater,
    answered F2 5F 55."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    transport.written.clear()

    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.living_room_heater_flash_display"},
        blocking=True,
    )

    sent = _sent_hex_frames(transport)
    expected = tf.build_frame(0x01, 0x02, bytes([0x5E, 0x01])).hex().upper()
    assert expected in sent


async def test_force_refresh_button_sends_status_request_to_every_heater(
    hass, monkeypatch, enable_custom_integrations
):
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    transport.written.clear()

    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.termoweb_local_force_refresh"},
        blocking=True,
    )

    sent = _sent_hex_frames(transport)
    expected = {
        tf.build_frame(0x01, node_id, bytes([0xB8])).hex().upper()
        for node_id in (0x02, 0x04)
    }
    assert set(sent) == expected
    assert len(sent) == 2


async def test_scan_for_heaters_button_discovers_a_new_heater(
    hass, monkeypatch, enable_custom_integrations
):
    """Owner direction 2026-09-06 task 2: pressing
    button.termoweb_local_scan_for_heaters reruns the ids-2-to-65 scan and
    registers any newly answering id as a heater, with no restart."""
    transport = FakeCulTransport(status_reply_ids={0x02, 0x04})
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    assert 0x07 not in coordinator.heaters

    transport.status_reply_ids = {0x02, 0x04, 0x07}
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.termoweb_local_scan_for_heaters"},
        blocking=True,
    )

    assert 0x07 in coordinator.heaters
    assert coordinator.heater_names[0x07] == "Heater 07"
    assert hass.states.get("climate.heater_07") is not None


async def test_pair_heater_button_opens_a_discovery_window(
    hass, monkeypatch, enable_custom_integrations
):
    """Owner direction 2026-09-06 task 3: pressing
    button.termoweb_local_pair_heater opens the discovery window for this
    entry's configured CONF_PAIR_HEATER_SECONDS."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    assert coordinator.network.discovery_active() is False

    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.termoweb_local_pair_heater"},
        blocking=True,
    )

    assert coordinator.network.discovery_active() is True


async def test_pair_heater_button_logs_and_notifies_its_window(
    hass, monkeypatch, enable_custom_integrations, caplog
):
    """2026-09-09 live run: the press armed a 120 s window in silence -- no
    log line, no notification, no visible entity change -- so the owner had
    no way to tell a registered press from a broken button, and neither did
    the log."""
    from homeassistant.components import persistent_notification

    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)
    coordinator = entry.runtime_data
    caplog.set_level(logging.INFO)

    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.termoweb_local_pair_heater"},
        blocking=True,
    )

    assert "pairing: discovery window open for 120s" in caplog.text
    notifications = persistent_notification._async_get_or_create_notifications(hass)
    assert any("Pairing window open for 120 seconds" in n["message"] for n in notifications.values())
    online = hass.states.get("binary_sensor.termoweb_local_gateway_online")
    assert online.attributes["pairing_active"] is True

    async_fire_time_changed(
        hass, dt_util.utcnow() + dt.timedelta(seconds=coordinator.pair_heater_seconds + 1)
    )
    await hass.async_block_till_done()

    assert "pairing: discovery window closed after 120s, nothing paired" in caplog.text
    notifications = persistent_notification._async_get_or_create_notifications(hass)
    assert any("no heater announced itself" in n["message"] for n in notifications.values())
    online = hass.states.get("binary_sensor.termoweb_local_gateway_online")
    assert online.attributes["pairing_active"] is False

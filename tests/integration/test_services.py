"""poll_now, sync_clock, set_schedule and set_preset_temperatures (backed),
and every placeholder/N-A service (docs/91-p4-parity-plan.md P4a parity
matrix)."""
import asyncio

import pytest
import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError

from termoweb_local import frame as tf

from custom_components.termoweb_local.const import DOMAIN
from .conftest import FakeCulTransport, setup_entry


def _sent_hex_frames(transport: FakeCulTransport) -> list[str]:
    return [w.strip()[1:].decode() for w in transport.written if w.startswith(b"T")]


async def test_poll_now_service_sends_poll_frame_to_targeted_entity_only(hass, monkeypatch, enable_custom_integrations):
    """poll_now sends the F3 B8 on-demand status request, not F2 57 55 (2026-09-06
    proof, notes.md 13:18:42Z: 57 55 alone elicits no report at all;
    f3-and-edges-results.md section 1: B8 is answered by an E6 status reply)."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    transport.written.clear()

    await hass.services.async_call(
        "termoweb_local",
        "poll_now",
        {"entity_id": "climate.master_bedroom_heater"},
        blocking=True,
    )

    sent = _sent_hex_frames(transport)
    assert len(sent) == 1
    expected = tf.build_frame(0x01, 0x04, bytes([0xB8]))
    assert sent[0] == expected.hex().upper()


async def test_sync_clock_service_sends_eb_frame_to_targeted_entity_only(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    transport.written.clear()

    await hass.services.async_call(
        "termoweb_local",
        "sync_clock",
        {"entity_id": "climate.living_room_heater"},
        blocking=True,
    )

    sent = _sent_hex_frames(transport)
    assert len(sent) == 1
    logical = tf.descramble(bytes.fromhex(sent[0]))
    # EB clock-sync payload: 52 YY MM DD DOW HH MM SS 03 (docs/PROTOCOL.md 5.6);
    # class byte 0x14 (EB), addressed gateway(01)->node 2 (bytes 3-4 = src, dst
    # per docs/PROTOCOL.md section 4), steady prefix 0x52.
    assert logical[0] == 0x14
    assert logical[3] == 0x01  # src = this station
    assert logical[4] == 0x02  # dst = living room (node 2)
    assert logical[12] == 0x52
    assert logical[20] == 0x03


async def test_set_schedule_round_trip(hass, monkeypatch, enable_custom_integrations):
    """set_schedule (docs/91-p4-parity-plan.md P4a): B2 write, then a B0
    read-back that must reflect what was actually written, not merely sent
    (FakeCulTransport now stores each node's last B2 write and answers a
    later B0 with it).

    The service still takes one hourly (168-value) week, but write_program
    expands each hour into its own two equal native half-hour slots before it
    goes on the air (network.py's own hourly branch, the only shape this
    station's B2 write ever sends), and the B0 read-back decodes that at its
    real 48-slot-a-day resolution (docs/80-handover.md "Owed" list, item 7):
    the `prog` attribute reflects native slots now, twice `prog`'s own
    length, each hourly value doubled across its own two equal halves."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    prog = [i % 3 for i in range(168)]
    await hass.services.async_call(
        DOMAIN,
        "set_schedule",
        {"entity_id": "climate.master_bedroom_heater", "prog": prog},
        blocking=True,
    )

    state = hass.states.get("climate.master_bedroom_heater")
    assert state.attributes["prog"] == [value for value in prog for _ in range(2)]


async def test_set_schedule_rejects_wrong_length(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "set_schedule",
            {"entity_id": "climate.master_bedroom_heater", "prog": [0, 1, 2]},
            blocking=True,
        )


async def test_set_schedule_accepts_a_native_half_hourly_week_round_trip(
    hass, monkeypatch, enable_custom_integrations
):
    """336 values is now accepted (Coordinator.get_prog / Network.program_record
    hand back each node's own native slots since fa62247, so a half-hourly write
    survives the read path end to end -- the old objection to widening this
    schema, pinned by this test's predecessor
    (test_set_schedule_rejects_a_half_hourly_week_deliberately), no longer
    holds).

    This is the round-trip proof: an asymmetric half-hourly week (each hour's
    two half-hour values deliberately different) goes in through the service
    and must come back unchanged, both from Coordinator.get_prog() directly
    and from the entity's own `prog` attribute, because a 336-value input is
    written to the wire as-is (network.py's write_program only equalises
    halves for a 24-slot-a-day input, never a 48-slot-a-day one)."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)

    # i % 3 cycles 0, 1, 2, 0, 1, 2, ... so every adjacent pair (each hour's
    # two half-hour slots) differs -- a genuinely half-hourly week throughout,
    # not one that merely happens to be expressible as an hourly one.
    half_hourly = [i % 3 for i in range(336)]
    await hass.services.async_call(
        DOMAIN,
        "set_schedule",
        {"entity_id": "climate.master_bedroom_heater", "prog": half_hourly},
        blocking=True,
    )

    assert entry.runtime_data.get_prog(0x04) == half_hourly

    state = hass.states.get("climate.master_bedroom_heater")
    assert state.attributes["prog"] == half_hourly


async def test_set_schedule_rejects_a_length_that_matches_neither_resolution(
    hass, monkeypatch, enable_custom_integrations
):
    """A length that is neither 168 (hourly) nor 336 (half-hourly) is refused
    by the schema before any frame is built, rather than guessed at (e.g.
    truncated/padded to the nearest known length)."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    for bad_length in (167, 169, 335, 337, 252):  # 252 = 168 and 336's midpoint
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN,
                "set_schedule",
                {
                    "entity_id": "climate.master_bedroom_heater",
                    "prog": [0] * bad_length,
                },
                blocking=True,
            )


async def test_set_schedule_hourly_write_rejected_over_a_real_half_hourly_boundary(
    hass, monkeypatch, enable_custom_integrations
):
    """Backwards compatibility does not mean an hourly write can silently
    flatten a half-hour boundary the heater already holds. Write a genuine
    half-hourly week first (Monday 00:00 on, 00:30 off, an asymmetric hour),
    then send an ordinary 168-value hourly week to the same heater:
    write_program's own _check_hourly_write_keeps_the_schedule (network.py)
    must still refuse it, and this pins that the widened schema does not
    bypass or duplicate that guard -- it is the same rejection path a plain
    168-value call always went through, unchanged by this widening."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    half_hourly = [0] * 336
    half_hourly[0], half_hourly[1] = 1, 0  # Monday 00:00 on, 00:30 off
    await hass.services.async_call(
        DOMAIN,
        "set_schedule",
        {"entity_id": "climate.master_bedroom_heater", "prog": half_hourly},
        blocking=True,
    )

    hourly = [0] * 168
    with pytest.raises(Exception, match="half-hourly"):
        await hass.services.async_call(
            DOMAIN,
            "set_schedule",
            {"entity_id": "climate.master_bedroom_heater", "prog": hourly},
            blocking=True,
        )


async def test_set_preset_temperatures_named_fields_round_trip(
    hass, monkeypatch, enable_custom_integrations
):
    """set_preset_temperatures (2026-09-12 X-18 proof,
    docs/captures/2026-09-12-x18-s7/notes.md): B6 write, then the same
    post-command F3 B8 status request every setpoint/mode/override command
    gets (docs/80-handover.md list C item 10 step 1), which FakeCulTransport
    now answers with the presets actually written (conftest.py's own
    `_presets`), not just the fixed default -- so `ptemp` reflects the real
    values, the same reconciliation test_set_schedule_round_trip pins for
    `prog`."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    transport.written.clear()

    await hass.services.async_call(
        DOMAIN,
        "set_preset_temperatures",
        {
            "entity_id": "climate.master_bedroom_heater",
            "cold": 7.0,
            "night": 18.5,
            "day": 21.0,
        },
        blocking=True,
    )

    expected = tf.build_frame(0x01, 0x04, bytes([0xB6, 0x0E, 0x25, 0x2A]))
    sent = _sent_hex_frames(transport)
    assert expected.hex().upper() in sent
    # docs/captures/2026-09-12-x18-s7/notes.md worked example
    assert expected.hex().upper() == "F09C8858B3A1CD20575E4B9CB8E7CF7A324D"

    state = hass.states.get("climate.master_bedroom_heater")
    assert state.attributes["ptemp"] == [7.0, 18.5, 21.0]
    assert state.attributes["ptemp_supported"] is True


async def test_set_preset_temperatures_ptemp_list_round_trip(
    hass, monkeypatch, enable_custom_integrations
):
    """The cloud's own `ptemp` list shape (docs/45-cloud-integration-api.md
    section 3) is accepted alongside the three named fields."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    await hass.services.async_call(
        DOMAIN,
        "set_preset_temperatures",
        {"entity_id": "climate.master_bedroom_heater", "ptemp": [7.0, 18.5, 21.0]},
        blocking=True,
    )

    state = hass.states.get("climate.master_bedroom_heater")
    assert state.attributes["ptemp"] == [7.0, 18.5, 21.0]


async def test_set_preset_temperatures_partial_field_keeps_the_others(
    hass, monkeypatch, enable_custom_integrations
):
    """A call naming only one field must not clobber the other two: it keeps
    whatever the entity's current preset values already are (the same
    fallback async_set_temperature/async_set_preset_mode give the values a
    call does not touch)."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    state = hass.states.get("climate.master_bedroom_heater")
    assert state.attributes["ptemp"] == [7.0, 23.0, 23.5]  # setup's own fixed E6

    await hass.services.async_call(
        DOMAIN,
        "set_preset_temperatures",
        {"entity_id": "climate.master_bedroom_heater", "night": 18.5},
        blocking=True,
    )

    state = hass.states.get("climate.master_bedroom_heater")
    assert state.attributes["ptemp"] == [7.0, 18.5, 23.5]


@pytest.mark.parametrize(
    ("service", "data"),
    [
        ("set_acm_preset", {"minutes": 60, "temperature": 20.0}),
    ],
)
async def test_climate_placeholder_services_raise(
    hass, monkeypatch, enable_custom_integrations, service, data
):
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    transport.written.clear()

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            service,
            {"entity_id": "climate.master_bedroom_heater", **data},
            blocking=True,
        )
    # No frame goes out for a service that only raises.
    assert _sent_hex_frames(transport) == []


async def test_start_boost_service_sends_d2_01(hass, monkeypatch, enable_custom_integrations):
    """D2 01 (2026-09-13 X-19 proof, PROTOCOL.md 5.6): backed now that a
    radio frame has been captured for it; `minutes` (kept for cloud-service-
    shape parity) is not sent anywhere, since the 60-minute duration is the
    heater's own boost-time setting."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    transport.written.clear()

    await hass.services.async_call(
        DOMAIN,
        "start_boost",
        {"entity_id": "climate.master_bedroom_heater", "minutes": 30},
        blocking=True,
    )

    sent = _sent_hex_frames(transport)
    payloads = [tf.parse_frame(bytes.fromhex(hexpart)).payload for hexpart in sent]
    assert bytes([0xD2, 0x01]) in payloads


async def test_cancel_boost_service_sends_d2_00(hass, monkeypatch, enable_custom_integrations):
    """D2 00 (2026-09-13 X-19 proof, PROTOCOL.md 5.6): backed now that a
    radio frame has been captured for it."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    transport.written.clear()

    await hass.services.async_call(
        DOMAIN,
        "cancel_boost",
        {"entity_id": "climate.master_bedroom_heater"},
        blocking=True,
    )

    sent = _sent_hex_frames(transport)
    payloads = [tf.parse_frame(bytes.fromhex(hexpart)).payload for hexpart in sent]
    assert bytes([0xD2, 0x00]) in payloads


@pytest.mark.parametrize(
    ("service", "data"),
    [
        ("import_energy_history", {}),
        ("import_energy_history", {"reset_progress": True, "max_history_retrieval": 30}),
        ("ws_debug_probe", {}),
        ("ws_debug_probe", {"entry_id": "some-entry", "dev_id": "some-dev"}),
    ],
)
async def test_domain_level_placeholder_services_raise(
    hass, monkeypatch, enable_custom_integrations, service, data
):
    """No entity target at all (docs/45-cloud-integration-api.md section 3);
    registered once at __init__.py's async_setup_entry, independent of any
    heater."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, service, data, blocking=True)


async def test_climate_services_have_the_expected_schema(hass, monkeypatch, enable_custom_integrations):
    """Every cloud-mirrored service (docs/45-cloud-integration-api.md section
    3) is registered: the climate entity services under this integration's
    own domain (EntityPlatform.async_register_entity_service's own
    convention, same as the pre-existing poll_now/sync_clock), the two
    no-target services also under this domain."""
    transport = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport)
    await asyncio.sleep(0)  # let entity_platform's service registration land

    for service in (
        "poll_now",
        "sync_clock",
        "set_schedule",
        "set_preset_temperatures",
        "set_acm_preset",
        "start_boost",
        "cancel_boost",
        "import_energy_history",
        "ws_debug_probe",
    ):
        assert hass.services.has_service(DOMAIN, service), service

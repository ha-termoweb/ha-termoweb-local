"""Config flow accept/reject cases, against a fake NanoCul transport (no serial port
opened): docs/90-phase3-plan.md P4 verification line. The user step asks only for
the serial URL and station id (owner direction 2026-09-06: heaters are
autodiscovered, never typed by hand); the options flow keeps the poll interval,
device id, heater association, and the "Pair heater" button's window length,
with no heater field of any kind.
"""
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.termoweb_local.const import (
    CONF_DEV_ID,
    CONF_PAIR_HEATER_SECONDS,
    CONF_POLL_INTERVAL,
    CONF_SERIAL_URL,
    CONF_STATION_ID,
    DEFAULT_DEV_ID,
    DEFAULT_PAIR_HEATER_SECONDS,
    DEFAULT_POLL_INTERVAL_S,
    DOMAIN,
)

from .conftest import FakeCulTransport, patch_config_flow_nanocul, setup_entry


async def _start_user_flow(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_user_step_accepts_with_banner_and_creates_entry(hass, monkeypatch, enable_custom_integrations):
    """NanoCul.__init__ clears the transport's input buffer on open when the
    transport supports it (real USB serial ports do), which would wipe a boot
    banner that arrived during the reset wait; a transport that does not support
    it (e.g. a socket:// TCP bridge to the bench nanoCUL) never clears anything, so
    the banner can still be sitting there when this validate call reads it."""
    transport = FakeCulTransport()
    transport.reset_input_buffer = None  # no-op: getattr(...) in NanoCul.__init__ skips it
    transport.queue_line(
        "# termoweb_rx 3.2 freq=869.525 rate=9.6k sync=2DE5 mode=dynamic tx=pa0xC0"
    )
    patch_config_flow_nanocul(monkeypatch, transport)

    result = await _start_user_flow(hass)
    assert result["type"] == FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_SERIAL_URL: "fake://good",
            CONF_STATION_ID: "01",
        },
    )
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SERIAL_URL] == "fake://good"
    assert result["data"][CONF_STATION_ID] == "01"
    # No heaters field anywhere in the flow: entry.data never carries one,
    # only entry.options, and only once the coordinator's own discovery
    # scan (or pairing) finds something.
    assert "heaters" not in result["data"]


async def test_user_step_accepts_with_q_reply_and_creates_entry(hass, monkeypatch, enable_custom_integrations):
    """The usual path: a real USB serial transport clears its input buffer on open,
    wiping any boot banner, so the Q reply (sent by _validate_port_sync itself,
    auto-answered by FakeCulTransport.write) is what a normal validate call sees."""
    transport = FakeCulTransport()
    patch_config_flow_nanocul(monkeypatch, transport)

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_SERIAL_URL: "fake://good-q",
            CONF_STATION_ID: "01",
        },
    )
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY


async def test_user_step_rejects_when_no_banner_arrives(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport(auto_reply_q=False)  # stick never answers Q or anything else
    patch_config_flow_nanocul(monkeypatch, transport)

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_SERIAL_URL: "fake://silent",
            CONF_STATION_ID: "01",
        },
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "cannot_connect"


async def test_user_step_rejects_open_failure(hass, monkeypatch, enable_custom_integrations):
    from termoweb_local import nanocul as nanocul_module

    def _raise(url, source_id):
        raise OSError("could not open port")

    monkeypatch.setattr(nanocul_module, "NanoCul", _raise)

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_SERIAL_URL: "fake://missing",
            CONF_STATION_ID: "01",
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"]["base"] == "cannot_connect"


async def test_user_step_rejects_invalid_station_id(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    transport.queue_line("# termoweb_rx 3.2 freq=869.525 mode=dynamic tx=pa0xC0")
    patch_config_flow_nanocul(monkeypatch, transport)

    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_SERIAL_URL: "fake://good",
            CONF_STATION_ID: "zz",
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"][CONF_STATION_ID] == "invalid_station_id"


async def test_options_flow_sets_dev_id(hass, monkeypatch, enable_custom_integrations):
    """docs/91-p4-parity-plan.md P4a: dev_id is an owner decision made
    switchable here, not just at initial setup."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL_S,
            CONF_DEV_ID: "custom-dev-id",
            CONF_PAIR_HEATER_SECONDS: DEFAULT_PAIR_HEATER_SECONDS,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_DEV_ID] == "custom-dev-id"


async def test_options_flow_defaults_dev_id_to_cloud_value(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["data_schema"]({})[CONF_DEV_ID] == DEFAULT_DEV_ID
    assert result["data_schema"]({})[CONF_PAIR_HEATER_SECONDS] == DEFAULT_PAIR_HEATER_SECONDS


async def test_options_flow_heaters_and_identities_survive_an_unrelated_save(
    hass, monkeypatch, enable_custom_integrations
):
    """The options flow has no heater field of its own (owner direction
    2026-09-06: adding/removing/renaming a heater is the scan/pairing
    buttons' and HA's own device UI's job), but an options-flow
    async_create_entry replaces entry.options wholesale -- saving the poll
    interval or dev id must not silently drop whatever heaters the
    discovery scan already found."""
    from custom_components.termoweb_local.const import CONF_HEATERS

    transport = FakeCulTransport(status_reply_ids={0x02, 0x04, 0x05})
    entry = await setup_entry(hass, monkeypatch, transport)
    assert {h["id"] for h in entry.options[CONF_HEATERS]} == {0x02, 0x04, 0x05}

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_POLL_INTERVAL: 600,
            CONF_DEV_ID: DEFAULT_DEV_ID,
            CONF_PAIR_HEATER_SECONDS: DEFAULT_PAIR_HEATER_SECONDS,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_POLL_INTERVAL] == 600
    assert {h["id"] for h in entry.options[CONF_HEATERS]} == {0x02, 0x04, 0x05}


async def test_options_flow_sets_pair_heater_seconds(hass, monkeypatch, enable_custom_integrations):
    """CONF_PAIR_HEATER_SECONDS is now a persisted option (owner direction
    2026-09-06 task 3), the window length the "Pair heater" button opens
    for -- not the one-shot trigger it used to be."""
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL_S,
            CONF_DEV_ID: DEFAULT_DEV_ID,
            CONF_PAIR_HEATER_SECONDS: 30,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_PAIR_HEATER_SECONDS] == 30
    # A real, persisted option change reloads the entry as normal (unlike a
    # heater the discovery/pairing workflow adds, which does not).
    reloaded = entry.runtime_data
    assert reloaded.pair_heater_seconds == 30.0


async def test_options_flow_rejects_invalid_pair_seconds(hass, monkeypatch, enable_custom_integrations):
    transport = FakeCulTransport()
    entry = await setup_entry(hass, monkeypatch, transport)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL_S,
            CONF_DEV_ID: DEFAULT_DEV_ID,
            CONF_PAIR_HEATER_SECONDS: 9999,
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"][CONF_PAIR_HEATER_SECONDS] == "invalid_pair_seconds"

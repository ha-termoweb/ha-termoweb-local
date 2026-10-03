"""Schedule card frontend registration (plans/schedule-card.md WP1): the
static path and extra JS module are registered once per hass, not once per
config entry, and a second entry's setup (or a reload) must not re-register
either or raise."""
from unittest.mock import AsyncMock, MagicMock

from homeassistant.components.http import HomeAssistantHTTP
from homeassistant.setup import async_setup_component

import custom_components.termoweb_local as termoweb_local_init

from .conftest import FakeCulTransport, setup_entry


async def test_frontend_registered_once_across_two_config_entries(
    hass, monkeypatch, enable_custom_integrations
):
    # http/frontend are real dependencies (manifest.json "dependencies"):
    # set them up for real, once, before mocking -- otherwise the mock below
    # would also catch frontend's own internal static-path registration for
    # its own assets, which is not what this test is about.
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "frontend", {})
    await hass.async_block_till_done()

    register_mock = AsyncMock()
    monkeypatch.setattr(HomeAssistantHTTP, "async_register_static_paths", register_mock)
    add_js_mock = MagicMock()
    monkeypatch.setattr(termoweb_local_init, "add_extra_js_url", add_js_mock)

    transport_a = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport_a, serial_url="fake://bench-a")

    assert register_mock.call_count == 1
    assert add_js_mock.call_count == 2
    registered_configs = register_mock.call_args[0][0]
    assert len(registered_configs) == 1
    assert registered_configs[0].url_path == "/termoweb_local_frontend"

    module_call, es5_call = add_js_mock.call_args_list
    module_url = module_call.args[1]
    assert module_url.startswith("/termoweb_local_frontend/termoweb-local-schedule-card.js?v=")
    assert module_call.kwargs["es5"] is False

    es5_url = es5_call.args[1]
    assert es5_url.startswith(
        "/termoweb_local_frontend/termoweb-local-schedule-card.es5.js?v="
    )
    assert es5_call.kwargs["es5"] is True

    transport_b = FakeCulTransport()
    await setup_entry(hass, monkeypatch, transport_b, serial_url="fake://bench-b")

    # A second config entry's setup must not register the static path or
    # push either extra JS module again.
    assert register_mock.call_count == 1
    assert add_js_mock.call_count == 2

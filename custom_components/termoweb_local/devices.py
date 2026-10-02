"""Device registry helpers shared by every platform.

docs/91-p4-parity-plan.md "Naming scheme": one gateway device, identifiers
`(DOMAIN, dev_id)`, representing the nanoCUL station; one device per heater,
identifiers `(DOMAIN, f"{dev_id}:{addr}")`, `via_device` pointing at the
gateway device. `dev_id` is the config option (CONF_DEV_ID, default `nanocul`,
settable to the cloud integration's own dev_id), not the config entry id.
"""
from __future__ import annotations

from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, GATEWAY_DEVICE_NAME, GATEWAY_MODEL


def gateway_device_info(dev_id: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, dev_id)},
        name=GATEWAY_DEVICE_NAME,
        manufacturer="ATC",
        model=GATEWAY_MODEL,
    )


def heater_device_info(
    dev_id: str, node_id: int, name: str, via_device_id: str | None = None
) -> DeviceInfo:
    """`via_device_id` is the gateway device's registry id (device_registry.
    DeviceEntry.id), not its identifiers tuple: DeviceInfo dropped the
    identifiers-based `via_device` key (deprecated, removed in 2027.8.0) in
    favour of this one, so the gateway device must already be registered
    (TermowebLocalCoordinator.async_setup) before any heater entity builds
    its own DeviceInfo."""
    info = DeviceInfo(
        identifiers={(DOMAIN, f"{dev_id}:{node_id}")},
        name=f"{name} heater",
        manufacturer="Sun Ray",
        model="Termoweb heater (local control)",
    )
    if via_device_id is not None:
        info["via_device_id"] = via_device_id
    return info

"""Shared service-error helpers.

docs/91-p4-parity-plan.md parity matrix: entities/services marked
"placeholder" (no radio frame captured yet, capture owed in P4b) or "N/A"
(accumulator-only or cloud-only, out of scope here) must not silently no-op;
each one logs a warning identifying the call, then raises HomeAssistantError
so an automation built against the cloud integration gets a clear failure.

raise_not_confirmed applies the same rule to the backed commands: when the
coordinator reports that the heater never acknowledged a write, the service
call fails instead of returning as if the write had landed.
"""
from __future__ import annotations

import logging

from homeassistant.exceptions import HomeAssistantError

from .const import ERROR_NOT_APPLICABLE, ERROR_NOT_CONFIRMED, ERROR_NOT_SUPPORTED_YET


def raise_not_supported(logger: logging.Logger, action: str) -> None:
    """For the "placeholder" rows: radio-backed in principle, no frame
    captured yet (P4b)."""
    logger.warning("%s: %s", action, ERROR_NOT_SUPPORTED_YET)
    raise HomeAssistantError(ERROR_NOT_SUPPORTED_YET)


def raise_not_applicable(logger: logging.Logger, action: str) -> None:
    """For the "N/A" rows: accumulator-only (no hardware here) or cloud-only
    (no local equivalent by design)."""
    logger.warning("%s: %s", action, ERROR_NOT_APPLICABLE)
    raise HomeAssistantError(ERROR_NOT_APPLICABLE)


def raise_not_confirmed(logger: logging.Logger, node_id: int, action: str) -> None:
    """For a backed command whose coordinator call returned False."""
    message = ERROR_NOT_CONFIRMED.format(node_id=node_id, action=action)
    logger.warning("%s; entity keeps its last confirmed value", message)
    raise HomeAssistantError(message)

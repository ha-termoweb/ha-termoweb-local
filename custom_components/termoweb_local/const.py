"""Shared constants for the termoweb_local integration.

Field meanings referenced here (mode codes, the heating-flag candidate byte, the
override preset) come from termoweb_local.heater and termoweb_local.network, which
in turn cite docs/PROTOCOL.md sections 5.1, 5.2, 5.4 and 5.7; this module does not
add any new protocol claim, it only names the config keys and defaults the config
flow and platforms share.
"""

DOMAIN = "termoweb_local"

CONF_SERIAL_URL = "serial_url"
CONF_STATION_ID = "station_id"
CONF_HEATERS = "heaters"
CONF_POLL_INTERVAL = "poll_interval"

# docs/91-p4-parity-plan.md P4a: dev_id is an owner decision the plan leaves
# pending, so it is an editable options-flow field rather than a baked-in
# guess, per the plan's "make each switchable in the config flow options"
# instruction. The per-heater rated power option that sat beside it is gone:
# the heater reports its own measured full-load power in every report
# (docs/PROTOCOL.md 5.4), which is both better than a nameplate figure and
# always present, so there is nothing left for an owner to configure.
CONF_DEV_ID = "dev_id"

# Per-heater EB association-family value (docs/PROTOCOL.md 5.6), sent at
# station start by Network.startup_sequence; optional, default skip (owner
# direction 2026-09-06, task 2). Stored as a hex string per heater (JSON
# options storage needs a string, not bytes).
CONF_HEATER_ASSOCIATION = "heater_association_hex"

# Per-heater advanced-setup record (C4, 2026-09-13 X-19 proof, PROTOCOL.md
# 5.6): control_mode/units/offset_tenths/away_mode/away_offset/
# modified_auto_span/window_mode/true_radiant, persisted so a restart still
# knows the record last written -- four of its eight fields (control_mode,
# units, window_mode, true_radiant) have no radio readback at all
# (docs/captures/2026-09-13-x19/notes.md), so this persisted copy is their
# only state across a restart. Keyed the same way as CONF_HEATER_ASSOCIATION.
CONF_HEATER_ADVANCED_SETUP = "heater_advanced_setup"

# Per-heater pairing identity (2026-09-06 17:12:17Z capture): the 12-byte E7
# identity tail an already-paired heater announces, stored so a later restart
# still recognises it and reuses its node id ("the same identity got the same
# id back"). Stored as a hex string per heater, keyed the same way as
# CONF_HEATER_ASSOCIATION.
CONF_HEATER_IDENTITIES = "heater_identities_hex"

# Persisted option (owner direction 2026-09-06 task 3, superseding the
# earlier one-shot options-flow trigger): how many seconds the "Pair
# heater" button's discovery window stays open when pressed.
CONF_PAIR_HEATER_SECONDS = "pair_heater_seconds"
DEFAULT_PAIR_HEATER_SECONDS = 120
MIN_PAIR_HEATER_SECONDS = 0
MAX_PAIR_HEATER_SECONDS = 600

# Heater dict keys stored in entry.options[CONF_HEATERS] (never entry.data --
# no heater is ever typed by hand, see config_flow.py's module docstring).
CONF_HEATER_ID = "id"
CONF_HEATER_NAME = "name"

# A heater's own node id is 2-65 (the gateway firmware's own node tables,
# Ghidra pass 5, docs/captures/2026-09-05-gateway-dump/analysis5.md), which
# supersedes the wider 2-254 the id byte's own range would otherwise allow;
# 1 is the station itself and 255 is the pairing broadcast id. The
# authoritative constants for that range are
# termoweb_local.network.MIN_PAIRED_HEATER_ID/MAX_PAIRED_HEATER_ID (the
# discovery scan and pairing's own id assignment both live there); nothing
# in this integration package duplicates them.

DEFAULT_SERIAL_URL = (
    "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_XXXXXXXX-if00-port0"
)
DEFAULT_STATION_ID = "01"
DEFAULT_POLL_INTERVAL_S = 300
MIN_POLL_INTERVAL_S = 30
MAX_POLL_INTERVAL_S = 3600

# Neutral default dev_id. Set it per config entry (CONF_DEV_ID) to the cloud
# integration's own dev_id for unique-id continuity across a cutover; this is
# only a default. Device identifiers
# are always namespaced under DOMAIN (and, per heater, the addr too), so this
# never collides with the cloud integration's own device registry entries
# even when the dev_id value itself is identical.
DEFAULT_DEV_ID = "nanocul"

# Clock sync cadence (docs/90-phase3-plan.md P4, docs/PROTOCOL.md 5.6 EB clock-sync
# payload): once at coordinator start, then once per day.
CLOCK_SYNC_INTERVAL_S = 24 * 60 * 60

# Link keepalive cadence (docs/captures/2026-09-13-x19/link-test.md): heater 04's
# panel raised the lost-gateway LINK indication after a few minutes with no EB
# frame from the station, cleared by a single EB clock-sync frame; the Tevolve
# gateway sent every heater an EB about every 157s, so 150s is comfortably under
# the panel's own timeout without changing the once-a-day CLOCK_SYNC_INTERVAL_S.
LINK_KEEPALIVE_INTERVAL_S = 150

# climate.py preset mapping (docs/PROTOCOL.md 5.1, 5.2; termoweb_local.network).
PRESET_TEMPORARY_OVERRIDE = "temporary_override"

SERVICE_POLL_NOW = "poll_now"
SERVICE_SYNC_CLOCK = "sync_clock"

# docs/45-cloud-integration-api.md section 3: the cloud's own service names,
# mirrored here so automations built against the cloud integration keep
# working (docs/91-p4-parity-plan.md P4a).
SERVICE_SET_SCHEDULE = "set_schedule"
SERVICE_SET_PRESET_TEMPERATURES = "set_preset_temperatures"
SERVICE_SET_ACM_PRESET = "set_acm_preset"
SERVICE_START_BOOST = "start_boost"
SERVICE_CANCEL_BOOST = "cancel_boost"
SERVICE_IMPORT_ENERGY_HISTORY = "import_energy_history"
SERVICE_WS_DEBUG_PROBE = "ws_debug_probe"

ATTR_NODE_ID = "node_id"
ATTR_PROG = "prog"
ATTR_PTEMP = "ptemp"
ATTR_COLD = "cold"
ATTR_NIGHT = "night"
ATTR_DAY = "day"
ATTR_MINUTES = "minutes"
ATTR_TEMPERATURE_FIELD = "temperature"
ATTR_RESET_PROGRESS = "reset_progress"
ATTR_MAX_HISTORY_RETRIEVAL = "max_history_retrieval"
ATTR_ENTRY_ID = "entry_id"
ATTR_DEV_ID = "dev_id"

# docs/91-p4-parity-plan.md parity matrix: every "placeholder"/"N/A" cloud
# surface raises one of these two errors instead of silently no-opping, so an
# automation built against the cloud integration gets a clear failure instead
# of a state that never changes.
ERROR_NOT_SUPPORTED_YET = (
    "not supported by the radio protocol yet, see docs/91-p4-parity-plan.md"
)
ERROR_NOT_APPLICABLE = (
    "not applicable to the local integration (cloud-only or accumulator-only "
    "feature), see docs/91-p4-parity-plan.md"
)

# docs/45-cloud-integration-api.md unique id shape: termoweb:<dev_id>:<node_type>:
# <addr>:<entity_kind>; the local integration keeps the same shape with only the
# domain prefix swapped (docs/91-p4-parity-plan.md "Naming scheme").
CLOUD_NODE_TYPE_HEATER = "htr"
CLOUD_NODE_TYPE_GATEWAY = "gateway"
GATEWAY_ADDR = "gateway"

GATEWAY_MODEL = "nanoCUL868 termoweb_rx"
GATEWAY_DEVICE_NAME = "Termoweb Local gateway (nanoCUL)"

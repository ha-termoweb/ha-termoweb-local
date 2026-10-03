# Termoweb Local (Home Assistant integration)

Local, cloud-free control of Sun Ray heaters over a [nanoCUL868](https://www.ebay.ie/itm/372221622516) stick, on top of the `termoweb_local` Python package vendored under `vendor/termoweb_local/`. No gateway, no cloud, no daemon: Home Assistant talks to the stick over serial directly.

## Versions this was built and tested against

- Home Assistant core 2026.9.1 on Home Assistant OS
- Python 3.14

## Install via HACS

1. In [HACS](https://hacs.xyz/), add this repository as a [**custom repository**](https://hacs.xyz/docs/faq/custom_repositories/) (category: Integration).
2. Install "Termoweb Local (unofficial)" and restart Home Assistant.
3. Add the integration from Settings -> Devices & services -> Add integration -> "Termoweb Local".

The full walkthrough, including flashing the stick, is [docs/installation.md](../../docs/installation.md).

The `termoweb_local` protocol package is vendored under `vendor/termoweb_local/`, so the only requirement Home Assistant installs is [`pyserial`](https://pyserial.readthedocs.io/). `_vendor_compat.py` puts the vendored copy on the import path only when no other `termoweb_local` is installed.

## Serial device passthrough on Home Assistant OS

Point the config flow's "Serial URL" field at the `by-id` path, not `/dev/ttyUSB0` (the device node's kernel-assigned number is not stable across reboots or replugs):

```
/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_XXXXXXXX-if00-port0
```

- **Supervised / HA OS, this integration running as a custom component inside HA core itself**: the `by-id` path above is visible directly, no extra passthrough step needed, since HA core runs on the host (or the OS's own container) with the full `/dev/serial/by-id/` tree already bind-mounted.
- If you instead run Home Assistant inside a container you built yourself (not HA OS's own supervised install), pass the whole `/dev/serial/by-id/` directory through (`devices:` in your container/compose config), not just the one node, so the symlink target still resolves inside the container.

## Development: `socket://` against a TCP bridge

During development the nanoCUL can stay on a bench host, exposed over TCP ([ser2net](https://github.com/cminyard/ser2net) in raw mode, `max-connections: 1`, and a `local` connector so a TCP reconnect never toggles DTR and resets the stick), with the integration's Serial URL set to:

```
socket://192.0.2.50:7000
```

`termoweb_local.nanocul.NanoCul` passes the URL straight to [`serial.serial_for_url`](https://pyserial.readthedocs.io/en/latest/url_handlers.html), so this needs no code change, only a different string in the config flow. The full setup is [docs/remote-stick.md](../../docs/remote-stick.md).

## One-owner-of-the-port rule

Exactly one process may hold the nanoCUL's serial port at a time. This integration's `DataUpdateCoordinator` opens the port once per config entry and keeps it open for the coordinator's lifetime; do not also run a serial logger or a second `NanoCul`/integration instance against the same port while this integration is running, and do not run this integration against a port another tool is currently using. Removing the integration (or reloading its config entry) closes the port; the reverse -- another tool grabbing the port while this integration still holds it -- will make both readers see garbled or dropped frames, not a clean error, so check first.

## Entities

This integration's API (entity kinds, attributes, service names and fields) mirrors the cloud [`termoweb` integration](https://github.com/ha-termoweb/ha-termoweb), so an automation built against the cloud's services and attributes keeps working unchanged, but entity ids do not need to match: this integration derives its own from the configured heater name, so the two integrations can run side by side without ever colliding on an entity id.

Naming rule: a heater's object id is a slug of its configured name plus `_heater` (`<slug>_heater`), with the entity kind appended for every platform except climate itself (`<slug>_heater_<kind>`) -- unless the slug already contains "heater" as one of its underscore-separated words, in which case the slug is used as-is with no `_heater` suffix appended (`<slug>_<kind>`). The three default heaters ("Living room", "Bedroom", "Master bedroom") give `living_room_heater`, `bedroom_heater` and `master_bedroom_heater`. A discovered heater's default name, "Heater `<id>`" (two hex digits, e.g. "Heater 02"), already contains "heater", so it gives `heater_02` rather than `heater_02_heater` (and `sensor.heater_02_temperature`, `sensor.heater_02_power`, `sensor.heater_02_energy`, `button.heater_02_flash_display`, `number.heater_02_priority`, `lock.heater_02_child_lock`); the same applies to a user-given name that already says "heater", such as "Bedroom heater" giving `bedroom_heater` rather than `bedroom_heater_heater`. Friendly names follow the same pattern: "Living room heater", "Living room heater temperature", and so on. Unique ids are `termoweb_local:<dev_id>:<node_type>:<addr>:<kind>` (API-parity shape, domain prefix `termoweb_local`), unaffected by heater renames. One gateway device (the nanoCUL station, named "Termoweb Local gateway (nanoCUL)") and one device per heater (named "<Name> heater"), `via_device` from every heater to the gateway. Device identifiers are namespaced under `termoweb_local` (and, per heater, the heater's own addr too), so they never collide with the cloud integration's device registry entries even when the `dev_id` option is left at its default (the cloud system's own value).

- `climate.<slug>_heater`: hvac modes off/heat/auto, preset none/temporary_override, target temperature in 0.5 C steps from 7 to 35 C, matching the range the Tevolve app itself offers. The heaters on this bench system were observed on 2026-09-06 applying only 7.0-26.0 C: a value above 26.0 is still acknowledged by the heater exactly like an accepted one, then silently ignored, so the target temperature this entity actually shows always follows the heater's own reported setpoint, never the value last requested. `min_temp`/`max_temp`/`current_temperature`/`temperature`/`preset_mode`/`dev_id`/`addr`/`units`/`icon`/`supported_features` matching the cloud's attribute set. `hvac_action` comes from the E5 heating-flag candidate byte (byte 24) once a report has arrived. `prog` is populated from `read_program` (an F3 B0 request) at setup and after every `set_schedule` call. `max_power` is the heater's own measured full-load power and `ptemp` its anti-frost, eco and comfort presets (`ptemp_supported: true`).
- `sensor.<slug>_heater_temperature`: E5 bytes 18-19, proven against Home Assistant per the protocol notes (handover item 1's reference-thermometer proof is still open, but the byte offset itself is already usable per the plan).
- `sensor.<slug>_heater_power`: **provisional**. E5 byte 23 (duty candidate) scaled by the heater's own measured full-load power, E5 bytes 21-22 in deciwatts: `duty / 100 * measured_power_w`. No owner configuration of any kind: every heater reports that rating in every report and holds its last value while idle, so the sensor is available as soon as any report has arrived. Still marked provisional because which of bytes 23 and 24 carries the duty percent is open; attributes `full_load_power_w` (the measured rating the duty was scaled by) and `provisional_reason` name what is measured and what is not.
- `sensor.<slug>_heater_energy`, `sensor.termoweb_local_total_energy`: **backed**. The heater's own cumulative watt-hour meter, read with F3 BC and carried by the EF reply, divided by 1000 for kWh; the gateway sensor sums the per-heater counters and carries a `heaters_reporting` attribute. Nothing is integrated from power or elapsed time, so `TOTAL_INCREASING` is exact and the value survives a station restart, a re-pairing and a factory reset of the station side. The coordinator polls each heater hourly, the cadence the real gateway uses. State is `unknown` only until a heater's first poll has answered.
- `sensor.<slug>_schedule`: the program value for the current local hour, as `cold`/`night`/`day` (`unknown` before any program has been read for that heater). Object id and friendly name ("<Name> schedule") both skip the `_heater` infix every other per-heater sensor kind gets. Attributes: `prog` (the same 168 hourly values, Monday 00:00 first, as the climate entity's own `prog`), `monday` through `sunday` (24 values each, sliced from `prog`), `current_slot_index` (0-167, the current hour's position in `prog`), `next_change` (local ISO time of the next hour whose value differs from the current one, or `None` if every slot holds the same value), and `source` (`read` after an F3 B0 read, `report` after a heater's own unsolicited 9E program-report push, `written` after a `set_schedule` call). Updates whenever the program cache changes through a path the coordinator already notifies its listeners for (a report, or `set_schedule`'s own push), and once a minute regardless, since the current-hour state changes on the hour even when the cache itself has not.
- `binary_sensor.termoweb_local_gateway_online`: one gateway-level entity (not per-heater). On when the serial link is open and at least one heater has reported within 2 of its own expected report periods; attributes `dev_id`, `name`, `connected`, `model`, `link_status`, `last_frame_at`, `link_healthy_minutes` (local equivalents of the cloud's `ws_status`/`ws_last_event_at`/`ws_healthy_minutes`). Derived only from report cadence, never RSSI/LQI.
- `button.<slug>_heater_flash_display`: backed. Sends F2 5E 01 and waits for the heater's own F2 5F 55 reply, logging a warning if it does not arrive.
- `button.termoweb_local_force_refresh`: backed. Sends an F3 B8 status request to every configured heater (wraps `poll_now` for all heaters).
- `button.termoweb_local_scan_for_heaters`: backed. Repeats the ids-2-to-65 discovery scan on demand; see "Adding heaters" below.
- `button.termoweb_local_pair_heater`: backed. Opens a discovery window; see "Adding heaters" below.
- `number.<slug>_heater_priority`: **placeholder**. Range 0-30 step 1, state always `unknown`; setting it raises `HomeAssistantError`.
- `number.<slug>_heater_temperature_offset`: **backed, panel-confirmed** (C4, X-19 2026-09-13). -3.0 to 3.0 C in 0.1 C steps, applied within seconds and confirmed by an unsolicited room-temperature report. The value matches the panel's own Temp Offset exactly: a negative offset raises the reported temperature, so the entity is the raw wire offset byte unchanged, not inverted.
- `lock.<slug>_heater_child_lock`: **backed** (BA, X-19 2026-09-13). Lock/unlock send `BA 01`/`BA 00`; state follows the E5/E6 byte-24 lock flag on every status report.
- `switch.<slug>_heater_boost`: **backed** (D2, X-19 2026-09-13). Starts/cancels Boost; state follows byte 24's own boost flag. The duration (60 minutes on this installation) is fixed by the heater's own boost-time setting, not something this entity can adjust. A `boost_temperature` attribute (from an F3 DA read taken at startup and after every toggle) and `boost_end_day`/`boost_end_min` appear on the climate entity while boost is active.
- `switch.<slug>_heater_easy_mode`: **backed** (D6, X-19 2026-09-13). Forces mode heat with the setpoint unchanged; turning it off restores the previous mode. State follows byte 24's own EASY flag.
- `switch.<slug>_heater_runback`: **backed** (D4, X-19 2026-09-13). Forces mode heat and the setpoint to the anti-frost preset; state follows byte 24's own Runback flag. Runback off does not itself restore the setpoint it displaced, so this integration caches the pre-toggle mode/setpoint and always restores both on the way off, whatever mode the heater was in beforehand, including auto/off; the `restored` attribute is `None` before this coordinator has ever turned Runback off for this heater (e.g. a panel-originated Runback off with no cache), `True` once a restore has run and every restore command succeeded, `False` only if one of those commands failed. The cache is in-memory only and does not survive a coordinator restart.
- `switch.<slug>_heater_open_window_detection`: **backed, no radio confirmation** (C4's own `window_mode` field, X-19 2026-09-13). State is read from the persisted record this integration last wrote, not from a radio reply: the field has no readback on this heater (the panel toggle is the only confirmation, per the capture notes), flagged unconfirmed for that reason.
- `switch.<slug>_heater_true_radiant`: **backed** (C4's own `true_radiant` field, X-19 2026-09-13). Panel-confirmed: the Sun Ray RF panel has no True Radiant menu at all, so the heater accepts the byte with no panel effect. Kept for API parity with the cloud integration; state is read from the persisted record, optimistic between writes.
- `select.<slug>_heater_control_mode`: **backed, panel-confirmed** (C4's own `control_mode` field, X-19 2026-09-13). Options: `PID`, `Hysteresis 0.25C`, `Hysteresis 0.35C`, `Hysteresis 0.5C`, `Hysteresis 0.75C` -- the full 0-4 wire-to-label map was confirmed at the panel. State is read from the persisted record, like the switches above.
- `select.<slug>_heater_units`: **backed, no radio confirmation** (C4's own `units` field, X-19 2026-09-13). Options C/F; accepted on this heater with no on-air change in temperature encoding either way, panel check owed.

### Options (parity-specific)

- **Device id** (`dev_id`, default `nanocul`, the cloud system's own value): used in unique ids and the device registry. Reusing the cloud's value gives continuity across a cutover; changing it does not collide with the cloud integration either way, since unique ids and device identifiers are namespaced per integration domain.
- **Heater association** (`heater_association_hex`, `id:<18 hex chars>` entries, comma separated, default none): the opaque 9-byte EB association-family value sent to that heater at station start; a heater with no entry here simply skips that frame.
- **Pair heater window** (`pair_heater_seconds`, default 120): how long the "Pair heater" button's discovery window stays open when pressed.

## Services

- `termoweb_local.poll_now`: send an immediate on-demand status request (F3 B8) to a targeted heater's climate entity, instead of waiting for the coordinator's own cadence.
- `termoweb_local.sync_clock`: send the EB clock-sync frame to a targeted heater's climate entity right now, instead of waiting for the daily sync.
- `termoweb_local.set_schedule`: backed. Write the weekly `prog` (168 hourly slot values, Monday 00:00 first) with `write_program`, then read it back with `read_program` so the climate entity's `prog` attribute reflects what the heater actually stored.
- `termoweb_local.set_preset_temperatures`: backed. Writes the anti-frost, eco and comfort presets (B6), waits for the heater's confirmation, then re-reads its status.
- `termoweb_local.set_acm_preset`, `termoweb_local.start_boost`, `termoweb_local.cancel_boost`: **N/A**. Accumulator nodes only; none of the three heaters on this bench system are accumulators. Always raise `HomeAssistantError`.
- `termoweb_local.import_energy_history`, `termoweb_local.ws_debug_probe`: **N/A**. Cloud-only (samples endpoint / websocket connection), no local equivalent. Always raise `HomeAssistantError`.

## Schedule card

A weekly schedule editor Lovelace card, `custom:termoweb-local-schedule-card`, ships inside this integration (`frontend/`, plain ES modules, no build step: `termoweb-local-schedule-card.js` is the entry, importing `schedule-card.js`, `schedule-card-editor.js`, `schedule-grid.js`, `schedule-styles.js` and `presets.js`). It is registered as an extra frontend module the first time any config entry of this integration loads, so it needs no `resources:` entry in your dashboard configuration.

Add it from the card picker: it now shows a live preview (a heater entity of this integration, if one exists, is pre-selected) and has a visual editor, so no YAML is required. The editor is a single field, "Heater" (a `climate.` entity picker filtered to this integration), plus an optional "Title". YAML still works if you prefer it:

```yaml
type: custom:termoweb-local-schedule-card
entity: climate.heater_02
title: Heater 02 schedule
```

`entity` is required once you start using the card and must be a `climate.<slug>_heater` entity of this integration; leaving it unset (as in the picker's blank preview) shows a "select a heater" message instead of an error. `title` is optional. The card reads its grid width from the entity's own `prog` length (168 hourly slots or 336 half-hourly slots) and writes back with `termoweb_local.set_schedule`.

Clicking an hour slot cycles it cold, night, day, cold; dragging across slots (mouse or touch) paints every slot it passes over with the value the first slot cycled to, and a focused slot (Tab/arrow keys) cycles the same way on Enter or Space. Each row's "Copy to" button starts copy mode for that row: the button turns into a check mark and a cross in place, and every other row's "Copy to" button turns into an unchecked checkbox. Check the days you want and confirm (the check mark) to copy that row's slots onto every checked day, locally, marking the grid dirty the usual way; the cross, or Escape, leaves copy mode with no change instead. Only one row can be in copy mode at a time, and it is unaffected by the heater's own state pushes arriving while it is open. Revert/Save behave as before; Save always sends the whole `prog` at the grid's current width.

The legend above the grid shows each preset's current temperature next to its colour; it does not select a paint colour. Clicking a preset turns that legend button in place into an inline stepper: its colour dot stays put on the left, then minus, the value (display only, not typeable), plus, a confirm button (a check mark) and a cancel button (a cross), in 0.5 C steps. Only one preset is editable at a time. Plus/minus are disabled at that preset's own bound under the ordering rule (7.0 C <= anti-frost < eco < comfort <= 35.0 C, each at least 0.5 C from its neighbour): anti-frost from 7.0 C up to eco minus 0.5 C, eco from anti-frost plus 0.5 C up to comfort minus 0.5 C, comfort from eco plus 0.5 C up to 35.0 C. Enter confirms and Escape cancels, matching the confirm/cancel buttons. Confirm calls `termoweb_local.set_preset_temperatures` with all three current preset values, the edited one changed; the stepper disables while that call is in flight and shows the heater's own rejection message if it fails, and closes and re-reads the entity's `ptemp` once it succeeds.

## Protocol behaviour this integration implements

- The coordinator's periodic refresh (and `poll_now`) send an F3 B8 on-demand status request per heater, answered by an E6 status reply; this is the cadence step, not F2 57 55, which elicits no reply at all.
- F2 57 55 is a report confirmation, not a poll: the coordinator sends it to a heater right after every report that heater sends (E5, E2, EA, or the 9E program report), mirroring what the real gateway does, regardless of the report's own on-air class.
- After a setpoint, mode, or override command, the coordinator waits briefly for the F2 B5 55 (or B3 55 after a program write) reply that means the heater actually processed the command, and logs a warning if it does not arrive within that window.
- Setpoint values are accepted (sent to the heater) in the 7.0-35.0 C range, matching the Tevolve app's own range; a value outside it is rejected before anything is sent. The heaters observed on 2026-09-06 applied only 7.0-26.0 C themselves: a value above 26.0 is acked and replied to exactly like an accepted one, then silently ignored by the heater, which is why the target temperature shown always follows the heater's own reported setpoint rather than the value last requested.

## Gateway emulation

This integration stands in for the real Termoweb gateway on the radio, not just for its cloud API: it runs the same station-side sequences the real gateway does, so a heater cannot tell the difference.

- **On every connect and reconnect**: the coordinator's `_configure_stick()` sends `I<station id>` then `A1` to the stick before the `Q` status query, setting the station id and hardware auto-ack explicitly rather than relying on inherited stick state or the firmware's `our_id` default of `0x01` happening to match. The firmware defaults hardware auto-ack off, and with a ser2net `local` connector, a TCP reconnect never toggles DTR, so the stick is never reset by the integration and otherwise comes up in whatever state it was left in; without the link-layer ack, heaters retransmit each report 3 times about 160 to 190 ms apart, separate from the application-layer repeat at 4.6 to 8.1 s that `F2 57 55` suppresses.
- **At station start**: the coordinator syncs the clock (EB, steady prefix) to every already-configured heater, runs each one's own power-up query burst (`Network.startup_sequence`: the heater's EB association value if one is configured for it, F3 B0 program read populating `prog`, F2 CA 00 burst terminator, then F3 C2, F3 5A identity, F3 D0 capability and F3 C6, each waited on for its own reply -- a missing ack or reply for one step never stops the rest), and then runs the ids-2-to-65 discovery scan (see "Adding heaters" below).
- **On a heater's own registration frame** (an F3-length frame with payload `50`, sent when a heater powers back up with the station already running): the coordinator replies with an EB clock sync using the *registration* prefix, not the station-start query burst above -- the burst is the gateway's own power-up behaviour, not its reply to a heater re-registering. An id this coordinator has never seen before still gets this reply, and is registered as a new heater at the same time (see below).
- **On every report** a heater sends (E5, E2, EA, or the 9E program report pushed as part of a registration reply), the coordinator confirms it with F2 57 55; the 9E program report is additionally decoded and stored exactly like a fresh `prog` read-back.

## Adding heaters

Heaters are never typed by hand. Three ways a heater joins the integration, all with no Home Assistant restart needed -- its device and every entity appear immediately:

- **The setup-time scan**: every time the integration starts (a fresh install, a restart, or a reload), the coordinator probes ids 2 to 65 with an on-demand status request; any id that answers and is not already configured is registered as a new heater named "Heater `<id>`" (two hex digits, e.g. "Heater 05"), and its startup burst runs like any other heater's. An id already configured is left alone -- the scan only confirms it is still there, it never re-adds or renames it.
- **`button.termoweb_local_scan_for_heaters`**: repeats that same scan on demand, for a heater that was powered on after Home Assistant already started.
- **`button.termoweb_local_pair_heater`**: opens a discovery window (default 120 s, the `pair_heater_seconds` option) during which a heater's own E7 pairing announcement (sent from the radio's broadcast id, once you put the physical heater into its own pairing mode -- see its [manual](https://atc.ie/wp-content/uploads/Manual-atc-Sun-Ray-RF_v07.pdf)) is acked, assigned the lowest free id from 2 to 65 (or its previously assigned id, if this exact heater paired before), and registered the same way. A persistent notification names the id that was assigned. `binary_sensor.termoweb_local_gateway_online`'s own `pairing_active` attribute shows whether the window is currently open.
- **A runtime report from an unknown id**: if a heater the scan missed sends a report or its own registration-opening frame while Home Assistant is already running, it is registered on the spot, the same way.

Every discovered heater is persisted (its id and name) to the config entry so a restart keeps it and its name, rather than losing it or renaming it back to the default.

### Renaming and removing a heater

Rename a heater from its own device page or from one of its entities' settings (Settings -> Devices & services -> Termoweb Local -> the heater's device, or any of its entities) -- this is Home Assistant's own rename, which changes the friendly name shown in the UI without touching the entity id or unique id created when the heater was first discovered.

Remove a heater by deleting its device from the same device page (the trash-can/Delete action). This drops it from the integration's persisted heater set and its stored pairing identity (freeing that id for a different physical heater to claim later) and removes every entity registered under that device, with no Home Assistant restart needed. The station's own gateway device cannot be removed this way; removing the whole integration is the equivalent action for it.

## Options

Settings -> Devices & services -> Termoweb Local -> Configure lets you change the poll interval (default 300 s, mirroring the gateway's own idle cadence, the protocol notes), the "Pair heater" button's window length, and the parity-specific options above (device id, heater association). There is no heater field here at all -- see "Adding heaters" above for how a heater joins, and "Renaming and removing a heater" for the other two.

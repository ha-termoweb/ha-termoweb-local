# Configuration

First setup (the Serial URL and station id) is in [installation.md](installation.md#4-add-the-integration). This document covers what you can change afterwards and how heaters join and leave.

## Options

Settings -> Devices & services -> Termoweb Local -> **Configure**.

| Option | Default | Range | What it does |
|---|---|---|---|
| Poll interval | 300 s | 30 to 3600 s | How often each heater is asked for its status. Heaters also report on their own when something changes, so a long interval does not delay those. |
| Pair heater window | 120 s | 0 to 600 s | How long the **Pair heater** button listens for a heater's pairing announcement. |
| Device id | `nanocul` | any non-empty text | Used in entity unique ids and device registry identifiers. See below. |
| Heater association | empty | `id:<18 hex>` entries, comma separated | An optional value sent to a heater at station start, which the original gateway sends. Heaters work without it; leave it empty unless you know you need it. |

### Device id

The default, `nanocul`, is fine for a new installation. If you are moving from the cloud [`termoweb` integration](https://github.com/ha-termoweb/ha-termoweb) and want unique ids shaped the same way, set it to the `dev_id` the cloud integration shows on its climate entities' attributes. Either way the two integrations never collide, because every id is namespaced under its own domain.

Your cloud `dev_id` identifies your account, so keep it out of anything you share ([privacy.md](privacy.md)). Change it only on a fresh setup: changing it later gives every entity a new unique id.

## Adding heaters

Heaters are never typed in. A heater joins in one of four ways, with no restart:

1. **The setup scan.** At every start the integration probes ids 2 to 65; any heater that answers and is new is added as "Heater `<id>`".
2. **The Scan for heaters button**, which repeats that scan on demand.
3. **A report from an unknown id.** A heater the scan missed is added the moment it reports.
4. **Pairing**, for heaters not bonded to this station: a new heater, a factory-reset one, or one from a different gateway.

The scan only finds heaters already bonded to station `01`. An unpaired heater is silent on the radio until it is put into pairing mode.

### Pairing a heater

1. Press **Pair heater**. A notification says the window is open.
2. Within the window (120 s by default), put the heater into pairing mode from its own panel, as its [manual](https://atc.ie/wp-content/uploads/Manual-atc-Sun-Ray-RF_v07.pdf) describes.
3. The integration answers the heater's announcement with the lowest free id from 2 to 65, or the id it had before if this exact heater paired previously.
4. The notification names the assigned id, and the heater's device and entities appear.

If nothing pairs, press the button again and repeat. A heater announces itself a number of times per attempt and the integration answers each one.

The integration remembers each heater's identity (12 bytes the heater announces) against its id, so re-pairing the same heater gives it the same id. These identities are stored in Home Assistant's config entry, not in this repository.

## Renaming and removing heaters

- **Rename** a heater from its device page or any of its entities' settings. This changes the friendly name only; entity ids and unique ids stay.
- **Remove** a heater by deleting its device. This removes its entities and its stored identity, and frees its id. The id is held back for 15 minutes so the same heater's next report does not immediately re-add it.

## Entities, services and the schedule card

See the [integration README](../custom_components/termoweb_local/README.md#entities).

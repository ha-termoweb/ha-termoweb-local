# Documentation

Start with the repository [README](../README.md) for what this project is, the safety notice and the radio notice. The documents below cover the rest.

## Using it

| Document | What it covers |
|---|---|
| [installation.md](installation.md) | Hardware, flashing the stick, installing the integration through HACS, first setup |
| [remote-stick.md](remote-stick.md) | Running the stick on another machine and reaching it over the network with ser2net |
| [configuration.md](configuration.md) | The setup form, the options, adding, pairing, renaming and removing heaters |
| [firmware.md](firmware.md) | Building and flashing termoweb_rx, its serial commands, restoring culfw |
| [troubleshooting.md](troubleshooting.md) | Common problems and what to check |
| [privacy.md](privacy.md) | Which values identify your installation, and keeping them out of issues, logs and pull requests |

The entities, services and the schedule card are described in the [integration README](../custom_components/termoweb_local/README.md). The firmware's design notes are in [firmware/README.md](../firmware/README.md).

## Working on it

| Document | What it covers |
|---|---|
| [development.md](development.md) | Repository layout, running the tests and the linter, the vendored package, the schedule card bundle |
| [ci.md](ci.md) | Every GitHub workflow: what it checks and when it runs |
| [releasing.md](releasing.md) | The `version` file and how a release is cut |
| [secrets.md](secrets.md) | The repository secrets the workflows use, where each value comes from and where to set it |

## Related projects

- [ha-termoweb](https://github.com/ha-termoweb/ha-termoweb): the cloud-based Home Assistant integration for the same heaters, through the Termoweb gateway and its cloud.
- [a-culfw](https://github.com/heliflieger/a-culfw): the stock nanoCUL firmware this project's firmware replaces.
- [HACS](https://hacs.xyz/): the Home Assistant Community Store, which installs this integration.
- [ser2net](https://github.com/cminyard/ser2net): shares the stick's serial port over the network ([remote-stick.md](remote-stick.md)).
- [Sun Ray RF manual](https://atc.ie/wp-content/uploads/Manual-atc-Sun-Ray-RF_v07.pdf) (ATC): the heaters' own panel menus and pairing mode.

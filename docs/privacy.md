# Keeping your installation's data private

This repository is shared by everyone who uses it, so it must contain nothing that identifies one person's home: no serial numbers, no account ids, no heater identities, no addresses. The same goes for issues and pull requests, which are public too.

## What identifies an installation

| Value | Why it matters | Where it shows up |
|---|---|---|
| nanoCUL USB serial | Ties a stick, and anything logged from it, to you. | The `/dev/serial/by-id/` path in the Serial URL, ser2net configs, logs. |
| Termoweb cloud device id | The key the vendor's cloud addresses your account by. | The cloud integration's attributes, the Device id option. |
| Gateway serial or MAC | Identifies your gateway to the vendor. | The gateway's label. |
| Heater identities | 12 bytes each heater carries for life, across factory resets. | Debug logs (`learned identity ...`, pairing lines, full frame hex), `.storage/core.config_entries`. |
| Hostnames and addresses | Locate your network. | Serial URLs, ser2net configs, logs, screenshots. |
| Room names | Describe your home. | Heater names and entity ids in logs and screenshots. |

The network id (`1b30`) and the station id (`01`) are protocol constants, not personal data.

## Sharing a log or a config

Before pasting anything into an issue:

1. Replace the stick serial with `XXXXXXXX`.
2. Replace heater identities with `<identity>`. In debug logs they appear in `learned identity`, `pairing:` lines and inside the hex of `E7` and `E0` frames.
3. Replace hostnames and addresses with `homeassistant.example.net` and `192.0.2.x`, names and addresses reserved for documentation ([RFC 2606](https://datatracker.ietf.org/doc/html/rfc2606), [RFC 5737](https://datatracker.ietf.org/doc/html/rfc5737)).
4. Rename heaters to generic names, or replace room names.

[troubleshooting.md](troubleshooting.md) shows how to turn on the debug logging an issue usually needs.

## Contributing code

- Use the placeholders above in code, tests and documentation. Tests use synthetic identities and frames built from synthetic fields ([development.md](development.md#tests)); never paste captured bytes from your own heaters.
- Configure git with a commit email you are happy to publish. GitHub's `<id>+<username>@users.noreply.github.com` address works ([how](https://docs.github.com/en/account-and-profile/setting-up-and-managing-your-personal-account-on-github/managing-email-preferences/setting-your-commit-email-address)).
- Maintainers: set the `LEAK_PATTERNS` secret so CI blocks your own identifiers ([secrets.md](secrets.md#leak_patterns)).

## If something was committed

Removing a value in a new commit leaves it in the history, where anyone can read it. If it reached a public branch:

1. Treat the value as public. Rotate it where possible; serials and heater identities cannot be rotated.
2. Rewrite the history to remove it, for example with [git filter-repo](https://github.com/newren/git-filter-repo) and its `--replace-text` option, then force-push. This changes every later commit id, so coordinate with anyone who has a clone.
3. Ask GitHub support to purge cached views ([GitHub's guide](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)) and pull request references if the value was sensitive.

Then add the value to `LEAK_PATTERNS` so it cannot come back ([secrets.md](secrets.md)).

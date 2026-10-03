# Repository secrets

The workflows ([ci.md](ci.md)) use one secret you set yourself, `LEAK_PATTERNS`, and the token GitHub provides to every workflow, `GITHUB_TOKEN`.

| Secret | Required | Used by | Set by |
|---|---|---|---|
| `LEAK_PATTERNS` | No, but strongly recommended on any repository you publish | Leak scan | You |
| `GITHUB_TOKEN` | Yes | Release (and every checkout) | GitHub, automatically |

This applies to the main repository and to any fork: secrets are per repository and are not copied when you fork, so set `LEAK_PATTERNS` again in your fork if you publish from it.

## LEAK_PATTERNS

### What it is

A list of strings that identify one real installation and must never appear in the repository: device serial numbers, cloud ids, heater identities, hostnames, addresses. [privacy.md](privacy.md) explains why each one matters.

The Leak scan workflow searches every tracked file for each line of the list, as a fixed string, ignoring case. On a match it fails and prints the file and line number only, never the matched text, so the log does not publish the value either.

The list lives in a secret rather than a file in the repository because a committed list would publish exactly the values it protects.

If the secret is not set, the step passes with a notice and only the generic checks run: gitleaks, private LAN address ranges and home directory paths.

### What goes in it, and where to find each value

One value per line. Blank lines are ignored. Add every value that applies to the installations whose data could end up in the repository; for a single maintainer, that is your own.

| Value | Example shape | Where to find it |
|---|---|---|
| nanoCUL USB serial | `A1B2C3D4` (8 characters) | `ls -l /dev/serial/by-id/` on the machine the stick is plugged into: the part after `FT232R_USB_UART_` in `usb-FTDI_FT232R_USB_UART_<serial>-if00-port0`. |
| Termoweb cloud device id | 18 hex characters | The `dev_id` attribute on any climate entity of the cloud `termoweb` integration, or the Termoweb app's gateway details. Also the value you may have set as Device id ([configuration.md](configuration.md#device-id)). |
| Termoweb gateway serial or MAC | as printed | The label on the gateway. |
| Heater identities | 24 hex characters each, one line per heater | The Home Assistant log, with debug logging on, prints `learned identity <24 hex> for node <id>` for each heater. They are also stored in `config/.storage/core.config_entries` under the `termoweb_local` entry's `heater_identities_hex` option. |
| Home Assistant hostname | `myhome.example.net` | Settings -> System -> Network, or your DNS. Include any external hostname or Nabu Casa remote URL too. |
| Public IP address | `203.0.113.7` | Your router, or any what-is-my-IP service. |
| LAN addresses outside the private ranges | | Only if your LAN does not use 10.x, 172.16-31.x or 192.168.x. Those ranges are already blocked for every address. |
| Your usernames and email | | The username on the machines you develop on (it appears in paths), and any email you would not want in a file. |

To catch values written with spaces between bytes, as logs sometimes print them (`a1 b2 c3 ...`), add that form as its own line.

### What to leave out

- **Short or common strings**, such as `04`, `heater` or a first name on its own. They match ordinary code and fail every build.
- **Protocol constants**, such as the network id `1b30` or the station id `01`. They are in the code legitimately.
- **Values already in the repository on purpose**, such as the placeholder serial `XXXXXXXX` or the documentation addresses `192.0.2.x` and `203.0.113.x`.

Before saving the list, check it does not match the current tree, so the scan starts green:

```
git grep -nIiF -f ~/leak-patterns.txt
```

No output means no matches.

### Where to put it

Write the list to a file outside any repository, for example `~/leak-patterns.txt`, then either:

- **Command line**, with the [GitHub CLI](https://cli.github.com/) logged in as a repository admin:

  ```
  gh secret set LEAK_PATTERNS -R <owner>/<repo> < ~/leak-patterns.txt
  ```

- **Web**: the repository's Settings -> Secrets and variables -> Actions -> New repository secret. Name `LEAK_PATTERNS`, paste the list as the value.

Then delete the file: `shred -u ~/leak-patterns.txt`, or `rm` it.

GitHub never shows a secret's value again, only when it was last updated. Keep the source list somewhere private, such as a password manager note, so you can extend it later. To change it, set it again with the full new list; it replaces the old one.

Run the Leak scan workflow from the Actions tab afterwards to confirm it passes.

### Limits

- It checks the files of the commit being built, not the history. gitleaks covers the history, but only for generic credentials such as API keys. If a value has already been committed, removing it in a later commit does not remove it from the history; see [privacy.md](privacy.md#if-something-was-committed).
- It matches exact strings only. A value written differently (split across lines, reformatted, encoded) is not caught.
- Pull requests from forks get no secrets, so the step skips there and runs once the change reaches `main` ([ci.md](ci.md#pull-requests-from-forks)).

## GITHUB_TOKEN

GitHub creates this token for every workflow run; there is nothing to set. Workflows here use it read-only, except the Release workflow's tagging job, which asks for `contents: write` to push the `v<integration>` and `firmware-v<firmware>` tags and create the release ([releasing.md](releasing.md)).

If an organisation policy restricts workflow permissions to read-only, the Release job fails at the tag push. Allow it under Settings -> Actions -> General -> Workflow permissions, or the organisation's equivalent.

No personal access token is needed for anything in this repository. If you add a workflow that needs one, document it here.

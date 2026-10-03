# Continuous integration

All workflows are in `.github/workflows/`. Unless noted, each runs on pushes to `main`, on pull requests and on demand.

| Workflow | File | What it checks |
|---|---|---|
| Hassfest | `hassfest.yml` | [Home Assistant's own validation](https://github.com/home-assistant/actions) of `manifest.json`, `strings.json`, the translations and `services.yaml`. Also weekly, to catch changes in Home Assistant. |
| HACS validation | `hacs.yml` | [HACS's checks](https://github.com/hacs/action) of `hacs.json`, the manifest and the repository's description and topics. The brands check is skipped because the icons ship in `brand/` inside the integration. Also weekly. |
| Tests | `tests.yml` | The full test suite on Python 3.14 ([development.md](development.md#tests)). Also called by Release before anything is tagged. |
| Lint | `lint.yml` | [ruff](https://docs.astral.sh/ruff/) with the rules in `pyproject.toml`. |
| Version | `version.yml` | `manifest.json` and `pyproject.toml` match the `version` file. On pull requests, also refuses a version that is already released ([releasing.md](releasing.md)). |
| Firmware | `firmware.yml` | Builds termoweb_rx at PA `0xC0`, runs `verify_ack.py`, uploads the hex as an artifact. Only when `firmware/`, the vendored package or `version` change. |
| Schedule card bundle | `frontend.yml` | Rebuilds the es5 bundle and fails if it differs from the committed one. Only when `frontend/` changes. |
| Vendored package | `vendored-package.yml` | The vendored package imports with only `pyserial` installed, and the firmware's network id matches the codec. Only when the package or the firmware change. |
| Leak scan | `leak-scan.yml` | [gitleaks](https://github.com/gitleaks/gitleaks) over the whole history; private LAN addresses and home paths in tracked files; and the installation identifiers listed in the `LEAK_PATTERNS` secret ([secrets.md](secrets.md#leak_patterns)). |
| Release | `release.yml` | Runs only when a push to `main` changes `version`, or on demand. Tags and publishes a release ([releasing.md](releasing.md)). |

[Dependabot](https://docs.github.com/en/code-security/dependabot/dependabot-version-updates) (`.github/dependabot.yml`) proposes weekly updates for the workflows' actions and for pip packages. It leaves `pytest-homeassistant-custom-component` and `home-assistant-frontend` alone, because they are pinned to one Home Assistant release and are bumped deliberately alongside `hacs.json`'s minimum version.

## Pull requests from forks

GitHub gives workflows triggered by a fork's pull request no secrets, so the `LEAK_PATTERNS` step skips itself with a notice. Every other check runs. The step runs again on the maintainer's side once the change reaches `main`.

## Scripts

`.github/scripts/version.py` reads the `version` file for the Version and Release workflows ([releasing.md](releasing.md)). It lives under `.github/` because it is CI tooling, not part of the integration or the firmware.

## Permissions

Every workflow runs with read-only repository access, except Release's tagging job, which needs `contents: write` to push tags and create the release. That uses the built-in [`GITHUB_TOKEN`](https://docs.github.com/en/actions/security-for-github-actions/security-guides/automatic-token-authentication); no personal token is needed ([secrets.md](secrets.md#github_token)).

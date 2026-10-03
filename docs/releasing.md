# Versions and releases

## The version file

[`version`](../version) at the repository root is the single source of release versions:

```
integration=0.1.1
firmware=3.5
```

- **integration** is the Home Assistant integration's version. `manifest.json` (which Home Assistant and HACS read) and `pyproject.toml` carry copies of it, written by `python3 scripts/version.py sync`. The Version workflow fails any change where a copy disagrees.
- **firmware** is termoweb_rx's version. Nothing copies it: the firmware Makefile reads it at build time and compiles it into the stick's banner, so `Q` on a flashed stick reports it ([firmware.md](firmware.md#checking-it)).

Both are dotted numbers with no `v` prefix. `scripts/version.py` rejects anything else.

## Cutting a release

1. Edit `version`. Every release bumps `integration`. Bump `firmware` too if `firmware/termoweb_rx` changed since the last release.
2. Run `python3 scripts/version.py sync` to update `manifest.json` and `pyproject.toml`.
3. Commit all three files and push to `main`, directly or through a pull request.

When that push reaches `main`, the Release workflow:

1. reads `version` and checks the derived files match it;
2. stops if `v<integration>` is already tagged (nothing to release);
3. runs the full test suite;
4. builds the firmware at PA `0xC0` and zips the integration;
5. tags the tip of `main` as `v<integration>`, and as `firmware-v<firmware>` when that firmware version has no tag yet;
6. publishes a GitHub release for `v<integration>` with `termoweb_local.zip` and `termoweb_rx-<firmware>-paC0.hex` attached, and notes generated from the merged pull requests.

HACS offers the new release to users from the `v<integration>` tag.

## Rules the workflows enforce

- **A firmware change needs an integration bump.** Releases are keyed on the integration version, so a new `firmware=` with an already-released `integration=` fails the Release workflow with a message saying so.
- **A released version is never reused.** On a pull request, the Version workflow fails if `integration=` names an existing tag and differs from `main`'s.
- **Tests gate the tag.** If the tests fail, nothing is tagged or published. Fix, push again, and the workflow retries, because the tag still does not exist.

## Re-running a release

If the release step itself fails (for example a network error), start the Release workflow by hand from the Actions tab. If the tag was already pushed but the release was not created, delete the tag first (`git push --delete origin v<integration>`) or the workflow will treat the version as released.

## Repository settings it relies on

- Tags matching `v*` and `firmware-v*` must not be protected against the GitHub Actions bot, or the tag push fails.
- No secret is needed: tagging and publishing use the built-in `GITHUB_TOKEN` ([secrets.md](secrets.md#github_token)).

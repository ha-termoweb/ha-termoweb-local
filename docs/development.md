# Development

## Layout

```
custom_components/termoweb_local/      the Home Assistant integration
  vendor/termoweb_local/               the protocol package: frame codec, nanoCUL client, heater model, network logic
  _vendor_compat.py                    puts vendor/ on the import path when no other termoweb_local is installed
  frontend/                            the schedule card: ES module sources plus the committed es5 bundle
firmware/termoweb_rx/                  the nanoCUL firmware (C, avr-gcc)
scripts/version.py                     reads the version file and keeps derived files in step
tests/                                 package tests (top level) and integration tests (tests/integration)
version                                release versions, see releasing.md
```

## Setting up

Python 3.14, as Home Assistant 2026.9 requires.

```
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt ruff
```

`requirements_test.txt` pins `pytest-homeassistant-custom-component` to the release matching the Home Assistant version under test, and adds `home-assistant-frontend`, which the integration's `frontend` dependency needs and the test plugin does not install.

## Tests

```
.venv/bin/python -m pytest -n auto tests
```

About 400 tests: the package tests under `tests/` drive the protocol package against a fake serial transport, and `tests/integration/` sets up the integration inside a test Home Assistant instance. Run them in parallel (`-n auto`): serially the integration suite takes many minutes, and it needs a few GB of memory per worker.

Every heater identity, address and frame in the tests is either synthetic or a protocol constant. Keep it that way: never paste values captured from your own installation into a test ([privacy.md](privacy.md)). Rebuild a frame from synthetic fields with `tf.build_frame(...)` instead.

## Lint

```
.venv/bin/ruff check .
```

The rule set in `pyproject.toml` is deliberately small for now (syntax errors and pyflakes).

## The vendored protocol package

The integration imports `termoweb_local` from `vendor/termoweb_local/`. `frame.py` loads the codec from `_vendored_frame.py` alongside it. The Vendored package workflow ([ci.md](ci.md)) checks that the package imports with only `pyserial` installed and that the firmware's hardcoded network id matches the codec.

`firmware/termoweb_rx/verify_ack.py` is a Python model of the firmware's ack builder, checked against the codec. Run it after touching either: `python3 firmware/termoweb_rx/verify_ack.py`.

## The schedule card

Edit the ES module sources in `frontend/`, then rebuild the bundle that older browsers load, and commit both:

```
bash custom_components/termoweb_local/frontend/build-es5.sh
```

It needs Node (it runs esbuild through `npx`). `tests/test_frontend_bundle.py` and the Schedule card bundle workflow fail when the committed bundle no longer matches its sources.

## Firmware

See [firmware.md](firmware.md). There are no firmware unit tests: the Firmware workflow builds it and runs `verify_ack.py`.

## Before opening a pull request

1. Tests and ruff pass locally.
2. If you changed a version, `python3 scripts/version.py check` passes ([releasing.md](releasing.md)).
3. Nothing in the diff identifies your installation ([privacy.md](privacy.md)).

CI runs the rest ([ci.md](ci.md)).

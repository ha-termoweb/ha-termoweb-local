"""Import shim so `from termoweb_local...` resolves for every consumer in this
integration, whether or not `termoweb_local` was pip-installed.

manifest.json's `requirements` used to point at a `git+https://...REPLACE_ME...`
URL for this repository so `pip install` would fetch and install the
`termoweb_local` package before this integration's own code ran. That only works
once this repository has a real git remote; today it has none (see the "How HACS
users get the termoweb_local package" note this replaced in
custom_components/termoweb_local/README.md), so HACS or a manual copy install
would fail every setup with a requirements-install error, not a config flow error.

Instead, `deploy/install_to_ha.sh` bundles a copy of the `termoweb_local` package
under `custom_components/termoweb_local/vendor/termoweb_local/` alongside this
integration (see that script's `build_vendor_copy`), and `manifest.json`'s
requirements now list only `termoweb_local`'s own runtime dependency
(`pyserial`), not a git URL. This module is what makes `vendor/termoweb_local`
resolve as `termoweb_local` when nothing else provides that name: every
integration module that does `from termoweb_local.X import Y` imports this
module first (`from . import _vendor_compat  # noqa: F401`), which runs
`ensure_termoweb_local_importable()` below as an import-time side effect, before
that `from termoweb_local...` line executes.

In the development checkout (this repository, `pip install -e .` in either
`.venv-hacs` or `.venv-termoweb-local`), `importlib.util.find_spec` already
resolves `termoweb_local` to the real top-level package, so this module leaves
`sys.path` untouched and every test in this suite continues to exercise the real
package, not the vendored copy.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

VENDOR_DIR = pathlib.Path(__file__).resolve().parent / "vendor"


def ensure_termoweb_local_importable(vendor_dir: pathlib.Path = VENDOR_DIR) -> None:
    """No-op if `termoweb_local` already resolves (a real pip install, or this
    repository's own editable install); otherwise prepend `vendor_dir` to
    `sys.path` so `import termoweb_local` finds the vendored copy there instead.
    Safe to call more than once."""
    if importlib.util.find_spec("termoweb_local") is not None:
        return
    vendor_str = str(vendor_dir)
    if vendor_str not in sys.path:
        sys.path.insert(0, vendor_str)


ensure_termoweb_local_importable()

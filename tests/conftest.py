import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = REPO_ROOT / "custom_components" / "termoweb_local" / "vendor"

# The termoweb_local protocol package ships vendored inside the integration;
# put it on sys.path so the package tests import it as `termoweb_local`.
for path in (REPO_ROOT, VENDOR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

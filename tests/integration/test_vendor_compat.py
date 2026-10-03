"""Vendor-import shim (custom_components/termoweb_local/_vendor_compat.py):
falls back to vendor/termoweb_local only when the top-level termoweb_local
package cannot be found, and is a no-op in this repository's dev checkout,
where termoweb_local is pip installed (both .venv-hacs and .venv-termoweb-local).
docs/90-phase3-plan.md P5, deploy/install_to_ha.sh's build_vendor_copy.
"""
import pathlib

from custom_components.termoweb_local import _vendor_compat


def test_noop_when_termoweb_local_already_resolves():
    """In this checkout, termoweb_local is pip installed (editable), so
    find_spec resolves it without help; the vendor dir argument is never even
    looked at, let alone added to sys.path."""
    before = list(_vendor_compat.sys.path)
    _vendor_compat.ensure_termoweb_local_importable(pathlib.Path("/nonexistent-vendor-dir"))
    assert _vendor_compat.sys.path == before


def test_prepends_vendor_dir_when_package_is_missing(monkeypatch, tmp_path):
    """The HACS-only-install scenario this shim exists for: nothing on sys.path
    resolves termoweb_local, so the vendor copy's parent directory goes to the
    front of sys.path, where `from termoweb_local.X import Y` will find it."""
    monkeypatch.setattr(_vendor_compat.importlib.util, "find_spec", lambda name: None)
    vendor_dir = tmp_path / "vendor"
    _vendor_compat.ensure_termoweb_local_importable(vendor_dir)
    try:
        assert _vendor_compat.sys.path[0] == str(vendor_dir)
    finally:
        _vendor_compat.sys.path.remove(str(vendor_dir))


def test_does_not_duplicate_vendor_dir_already_on_sys_path(monkeypatch, tmp_path):
    """Calling it twice (every integration module that imports termoweb_local
    imports this shim first) must not grow sys.path without bound."""
    monkeypatch.setattr(_vendor_compat.importlib.util, "find_spec", lambda name: None)
    vendor_dir = tmp_path / "vendor"
    _vendor_compat.sys.path.insert(0, str(vendor_dir))
    try:
        before_len = len(_vendor_compat.sys.path)
        _vendor_compat.ensure_termoweb_local_importable(vendor_dir)
        assert len(_vendor_compat.sys.path) == before_len
    finally:
        _vendor_compat.sys.path.remove(str(vendor_dir))

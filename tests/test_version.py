"""The `version` file is the single source of release versions: every derived
file must agree with it, and the firmware build must read it."""
import importlib.util
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("repo_version", ROOT / "scripts" / "version.py")
version = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(version)


def test_version_file_parses():
    versions = version.read_versions()
    assert set(versions) == {"integration", "firmware"}


def test_derived_files_are_in_step():
    assert version.stale_files() == []


def test_firmware_takes_its_version_from_the_makefile():
    main_c = (ROOT / "firmware" / "termoweb_rx" / "main.c").read_text()
    makefile = (ROOT / "firmware" / "termoweb_rx" / "Makefile").read_text()
    assert not re.search(r'#define\s+VERSION\s+"', main_c), "main.c must not hardcode its version"
    assert "#define VERSION FW_VERSION" in main_c
    assert "../../version" in makefile and "-DFW_VERSION=" in makefile


@pytest.mark.parametrize(
    "text",
    [
        "integration=0.1.1\n",
        "integration=0.1.1\nfirmware=3.5\nfirmware=3.6\n",
        "integration=0.1.1\nfirmware=v3.5\n",
        "integration=0.1.1\nfirmware=3.5\nother=1\n",
    ],
)
def test_malformed_version_files_are_rejected(tmp_path, text):
    path = tmp_path / "version"
    path.write_text(text)
    with pytest.raises(ValueError):
        version.read_versions(path)

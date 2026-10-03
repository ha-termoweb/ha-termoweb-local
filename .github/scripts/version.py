#!/usr/bin/env python3
"""Read the repository's `version` file and keep everything that carries a
version in step with it.

    python3 .github/scripts/version.py get integration    print one version
    python3 .github/scripts/version.py sync               rewrite the derived files
    python3 .github/scripts/version.py check              exit 1 if any derived file differs

The firmware needs no derived file: its Makefile reads `version` at build
time and passes the value to the compiler.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]  # .github/scripts/ -> repository root
VERSION_FILE = ROOT / "version"
MANIFEST = ROOT / "custom_components" / "termoweb_local" / "manifest.json"
PYPROJECT = ROOT / "pyproject.toml"
FIRMWARE_README = ROOT / "firmware" / "README.md"

KEYS = ("integration", "firmware")
_VALID = re.compile(r"^[0-9]+(\.[0-9]+)*$")
_PYPROJECT_VERSION = re.compile(r'^(version\s*=\s*")([^"]*)(")', re.M)
_README_VERSION = re.compile(r"(Current version )([0-9][0-9.]*)(\.)")


def read_versions(path: pathlib.Path = VERSION_FILE) -> dict[str, str]:
    versions: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key not in KEYS:
            raise ValueError(f"{path.name}:{lineno}: expected integration=X or firmware=X, got {raw!r}")
        if not _VALID.match(value):
            raise ValueError(f"{path.name}:{lineno}: {key} version {value!r} is not dotted numbers")
        if key in versions:
            raise ValueError(f"{path.name}:{lineno}: {key} given twice")
        versions[key] = value
    missing = [k for k in KEYS if k not in versions]
    if missing:
        raise ValueError(f"{path.name}: missing {', '.join(missing)}")
    return versions


def _derived(versions: dict[str, str]) -> dict[pathlib.Path, str]:
    """The text each derived file should have."""
    out: dict[pathlib.Path, str] = {}

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["version"] = versions["integration"]
    out[MANIFEST] = json.dumps(manifest, indent=2) + "\n"

    pyproject = PYPROJECT.read_text(encoding="utf-8")
    if not _PYPROJECT_VERSION.search(pyproject):
        raise ValueError("pyproject.toml has no version line")
    out[PYPROJECT] = _PYPROJECT_VERSION.sub(
        lambda m: m.group(1) + versions["integration"] + m.group(3), pyproject, count=1
    )

    readme = FIRMWARE_README.read_text(encoding="utf-8")
    if not _README_VERSION.search(readme):
        raise ValueError("firmware/README.md has no 'Current version X.' sentence")
    out[FIRMWARE_README] = _README_VERSION.sub(
        lambda m: m.group(1) + versions["firmware"] + m.group(3), readme, count=1
    )
    return out


def stale_files(versions: dict[str, str] | None = None) -> list[pathlib.Path]:
    versions = versions or read_versions()
    return [p for p, text in _derived(versions).items() if p.read_text(encoding="utf-8") != text]


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "get" and argv[1] in KEYS:
        print(read_versions()[argv[1]])
        return 0
    if argv == ["sync"]:
        for path, text in _derived(read_versions()).items():
            if path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8")
                print(f"updated {path.relative_to(ROOT)}")
        return 0
    if argv == ["check"]:
        stale = stale_files()
        for path in stale:
            print(f"::error file={path.relative_to(ROOT)}::out of step with the version file; "
                  "run python3 .github/scripts/version.py sync")
        return 1 if stale else 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

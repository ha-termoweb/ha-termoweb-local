"""The committed es5 bundle must stay derived from its six source files: this
test recomputes the same hash build-es5.sh computes and compares it against
the header the bundle carries, and separately checks the bundle text for
tokens a real ES5 target must not contain."""
import hashlib
import pathlib
import re

FRONTEND_DIR = (
    pathlib.Path(__file__).resolve().parent.parent
    / "custom_components"
    / "termoweb_local"
    / "frontend"
)
BUNDLE_PATH = FRONTEND_DIR / "termoweb-local-schedule-card.es5.js"
BUILD_SCRIPT = "bash custom_components/termoweb_local/frontend/build-es5.sh"

# Sorted alphabetically by filename, same order build-es5.sh hashes.
SOURCE_FILENAMES = [
    "presets.js",
    "schedule-card-editor.js",
    "schedule-card.js",
    "schedule-grid.js",
    "schedule-styles.js",
    "termoweb-local-schedule-card.js",
]

SHA_HEADER_RE = re.compile(r"sources-sha256:\s*([0-9a-f]{64})")

# Strips quoted strings and template literals before scanning for forbidden
# tokens, so a string like "a??b" inside the card's own logic cannot trip a
# false failure on the bundled output.
_STRING_OR_TEMPLATE_RE = re.compile(
    r'"(?:[^"\\]|\\.)*"' r"|'(?:[^'\\]|\\.)*'" r"|`(?:[^`\\]|\\.)*`"
)


def _expected_sources_sha256() -> str:
    hasher = hashlib.sha256()
    for filename in SOURCE_FILENAMES:
        hasher.update((FRONTEND_DIR / filename).read_bytes())
    return hasher.hexdigest()


def test_bundle_matches_current_sources():
    bundle_text = BUNDLE_PATH.read_text(encoding="utf-8")
    first_line = bundle_text.splitlines()[0]
    match = SHA_HEADER_RE.search(first_line)
    assert match, f"no sources-sha256 header found on the bundle's first line: {first_line!r}"
    committed_hash = match.group(1)
    expected_hash = _expected_sources_sha256()
    assert committed_hash == expected_hash, (
        "termoweb-local-schedule-card.es5.js is stale: its sources-sha256 header "
        f"does not match the current source files. Rebuild it with: {BUILD_SCRIPT}"
    )


def test_bundle_has_no_module_syntax_or_optional_chaining():
    bundle_text = BUNDLE_PATH.read_text(encoding="utf-8")
    stripped = _STRING_OR_TEMPLATE_RE.sub("", bundle_text)
    assert "import " not in stripped, "bundle contains an ES module import statement"
    assert "export " not in stripped, "bundle contains an ES module export statement"
    assert "??" not in stripped, "bundle contains a bare nullish-coalescing token"
    assert "?." not in stripped, "bundle contains a bare optional-chaining token"

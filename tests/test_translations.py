"""Home Assistant translation files must match strings.json's own key tree
exactly (every leaf translated, no key added or dropped) and must carry every
{placeholder} an English leaf uses through into the translated leaf."""
import json
import pathlib
import re

import pytest

TRANSLATIONS_DIR = (
    pathlib.Path(__file__).resolve().parent.parent
    / "custom_components"
    / "termoweb_local"
    / "translations"
)
STRINGS_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "custom_components"
    / "termoweb_local"
    / "strings.json"
)

LANGUAGES = ["en", "fr", "es", "it", "de", "pt", "pl"]

PLACEHOLDER_RE = re.compile(r"\{[^{}]+\}")


def _flatten(node, prefix=""):
    """Yield (dotted key path, leaf value) pairs from a nested translation tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            yield from _flatten(value, path)
    else:
        yield prefix, node


@pytest.fixture(scope="module")
def strings_tree():
    return json.loads(STRINGS_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def strings_leaves(strings_tree):
    return dict(_flatten(strings_tree))


@pytest.mark.parametrize("language", LANGUAGES)
def test_translation_file_parses_as_json(language):
    path = TRANSLATIONS_DIR / f"{language}.json"
    json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("language", LANGUAGES)
def test_translation_key_tree_matches_strings_json(language, strings_leaves):
    path = TRANSLATIONS_DIR / f"{language}.json"
    tree = json.loads(path.read_text(encoding="utf-8"))
    leaves = dict(_flatten(tree))

    assert set(leaves) == set(strings_leaves), (
        f"{language}.json key set differs from strings.json: "
        f"missing={sorted(set(strings_leaves) - set(leaves))}, "
        f"extra={sorted(set(leaves) - set(strings_leaves))}"
    )


@pytest.mark.parametrize("language", LANGUAGES)
def test_translation_placeholders_match_english(language, strings_leaves):
    path = TRANSLATIONS_DIR / f"{language}.json"
    tree = json.loads(path.read_text(encoding="utf-8"))
    leaves = dict(_flatten(tree))

    for key, english_value in strings_leaves.items():
        english_placeholders = set(PLACEHOLDER_RE.findall(str(english_value)))
        if not english_placeholders:
            continue
        translated_placeholders = set(PLACEHOLDER_RE.findall(str(leaves[key])))
        assert translated_placeholders == english_placeholders, (
            f"{language}.json[{key}] placeholders {translated_placeholders} "
            f"do not match strings.json's {english_placeholders}"
        )

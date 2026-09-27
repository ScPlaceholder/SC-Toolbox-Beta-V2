# -*- coding: utf-8 -*-
"""Anti-rot test for dps_ui/tooltips.py.

THE POINT OF THIS FILE
    A tooltip table is a second list of the UI's column keys, kept by hand.
    Lists like that rot silently: rename a field key in ``dps_ui/constants.py``
    and the tooltip does not break, it just stops appearing, and nobody notices
    because nothing was ever loud. So every key tooltips.py defines is asserted
    to still exist in the source that renders it.

WHAT "EXISTS" MEANS HERE
    * For a picker table, the key must appear as a QUOTED string inside that
      table's own ``*_TABLE_COLS`` block in ``dps_ui/constants.py``. Scoping to
      the block is deliberate: a key that moved from the weapon picker to the
      shield picker would still be findable file-wide, and the tooltip would
      then be attached to the wrong column.
    * For the footer, power allocator, ship panel and TTK panel, the key must
      appear as a quoted string in the file that renders it.
    * The quoting matters. A bare substring search for ``size`` matches
      ``port_max_size``, so an unquoted search cannot tell a real column from a
      coincidence.
    * Two keys are pseudo-columns with no field key at all; they are pinned to
      an explicit literal via ``tooltips.ANCHORS``.

    The source is read as TEXT, never imported. ``dps_ui.constants`` pulls in
    PySide6 and ``shared.qt.theme``; making this test depend on Qt would mean a
    missing GUI toolkit turns the check into a collection error, and a check
    that cannot run is indistinguishable from one that passes.

    Non-ASCII in the source is written as ``\\uXXXX`` escapes, so the text is
    unescaped before searching -- otherwise an anchor like the cart glyph could
    never match its own source line.
"""
import os
import re
import sys

import pytest

# Bootstrap project root so shared.path_setup is importable (same preamble as
# the other suites in this directory).
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dps_ui import tooltips  # noqa: E402


SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONSTANTS = os.path.join(SKILL_ROOT, "dps_ui", "constants.py")


def _read(rel_or_abs: str) -> str:
    """Source text with ``\\uXXXX`` escapes decoded to the characters they mean."""
    path = rel_or_abs
    if not os.path.isabs(path):
        path = os.path.join(SKILL_ROOT, rel_or_abs.replace("/", os.sep))
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    return re.sub(
        r"\\U([0-9a-fA-F]{8})|\\u([0-9a-fA-F]{4})",
        lambda m: chr(int(m.group(1) or m.group(2), 16)),
        src,
    )


def _spec_block(spec_name: str) -> str:
    """The text of one ``NAME = [ ... ]`` assignment in constants.py.

    Scoped extraction, not a file-wide search: the whole value of this test is
    that a key is checked against the table it is supposed to describe.
    """
    src = _read(CONSTANTS)
    m = re.search(
        r"^%s\s*=\s*\[(.*?)^\]" % re.escape(spec_name),
        src,
        re.DOTALL | re.MULTILINE,
    )
    assert m, (
        "tooltips.SOURCE_MAP names a column spec %r that is not defined in "
        "dps_ui/constants.py" % spec_name
    )
    return m.group(1)


def _quoted(key: str) -> tuple:
    return ('"%s"' % key, "'%s'" % key)


# Cache file reads: 24 namespaces over 6 files.
_SRC_CACHE: dict = {}


def _cached(path: str) -> str:
    if path not in _SRC_CACHE:
        _SRC_CACHE[path] = _read(path)
    return _SRC_CACHE[path]


def _all_keys():
    for ns, grp in sorted(tooltips.GROUPS.items()):
        for field in sorted(grp):
            yield ns, field


# ---------------------------------------------------------------------------
# The load-bearing test
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ns,field", list(_all_keys()), ids=lambda v: str(v))
def test_every_key_exists_in_the_ui_source(ns, field):
    """Every tooltip key still names something the UI actually renders."""
    fq = "%s.%s" % (ns, field)
    src_spec = tooltips.SOURCE_MAP.get(ns)
    assert src_spec, "tooltips.GROUPS has namespace %r with no SOURCE_MAP entry" % ns

    needles = tooltips.ANCHORS.get(fq)
    needles = (needles,) if needles else _quoted(field)

    searched = []

    spec_name = src_spec.get("spec")
    if spec_name:
        block = _spec_block(spec_name)
        searched.append("%s in dps_ui/constants.py" % spec_name)
        if any(n in block for n in needles):
            return

    for rel in src_spec.get("files", ()):
        searched.append(rel)
        if any(n in _cached(rel) for n in needles):
            return

    pytest.fail(
        "tooltip key %r is not present in the UI source.\n"
        "  looked for: %s\n"
        "  in: %s\n"
        "Either the column was renamed or removed (delete or re-key the "
        "tooltip), or SOURCE_MAP points at the wrong place."
        % (fq, " or ".join(repr(n) for n in needles), ", ".join(searched))
    )


# ---------------------------------------------------------------------------
# Accessor contract -- a missing tooltip must never break a table build
# ---------------------------------------------------------------------------

def test_unknown_key_returns_default_and_does_not_raise():
    assert tooltips.tip("nope.not_a_column") == ""
    assert tooltips.tip("nope.not_a_column", "fallback") == "fallback"
    assert tooltips.tip("") == ""
    assert tooltips.tip("no_namespace_at_all") == ""
    # Non-string keys are the realistic accident (a column index, a None) and
    # must not raise either.
    assert tooltips.tip(None, "d") == "d"          # type: ignore[arg-type]
    assert tooltips.tip(7) == ""                   # type: ignore[arg-type]
    assert tooltips.tip_for(None, "hp") == ""      # type: ignore[arg-type]


def test_known_keys_return_text():
    assert tooltips.tip("weapon.efficiency").startswith("Burst DPS divided by")
    assert tooltips.tip_for("weapon", "rps").startswith("Shots per second")
    assert tooltips.tip("footer.dps_sus")
    assert tooltips.tip("power.consumption_pct")


def test_shared_columns_fall_back_to_the_table_group():
    # A cooler's HP is a component's durability -> the shared text.
    assert tooltips.tip("cooler.hp") == tooltips.TABLE_TIPS["hp"]
    assert tooltips.tip("powerplant.name") == tooltips.TABLE_TIPS["name"]


def test_shield_hp_overrides_the_shared_text():
    """The trap the namespacing exists for: shield.hp is a POOL, table.hp is durability."""
    assert tooltips.tip("shield.hp") != tooltips.TABLE_TIPS["hp"]
    assert "pool" in tooltips.tip("shield.hp")
    # ...and the weapon table calls its durability wp_hp for the same reason.
    assert tooltips.tip("weapon.wp_hp") != tooltips.TABLE_TIPS["hp"]


def test_keys_export_matches_the_groups():
    expected = {"%s.%s" % (ns, f) for ns, f in _all_keys()}
    assert set(tooltips.KEYS) == expected
    assert set(tooltips.TIPS) == expected


def test_every_group_has_a_source_map_entry_and_vice_versa():
    assert set(tooltips.GROUPS) == set(tooltips.SOURCE_MAP)


def test_anchors_only_cover_keys_that_exist():
    assert set(tooltips.ANCHORS) <= set(tooltips.KEYS)


# ---------------------------------------------------------------------------
# Copy style -- the issue asked for plain English, not marketing
# ---------------------------------------------------------------------------

def test_copy_has_no_exclamation_marks_or_marketing_openers():
    bad_openers = ("great for", "perfect for", "simply ", "just ")
    for key, text in sorted(tooltips.TIPS.items()):
        assert "!" not in text, "%s: tooltip copy uses an exclamation mark" % key
        low = text.lower()
        for opener in bad_openers:
            assert not low.startswith(opener), "%s: marketing opener %r" % (key, opener)


def test_copy_is_non_empty_and_a_sentence():
    for key, text in sorted(tooltips.TIPS.items()):
        assert text.strip(), "%s: empty tooltip" % key
        assert len(text) > 20, "%s: tooltip too short to say anything useful" % key
        assert text.rstrip().endswith((".", "%")), (
            "%s: tooltip does not end in a full stop" % key
        )


def test_omitted_documents_why_and_does_not_overlap_the_live_keys():
    """Columns we could not pin down are listed, not silently dropped."""
    assert tooltips.OMITTED, "OMITTED should record the columns left unexplained"
    for key, reason in tooltips.OMITTED.items():
        assert key not in tooltips.KEYS, (
            "%s is listed as omitted but also has a tooltip" % key
        )
        assert len(reason) > 30, "%s: omission reason is too thin to act on" % key

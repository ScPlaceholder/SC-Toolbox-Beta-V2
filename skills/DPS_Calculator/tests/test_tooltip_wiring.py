"""The tooltips must actually REACH the screen — the half a data module cannot prove.

`test_tooltips.py` proves the copy exists and that every key it defines names a real column.
That is a claim about the MODULE. This file is the other claim, and for a while tonight it was
the one that would have been false: `dps_ui/tooltips.py` can be complete, correct and imported
by nothing.

⛔ WHY THIS NEEDS A GUARD AT ALL, stated because "a missing tooltip is harmless" is exactly the
  reasoning that lets it rot. The wiring degrades SILENTLY by design: `tip_for()` returns "" for
  an unknown namespace and the label simply gets no hint. That is the right runtime behaviour —
  a missing hint must never break a table build — and it means a call site that stops passing
  `tip_ns`, or a new picker nobody wired, produces NO error, NO exception and NO visible
  difference except help text that quietly is not there. Nothing would ever tell us.

★ THE TRAP THIS EXISTS TO CATCH, in particular: `section_key` looks like the natural key and is
  the wrong one. It names the SELECTION BUCKET, not the table — coolers, radars and power plants
  are all "components", shields are "defenses". A namespace derived from it resolves, prints and
  hands a radar the cooler's help text. So the wiring keys on the column SPEC by identity, and
  `test_the_call_site_derives_the_namespace_from_the_column_spec` is what stops a future edit
  from "simplifying" it back.

⚠ WHAT THIS DOES NOT PROVE: that a tooltip appears when a human hovers. Driving these widgets
  needs a QApplication and a populated repository, and the catalogs load through an async path no
  test in this directory drives. This is a static guard — the weaker kind — and it is here
  because the alternative was nothing, which is how the wiring went missing in the first place.
"""
import ast
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
APP = os.path.join(SKILL, "dps_ui", "app.py")
WIDGETS = os.path.join(SKILL, "dps_ui", "widgets.py")


def _src(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _specs_passed_to_build_table_slot(src):
    """-> the set of *_TABLE_COLS / *_COLS names handed to `_build_table_slot`.

    Read from the AST rather than by regex, because these calls run to five and six lines and a
    line-oriented pattern would miss the ones whose spec argument wraps.
    """
    tree = ast.parse(src)
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "_build_table_slot"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Name) and arg.id.endswith("_COLS"):
                found.add(arg.id)
    return found


def _names_in_cols_namespace(src):
    """-> the spec names listed in app.py's `_COLS_NAMESPACE` table."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        tgt = node.targets[0]
        if not (isinstance(tgt, ast.Name) and tgt.id == "_COLS_NAMESPACE"):
            continue
        out = {}
        for pair in node.value.elts:
            spec, ns = pair.elts
            out[spec.id] = ns.value
        return out
    raise AssertionError("app.py no longer defines _COLS_NAMESPACE")


def test_every_table_the_ui_builds_has_a_tooltip_namespace():
    """A picker missing from the map gets NO tooltips, and says nothing about it."""
    src = _src(APP)
    used = _specs_passed_to_build_table_slot(src)
    assert used, "found no _build_table_slot calls at all — this test is not looking at the UI"
    mapped = _names_in_cols_namespace(src)
    missing = sorted(used - set(mapped))
    assert not missing, (
        "these column specs are drawn on screen but have no tooltips namespace, so every column "
        "in them silently gets no help text: %s" % missing)


def test_the_namespaces_named_in_app_exist_in_the_tooltips_module():
    """A typo'd namespace is indistinguishable at runtime from a column with no copy written."""
    import dps_ui.tooltips as T          # pure data, no Qt — safe to import here
    mapped = _names_in_cols_namespace(_src(APP))
    unknown = sorted({ns for ns in mapped.values() if ns not in T.GROUPS})
    assert not unknown, (
        "app.py names tooltip namespaces that dps_ui/tooltips.py does not define, so they resolve "
        "to nothing: %s" % unknown)


def test_the_call_site_derives_the_namespace_from_the_column_spec():
    """NOT from `section_key` — see the module docstring. This is the whole correctness argument."""
    src = _src(APP)
    assert "tip_ns=_cols_namespace(table_cols)" in src, (
        "_build_table_slot no longer derives the tooltip namespace from the column spec. If this "
        "was changed to section_key, note that coolers, radars and power plants all share the "
        "section key 'components' — the lookup would still resolve and would show the wrong help.")
    assert not re.search(r"tip_ns\s*=\s*section_key", src), (
        "the tooltip namespace is being taken from section_key, which names the selection bucket "
        "and not the table")


def test_both_render_sites_actually_attach_the_tooltip():
    """The row of stats, and the picker popup's column headers. Either alone is half the fix."""
    src = _src(WIDGETS)
    assert "from dps_ui.tooltips import tip_for" in src, "widgets.py does not import tip_for"
    assert src.count("setToolTip(hint)") >= 1, "the selected-component row attaches no tooltip"
    assert "item.setToolTip(hint)" in src, (
        "the picker popup's column headers attach no tooltip — and those headers are the exact "
        "thing issue #7 reported as undescribed")


def test_the_footer_readouts_are_wired():
    src = _src(APP)
    assert 'tip_for("footer", key)' in src, "the footer stats attach no tooltips"
    assert src.count("val_lbl.setToolTip(hint)") == 1 and src.count("lbl.setToolTip(hint)") >= 1, (
        "the footer hint must go on BOTH the caption and the value label — they are two separate "
        "widgets side by side, and hinting one makes the tooltip appear or not depending on which "
        "half of 'DPS: 4,210' the pointer is over")

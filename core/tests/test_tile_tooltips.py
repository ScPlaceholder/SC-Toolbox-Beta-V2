"""Hovering a tool's launcher tile says what the tool does, and which key opens it.

J, 2026-10-05: "Can we also have a tool tip pop up when users hover over each tool that summarizes
its functionality?"

The text is each tool's own: the "summary" in its skill.json (SkillConfig.summary), put together
with the name and the hotkey by shared/tool_tips.py and set on the tile by ui/main_window.py.
What is pinned here:

    the text          every tool has a summary, in its own skill.json, short enough to be a tooltip
    the hotkey        the tooltip names the key in force NOW: saved binding, rebind, switched off
    no summary        the tooltip is the name and the hotkey; never an empty bubble, never a crash
    hidden tools      a tool that is a tab of another (SuitMk2) is described on its host's tile, and
                      the tools folded into the Everything Finder are named in its summary
    width             no line longer than WRAP characters, so the bubble is narrower than the launcher
    the real window   the tile carries the tooltip, a hover over a label INSIDE the tile shows it,
                      and the launcher shows tooltips while the game has the keyboard

The window tests build the real LauncherWindow in a child process (_tile_tooltip_probe.py).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, REPO)

from core.skill_registry import _BUILTIN_SKILLS, discover_skills, resolve_skill_path, tile_skills  # noqa: E402
from shared import tool_tips  # noqa: E402
from shared.config_models import SkillConfig  # noqa: E402
from shared.tool_tips import SUMMARY_MAX, WRAP, tile_sections, tooltip_html, tooltip_lines, tooltip_text  # noqa: E402

PROBE = os.path.join(REPO, "core", "tests", "_tile_tooltip_probe.py")

# Hidden tools with no tile and no tab_of: they are tabs of the Everything Finder, which builds them
# itself. Its summary is where a player reads that they exist: id -> the words it must contain.
FOLDED_INTO = {"market": ("everything_finder", "Item Finder"),
               "trade": ("everything_finder", "Trade Hub"),
               "starmap": ("everything_finder", "Star Map")}
# Hidden because J asked for the tile to go (2026-10-04), not because another tool hosts it.
HIDDEN_ON_ITS_OWN = {"craft_db"}
# Tools that had only a built-in entry in core/skill_registry.py and were given a skill.json for the summary.
# (Craft Database and Mouse Blocker had both before this, and Mouse Blocker's already adds "preload".)
MIRRORS_BUILT_IN = {"dps", "cargo", "missions", "mining", "market", "trade", "everything_finder", "battle_buddy"}


def _skill(sid: str, **kw) -> SkillConfig:
    kw.setdefault("name", sid.title())
    return SkillConfig(id=sid, icon="*", color="#fff", folder=sid, script="app.py", **kw)


def _probe(*args: str) -> dict:
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("QT_SCALE_FACTOR", None)
    proc = subprocess.run([sys.executable, PROBE, *args], cwd=REPO, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=180)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    assert proc.returncode == 0 and lines, (
        f"probe failed (rc={proc.returncode})\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr[-3000:]}")
    return json.loads(lines[-1])


@pytest.fixture(scope="module")
def registry() -> list[SkillConfig]:
    return discover_skills(REPO)


@pytest.fixture(scope="module")
def window() -> dict:
    return _probe()


# ── the text ─────────────────────────────────────────────────────────────────

def test_every_tool_has_a_summary_short_enough_to_be_a_tooltip(registry):
    assert len(registry) >= 17
    for s in registry:
        assert s.summary, f"{s.name} ({s.id}) has no summary: its tile would say only its name"
        assert len(s.summary) <= SUMMARY_MAX, f"{s.name}: {len(s.summary)} characters, the cap is {SUMMARY_MAX}"
        assert s.summary[0].isupper() and s.summary.endswith("."), f"{s.name}: not a sentence: {s.summary!r}"
        assert s.summary == " ".join(s.summary.split()), f"{s.name}: stray whitespace"


def test_the_summary_is_in_the_tools_own_skill_json(registry):
    for s in registry:
        path = os.path.join(resolve_skill_path(s, REPO), "skill.json")
        assert os.path.isfile(path), f"{s.name} ({s.id}) has no skill.json to carry its summary"
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["id"] == s.id
        assert data.get("summary") == s.summary, f"{s.name}: the summary shown is not the one in {path}"
        assert tool_tips.read_summary(os.path.dirname(path)) == s.summary


def test_a_skill_json_beside_a_built_in_entry_says_the_same_thing(registry):
    """Eight tools got a skill.json only to carry their summary. A skill.json WINS over the built-in
    entry in core/skill_registry.py, so one that disagreed would silently change a hotkey, a script
    or whether the tool has a tile."""
    by_id = {s.id: s for s in registry}
    assert MIRRORS_BUILT_IN <= {b["id"] for b in _BUILTIN_SKILLS}
    for b in _BUILTIN_SKILLS:
        if b["id"] not in MIRRORS_BUILT_IN:
            continue
        want = SkillConfig.from_dict(b).to_dict()
        got = by_id[b["id"]].to_dict()
        got.pop("summary", None)
        assert got == want, f"{b['id']}: its skill.json and the built-in entry differ"


def test_summary_is_optional_and_round_trips():
    plain = SkillConfig.from_dict({"id": "a", "name": "A", "script": "a.py"})
    assert plain.summary == ""
    assert "summary" not in plain.to_dict(), "a tool without a summary must not grow an empty one"
    told = SkillConfig.from_dict({"id": "a", "name": "A", "script": "a.py", "summary": "Does  a\n thing."})
    assert told.summary == "Does a thing."
    assert SkillConfig.from_dict(told.to_dict()).summary == "Does a thing."
    assert SkillConfig.from_dict({"id": "a", "name": "A", "script": "a.py", "summary": None}).summary == ""


def test_read_summary_of_a_folder_that_cannot_give_one(tmp_path):
    assert tool_tips.read_summary(str(tmp_path)) == ""                      # no skill.json
    (tmp_path / "skill.json").write_text("{ not json", encoding="utf-8")
    assert tool_tips.read_summary(str(tmp_path)) == ""
    (tmp_path / "skill.json").write_text('["a list"]', encoding="utf-8")
    assert tool_tips.read_summary(str(tmp_path)) == ""
    (tmp_path / "skill.json").write_text('{"id": "a"}', encoding="utf-8")
    assert tool_tips.read_summary(str(tmp_path)) == ""


# ── the hotkey ───────────────────────────────────────────────────────────────

def test_the_tooltip_names_the_hotkey_and_follows_a_change():
    a = _skill("a", summary="Does a thing.", hotkey="<shift>+1")
    assert tooltip_lines(tile_sections(a, [a])) == ["A", "Does a thing.", "Hotkey: Shift+1"]
    # a key changed in Settings while the window is up (LauncherWindow.update_hotkey_badges)
    assert tooltip_lines(tile_sections(a, [a], hotkeys={"a": "<ctrl>+<alt>+f5"}))[-1] == "Hotkey: Ctrl+Alt+F5"
    # a binding loaded from the settings file (the launcher writes it to skill.hotkey)
    a.hotkey = "<ctrl>+9"
    lines = tooltip_lines(tile_sections(a, [a]))
    assert lines[-1] == "Hotkey: Ctrl+9" and "Shift+1" not in "\n".join(lines)


def test_a_hotkey_that_is_switched_off_or_missing_is_not_shown():
    a = _skill("a", summary="Does a thing.", hotkey="<shift>+1")
    assert tooltip_lines(tile_sections(a, [a], keybinds_off=["a"])) == ["A", "Does a thing."]
    none = _skill("b", summary="Does b.")
    assert tooltip_lines(tile_sections(none, [none])) == ["B", "Does b."]


# ── no summary ───────────────────────────────────────────────────────────────

def test_a_tool_with_no_summary_shows_its_name_and_hotkey_only():
    a = _skill("a", hotkey="<shift>+1")
    assert tooltip_lines(tile_sections(a, [a])) == ["A", "Hotkey: Shift+1"]
    html = tooltip_html(tile_sections(a, [a]))
    assert html and "<br><br>" not in html, "an empty line where the summary would be"
    bare = _skill("b")
    assert tooltip_text(tile_sections(bare, [bare])) == "B"
    assert tooltip_html(tile_sections(bare, [bare]))


def test_nothing_to_say_is_no_tooltip_rather_than_an_empty_bubble():
    assert tooltip_html([]) == "" and tooltip_text([]) == ""
    assert tooltip_html([tool_tips.Section("", "", "")]) == ""


def test_a_name_or_summary_is_shown_not_parsed():
    a = _skill("a", name="Cut & <b>Paste</b>", summary="Costs < 5 aUEC & more.")
    html = tooltip_html(tile_sections(a, [a]))
    assert "Cut &amp; &lt;b&gt;Paste&lt;/b&gt;" in html and "Costs &lt; 5 aUEC &amp; more." in html


# ── hidden tools ─────────────────────────────────────────────────────────────

def test_a_tab_tool_is_described_on_its_hosts_tile():
    host = _skill("host", summary="The host.", hotkey="<ctrl>+3")
    tab = _skill("tab", name="Tabby", summary="The tab.", hotkey="<ctrl>+2", hidden=True, tab_of="host")
    other = _skill("other", summary="Unrelated.", hotkey="<ctrl>+4")
    lines = tooltip_lines(tile_sections(host, [host, tab, other]))
    assert lines == ["Host", "The host.", "Hotkey: Ctrl+3", "", "Tab: Tabby", "The tab.", "Hotkey: Ctrl+2"]
    # the tab's own key is the one that follows a rebind of the tab
    assert tooltip_lines(tile_sections(host, [host, tab], hotkeys={"tab": "<alt>+9"}))[-1] == "Hotkey: Alt+9"
    # a tab tool the user switched off is not a tab (SC_TOOLBOX_TABS_OFF), so the tile does not promise it
    assert tooltip_lines(tile_sections(host, [host, tab], disabled=["tab"])) == ["Host", "The host.", "Hotkey: Ctrl+3"]
    # and the tab tool's own tile, if the user asked for it back, is about itself only
    assert tooltip_lines(tile_sections(tab, [host, tab])) == ["Tabby", "The tab.", "Hotkey: Ctrl+2"]


def test_every_hidden_tool_is_covered_by_a_tile(registry, window):
    by_id = {s.id: s for s in registry}
    shown = {s.id for s in tile_skills(registry)}
    for s in registry:
        if not s.hidden:
            continue
        if s.tab_of:
            assert s.tab_of in shown, f"{s.name} is a tab of {s.tab_of}, which has no tile"
            text = window["tiles"][s.tab_of]["text"]
            assert s.name in text, f"the {s.tab_of} tile does not name its tab {s.name}"
            assert " ".join(text.split()).count(s.summary) == 1, f"the {s.tab_of} tile does not describe {s.name}"
        elif s.id in FOLDED_INTO:
            host, words = FOLDED_INTO[s.id]
            assert host in shown and words in by_id[host].summary, f"{by_id[host].name}'s summary does not name {words}"
        else:
            assert s.id in HIDDEN_ON_ITS_OWN, f"{s.name} ({s.id}) is hidden and no tile mentions it"


# ── width ────────────────────────────────────────────────────────────────────

def test_no_line_is_longer_than_the_wrap():
    long = _skill("a", name="N" * 70, summary="word " * 31 + "end.", hotkey="<ctrl>+<shift>+<alt>+f12")
    lines = tooltip_lines(tile_sections(long, [long]))
    assert max(map(len, lines)) <= WRAP and len(lines) > 4
    assert " ".join(lines[2:-1]) == ("word " * 31 + "end."), "wrapping lost or changed words"
    # the HTML is the same lines, and tells Qt not to wrap them again
    html = tooltip_html(tile_sections(long, [long]))
    assert html.count("<br>") == len(lines) - 1 and "white-space:pre" in html


def test_real_tiles_wrap(window):
    for sid, tip in window["tiles"].items():
        lines = tip["text"].split("\n")
        assert max(map(len, lines)) <= WRAP, f"{sid}: a {max(map(len, lines))}-character line"


# ── the real window ──────────────────────────────────────────────────────────

def test_every_tile_in_the_real_window_says_name_summary_and_hotkey(registry, window):
    tiles = window["tiles"]
    assert set(tiles) == {s.id for s in tile_skills(registry)}
    for s in tile_skills(registry):
        text = tiles[s.id]["text"]
        flat = " ".join(text.split())
        assert text.split("\n")[0] and s.name.startswith(text.split("\n")[0][:10]), f"{s.id}: the name is not first"
        assert s.summary in flat, f"{s.id}: the summary is not on its tile"
        assert tool_tips.hotkey_line(s.hotkey) in text.split("\n"), f"{s.id}: no hotkey line"
        assert "<b>" in tiles[s.id]["html"]


def test_the_real_tile_follows_a_saved_binding_and_a_rebind():
    saved = _probe("--saved", "dps=<ctrl>+<shift>+f7")["tiles"]["dps"]
    assert "Hotkey: Ctrl+Shift+F7" in saved["text"] and "Shift+1" not in saved["text"].replace("Ctrl+Shift+F7", "")
    out = _probe("--rebind", "dps=<alt>+8,suitmk2=<alt>+9")["tiles"]
    assert "Hotkey: Alt+8" in out["dps"]["text"] and "Shift+1" not in out["dps"]["text"]
    assert out["dps"]["badge"], "the badge went blank"
    assert "Hotkey: Alt+9" in out["assistant"]["text"], "the Suit Mk2 tab's key on the Assistant tile did not follow"
    assert "Hotkey: Ctrl+3" in out["assistant"]["text"], "the Assistant's own key changed with it"


def test_the_real_tile_with_its_keybind_off_or_no_summary():
    out = _probe("--keybinds-off", "dps", "--no-summary", "cargo")["tiles"]
    assert "Hotkey" not in out["dps"]["text"] and out["dps"]["badge"] == ""
    assert out["cargo"]["text"].split("\n") == ["Cargo Loader", "Hotkey: Shift+2"]


def test_a_tab_tool_switched_off_is_not_promised_on_the_real_tile(registry):
    suit = next(s for s in registry if s.id == "suitmk2")
    out = _probe("--disabled", "suitmk2")["tiles"]["assistant"]["text"]
    assert suit.name not in out and "Ctrl+2" not in out


def test_hovering_a_label_inside_the_tile_shows_the_tiles_tooltip():
    out = _probe("--hover", "dps")
    assert out["hover"]["visible"] and out["hover"]["same"], out["hover"]
    assert "DPS Calculator" in out["hover"]["text"]


def test_the_launcher_shows_tooltips_while_another_window_has_the_keyboard(window):
    assert window["always"] is True

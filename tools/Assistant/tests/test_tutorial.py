"""The Toolbox Assistant window has a tutorial: a button for it, text on every tab, and only names that exist.

One tutorial for both tabs of the window (the Assistant and Suit Mk2). Every
name set in bold in it must be a string in the source of the tool it belongs
to (the Assistant, Suit Mk2's panel, the launcher's Settings), an alias for
one the tool builds at run time, or listed below as prose. The rule is in
shared/tutorial_guard.py. Rename a button on either tab and this goes red
until the tutorial says the new name.

Neither tool is built: the window gets stand-in tabs, as in test_hub_window.py.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

HERE = os.path.dirname(os.path.abspath(__file__))
A_ROOT = os.path.normpath(os.path.join(HERE, ".."))
REPO = os.path.normpath(os.path.join(A_ROOT, "..", ".."))

from assistant import hub  # noqa: E402
from assistant import tutorial  # noqa: E402
from shared import tutorial_guard as guard  # noqa: E402

SUIT = os.path.join(REPO, "tools", "SuitMk2")
TUTORIAL = os.path.join(A_ROOT, "assistant", "tutorial.py")
SOURCES = [
    os.path.join(A_ROOT, "assistant"),
    os.path.join(A_ROOT, "toolbox_assistant_app.py"),
    os.path.join(SUIT, "ui"),
    os.path.join(SUIT, "core", "pacing.py"),
    os.path.join(REPO, "ui", "settings_panel.py"),
]

# A tab of the tutorial itself.
PROSE = ["Set Route"]
# Names the tools build at run time, and the source text behind each.
ALIASES = {
    "Elah volume": "volume",            # f"{name} volume", for each of the two
    "Montaigne volume": "volume",
}


@pytest.fixture
def app(monkeypatch):
    a = QApplication.instance() or QApplication([])
    monkeypatch.setattr(hub, "_save_window_state", lambda _w: None)     # a test must not write <repo>/logs
    monkeypatch.setattr(hub, "_LIVE", None)
    yield a
    from shared.qt.tutorial_popup import TutorialPopup
    for popup in list(TutorialPopup._open.values()):
        popup.close()
    for w in list(a.topLevelWidgets()):
        w.hide()
        w.deleteLater()
    a.processEvents()


def _entry():
    """toolbox_assistant_app.py, loaded as test_hub_window.py loads it."""
    name = "_toolbox_assistant_app_for_tutorial_test"
    if name in sys.modules:
        return sys.modules[name]
    saved_path, saved_ui = list(sys.path), sys.modules.get("ui")
    spec = importlib.util.spec_from_file_location(name, os.path.join(A_ROOT, "toolbox_assistant_app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path[:] = saved_path
        if saved_ui is not None:
            sys.modules["ui"] = saved_ui
    return mod


def _window(keys=("assistant", "suitmk2"), extra_buttons=None):
    labels = {"assistant": "Assistant", "suitmk2": "Suit Mk2"}
    specs = [hub.TabSpec(k, labels[k], lambda mic, k=k: QLabel("tab " + k)) for k in keys]
    return hub.HubWindow(title="Toolbox Assistant / AI Crew", tabs=specs, extra_buttons=extra_buttons)


# ── the text ─────────────────────────────────────────────────────────────────

def test_every_tab_has_a_title_and_text():
    tabs = tutorial.tabs()
    assert [t.title for t in tabs] == ["Start", "Assistant", "Set Route", "Suit Mk2", "Suit controls"]
    for tab in tabs:
        assert len(guard.plain_text(tab.html)) > 200, "tab %r is nearly empty" % tab.title


def test_a_tool_that_has_no_tab_in_the_window_gets_none_in_the_tutorial():
    assert [t.title for t in tutorial.tabs(["assistant"])] == ["Start", "Assistant", "Set Route"]
    assert [t.title for t in tutorial.tabs(["suitmk2"])] == ["Start", "Suit Mk2", "Suit controls"]
    only_assistant = tutorial.markup(["assistant"])
    assert "Montaigne" not in only_assistant and "push-to-talk key" not in only_assistant


def test_the_keys_shown_are_read_when_it_opens_not_written_in(monkeypatch):
    import shared.hotkey_label as hk
    from shared import ptt_keys
    monkeypatch.setattr(hk, "hotkey_label", lambda key, default: {"hotkey_assistant": "Ctrl+Alt+F9",
                                                                  "hotkey_suitmk2": "Ctrl+Alt+F8"}.get(key, default))
    monkeypatch.setattr(ptt_keys, "saved_labels", lambda: {"assistant": "F13", "suitmk2": "F14"})
    text = guard.plain_text(tutorial.markup())
    for shown in ("Ctrl+Alt+F9", "Ctrl+Alt+F8", "Hold F13 to talk to the Assistant", "Hold F14 to talk to Elah"):
        assert shown in text, shown
    assert "Pause" not in text and "Scroll Lock" not in text        # the defaults are only a fallback


def test_the_default_keys_it_falls_back_to_are_the_real_defaults():
    from shared import ptt_keys
    assert ptt_keys.label(ptt_keys.ASSISTANT_DEFAULT) == "Pause"
    assert ptt_keys.label(ptt_keys.SUIT_DEFAULT) == "Scroll Lock"
    src = open(TUTORIAL, encoding="utf-8").read()
    assert '_ptt(TAB_ASSISTANT, "Pause")' in src and '_ptt(TAB_SUIT, "Scroll Lock")' in src


def test_every_name_in_bold_exists_in_the_tool_it_belongs_to():
    markup = tutorial.markup()
    assert len(guard.bold_spans(markup)) > 50          # the check below is not passing on an empty list
    strings = guard.source_strings(SOURCES, exclude=[TUTORIAL])
    assert guard.problems(markup, strings, PROSE, ALIASES) == []


# ── the button ───────────────────────────────────────────────────────────────

def test_the_window_puts_extra_buttons_in_its_title_bar(app):
    pressed = []
    w = _window(extra_buttons=[("? Tutorial", lambda: pressed.append(True))])
    buttons = [b for b in w._title_bar.findChildren(QPushButton) if b.text() == "? Tutorial"]
    assert len(buttons) == 1
    buttons[0].click()
    assert pressed == [True]


def test_a_window_given_no_extra_buttons_has_none(app):
    w = _window()
    assert [b for b in w._title_bar.findChildren(QPushButton) if b.text() == "? Tutorial"] == []


def test_the_entry_script_opens_the_tutorial_for_the_tabs_the_window_has(app):
    entry = _entry()
    from shared.qt.tutorial_popup import TutorialPopup
    popup = entry.show_tutorial(_window(keys=("suitmk2",)))
    assert popup is TutorialPopup._open["toolbox_assistant"] and popup.isVisible()
    assert [popup.tab_widget.tabText(i) for i in range(popup.tab_widget.count())] == [
        "Start", "Suit Mk2", "Suit controls"]
    for i in range(popup.tab_widget.count()):
        assert guard.plain_text(popup.tab_text(i))


def test_the_entry_script_gives_the_window_the_button():
    src = open(os.path.join(A_ROOT, "toolbox_assistant_app.py"), encoding="utf-8").read()
    assert 'extra_buttons=[("? Tutorial", lambda: show_tutorial(holder["window"]))]' in src


def test_opening_the_tutorial_builds_no_tab(app):
    built = []
    specs = [hub.TabSpec(k, k, lambda mic, k=k: built.append(k) or QLabel(k)) for k in ("assistant", "suitmk2")]
    w = hub.HubWindow(title="T", tabs=specs)
    assert built == ["assistant"]                       # the first tab, as always
    _entry().show_tutorial(w)
    app.processEvents()
    assert built == ["assistant"], "the tutorial must not build (and so start) the other tool"

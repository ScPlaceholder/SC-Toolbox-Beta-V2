"""Shared safety net for the Cargo Loader UI tests.

Since 2026-10-03 Optimize / Auto ask "are you sure?" when the hold holds
hand-placed objects, and Load Plan shows a notice for items it refused. Both
are real dialogs. Offscreen, a modal dialog nobody answers hangs the whole run
silently - which is exactly what the first run after adding the prompt did.

So by default an unexpected prompt FAILS the test that triggered it, loudly,
instead of hanging; a test that means to answer it replaces the method on its
window (monkeypatch.setattr(win, "_ask_reorganise", ...)). Notices are
recorded on the window as `_notices` instead of being shown.
"""

import sys

import pytest


@pytest.fixture(autouse=True)
def _no_unanswered_dialogs(monkeypatch):
    # The test modules import cargo_app themselves (after their own sys.path
    # setup), so by the time a test runs it is already in sys.modules. Never
    # import it from here: suites that do not use the app must not pay for it.
    app = sys.modules.get("cargo_app")
    cls = getattr(app, "CargoApp", None)
    if cls is not None:
        def _ask(self, text):
            raise AssertionError("unexpected 'reorganise the hold?' prompt: " + text)

        def _notice(self, title, text):
            self.__dict__.setdefault("_notices", []).append((title, text))

        if hasattr(cls, "_ask_reorganise"):
            monkeypatch.setattr(cls, "_ask_reorganise", _ask)
        if hasattr(cls, "_show_notice"):
            monkeypatch.setattr(cls, "_show_notice", _notice)
    yield

"""The Mouse Blocker's tutorial text.

Shown in shared/qt/tutorial_popup.py's popup, opened from "? Tutorial" in the
blocker's title bar (ui/app.py). Every name in <b> was read out of the source
it belongs to, and tests/test_tutorial.py checks each one is still there:
  ui/blocker_overlay.py   MOUSE BLOCKER ACTIVE, Star Citizen input is currently blocked
  ui/app.py               Mouse Blocker (the title and the launcher tile)
  ui/settings_panel.py    SETTINGS, Tools (the launcher's settings)
"""
from __future__ import annotations

from typing import List

from shared.qt.tutorial_popup import DIM, Tab, h3, h4, page

ACCENT = "#ff3355"              # the blocker's own accent (ui/app.py)
_ACC = f"color: {ACCENT};"


def _hotkey() -> str:
    """This tool's hotkey as the launcher has it now (follows a rebind)."""
    try:
        from shared.hotkey_label import hotkey_label
        return hotkey_label("hotkey_mouse_blocker", "<shift>+0")
    except Exception:                       # noqa: BLE001 - a tutorial is not worth a failed open
        return "Shift+0"


def _getting_started() -> str:
    key = f'<span style="{_ACC}">{_hotkey()}</span>'
    return page(f"""
{h3("Mouse Blocker", ACCENT)}
<p>Use the Mouse Blocker to keep mouse clicks away from Star Citizen. It
covers your main screen with a see-through sheet that takes every click, so
a click that misses a toolbox window does nothing in the game.</p>

{h4("Using it", ACCENT)}
<ol>
  <li>Press {key}, or click the <b>Mouse Blocker</b> tile on the launcher. A
      red border appears round your main screen, with
      <b>MOUSE BLOCKER ACTIVE</b> in the middle.</li>
  <li>Use your toolbox windows as usual. They stay above the blocker and
      still take your clicks.</li>
  <li>Press {key} again to take the blocker away.</li>
</ol>

{h4("What it blocks", ACCENT)}
<ul>
  <li>Clicks, drags and the scroll wheel, anywhere on the sheet.</li>
  <li>Not the keyboard. Keys still go to the game.</li>
  <li>Your main screen only. A game on another monitor is not covered.</li>
</ul>
<p style="{DIM}">While it is up, the line under the big text reads
<b>Star Citizen input is currently blocked</b>. That means mouse input.</p>
""")


def _good_to_know() -> str:
    key = f'<span style="{_ACC}">{_hotkey()}</span>'
    return page(f"""
{h3("Good to know", ACCENT)}

{h4("The bar at the top", ACCENT)}
<ul>
  <li>The slider makes the red border and the text fainter or stronger.</li>
  <li>The <b>x</b> hides the blocker, the same as {key}.</li>
  <li>The blocker always covers the whole screen. If it ever does not, the
      round-arrow button next to the slider stretches it back over the
      screen.</li>
</ul>

{h4("It is ready before you need it", ACCENT)}
<p>The launcher starts the Mouse Blocker hidden when it starts, so the
hotkey brings it up at once.</p>

{h4("Changing the hotkey", ACCENT)}
<p>Open the launcher's <b>SETTINGS</b> and find <b>Mouse Blocker</b> on the
<b>Tools</b> tab. The key shown in the blocker's title bar is the one in
use.</p>
""")


def tabs() -> List[Tab]:
    """The tutorial's tabs, built when it opens so the hotkey shown is the current one."""
    return [Tab("Getting Started", _getting_started()), Tab("Good to Know", _good_to_know())]


def markup() -> str:
    """All the text, for the tests."""
    return "".join(t.html for t in tabs())

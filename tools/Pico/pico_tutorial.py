"""How Pico works: Pico's tutorial.

A plain Qt dialog with a tab for each subject, like his Customise box: Pico is
a process of his own with no toolbox theme, so this does not use the toolbox's
tutorial popup. It is opened from his right-click menu ("How Pico works...")
and from a button of the same name in Customise Pico, which is the box every
start already opens. Nothing here opens by itself.

The text is in pages(); the window is Tutorial. This file is beside
sprite_pal.py and not under pico/, because nothing under pico/ may name Qt
(pico.selftest.check_no_qt).

Every name in <b> is something the user can read on screen, and
tests/test_pico_tutorial.py checks each one is still a string in sprite_pal.py,
pico/signs.py or the launcher. What he does was read out of:
  sprite_pal.py     the menu, Customise, the drag, the hover text, GAME_QUIET_S
  pico/sprites.py   MOODS, EVENT_LOOPS, HAND_LOOPS, RARE_LOOPS, GAG_COOLDOWN_S,
                    REST_S, LIVELINESS, SIGN_HOLD_S
  pico/signs.py     SIGNS, IDLE_COOLDOWN_S, MOOD_VETO
"""
from __future__ import annotations

import os
import sys
from typing import List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))

TITLE = "How Pico works"
MENU_TEXT = "How Pico works..."
SIZE = (560, 500)


def hotkey() -> str:
    """Pico's hotkey as the launcher has it now (follows a rebind). Pico runs outside the toolbox's
    start-up, so the toolbox root may not be on sys.path yet."""
    try:
        if ROOT not in sys.path:
            sys.path.append(ROOT)
        from shared.hotkey_label import hotkey_label
        return hotkey_label("hotkey_pico", "<ctrl>+7")
    except Exception:                       # noqa: BLE001 - a tutorial is not worth a failed open
        return "Ctrl+7"


def pages() -> List[Tuple[str, str]]:
    """(tab title, html) per tab, built when the window opens so the hotkey shown is the current one."""
    key = hotkey()
    meet = f"""
<h3>Meet Pico</h3>
<p>Pico is a penguin who stands on top of your screen and reacts to what
happens to you in Star Citizen. He needs nothing from you. This is what you
can do with him.</p>
<h4>Move him</h4>
<p>Drag him with the left mouse button. He stays where you leave him, and he
is there next time.</p>
<h4>Right-click him</h4>
<ul>
  <li><b>Customise Pico...</b> &mdash; his outfit, his size and how he
      behaves</li>
  <li><b>How Pico works...</b> &mdash; this window</li>
  <li><b>Quit Pico</b> &mdash; closes him</li>
</ul>
<h4>Hide him and bring him back</h4>
<p>Press {key}, or click the <b>Pico Pals</b> tile on the launcher, to hide
him. Do it again to bring him back. While he is hidden he keeps following the
game. After <b>Quit Pico</b>, the same key or tile starts him again.</p>
<h4>Ask him how he feels</h4>
<p>Hold the mouse over him. A small note says what mood he is in and why.</p>
<h4>Why Customise opens every time</h4>
<p><b>Customise Pico</b> opens each time Pico starts, so it is always easy to
find. Press Cancel if you do not want to change anything.</p>
"""
    does = """
<h3>What he does</h3>
<h4>He follows your game</h4>
<p>Pico reads Game.log, the file Star Citizen writes as you play. He only
reads it. He does not touch the game.</p>
<h4>His moods</h4>
<p>He is calm, alert, hurt, happy, startled or irritated, depending on what
has been happening to you. If he cannot find the log, or the game has not
written to it for 15 minutes, he looks confused: he does not know what is
going on.</p>
<h4>He reacts to events</h4>
<p>Some things in the game get a gesture of their own, for example accepting,
finishing or failing a contract, arriving from quantum travel, a hangar being
ready, an incoming call, and getting hurt.</p>
<h4>He copies what is in your hand</h4>
<p>Take out a weapon and he takes out one of his own. The same goes for a
multitool, a med pen, a grenade, a mining gadget, a knife, and food or drink:
when you eat, he eats.</p>
<h4>He rests</h4>
<p>Between animations he stands still for 10 to 20 seconds. <b>How lively</b>
in <b>Customise Pico</b> makes the rests longer or shorter. Something
happening in the game ends a rest at once.</p>
"""
    gags = """
<h3>Gags and signs</h3>
<h4>Gags</h4>
<p>Now and then, when he is calm or happy, Pico does a longer routine with a
prop: a puppet, an action figure, or a Whale certificate. One outfit, Banu,
has a gag of its own.</p>
<p>Gags are rare on purpose. After one, he does not do another for a while.
That gap is <b>Gags at most</b> in <b>Customise Pico</b>, and it starts at
<b>every 15 min</b>, so you see four an hour at the very most.</p>
<h4>Signs</h4>
<p>Pico sometimes holds up a sign with a slogan, such as
<b>BUY MORE SHIPS!</b> or <b>FLY DANGEROUSLY!</b>, for a few seconds.</p>
<ul>
  <li>Signs come up by themselves every so often, at least three minutes
      apart.</li>
  <li>Some game events can bring one up too, such as finishing a contract or
      arriving from quantum travel.</li>
  <li>He does not hold up signs while he is hurt, startled or irritated.</li>
</ul>
<h4>Turning them off</h4>
<p>In <b>Customise Pico</b>, untick
<b>Gag props (puppet, action figure, Whale certificate)</b> for no gags, and
<b>Signs on game events</b> for no signs. That second box turns off every
sign, including the ones that come up by themselves.</p>
"""
    custom = """
<h3>Customise Pico</h3>
<p>Right-click Pico and choose <b>Customise Pico...</b></p>
<ul>
  <li><b>Outfit</b> &mdash; what he wears. There is one for each ship maker
      installed on this PC, and <b>Drake</b> is the one he starts in.</li>
  <li><b>Size</b> &mdash; drag the slider to make him smaller or bigger.</li>
  <li><b>Gag props (puppet, action figure, Whale certificate)</b> &mdash;
      untick it and he does no gags.</li>
  <li><b>Gags at most</b> &mdash; the shortest gap between two gags, from
      <b>every 15 min</b> to <b>every 4 hours</b>.</li>
  <li><b>How lively</b> &mdash; <b>Calm (long rests)</b>, <b>Normal</b> or
      <b>Lively (short rests)</b>.</li>
  <li><b>Signs on game events</b> &mdash; untick it and he holds up no
      signs.</li>
</ul>
<p>OK saves only what you changed. Cancel changes nothing.</p>
"""
    return [("Meet Pico", meet), ("What he does", does), ("Gags and signs", gags), ("Customise", custom)]


def markup() -> str:
    """All the text, for the tests."""
    return "".join(html for _title, html in pages())


def Tutorial(parent=None):
    """The tutorial window (a QDialog). Qt is imported here, not at the top, so that pages() can be
    read by something with no display."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog, QDialogButtonBox, QTabWidget, QTextBrowser, QVBoxLayout

    dlg = QDialog(parent)
    dlg.setWindowTitle(TITLE)
    # His window is always on top; a window of his that was not would open underneath the game.
    dlg.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    dlg.resize(*SIZE)
    lay = QVBoxLayout(dlg)
    tabs = QTabWidget(dlg)
    for title, html in pages():
        view = QTextBrowser(tabs)
        view.setOpenExternalLinks(False)
        view.setHtml(html)
        tabs.addTab(view, title)
    lay.addWidget(tabs)
    buttons = QDialogButtonBox(QDialogButtonBox.Close, dlg)
    buttons.rejected.connect(dlg.reject)
    buttons.accepted.connect(dlg.accept)
    lay.addWidget(buttons)
    dlg.tabs = tabs
    return dlg

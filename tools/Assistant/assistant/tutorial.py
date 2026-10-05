"""The tutorial of the Toolbox Assistant window: the Assistant tab and the Suit Mk2 tab.

Shown in shared/qt/tutorial_popup.py's popup, opened from "? Tutorial" in the
window's title bar (toolbox_assistant_app.py passes it to HubWindow). One
popup for both tools because they are one window; a tool switched off in the
launcher's Settings has no tab, and its tabs are left out of the tutorial too.

Every name in <b> was read out of the source it belongs to, and
tests/test_tutorial.py checks each one is still there:
  toolbox_assistant_app.py           Assistant, Suit Mk2 (the two tab buttons)
  assistant/panel.py                 Push-to-talk, Always on, Set Mic Key, Mic key,
                                     Voice Replies, Settings, In-Game, Calibrate
                                     Route, Send, LLM Settings and its rows
  assistant/set_route/route_setter.py   Begin
  tools/SuitMk2/ui/suit_window.py    Mute, Presence, Talk key, Test voices,
                                     Chattiness, Good one, Shut up, Resume, the
                                     volume sliders, Export / Import memory, the
                                     tick boxes, Speaker models in VRAM, Use Claude
                                     for lines, the status rows
  tools/SuitMk2/ui/setup_panel.py    Set up Elah and Montaigne (about 1.9 GB)
  tools/SuitMk2/core/pacing.py       silent ... very chatty

WHAT THE SUIT MK2 TABS WERE WRITTEN AGAINST: tools/SuitMk2 at commit 596bff8
(2026-10-05), "the Suit decides who answers, unless you say which of them".
Work on what the companions know and on free conversation was in progress and
not committed then, and is not described here.
"""
from __future__ import annotations

from typing import Iterable, List, Optional

from shared.qt.theme import P
from shared.qt.tutorial_popup import DIM, YELLOW, Tab, h3, h4, page

ACCENT = P.energy_cyan          # the window's accent (toolbox_assistant_app.py)
_C_SUIT = "#7fd1b9"             # Suit Mk2's own colour (its skill.json)
_C_ROUTE = "#ffb347"
_ACC = f"color: {ACCENT};"

TAB_ASSISTANT = "assistant"
TAB_SUIT = "suitmk2"


def _hotkey(settings_key: str, default: str, fallback: str) -> str:
    """A tool's hotkey as the launcher has it now (follows a rebind)."""
    try:
        from shared.hotkey_label import hotkey_label
        return hotkey_label(settings_key, default)
    except Exception:                       # noqa: BLE001 - a tutorial is not worth a failed open
        return fallback


def _ptt(tab: str, fallback: str) -> str:
    """A tab's push-to-talk key as it is saved now (the default when none was chosen)."""
    try:
        from shared import ptt_keys
        return ptt_keys.saved_labels().get(tab) or fallback
    except Exception:                       # noqa: BLE001
        return fallback


def _key(text: str) -> str:
    return f'<span style="{_ACC}">{text}</span>'


def _start(with_assistant: bool, with_suit: bool) -> str:
    a_hot = _key(_hotkey("hotkey_assistant", "<ctrl>+3", "Ctrl+3"))
    s_hot = _key(_hotkey("hotkey_suitmk2", "<ctrl>+2", "Ctrl+2"))
    a_ptt = _key(_ptt(TAB_ASSISTANT, "Pause"))
    s_ptt = _key(_ptt(TAB_SUIT, "Scroll Lock"))
    tabs = ""
    if with_assistant:
        tabs += (f"<li><b>Assistant</b> &mdash; ask it things: where to buy an item, the best trade route, "
                 f"where to mine. It looks the answer up in the toolbox's own tools. Hotkey {a_hot}.</li>")
    if with_suit:
        tabs += (f"<li><b>Suit Mk2</b> &mdash; Elah, your suit, and Montaigne, your ship. They follow your game "
                 f"and talk to you about it. Hotkey {s_hot}.</li>")
    keys = ""
    if with_assistant and with_suit:
        keys = f"""
{h4("Talking to them", ACCENT)}
<p>Each of the two has its own push-to-talk key. Hold a key, speak, and let
go: the key you held decides who hears you, whichever tab is showing, and
even with this window closed.</p>
<ul>
  <li>Hold {a_ptt} to talk to the Assistant.</li>
  <li>Hold {s_ptt} to talk to Elah and Montaigne.</li>
</ul>
<p>While a key is held, a strip at the top of the screen says who is
listening. A game in exclusive fullscreen covers the strip; windowed and
borderless do not. Each key is shown on its tab's button, and is changed on
that tab.</p>
"""
    return page(f"""
{h3("Toolbox Assistant", ACCENT)}
<p>This window holds the toolbox's two talking tools, a tab each.</p>
<ul>{tabs}</ul>

{h4("Opening and closing", ACCENT)}
<ul>
  <li>A tab's hotkey, or the tile on the launcher, opens the window on that
      tab. Pressed again while that tab is showing, it hides the window.</li>
  <li>The <b>x</b> in the title bar hides the window. Nothing stops: both
      tools keep running until you close the launcher.</li>
</ul>
{keys}
{h4("One microphone", ACCENT)}
<p>Only one of the two listens with an open microphone at a time: the one
whose tab is showing. Push-to-talk is not affected by which tab is
showing.</p>
""")


def _assistant() -> str:
    a_ptt = _key(_ptt(TAB_ASSISTANT, "Pause"))
    return page(f"""
{h3("Assistant", ACCENT)}
<p>Use the <b>Assistant</b> tab to ask a question and get the answer from the
toolbox's tools without opening them.</p>

{h4("Asking", ACCENT)}
<ul>
  <li>Hold {a_ptt}, ask, and let go. Or type in the box at the bottom and
      press <b>Send</b>.</li>
  <li><b>You:</b> shows what it heard. <b>AI:</b> shows the answer, and it
      is spoken too.</li>
</ul>

{h4("Things to ask", ACCENT)}
<ul>
  <li>The best cargo route for your ship</li>
  <li>Where to buy an item, a ship part or a ship, and what it costs</li>
  <li>How much cargo a ship holds</li>
  <li>The best guns for a ship</li>
  <li>Where a resource is mined, and what a scanner signal number means</li>
  <li>Which missions give a blueprint, and what a blueprint needs</li>
  <li>What you are carrying, and how long you have played</li>
  <li><em>navigate to Area 18</em>: see the <b>Set Route</b> tab of this
      tutorial</li>
</ul>

{h4("The buttons", ACCENT)}
<ul>
  <li><b>Push-to-talk</b> or <b>Always on</b> &mdash; hold a key to talk, or
      leave the microphone open and just talk.</li>
  <li><b>Mic key:</b> &mdash; the key you hold. Click it and press another
      key to change it. It reads <b>Set Mic Key</b> when none is set. The
      left and right mouse buttons are not allowed.</li>
  <li><b>Voice Replies</b> &mdash; on, answers are spoken. Off, they are
      only written.</li>
  <li><b>Settings&hellip;</b> &mdash; opens <b>LLM Settings</b>: which
      language model helps the Assistant choose and word its answers. You do
      not need to change it. With no model available, the Assistant still
      answers by itself.</li>
</ul>

{h4("The voice", ACCENT)}
<p>The Assistant speaks in the voice chosen under <b>Tool voice</b> in the
launcher's <b>SETTINGS</b>. The microphone it listens to is chosen there too,
under <b>Microphone</b>.</p>
""")


def _set_route() -> str:
    return page(f"""
{h3("Setting a route in the game", _C_ROUTE)}
<p>The Assistant can set your route inside Star Citizen: it opens the game's
own map, types the destination and sets the route, using your mouse and
keyboard.</p>

{h4("Set it up once", _C_ROUTE)}
<ol>
  <li>On the <b>Assistant</b> tab, turn on <b>In-Game</b>. While it is off,
      nothing is ever sent to the game. It is the same switch as
      <b>In-Game</b> on the Star Map.</li>
  <li>Open Star Citizen, click on the game, and say
      <em>calibrate star map</em>. With no microphone, press
      <b>Calibrate Route</b> and then <b>Begin</b>.</li>
  <li>The Assistant tells you each step, out loud and in a small window:
    <ol>
      <li>Press F2 for the game's map and zoom out. Left-click the search
          bar, then press Enter.</li>
      <li>Type a destination in your current system and left-click its
          result.</li>
      <li>Left-click the centre of the map.</li>
    </ol>
  </li>
</ol>
<p>Esc cancels. When it says <em>Calibration saved</em>, it is done, and it
is remembered.</p>

{h4("Setting a route", _C_ROUTE)}
<ol>
  <li>Say <em>navigate to Area 18</em> or <em>set route to Area 18</em>.</li>
  <li>The Assistant names the place and asks whether to set the route. Say
      <em>yes</em> or <em>no</em>.</li>
  <li>On yes, leave the mouse and keyboard alone until it says the route is
      plotted.</li>
</ol>
<ul>
  <li>If several places fit, it asks which one.</li>
  <li>With <b>In-Game</b> off it tells you so and does nothing in the
      game.</li>
  <li>It pastes the destination's name into the game, so whatever you had
      copied is replaced.</li>
</ul>
<p style="{DIM}">The Star Map does not need to be open for any of this.</p>
""")


def _suit() -> str:
    s_ptt = _key(_ptt(TAB_SUIT, "Scroll Lock"))
    return page(f"""
{h3("Suit Mk2", _C_SUIT)}
<p>The <b>Suit Mk2</b> tab is two companions who follow your game and talk
to you about it: <b>Elah</b>, your suit, and <b>Montaigne</b>, your ship.
They read the game's Game.log file. They cannot do anything in the game for
you.</p>

{h4("The first time", _C_SUIT)}
<p>The companions run on your own PC. If they are not installed yet, the tab
shows <b>Set up Elah and Montaigne (about 1.9 GB)</b>. Press it and wait for
the download.</p>

{h4("When they speak", _C_SUIT)}
<p><span style="{YELLOW}">They speak on their own only while this window is
open.</span> Hide it and they go quiet, though they keep following the game.
A question you ask with the talk key is still answered with the window
hidden.</p>

{h4("Asking them something", _C_SUIT)}
<ol>
  <li>Hold {s_ptt}, ask, and let go.</li>
  <li>The Suit decides which of the two answers.</li>
  <li>To choose, say a name: start with it (<em>Montaigne, how is the
      ship</em>), end with it (<em>where are we, Elah</em>), or say
      <em>hey Elah</em>.</li>
</ol>
<p>A name in the middle of a sentence, as in <em>what did Montaigne say
about cargo</em>, does not choose anyone.</p>
<p>They answer from what the game has told them: where you are, which
system, whether you are hurt, what you are carrying, what you have earned.
When they do not know, they say so.</p>

{h4("Reading the tab", _C_SUIT)}
<p>The rows at the top (<b>Game.log</b>, <b>Model service</b>,
<b>Elah voice</b>, <b>Montaigne voice</b> and the rest) show what is working.
The list at the bottom is what they have said lately.</p>
""")


def _suit_controls() -> str:
    return page(f"""
{h3("Suit Mk2 controls", _C_SUIT)}

{h4("How much they talk", _C_SUIT)}
<ul>
  <li><b>Mute</b> &mdash; silences them completely, answers included.</li>
  <li><b>Chattiness</b> &mdash; from <b>silent</b> (only urgent things and
      answers to your questions) up to <b>very chatty</b>.</li>
  <li><b>Shut up</b> &mdash; set a key for it, and press that key to quiet
      them for a while. <b>Resume</b> ends the quiet early.</li>
  <li><b>Good one</b> &mdash; set a key for it, and press that key to tell
      them you liked the last line.</li>
</ul>

{h4("Talking to them", _C_SUIT)}
<ul>
  <li><b>Talk key:</b> &mdash; the key you hold to talk to them. Click it to
      change it.</li>
  <li><b>Push-to-talk</b> or <b>Always on</b> &mdash; hold the key, or leave
      the microphone open while this tab is showing.</li>
</ul>

{h4("How they sound", _C_SUIT)}
<ul>
  <li><b>Elah volume</b> and <b>Montaigne volume</b> &mdash; one slider
      each, up to 200%.</li>
  <li><b>Test voices</b> &mdash; plays a line from each.</li>
</ul>

{h4("What they notice", _C_SUIT)}
<ul>
  <li><b>Presence</b> &mdash; how often they look at the game's picture,
      and only while Star Citizen is the window in front: <b>off</b>,
      <b>occasional</b>, <b>present</b> or <b>curious</b>. A change takes
      effect the next time the model service starts.</li>
  <li><b>Dev history fun facts</b> &mdash; off to start with. On, Montaigne
      now and then tells you something true about how Star Citizen was made.
      You can also say <em>fun facts on</em> or <em>fun facts off</em>.</li>
  <li><b>Keep training screenshots</b> &mdash; keeps, on this PC only, the
      small pictures they looked at. <b>Export training screenshots</b> zips
      them if you choose to share them.</li>
</ul>

{h4("Their memory", _C_SUIT)}
<p><b>Export memory</b> saves what they remember about you to a file, to
back it up or move it to another PC. <b>Import memory</b> puts such a file
back.</p>

{h4("If your graphics card is short of memory", _C_SUIT)}
<p><b>Speaker models in VRAM</b> starts on <b>One at a time</b>, which keeps
only the companion who is speaking loaded. <b>Keep both warm</b> avoids a
short wait when the other one speaks, and uses about twice the video
memory.</p>

{h4("Smarter lines", _C_SUIT)}
<p><b>Use Claude for lines</b> has the companions' lines worded by Claude,
using your own Anthropic API key and billed to your account. Enter the key,
press <b>Test</b> (which costs nothing), then <b>Save</b>. Without it they
use the models on your PC.</p>
""")


def tabs(shown: Optional[Iterable[str]] = None) -> List[Tab]:
    """The tutorial's tabs, built when it opens so the keys shown are the current ones.

    *shown* is the window's tab keys ("assistant", "suitmk2"); None means both. A tool switched off
    in the launcher's Settings has no tab in the window, and gets none here."""
    keys = set(shown) if shown is not None else {TAB_ASSISTANT, TAB_SUIT}
    a, s = TAB_ASSISTANT in keys, TAB_SUIT in keys
    if not (a or s):
        a = s = True
    out = [Tab("Start", _start(a, s))]
    if a:
        out += [Tab("Assistant", _assistant()), Tab("Set Route", _set_route())]
    if s:
        out += [Tab("Suit Mk2", _suit()), Tab("Suit controls", _suit_controls())]
    return out


def markup(shown: Optional[Iterable[str]] = None) -> str:
    """All the text, for the tests."""
    return "".join(t.html for t in tabs(shown))

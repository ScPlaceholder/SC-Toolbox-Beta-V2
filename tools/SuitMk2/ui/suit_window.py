"""suit_window.py - the SuitMk2 toolbox window: status, mute, presence, voice test, recent lines.

The companion itself runs whether or not this window is visible (preload: the toolbox starts it hidden). Closing the
window hides it; `quit` from the launcher stops the core, closes the session record and stops the model service
if this process started it.

TWO RULES ABOUT WHEN THE COMPANIONS SPEAK, and how they fit together:

  J 2026-10-04  "make sure that suitmk2 only have the AI's talk while it is launched": with the window hidden
                (preloaded, closed with X, toggled off) Elah and Montaigne say nothing of their own accord.
  J 2026-10-05  "individual push to talk buttons which also auto-route to the right ai": holding SuitMk2's talk
                key asks them something from anywhere, the window hidden included, and they answer.

The first rule is about UNPROMPTED speech; a held key is the pilot speaking to them. So the gate has two parts
(_voice_gate): everything is muted while the window is hidden, and a push-to-talk transcript opens an ANSWER
PASS for a short while, through which only a line that answers the pilot is let (speech.py: say(...,
addressed=True); the core marks its answers that way and nothing else). Ambient lines, event lines, banter and
dev facts are still refused while the window is hidden, pass or no pass. His Mute button silences answers too.
An always-open mic never opens the pass: only a held key is an explicit request.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton, QSlider,
                               QVBoxLayout, QWidget)

from shared import ptt_keys
from shared.mic_floor import FLOOR, OneMicMixin
from shared.qt.base_window import SCWindow
from shared.qt.theme import P
from shared.qt.title_bar import SCTitleBar

CORE = Path(__file__).resolve().parent.parent / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

import settings as st                          # noqa: E402
from companion_core import CompanionCore, find_game_log   # noqa: E402
from activity_mode import os_idle_seconds, DEFAULT_AFK_MINUTES   # noqa: E402
from speech import Speech, PRIORITY_EVENT      # noqa: E402
from sidecar import Sidecar, health            # noqa: E402
from conversation import ConversationLane, lane_state_from_core, without_departed   # noqa: E402
from voice_in.ears import EarsController      # noqa: E402
from voice_in.input_devices import InputBinding, BindingCaptureDialog, HotkeyMonitor   # noqa: E402
from pacing import LEVEL_NAMES                  # noqa: E402
from dev_facts import DevFacts                  # noqa: E402
import picture_pace as pp                       # noqa: E402
import chat_models                              # noqa: E402
import hardware_guard                           # noqa: E402

log = logging.getLogger("suitmk2.ui")

try:                                            # first-run "Set up Elah and Montaigne"; optional, never fatal
    from ui.setup_panel import SetupPanel       # noqa: E402
    from pair_realizer import MODEL_PREFIXES    # noqa: E402  same "complete set" rule the realizer uses
except Exception:                               # pragma: no cover - a broken panel must not take the window down
    log.exception("setup panel unavailable; the window runs without first-run setup")
    SetupPanel, MODEL_PREFIXES = None, ("suitmk2-", "realizer-")


class _Ears(OneMicMixin, EarsController):
    """SuitMk2's ears behind the process's one microphone (shared/mic_floor.py): while the Assistant's key is
    held, this one's is refused, and the other way round."""

    captureFailed = Signal(str)
    captureBusy = Signal(str)


# How long after a push-to-talk question the companions may answer with the window hidden. Long enough for a cold
# speaker model (about two seconds, more with the game holding the card) and the line itself; nothing unprompted
# can use it, so its length is not what keeps the 2026-10-04 rule.
ANSWER_PASS_S = 45.0


# What the Suit tab says about conversation memory, beside the button that clears it.
KEPT_NOTICE = ("What you say to Elah and Montaigne, and what they say back, is kept as text on this PC until you "
               "clear it.")
NOT_KEPT_NOTICE = "Conversations are not being kept."


def forget_conversations(core, pilot_dir) -> int:
    """Delete the conversation log and its summaries for this pilot, whether or not they are being kept right now.
    Returns how many log lines there were."""
    import tree_memory
    tree = getattr(core, "tree", None) if core is not None else None
    if tree is None:
        tree = tree_memory.open_tree(pilot_dir)
    return tree.clear()


# What the window says while the companions are disabled (J 2026-10-05: "a disable companions checkbox which keeps
# them from running for people who don't want them or have potato computers").
COMPANIONS_OFF_NOTICE = ("Companions are off. Nothing of theirs is running: no model, no eyes, no game log, no "
                         "voices, no talk key. Untick to start them.")


class _NoSpeech:
    """What stands where the voices stand while the companions are disabled. It loads nothing and says nothing, so
    the rest of the window (volume sliders, the voice gate, quit) can go on calling it."""

    ducker = None
    muted = True

    def mute(self, on) -> None:
        pass

    def allow_addressed(self, on) -> None:
        pass

    def set_level(self, who, level) -> None:
        pass

    def preload(self) -> None:
        pass

    def say(self, *a, **k) -> bool:
        return False

    def voice_source(self, who) -> str:
        return "off"

    def pending(self) -> int:
        return 0

    def close(self) -> None:
        pass


def _voice_gate(speech, user_muted: bool, tool_open: bool, answer_pass: bool) -> None:
    """Set what *speech* will say. Everything, when the tool is open and he has not muted it. With the tool hidden:
    nothing, except an answer to a question he asked with the talk key while *answer_pass* is open. Muted by
    him: nothing at all."""
    speech.mute(bool(user_muted) or not tool_open)
    allow = getattr(speech, "allow_addressed", None)
    if callable(allow):
        allow(bool(answer_pass) and not user_muted)


class _Slider(QSlider):
    """A slider the mouse wheel cannot move unless it was clicked first. Dry run 2026-09-23: the Chattiness slider
    flickered 1/0 six times in 7 s (a wheel passing over the dashboard) and came to rest on SILENT, so J played with
    the companions muted while believing he had set them to the chattiest level."""

    def __init__(self, *a):
        super().__init__(*a)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, e):
        if self.hasFocus():
            super().wheelEvent(e)
        else:
            e.ignore()


ACCENT = "#7fd1b9"


def _btn_ss(checked_color: str = ACCENT) -> str:
    return (f"QPushButton {{ background: {P.bg_input}; color: {P.fg}; border: 1px solid {P.border};"
            f" border-radius: 4px; padding: 4px 10px; }}"
            f"QPushButton:checked {{ background: {checked_color}; color: {P.bg_deepest}; }}"
            f"QPushButton:hover {{ border-color: {ACCENT}; }}")


# Offered in the window. Sonnet first: the production tier for high-volume short lines. Haiku is the cheapest.
API_MODELS = [("Claude Sonnet 5 (recommended)", "claude-sonnet-5"),
              ("Claude Haiku 4.5 (cheapest)", "claude-haiku-4-5"),
              ("Claude Opus 5 (best, costs most)", "claude-opus-5")]


class _SuitBody:
    """Everything the SuitMk2 dashboard is and does, without the window around it.

    Two classes at the bottom of this file put it on screen: SuitWindow (its own window, as it has always been) and
    SuitPanel (one tab of the Toolbox Assistant window, J 2026-10-04). Both get every method here; they differ only
    in the frame, in what "the window is open" means for the voice gate, and in who owns the microphone.
    """

    # False while another tab of a shared window is the one in front; see mic_take / mic_release.
    _mic_mine = True
    # True when the window has said this tab may go on watching its talk key while it is not in front.
    _ptt_bg = False
    # True for a short while after a push-to-talk question: the answer may be spoken with the window hidden.
    _ptt_pass = False
    # What to say on this tab when the Assistant's key is the same key.
    _ptt_clash = ""

    def _build(self, hotkey_text: str = "", cmd_file: Optional[str] = None, chrome: bool = True) -> None:
        self._standalone = not cmd_file or cmd_file == os.devnull
        self._quitting = False       # see _quit: three wirings onto one method, and it must run once
        self.s = st.load()
        self.core: Optional[CompanionCore] = None
        self.sidecar: Optional[Sidecar] = None
        self._boot_gen = 0           # goes up each time the companions are started or stopped; see _boot
        self.speech = self._make_speech()

        if chrome:                               # as a tab, the window it sits in has the title bar
            tb = SCTitleBar(window=self, title="SUIT MK2", accent_color=ACCENT, hotkey_text=hotkey_text,
                            show_minimize=True)
            tb.minimize_clicked.connect(self.showMinimized)
            tb.close_clicked.connect(self._on_close)
            self.content_layout.addWidget(tb)

        body = QWidget(self)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(12, 8, 12, 10)
        lay.setSpacing(8)

        # First-run setup. Hidden until _boot has woken the model service (and, through it, the local runtime), then
        # checked ONCE from _refresh: shown only when no COMPLETE character set exists (suitmk2-* or the dev
        # realizer-*). One click (or none, with settings "auto_setup") installs/wakes the runtime and provisions both.
        self.setup = None
        self._sidecar_ready = False             # set by _boot once sidecar.ensure() has returned
        self._setup_checked = False
        if SetupPanel is not None:
            try:
                self.setup = SetupPanel(self, auto_check=False, ready_prefixes=MODEL_PREFIXES,
                                        auto_start=bool(self.s.get("auto_setup", False)))
                self.setup.setVisible(False)
                self.setup.vision_chk.setChecked(bool(self.s.get("vision_glance")))
                self.setup.ready.connect(self._on_models_ready)
                lay.addWidget(self.setup)
            except Exception:
                log.exception("setup panel failed to build; continuing without it")
                self.setup = None

        # Disable companions (J 2026-10-05). Not a mute: ticked, nothing of theirs runs at all.
        power = QHBoxLayout()
        self._disable = QCheckBox("Disable companions")
        self._disable.setChecked(not self._companions_on())
        self._disable.setToolTip("Stops Elah and Montaigne from running at all: no model service, no model loaded, "
                                 "no eyes, no reading of the game log, no voices, no talk key.\nFor a PC with "
                                 "nothing to spare, or if you do not want them. Untick to start them again.")
        self._disable.toggled.connect(self._set_disabled)
        self._off_lbl = QLabel("")
        self._off_lbl.setStyleSheet(f"color: {P.yellow}; font-size: 9pt;")
        self._off_lbl.setWordWrap(True)
        power.addWidget(self._disable)
        power.addWidget(self._off_lbl, 1)
        lay.addLayout(power)

        grid = QGridLayout()
        self._rows = {}
        for i, key in enumerate(("Game.log", "Model service", "Elah voice", "Montaigne voice", "Heard / spoken",
                                 "Pacing", "Combat", "Game ears", "Eyes", "Hardware")):
            k = QLabel(key)
            k.setStyleSheet(f"color: {P.fg_dim}; font-size: 9pt;")
            v = QLabel("...")
            v.setStyleSheet(f"color: {P.fg}; font-family: Consolas; font-size: 9pt;")
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            grid.addWidget(k, i, 0)
            grid.addWidget(v, i, 1)
            self._rows[key] = v
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)

        ctl = QHBoxLayout()
        self._mute = QPushButton("Mute")
        self._mute.setCheckable(True)
        self._mute.setChecked(bool(self.s.get("muted")))      # his setting, not the window gate
        self._mute.setStyleSheet(_btn_ss(P.red))
        self._mute.toggled.connect(self._toggle_mute)
        ctl.addWidget(self._mute)
        pl = QLabel("Presence")
        pl.setStyleSheet(f"color: {P.fg_dim}; font-size: 9pt;")
        ctl.addWidget(pl)
        self._presence = QComboBox()
        self._presence.addItems(["off", "occasional", "present", "curious"])
        self._presence.setCurrentText(self.s["presence"])
        self._presence.setToolTip("How often the local eyes look at the game (only while Star Citizen is focused). "
                                  "Takes effect when the model service restarts.")
        self._presence.currentTextChanged.connect(self._set_presence)
        ctl.addWidget(self._presence)
        self._talk = QPushButton("Talk key: set...")
        self._talk.setStyleSheet(_btn_ss())
        self._talk.setToolTip("Suit Mk2's own push-to-talk key: hold it and ask Elah or Montaigne something "
                              "(local speech-to-text).\nIt works whichever tab is showing and with this window "
                              "closed; they answer what you asked and say nothing else while it is closed.\n"
                              "The Assistant has a key of its own on its tab. Click to change this one.")
        self._talk.clicked.connect(self._set_talk_key)
        ctl.addWidget(self._talk)
        # J 2026-09-26: ears are always on; the player picks how the mic listens.
        self._talk_mode = QComboBox()
        self._talk_mode.addItem("Push-to-talk", "push")
        self._talk_mode.addItem("Always on", "always")
        self._talk_mode.setCurrentIndex(1 if self.s.get("talk_mode") == "always" else 0)
        self._talk_mode.setToolTip("Push-to-talk: hold the talk key.  Always on: the mic stays open, just talk.")
        self._talk_mode.currentIndexChanged.connect(self._set_talk_mode)
        ctl.addWidget(self._talk_mode)
        # says why holding a key does nothing: no talk key yet, or voice libraries missing
        self._talk_hint = QLabel("")
        self._talk_hint.setStyleSheet(f"color: {P.yellow}; font-size: 9pt;")
        self._talk_hint.setWordWrap(True)
        ctl.addWidget(self._talk_hint)
        test = QPushButton("Test voices")
        test.setStyleSheet(_btn_ss())
        test.clicked.connect(self._test_voices)
        ctl.addWidget(test)
        ctl.addStretch(1)
        lay.addLayout(ctl)

        pace = QHBoxLayout()
        cl = QLabel("Chattiness")
        cl.setStyleSheet(f"color: {P.fg_dim}; font-size: 9pt;")
        pace.addWidget(cl)
        self._chat = _Slider(Qt.Horizontal)
        self._chat.setRange(0, 4)
        self._chat.setPageStep(1)
        self._chat.setTickPosition(QSlider.TicksBelow)
        self._chat.setTickInterval(1)
        self._chat.setValue(int(self.s.get("chattiness", 2)))
        self._chat.setToolTip("0 silent (only urgent + answers to your questions) ... 4 very chatty")
        self._chat_lbl = QLabel(LEVEL_NAMES[self._chat.value()])
        self._chat.valueChanged.connect(self._set_chattiness)
        pace.addWidget(self._chat, 1)
        pace.addWidget(self._chat_lbl)
        self._fb_btn, self._fb_mon = {}, {}
        for name, key, label in (("good_one", "good_key", "Good one"), ("shut_up", "shutup_key", "Shut up")):
            b = QPushButton(f"{label}: " + (InputBinding.from_dict(self.s[key]).describe() if self.s.get(key)
                                            else "set..."))
            b.setStyleSheet(_btn_ss())
            b.clicked.connect(lambda _=False, n=name, k=key, l=label: self._set_fb_key(n, k, l))
            pace.addWidget(b)
            self._fb_btn[name] = b
            mon = HotkeyMonitor(self)
            mon.triggered.connect(lambda down, n=name: down and self.core and self.core.feedback.press(n))
            self._fb_mon[name] = mon
            if self.s.get(key) and self._companions_on():
                mon.start(InputBinding.from_dict(self.s[key]))
        resume = QPushButton("Resume")
        resume.setStyleSheet(_btn_ss())
        resume.setToolTip("Cancel a 'Shut up' early")
        resume.clicked.connect(lambda: self.core and self.core.not_now.cancel())
        pace.addWidget(resume)
        lay.addLayout(pace)

        # How often the eyes take a picture, by what the pilot is doing (J 2026-10-05, core/picture_pace.py): one
        # slider and one "never" box per activity. The slider moves along a table of stops (5 s ... 120 min), fine
        # at the low end and coarse at the top, because one even scale would give the first minute no room at all.
        eyes = QGridLayout()
        head = QLabel("Eyes: take a picture every")
        head.setStyleSheet(f"color: {P.fg_dim}; font-size: 9pt;")
        head.setToolTip("A picture is one frame of the game handed to the local vision model. Only while Star "
                        "Citizen is the window in front, never in a fight, and never while the PC has no room to "
                        "spare.\nThey speak only about something that is in the picture, and may say nothing.")
        eyes.addWidget(head, 0, 0, 1, 4)
        self._pic_sl, self._pic_lbl, self._pic_never = {}, {}, {}
        for row, act in enumerate(pp.ACTIVITIES, 1):
            name = QLabel(pp.LABELS[act])
            name.setStyleSheet(f"color: {P.fg_dim}; font-size: 9pt;")
            sl = _Slider(Qt.Horizontal)
            sl.setRange(0, len(pp.STOPS) - 1)
            sl.setPageStep(1)
            sl.setTickPosition(QSlider.TicksBelow)
            sl.setTickInterval(1)
            sl.setValue(pp.stop_index(self.s.get(pp.every_key(act), pp.DEFAULT_EVERY_S[act])))
            sl.setToolTip("5 seconds to 120 minutes")
            val = QLabel("")
            val.setMinimumWidth(78)
            never = QCheckBox("never")
            never.setChecked(bool(self.s.get(pp.never_key(act))))
            never.setToolTip(f"No pictures of their own accord while this is what you are doing ({pp.LABELS[act]}).")
            sl.valueChanged.connect(lambda i, a=act: self._set_picture_every(a, i))
            never.toggled.connect(lambda on, a=act: self._set_picture_never(a, on))
            eyes.addWidget(name, row, 0)
            eyes.addWidget(sl, row, 1)
            eyes.addWidget(val, row, 2)
            eyes.addWidget(never, row, 3)
            self._pic_sl[act], self._pic_lbl[act], self._pic_never[act] = sl, val, never
            self._show_picture(act)
        # Talking about what they saw is its own dial (J: "cooldown periods for chatting about what it sees with a
        # chattiness slider for that as well"): looking and speaking are two things.
        row = len(pp.ACTIVITIES) + 1
        tl = QLabel("Talk about what they see")
        tl.setStyleSheet(f"color: {P.fg_dim}; font-size: 9pt;")
        self._eye_chat = _Slider(Qt.Horizontal)
        self._eye_chat.setRange(0, 4)
        self._eye_chat.setPageStep(1)
        self._eye_chat.setTickPosition(QSlider.TicksBelow)
        self._eye_chat.setTickInterval(1)
        self._eye_chat.setValue(int(self.s.get("eyes_chattiness", 2)))
        self._eye_chat.setToolTip("How long they wait between two remarks about something the eyes saw.\n"
                                  "0 never ... 2 one in four minutes at most ... 4 no wait of its own.\n"
                                  "Separate from how often the eyes look, and from Chattiness above.")
        self._eye_chat_lbl = QLabel(LEVEL_NAMES[self._eye_chat.value()])
        self._eye_chat_lbl.setMinimumWidth(78)
        self._eye_chat.valueChanged.connect(self._set_eye_chattiness)
        eyes.addWidget(tl, row, 0)
        eyes.addWidget(self._eye_chat, row, 1)
        eyes.addWidget(self._eye_chat_lbl, row, 2)
        eyes.setColumnStretch(1, 1)
        lay.addLayout(eyes)

        # Per-character volume, 0-200%. Applied from the next line; above 100% the limiter keeps it from clipping.
        vol = QHBoxLayout()
        self._vol_lbl = {}
        for who, name in (("elah", "Elah"), ("montaigne", "Montaigne")):
            lbl = QLabel(f"{name} volume")
            lbl.setStyleSheet(f"color: {P.fg_dim}; font-size: 9pt;")
            vol.addWidget(lbl)
            sl = _Slider(Qt.Horizontal)
            sl.setRange(0, 200)
            sl.setPageStep(10)
            sl.setValue(int(round(float(self.s.get(f"volume_{who}", 1.5)) * 100)))
            sl.setToolTip("100% = as rendered; above 100% is a limiter-protected boost")
            pct = QLabel(f"{sl.value()}%")
            pct.setMinimumWidth(40)
            sl.valueChanged.connect(lambda v, w=who: self._set_volume(w, v))
            vol.addWidget(sl, 1)
            vol.addWidget(pct)
            self._vol_lbl[who] = pct
        lay.addLayout(vol)

        # Portable memory: everything Elah and Montaigne remember about this pilot, as one tamper-checked zip.
        mem = QHBoxLayout()
        exp = QPushButton("Export memory")
        exp.setStyleSheet(_btn_ss())
        exp.setToolTip("Save what Elah and Montaigne remember about you to a zip (move PCs, back up)")
        exp.clicked.connect(self._export_memory)
        imp = QPushButton("Import memory")
        imp.setStyleSheet(_btn_ss())
        imp.setToolTip("Restore a memory zip exported from SuitMk2 (checksums verified before anything is written)")
        imp.clicked.connect(self._import_memory)
        mem.addWidget(exp)
        mem.addWidget(imp)
        # Training screenshots (J 2026-09-24): the frames the eyes looked at, kept only if the pilot opts in.
        self._keep_shots = QCheckBox("Keep training screenshots")
        self._keep_shots.setChecked(bool(self.s.get("keep_training_shots", False)))
        self._keep_shots.setToolTip("Keep the small screenshots the eyes looked at, on this PC only, to help the "
                                    "companion notice more and make fewer mistakes. Never used to train a bot to play.")
        self._keep_shots.toggled.connect(self._set_keep_shots)
        # Dev-history fun facts (J 2026-09-25): OFF by default. Also toggled by voice ("fun facts on" / "fun facts off").
        self._dev_facts = QCheckBox("Dev history fun facts")
        self._dev_facts.setChecked(bool(self.s.get("dev_facts", False)))
        self._dev_facts.setToolTip("Now and then, in a quiet moment, Montaigne shares a real fact about how Star "
                                   "Citizen was made, from the dev history, as an aside. Breaks the fourth wall, so "
                                   "it is off by default. Say \"fun facts on\" or \"fun facts off\" any time.")
        self._dev_facts.toggled.connect(self._set_dev_facts)
        shots = QPushButton("Export training screenshots")
        shots.setStyleSheet(_btn_ss())
        shots.setToolTip("Zip the kept screenshots + what the eyes thought they showed, to share if you choose")
        shots.clicked.connect(self._export_shots)
        mem.addWidget(self._keep_shots)
        mem.addWidget(self._dev_facts)
        mem.addWidget(shots)
        mem.addStretch(1)
        lay.addLayout(mem)

        # Conversation memory (J 2026-10-05, tree_memory.py). What is kept is said HERE, in plain words, beside the
        # button that removes it: the default keeps conversations, and that is only acceptable where he can see it.
        conv = QHBoxLayout()
        self._remember = QCheckBox("Remember conversations")
        self._remember.setChecked(bool(self.s.get("remember_conversations", st.RECORD_CONVERSATIONS_DEFAULT)))
        self._remember.setToolTip("Keep what you say to Elah and Montaigne, and what they say back, so they can "
                                  "bring it up later. Text only, never audio. Included in Export memory.")
        self._remember.toggled.connect(self._set_remember)
        self._kept_lbl = QLabel("")
        self._kept_lbl.setStyleSheet(f"color: {P.fg_dim}; font-size: 9pt;")
        self._kept_lbl.setWordWrap(True)
        forget = QPushButton("Forget conversations")
        forget.setStyleSheet(_btn_ss())
        forget.setToolTip("Delete everything kept of your conversations with Elah and Montaigne from this PC")
        forget.clicked.connect(self._forget_conversations)
        conv.addWidget(self._remember)
        conv.addWidget(self._kept_lbl, 1)
        conv.addWidget(forget)
        lay.addLayout(conv)
        self._show_kept()

        # Speaker models in VRAM (J 2026-09-26). One local model per speaker, 1.83 GB of video memory each.
        # Default: only the speaker being asked stays loaded, so the companion's floor is one model, not two -
        # "not everyone will have a card that's beefy enough to do both". The price is a cold load when the speaker
        # changes (measured 2.19s on an idle card; worse with the game holding it, and that number is not measured).
        res = QHBoxLayout()
        rl = QLabel("Speaker models in VRAM")
        rl.setStyleSheet(f"color: {P.fg_dim}; font-size: 9pt;")
        res.addWidget(rl)
        self._residency = QComboBox()
        self._residency.addItem("One at a time (frees ~1.8 GB)", "evict")
        self._residency.addItem("Keep both warm (needs ~3.7 GB)", "both")
        cur = str(self.s.get("speaker_residency", "evict")).strip().lower()
        self._residency.setCurrentIndex(1 if cur == "both" else 0)
        self._residency.setToolTip("Elah and Montaigne have one local model each, about 1.8 GB of video memory per "
                                   "speaker.\nOne at a time: the idle one is unloaded, so the game keeps that "
                                   "memory. The first line from the other speaker then takes about two seconds "
                                   "longer.\nKeep both warm: no wait when they swap, and about 3.7 GB stays in use. "
                                   "Pick this only if your card has the room.")
        self._residency.currentIndexChanged.connect(self._set_residency)
        res.addWidget(self._residency)
        res.addStretch(1)
        lay.addLayout(res)

        # Free talk (J 2026-10-05): a checkbox, and a drop-down of the models Ollama has on this PC ("a drop down
        # that ... auto-detects local models to make that easy"). Picking a model checks first that it fits in the
        # memory that is free (core/chat_models.py); one that does not is refused and is not saved. Only gemma3:4b has
        # been measured with the prompt in use; the others are listed and marked untested.
        chat = QGridLayout()
        self._chat_on = QCheckBox("Free talk (chat model)")
        self._chat_on.setChecked(st.chat_on(self.s))
        self._chat_on.setToolTip("An ordinary remark, a greeting or a question about Elah or Montaigne themselves is "
                                 "worded by the local model chosen here, then cut and checked before it is spoken.\n"
                                 "Questions the Suit can answer from what it knows are answered as before.")
        self._chat_on.toggled.connect(self._set_chat)
        self._chat_model = QComboBox()
        self._chat_model.setToolTip("The models Ollama has on this PC. A model is only accepted if it fits in the "
                                    "video memory and system memory that are free right now.")
        self._chat_model.activated.connect(self._pick_chat_model)     # a pick by hand, not a refill of the list
        relist = QPushButton("Refresh")
        relist.setStyleSheet(_btn_ss())
        relist.setToolTip("Ask Ollama again which models are installed")
        relist.clicked.connect(self._list_chat_models)
        self._chat_status = QLabel("")
        self._chat_status.setStyleSheet(f"color: {P.yellow}; font-size: 9pt;")
        self._chat_status.setWordWrap(True)
        chat.addWidget(self._chat_on, 0, 0)
        chat.addWidget(self._chat_model, 0, 1)
        chat.addWidget(relist, 0, 2)
        chat.addWidget(self._chat_status, 1, 0, 1, 3)
        chat.setColumnStretch(1, 1)
        lay.addLayout(chat)
        self._chat_models = None                # what Ollama listed; None = not asked yet, or it did not answer
        self._fill_chat_models(None, asked=False)
        self._list_chat_models()

        # Smarter lines (J 2026-09-24): the pilot's own Claude API key words each line; the local models stay the
        # fallback, and grounding still checks every fact either way. Test uses models.retrieve: it costs nothing.
        api = QGridLayout()
        api.addWidget(QLabel("Smarter lines (Claude API)"), 0, 0, 1, 4)
        self._api_on = QCheckBox("Use Claude for lines")
        self._api_on.setChecked(self.s.get("backend") == "api")
        self._api_on.setToolTip("Better wording and more variety, billed to YOUR Anthropic account. If the API "
                                "fails, the local models take over. Facts are checked the same way either way.")
        self._api_model = QComboBox()
        for label, mid in API_MODELS:
            self._api_model.addItem(label, mid)
        i = self._api_model.findData(self.s.get("api_model") or API_MODELS[0][1])
        self._api_model.setCurrentIndex(max(0, i))
        self._api_key = QLineEdit(self.s.get("anthropic_api_key") or "")
        self._api_key.setEchoMode(QLineEdit.Password)
        self._api_key.setPlaceholderText("Anthropic API key (sk-ant-...)")
        self._api_key.setToolTip("Stored only in your SuitMk2 settings on this PC. Leave blank to use the "
                                 "ANTHROPIC_API_KEY environment variable.")
        test = QPushButton("Test")
        test.setStyleSheet(_btn_ss())
        test.setToolTip("Checks the key and model with the API. Costs nothing: no line is generated.")
        test.clicked.connect(self._api_test)
        save = QPushButton("Save")
        save.setStyleSheet(_btn_ss())
        save.clicked.connect(self._api_save)
        self._api_status = QLabel("")
        self._api_status.setWordWrap(True)
        api.addWidget(self._api_on, 1, 0)
        api.addWidget(self._api_model, 1, 1, 1, 3)
        api.addWidget(self._api_key, 2, 0, 1, 2)
        api.addWidget(test, 2, 2)
        api.addWidget(save, 2, 3)
        api.addWidget(self._api_status, 3, 0, 1, 4)
        lay.addLayout(api)

        self._recent = QListWidget()
        self._recent.setStyleSheet(f"QListWidget {{ background: {P.bg_primary}; color: {P.fg}; border: 1px solid "
                                   f"{P.border}; font-family: Consolas; font-size: 9pt; }}")
        self._recent.setWordWrap(True)
        lay.addWidget(self._recent, 1)
        self.content_layout.addWidget(body, 1)

        # Direct conversation: push-to-talk -> local Whisper -> ConversationLane -> core.answer()
        self.lane = ConversationLane()
        self.store = None
        self._voice_missing = []
        self._make_ears()
        if self.s.get("talk_key") or self.ears.mode() == "always":
            QTimer.singleShot(1500, self._arm_ears)
        self._show_talk_key()
        # arm() is what reports missing voice libraries, and it only runs once a
        # talk key is set, so check up front as well
        try:
            from voice_in import missing_deps
            _miss = missing_deps()
        except Exception:
            _miss = []
        if _miss:
            self._on_needs_install(_miss)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(1000)
        # The notice waits for the window to be SEEN. The launcher preloads SuitMk2 hidden at startup,
        # and a 600 ms timer from construction popped this dialog on the first launch of the toolbox,
        # on top of the Star Citizen folder prompt, for a tool the user had not opened (2.4.0 laptop test).
        self._notice_pending = True
        if self.isVisible():
            QTimer.singleShot(600, self._maybe_show_notice)
        self._show_disabled()
        if self._companions_on():
            threading.Thread(target=self._boot, name="suitmk2_boot", daemon=True).start()

    # -- disable companions ---------------------------------------------------------------------------------------
    def _companions_on(self) -> bool:
        return self.s.get("companions_enabled", True) is not False

    def _make_speech(self):
        """The two voices, silent until the window is seen. With the companions disabled: nothing is loaded."""
        if not self._companions_on():
            return _NoSpeech()
        ducker = None
        if self.s.get("ducking", True):          # wait/duck under Star Citizen's own dialogue (voice_fx)
            try:
                import voice_fx
                ducker = voice_fx.DuckingMonitor(duck_scale=float(self.s.get("duck_scale", 0.8))).start()
            except Exception:
                log.exception("ducking unavailable; voices never wait for the game")
        speech = Speech(Path(self.s["voices_dir"]), volume=float(self.s["volume"]), ducker=ducker)
        for who in ("elah", "montaigne"):
            speech.set_level(who, float(self.s.get(f"volume_{who}", 1.5)))
        # J 2026-10-04: "make sure that suitmk2 only have the AI's talk while it is launched". The launcher
        # preloads this tool HIDDEN when the launcher itself starts, and closing the window only hides it, so
        # the companions used to talk for a tool the user had never opened, or had closed. They are now silent
        # whenever the window is not open; see _apply_voice_gate. Start silent and let the gate open it.
        speech.mute(True)
        threading.Thread(target=speech.preload, name="suitmk2_voice_preload", daemon=True).start()
        return speech

    def _show_disabled(self) -> None:
        self._off_lbl.setText("" if self._companions_on() else COMPANIONS_OFF_NOTICE)

    def _set_disabled(self, disabled: bool) -> None:
        """The checkbox. Saved, and it takes effect now in both directions: no restart is needed."""
        if bool(disabled) == (not self._companions_on()):
            return
        self.s["companions_enabled"] = not disabled
        st.save(self.s)
        self._boot_gen += 1                      # a boot still in flight sees this and stops what it started
        if disabled:
            self._stop_companions()
        else:
            self.speech = self._make_speech()
            self._apply_voice_gate()
            for name, key in (("good_one", "good_key"), ("shut_up", "shutup_key")):
                if self.s.get(key):
                    self._fb_mon[name].start(InputBinding.from_dict(self.s[key]))
            self._arm_ears()
            threading.Thread(target=self._boot, name="suitmk2_boot", daemon=True).start()
            self._list_chat_models()
        self._show_disabled()

    def _stop_companions(self) -> None:
        """Stop everything of theirs that runs, and leave the window up. The same steps as _quit, without the quit."""
        try:
            if self.ears.armed() or self.ears.recording():
                self.ears.disarm()
            for m in self._fb_mon.values():
                m.stop()
        except Exception:
            log.exception("disable companions: the talk key or a feedback key did not stop")
        core, speech, sidecar = self.core, self.speech, self.sidecar
        self.core, self.sidecar, self.speech = None, None, _NoSpeech()
        self._sidecar_ready = False

        def work():                              # off the GUI thread: stopping the service can take seconds
            for what, step in (("core", core and core.stop), ("voices", speech.close),
                               ("model service", sidecar and sidecar.stop)):
                try:
                    if step:
                        step()
                except Exception:
                    log.exception("disable companions: the %s did not stop cleanly", what)
        threading.Thread(target=work, name="suitmk2_disable", daemon=True).start()

    # -- boot off the UI thread: finding the log and waking the model service can take seconds ------------------
    def _boot(self) -> None:
        if not self._companions_on():
            return                              # disabled: no service, no core, no eyes, no log, no voices
        gen = self._boot_gen
        core_dir = CORE
        self.sidecar = Sidecar(self.s["model_python"], core_dir / "companion_service.py", Path(self.s["adapters_dir"]),
                               presence=self.s["presence"], glance=bool(self.s["vision_glance"]),
                               log_path=st.DIR / "companion_service.log")
        st.DIR.mkdir(parents=True, exist_ok=True)
        try:
            up = self.sidecar.ensure()          # also wakes a sleeping local runtime (auto/ollama backends)
        finally:
            self._sidecar_ready = True          # the Setup panel may now check (from _refresh, on the GUI thread)
        if gen != self._boot_gen:               # disabled while the service was starting: undo, build nothing
            sc, self.sidecar, self._sidecar_ready = self.sidecar, None, False
            if sc is not None:
                sc.stop()
            return
        from companion_service import RemoteRealizer, RemoteEyes
        realizer = RemoteRealizer() if up else None
        eyes = RemoteEyes() if up and self.s["presence"] != "off" else None
        recorder = dreams = None
        try:
            import memory_store as ms
            from dream_queue import SessionRecorder, DreamQueue
            store = ms.open_store(st.DIR / "memory", self.s["pilot_id"])
            ddir = st.DIR / "memory" / self.s["pilot_id"] / "dreams"
            recorder, dreams = SessionRecorder(ddir), DreamQueue(store, ddir)
            self.store = store
        except Exception:
            log.exception("memory/dreams unavailable; continuing without them")

        def headroom():
            h = health()
            return (h or {}).get("headroom") or "TIGHT"

        def hardware_reading():                 # for the overload guard: None when there is no reading at all
            return (health() or {}).get("headroom")
        lifecycle = None
        try:
            from move_lifecycle import MoveLifecycle
            if self.store is not None:
                lifecycle = MoveLifecycle(self.store)
                lifecycle.tick()                 # deterministic, no model
        except Exception:
            log.exception("move lifecycle unavailable")
        sound = None
        if self.s.get("sound_classifier", True):   # game ears: hear ONLY StarCitizen.exe, classify it on the CPU
            try:
                from sound_classifier import SoundClassifier
                sound = SoundClassifier()
            except Exception:
                log.exception("sound classifier unavailable; combat confirm falls back to the eyes / meter")
        core = CompanionCore(self.speech, realizer=realizer, eyes=eyes, recorder=recorder, dreams=dreams,
                             headroom=headroom, ambient_every_s=float(self.s["ambient_every_s"]),
                             chattiness=int(self.s.get("chattiness", 2)), feedback_dir=st.DIR / "feedback",
                             lifecycle=lifecycle, store=self.store, sound=sound,
                             idle_source=os_idle_seconds,
                             afk_after_s=float(self.s.get("afk_minutes", DEFAULT_AFK_MINUTES)) * 60.0,
                             dev_facts=DevFacts.from_settings(self.s),
                             features=self.s,      # the optional April-spec features (CompanionCore.FEATURE_KEYS)
                             pace=pp.PicturePace(self.s),      # a picture every N, per activity (the sliders)
                             eye_chattiness=int(self.s.get("eyes_chattiness", 2)),
                             hardware_reading=hardware_reading)
        if gen != self._boot_gen:               # disabled while this was being built: it never starts
            return
        self.core = core
        self.core.dev_facts_persist = self._persist_dev_facts
        self._attach_tree()
        self._attach_talker()
        # Game ears for combat: the ducking meter already reads StarCitizen.exe's own output ~20x a second.
        if self.speech.ducker is not None:
            self.speech.ducker.listeners.append(self.core.combat.feed)
        # Mood as stance text: always for the Claude API; for the local model only since the 09-24 retrain taught it
        # to show a stance instead of narrating it ("the scare remains hidden..." was the untrained failure).
        from emotion import LOCAL_STANCE_TRAINED
        self.core.affect.stance_text = self.s.get("backend") == "api" or LOCAL_STANCE_TRAINED
        self.core.start(find_game_log(self.s.get("game_log") or None))
        if gen != self._boot_gen:               # disabled in the moment it started: stop it again
            c, sc, self.core, self.sidecar = self.core, self.sidecar, None, None
            c.stop()
            if sc is not None:
                sc.stop()

    # -- free talk: the chat model ----------------------------------------------------------------------------------
    def _attach_talker(self) -> None:
        """Give the running core its talker, or take it away, according to the two settings. No restart."""
        if self.core is None:
            return
        self.core.talker = None
        # Free talk (core/chat_talker.py): only with "chat" on AND a "chat_model" named in the settings. Otherwise
        # nothing is imported or attached, and every sentence is answered as it always was.
        if st.chat_on(self.s):
            try:
                import chat_talker
                self.core.talker = chat_talker.from_settings(self.s, note=self.core._note)
            except Exception:
                log.exception("chat talker unavailable; talk is answered as with chat off")

    def _list_chat_models(self) -> None:
        """Ask Ollama which models are installed: one request for its list, off the GUI thread, 1.5 s at most.
        Ollama not running is not an error; the list is then empty and the label says so."""
        if not self._companions_on():
            self._chat_status.setText("")
            return
        self._chat_listing = None

        def work():
            try:
                found = chat_models.installed()
            except Exception:
                log.exception("listing the local models failed")
                found = None
            self._chat_listing = (found,)
        threading.Thread(target=work, name="suitmk2_chat_models", daemon=True).start()
        self._poll_chat_models()

    def _poll_chat_models(self) -> None:
        got = getattr(self, "_chat_listing", None)       # set by the worker; the widgets are touched only here
        if got is None:
            QTimer.singleShot(150, self._poll_chat_models)
            return
        self._chat_listing = None
        self._fill_chat_models(got[0])

    def _fill_chat_models(self, models: Optional[list], asked: bool = True) -> None:
        """Put the list in the drop-down and select the saved model. Never changes the settings."""
        self._chat_models = models
        saved = str(self.s.get("chat_model") or "").strip()
        cb = self._chat_model
        cb.blockSignals(True)
        cb.clear()
        cb.addItem("(no chat model)", "")
        for m in models or []:
            cb.addItem(chat_models.entry_label(m["name"]), m["name"])
        if saved and cb.findData(saved) < 0:
            cb.addItem(f"{saved} (saved; not found in Ollama)", saved)
        cb.setCurrentIndex(max(0, cb.findData(saved)))
        cb.blockSignals(False)
        if asked:
            self._chat_status.setText(chat_models.NONE_FOUND if not models else
                                      (chat_models.why_chat_cannot_be_on(self.s, models) if self.s.get("chat") else ""))

    def _pick_chat_model(self, index: int) -> None:
        """A model was picked. Read what memory is free (about a quarter of a second, off the GUI thread), then
        decide in _apply_chat_pick."""
        name = str(self._chat_model.itemData(index) or "")
        self._chat_status.setText("checking that it fits..." if name else "")
        self._chat_pick = None

        def work():
            free = None
            if name:
                try:
                    free = hardware_guard.read_free_memory()
                except Exception:
                    log.exception("free memory could not be read")
            self._chat_pick = (name, free)
        threading.Thread(target=work, name="suitmk2_chat_fit", daemon=True).start()
        self._poll_chat_pick()

    def _poll_chat_pick(self) -> None:
        got = getattr(self, "_chat_pick", None)
        if got is None:
            QTimer.singleShot(100, self._poll_chat_pick)
            return
        self._chat_pick = None
        self._apply_chat_pick(*got)

    def _apply_chat_pick(self, name: str, free) -> None:
        """Accept the pick if the model fits, and save it; refuse it otherwise, say why with the numbers, and put
        the drop-down back on what was saved. A refused model is never written to the settings."""
        ok, why = chat_models.choose(self.s, name, self._chat_models, free)
        if ok:
            st.save(self.s)
            if not st.chat_on(self.s) and self._chat_on.isChecked():
                self._chat_on.blockSignals(True)
                self._chat_on.setChecked(False)          # no model chosen: there is nothing to talk with
                self._chat_on.blockSignals(False)
            self._attach_talker()
        else:
            self._chat_model.blockSignals(True)
            self._chat_model.setCurrentIndex(max(0, self._chat_model.findData(str(self.s.get("chat_model") or ""))))
            self._chat_model.blockSignals(False)
        self._chat_status.setText(why)

    def _set_chat(self, on: bool) -> None:
        """The checkbox. It cannot be ticked with no model chosen or with Ollama not reachable, and says which."""
        if on:
            why = chat_models.why_chat_cannot_be_on(self.s, self._chat_models)
            if why:
                self._chat_on.blockSignals(True)
                self._chat_on.setChecked(False)
                self._chat_on.blockSignals(False)
                self._chat_status.setText(why)
                return
        self.s["chat"] = bool(on)
        st.save(self.s)
        self._attach_talker()
        self._chat_status.setText("")

    def _maybe_check_setup(self) -> None:
        """GUI thread, once: after the sidecar had its chance to wake the runtime, ask the panel whether a complete
        character set exists. Skipped for backend hf/none (a dev torch setup, or the player chose silence)."""
        if self.setup is None or self._setup_checked or not self._sidecar_ready:
            return
        self._setup_checked = True
        if self.sidecar is not None and self.sidecar.backend not in ("auto", "ollama"):
            return
        try:
            self.setup.check()
        except Exception:
            log.exception("setup check failed; continuing without it")

    def _on_models_ready(self) -> None:
        """The panel says a complete set exists. If it got there by running setup THIS session, make the running
        service re-resolve its backend (none -> ollama) without a restart. Off the GUI thread: /reload waits for an
        in-flight line. At startup with everything already in place there is nothing to reload."""
        if self.setup is None or self.setup.job is None:
            return

        def work():
            try:
                if self.sidecar is not None:
                    self.sidecar.reload()
            except Exception:
                log.exception("reload after setup failed; the next service restart picks the models up")
        threading.Thread(target=work, name="suitmk2_reload", daemon=True).start()

    # -- UI ---------------------------------------------------------------------------------------------------------
    def _refresh(self) -> None:
        if not self._companions_on():
            for v in self._rows.values():
                v.setText("off")
            return
        self._maybe_check_setup()
        c, h = self.core, None
        self._rows["Game.log"].setText(("reading" if c and c._monitor else "not found") if c else "starting...")
        if self.sidecar:
            h = health(timeout=0.3)
            extra = f" | headroom {h.get('headroom')} | model {'loaded' if h.get('loaded') else 'idle'}" if h else ""
            self._rows["Model service"].setText(self.sidecar.status + extra)
        for spk, key in (("elah", "Elah voice"), ("montaigne", "Montaigne voice")):
            trained = (Path(self.s["voices_dir"]) / f"{spk}.onnx").exists()
            self._rows[key].setText(f"{'trained' if trained else 'stock'} | loaded: {self.speech.voice_source(spk)}")
        if c:
            s = c.stats
            self._rows["Heard / spoken"].setText(f"{s['events']} events, {s['spoken']} spoken, "
                                                 f"{s['gated'] + s['silent'] + s['ungrounded']} held back")
            if self._recent.count() != len(c.last):
                self._recent.clear()
                self._recent.addItems(list(reversed(c.last)))
            left = c.not_now.remaining_s()
            self._rows["Pacing"].setText(f"{LEVEL_NAMES[c.pacer.params.level]}"
                                         + (f" | not now {int(left) // 60}:{int(left) % 60:02d} left" if left else "")
                                         + f" | +{c.feedback.counts['good_one']} / -{c.feedback.counts['shut_up']}")
            # The new senses, visible so a test session can be tuned from facts (J 2026-09-23). No I/O here: every value
            # below is already in memory (the eyes' last answer is kept by the core's own ambient tick).
            cs = c.combat.stats
            self._rows["Combat"].setText(f"{c.combat.state} | onsets heard {cs['spikes']}, on {cs['on']}, "
                                         f"unconfirmed {cs['unconfirmed']}"
                                         + (f" | last {getattr(c, 'combat_reason', '')}" if getattr(c, 'combat_reason', '') else ""))
            if c.sound is not None:
                try:
                    r = c.sound.recent(5.0) or {}
                    top = sorted(r.items(), key=lambda kv: -kv[1])[:3]
                    self._rows["Game ears"].setText(", ".join(f"{k} {v:.2f}" for k, v in top) or "listening")
                except Exception:
                    self._rows["Game ears"].setText("unavailable")
            else:
                self._rows["Game ears"].setText("off (meter only)")
            e = getattr(c, "last_eyes", None) or {}
            every = c.pace.every(c.doing_now)
            doing = f" | {pp.LABELS[c.doing_now].lower()}: " + ("no pictures" if every is None
                                                               else f"a picture every {pp.label(every)}")
            self._rows["Eyes"].setText((e.get("scene") or "not looking") + (" | sees combat" if e.get("in_combat") else "")
                                       + doing if c.eyes is not None else "off")
            # The hard limit (core/hardware_guard.py). Said here, never out loud, and there is nothing to untick.
            hold = c._picture_hold()
            self._rows["Hardware"].setText(c.hardware_notice or ("a fight is on: no pictures, chat model not asked"
                                                                 if hold == "combat" else "room to spare"))

    def _set_talk_key(self) -> None:
        dlg = BindingCaptureDialog(self)
        if dlg.exec() and dlg.result is not None:
            self.s["talk_key"] = dlg.result.to_dict()
            st.save(self.s)
            self.ears.disarm()
            self.ears.set_binding(dlg.result)
            self._show_talk_key()
            self._arm_ears()
            self.pttChanged.emit()

    def _set_talk_mode(self, _index: int = 0) -> None:
        mode = self._talk_mode.currentData() or "push"
        self.s["talk_mode"] = mode
        st.save(self.s)
        self.ears.disarm()
        self.ears.set_mode(mode)
        self._show_talk_key()
        self._arm_ears()
        self.pttChanged.emit()

    def _make_ears(self) -> None:
        """The ears, set up from the saved settings and wired to this dashboard. What they hear comes back to THIS
        tool (_on_transcript) and to no other: that wiring, plus the one-microphone floor, is the whole of "the key
        decides which AI hears it"."""
        self.ears = _Ears(self)
        self.ears.use_floor(FLOOR, ptt_keys.SUIT)
        self.ears.set_mode("always" if self.s.get("talk_mode") == "always" else "push")
        self.ears.set_model("small.en")
        self.ears.transcript.connect(self._on_transcript)
        self.ears.listeningChanged.connect(self._on_listening)
        self.ears.statusChanged.connect(lambda m: self.core and self.core._note(f"ears: {m}"))
        self.ears.statusChanged.connect(self._on_ears_status)
        self.ears.needsInstall.connect(self._on_needs_install)
        self.ears.captureFailed.connect(self._on_capture_failed)
        self.ears.captureBusy.connect(
            lambda _who: self.core and self.core._note("ears: the other tab's key is being held; let go of it first"))
        if self.s.get("talk_key"):
            self.ears.set_binding(InputBinding.from_dict(self.s["talk_key"]))

    # -- the microphone ---------------------------------------------------------------------------------------------
    # In its own window this dashboard always owns its microphone. As a tab of the Toolbox Assistant window it shares
    # the window with the Assistant, which has ears of its own, and two tabs must never listen at once: the same
    # sentence would be answered twice.
    #
    # An ALWAYS-OPEN mic belongs to the tab that is showing: the window hands it over with mic_take and takes it away
    # with mic_release.
    #
    # The TALK KEY is different (J, 2026-10-05): each tool has its own, and holding one talks to that tool whichever
    # tab is showing. So after mic_release the window says ptt_background(True), and this tab goes on watching its
    # key. Two keys still cannot open the microphone twice: the ears ask shared/mic_floor.py before every capture,
    # and the second key held is refused. The window does not say ptt_background(True) when both tools are set to
    # the SAME key; then only the tab in front has it.
    #
    # Every place that arms the ears goes through _arm_ears, so a tab that may not listen cannot open the microphone
    # by a side door (a new talk key, a mode change, the start-up timer).
    def _may_listen(self) -> bool:
        """In front: yes, as the saved mode says. Not in front: only the talk key, once the window has allowed it."""
        return getattr(self, "_mic_mine", True) or (getattr(self, "_ptt_bg", False) and self.ears.mode() == "push")

    def _arm_ears(self) -> None:
        """Arm the ears if this dashboard may listen and there is a way to talk (a key, or always on)."""
        if not self._companions_on() or not self._may_listen():
            return                              # disabled companions do not watch a key or open the microphone
        if self.s.get("talk_key") or self.ears.mode() == "always":
            self.ears.arm()

    def mic_release(self) -> None:
        """Another tab is in front now: close the mic and stop watching the talk key (ptt_background says
        afterwards whether the key may be watched)."""
        self._mic_mine = False
        self._ptt_bg = False
        if self.ears.armed() or self.ears.recording():
            self.ears.disarm()

    def mic_take(self) -> None:
        """This tab is the one showing: listen again, the way the saved settings say."""
        self._mic_mine = True
        self._arm_ears()

    def ptt_background(self, on: bool) -> None:
        """Whether this tab may watch its talk key while another tab is in front. Says nothing to a tab in front."""
        self._ptt_bg = bool(on)
        if getattr(self, "_mic_mine", True):
            return
        if self._may_listen():
            if not self.ears.armed():
                self._arm_ears()
        elif self.ears.armed() or self.ears.recording():
            self.ears.disarm()

    def ptt_binding(self) -> Optional[dict]:
        """The talk key as a dict (shared/ptt_keys.py reads it), or None when holding a key does nothing here
        (the mic is always open, or no key is set)."""
        if self.ears.mode() != "push" or not self.s.get("talk_key"):
            return None
        return dict(self.s["talk_key"])

    def ptt_clash(self, other: str) -> None:
        """The window found the talk key is also *other*'s key ("" = it is not)."""
        self._ptt_clash = ("This is also %s's key, so it only works for the tab that is showing. "
                           "Give one of them a different key." % other) if other else ""
        self._show_talk_key()

    def pilot_talking(self, on: bool) -> None:
        """The pilot is holding ANOTHER tab's key and talking: hold every non-urgent line, as for our own key."""
        if self.core is not None:
            self.core.gate_state.pilot_speaking = bool(on) or self.ears.recording()

    def _on_capture_failed(self, why: str) -> None:
        """The talk key was held and the microphone did not open. Said, and nothing else happens: what was meant
        for the companions is never sent to the Assistant instead."""
        self.pttState.emit("error", why or "the microphone did not open")

    def _on_ears_status(self, msg: str) -> None:
        if self.ears.mode() != "push":
            return
        if msg.startswith("ears error"):            # speech-to-text failed after the key was released
            self.pttState.emit("error", msg)
        elif msg == "heard nothing":
            self.pttState.emit("note", msg)

    # -- the answer pass (see the module docstring) -------------------------------------------------------------
    def _open_answer_pass(self) -> None:
        """He asked with the talk key: for ANSWER_PASS_S the answer may be spoken even with the window hidden."""
        self._ptt_pass = True
        self._apply_voice_gate()
        t = getattr(self, "_pass_timer", None)
        if t is None:
            t = self._pass_timer = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(self._close_answer_pass)
        t.start(int(ANSWER_PASS_S * 1000))

    def _close_answer_pass(self) -> None:
        self._ptt_pass = False
        self._apply_voice_gate()

    def _show_talk_key(self) -> None:
        """Talk button text + the hint beside it."""
        if self.s.get("talk_key"):
            self._talk.setText("Talk key: " + InputBinding.from_dict(self.s["talk_key"]).describe())
        else:
            self._talk.setText("Talk key: set...")
        if self._voice_missing:
            # Installed users have an embedded Python and no way to "pip install", so the
            # hint says what is missing, not a command they cannot run (2.4.0 install test).
            self._talk_hint.setText("Voice input is not included in this build (missing: "
                                    + ", ".join(self._voice_missing) + "). Everything else still works.")
        elif not self.s.get("talk_key") and self.s.get("talk_mode") != "always":
            self._talk_hint.setText("No talk key set: click Talk key and press the key to hold while you talk.")
        else:
            self._talk_hint.setText(getattr(self, "_ptt_clash", ""))
        self._talk_hint.setVisible(bool(self._talk_hint.text()))

    def _on_needs_install(self, packages: list) -> None:
        """EarsController.needsInstall: voice libraries are missing, so say which."""
        self._voice_missing = list(packages or [])
        self._show_talk_key()
        if self.core is not None:
            self.core._note("ears: voice input needs pip install " + " ".join(self._voice_missing))

    def _on_listening(self, on: bool) -> None:
        if self.core is not None:
            self.core.gate_state.pilot_speaking = bool(on)   # hold every non-urgent line while the pilot talks
        if self.ears.mode() == "push":                       # a held key; an always-open mic is nobody holding anything
            self.pttState.emit("listening" if on else "released", "")

    def _on_transcript(self, text: str) -> None:
        if not text.strip():
            return
        by_key = self.ears.mode() == "push"
        if by_key:
            self.pttState.emit("heard", text)
        if self.core is None:
            if by_key:
                self.pttState.emit("note", "Suit Mk2 is still starting; ask again in a moment")
            return
        if by_key:
            # He asked them something. The answer may be spoken even with the window hidden; nothing unprompted may.
            self._open_answer_pass()
            if self.s.get("muted"):
                self.pttState.emit("note", "Suit Mk2 is muted: un-mute it on its tab to hear the answer")
        from dream_queue import history_facts
        # The trackers' values, minus a place the pilot has left (2026-10-05: the log never clears a name, so a
        # question asked on the way out of Lorville used to be answered "Lorville").
        state = without_departed(lane_state_from_core(self.core.state, self.core.volatile),
                                 getattr(self.core, "_departed", None))
        hist = {}
        if self.store is not None:
            try:
                hist = history_facts(self.store, location=state.get("location"), ship=state.get("ship"))
            except Exception:
                hist = {}
        self.core._note(f"heard: {text}")
        if callable(getattr(self.core, "heard", None)):
            self.core.heard(text)                # kept on this PC if he has "Remember conversations" on
        # "Fun facts on" / "fun facts off" (J 2026-09-25): a control, not a question. Saved; Montaigne acknowledges.
        if self.core.voice_command(text):
            return
        # "Good one" / "Shut up" said out loud work like the keys (J 2026-09-24), and are not questions to answer.
        from feedback import verbal_reaction
        reaction = verbal_reaction(text)
        if reaction:
            self.core.feedback.press(reaction)
            self.core._note(f"feedback (spoken): {reaction}")
            return
        if hasattr(self.lane, "memory"):          # the conversation log, for "what did I say about ..."
            self.lane.memory = getattr(self.core, "tree", None)
        know = getattr(self.core, "place_knowledge", None)
        if callable(know) and getattr(self.lane, "knowledge", True) is None:
            self.lane.knowledge = know()         # lore, brochures and dev history the core already has in memory
        spec = self.lane.handle(text, state, hist)
        if spec is not None:
            self.core.answer(spec, text)

    # -- conversation memory (tree_memory.py) -------------------------------------------------------------------
    def _tree_dir(self) -> Path:
        return st.DIR / "memory" / self.s["pilot_id"]

    def _attach_tree(self) -> None:
        """Give the core the conversation memory, or take it away, according to the setting. Building the summary
        nodes for finished sessions is plain code and takes milliseconds; it runs here, off the UI thread at boot."""
        core = getattr(self, "core", None)
        if core is None:
            return
        if not self.s.get("remember_conversations", st.RECORD_CONVERSATIONS_DEFAULT):
            core.tree = None
            return
        try:
            import tree_memory
            core.tree = tree_memory.open_tree(self._tree_dir(), session=core.session_id)
            for who in tree_memory.COMPANIONS:
                core.tree.build(who)
        except Exception:
            log.exception("conversation memory unavailable; continuing without it")
            core.tree = None

    def _show_kept(self) -> None:
        on = bool(self.s.get("remember_conversations", st.RECORD_CONVERSATIONS_DEFAULT))
        self._kept_lbl.setText(KEPT_NOTICE if on else NOT_KEPT_NOTICE)

    def _set_remember(self, on: bool) -> None:
        self.s["remember_conversations"] = bool(on)
        st.save(self.s)
        self._attach_tree()
        self._show_kept()

    def _forget_conversations(self) -> None:
        from PySide6.QtWidgets import QMessageBox
        if QMessageBox.question(
                self, "Forget conversations",
                "Delete everything kept of your conversations with Elah and Montaigne from this PC?\n"
                "This cannot be undone. (An exported memory file is not touched.)") != QMessageBox.Yes:
            return
        n = forget_conversations(getattr(self, "core", None), self._tree_dir())
        if self.core is not None:
            self.core._note(f"conversations forgotten: {n} line(s) deleted")

    def _export_memory(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        import time as _t
        import memory_store as ms
        default = str(Path.home() / "Documents" / f"SuitMk2_memory_{self.s['pilot_id']}_{_t.strftime('%Y%m%d')}.zip")
        path, _ = QFileDialog.getSaveFileName(self, "Export SuitMk2 memory", default, "Zip (*.zip)")
        if not path:
            return
        try:
            out = ms.export_pilot(st.DIR / "memory", self.s["pilot_id"], path)
            self.core and self.core._note(f"memory exported -> {out}")
        except Exception as e:
            self.core and self.core._note(f"memory export FAILED: {type(e).__name__}: {e}")

    def _apply_voice_gate(self) -> None:
        """The companions speak only while this window is open AND the user has not muted them.

        "Open" is isVisible(): a minimised window still counts (he launched it and it is on the taskbar); a
        hidden one (preloaded by the launcher, closed with X, or toggled off from its tile) does not. The
        Mute button keeps its own saved value in self.s["muted"] and this never writes it, so closing and
        reopening the window cannot change what he chose.

        One thing gets through a hidden window: the answer to a question he asked with the talk key
        (_open_answer_pass; see the module docstring)."""
        _voice_gate(self.speech, bool(self.s.get("muted")), self.isVisible(), getattr(self, "_ptt_pass", False))

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._apply_voice_gate()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._apply_voice_gate()
        if getattr(self, "_notice_pending", False):
            QTimer.singleShot(600, self._maybe_show_notice)

    def _maybe_show_notice(self) -> None:
        """Once per notice wording: what the companion is (a narrator, nothing more) + the screenshot opt-in."""
        if not getattr(self, "_notice_pending", False) or not self.isVisible():
            return                                  # shown already this run, or the window is hidden again
        self._notice_pending = False
        from PySide6.QtWidgets import QMessageBox
        import training_shots as ts
        if int(self.s.get("notice_ack", 0) or 0) >= ts.NOTICE_VERSION:
            return
        box = QMessageBox(self)
        box.setWindowTitle(ts.NOTICE_TITLE)
        box.setIcon(QMessageBox.Information)
        box.setText(ts.NOTICE)
        cb = QCheckBox("Keep training screenshots on this PC (optional)")
        cb.setChecked(bool(self.s.get("keep_training_shots", False)))
        box.setCheckBox(cb)
        box.addButton("I understand", QMessageBox.AcceptRole)
        box.exec()
        self.s["notice_ack"] = ts.NOTICE_VERSION
        self._keep_shots.setChecked(cb.isChecked())        # -> _set_keep_shots saves both
        st.save(self.s)

    def _persist_dev_facts(self, on: bool) -> None:
        """Called by the core on every dev-facts toggle (voice or checkbox): save it, and keep the checkbox honest."""
        self.s["dev_facts"] = bool(on)
        st.save(self.s)
        cb = getattr(self, "_dev_facts", None)
        if cb is not None and cb.isChecked() != bool(on):
            QTimer.singleShot(0, lambda: (cb.blockSignals(True), cb.setChecked(bool(on)), cb.blockSignals(False)))

    def _set_dev_facts(self, on: bool) -> None:
        if self.core is not None:
            self.core.set_dev_facts(bool(on), "window", ack=False)
        else:
            self._persist_dev_facts(bool(on))

    def _set_residency(self, _idx: int = 0) -> None:
        """Save it, then ask the running line service to re-resolve so it takes effect with no restart.

        The service is a SEPARATE process reading the same settings.json, so the order matters: save first, then
        /reload. plan_for() reads speaker_residency once per resolve, never per line."""
        val = self._residency.currentData() or "evict"
        self.s["speaker_residency"] = val
        st.save(self.s)
        self.core and self.core._note("speaker models: " + ("one at a time - the idle speaker's VRAM goes back to "
                                                            "the game" if val == "evict" else "both kept warm"))

        def work():
            sc = getattr(self, "sidecar", None)              # set in _boot; the combo can be touched before that
            if sc is not None and not sc.reload():
                log.warning("residency saved but the line service did not answer; it will use this on next start")
        threading.Thread(target=work, name="suitmk2_residency", daemon=True).start()

    def _set_keep_shots(self, on: bool) -> None:
        self.s["keep_training_shots"] = bool(on)
        st.save(self.s)                     # the service re-reads it on every shot: takes effect with no restart
        self.core and self.core._note(f"training screenshots {'ON' if on else 'OFF'}")

    def _export_shots(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        import time as _t
        import training_shots as ts
        store = ts.default_store()
        if store.count() == 0:
            self.core and self.core._note("no training screenshots kept yet"
                                          + ("" if self.s.get("keep_training_shots") else " (keeping is OFF)"))
            return
        default = str(Path.home() / "Documents" / f"SuitMk2_training_shots_{_t.strftime('%Y%m%d')}.zip")
        path, _ = QFileDialog.getSaveFileName(self, "Export training screenshots", default, "Zip (*.zip)")
        if not path:
            return
        try:
            n = store.export(Path(path))
            self.core and self.core._note(f"{n} training screenshots exported -> {path}")
        except Exception as e:
            self.core and self.core._note(f"training screenshot export FAILED: {type(e).__name__}: {e}")

    def _api_key_now(self) -> str:
        import os
        return self._api_key.text().strip() or os.environ.get("ANTHROPIC_API_KEY", "").strip()

    def _api_test(self) -> None:
        from pair_realizer import api_check
        key, model = self._api_key_now(), self._api_model.currentData()
        self._api_status.setText("checking...")

        def work():
            ok, msg = api_check(key, model)
            self._api_result = ("OK: " if ok else "Not working: ") + msg
        self._api_result = None
        threading.Thread(target=work, name="suitmk2_api_test", daemon=True).start()
        self._poll_api_result()

    def _poll_api_result(self) -> None:
        # The check runs off the GUI thread; the label is only ever touched from here (the GUI thread).
        if getattr(self, "_api_result", None) is None:
            QTimer.singleShot(200, self._poll_api_result)
            return
        self._api_status.setText(self._api_result)

    def _api_save(self) -> None:
        on = self._api_on.isChecked()
        if on and not self._api_key_now():
            self._api_status.setText("Enter a key first (or set ANTHROPIC_API_KEY). Still using the local models.")
            self._api_on.setChecked(False)
            return                          # nothing saved, nothing reloaded: the refusal must stay on screen
        self.s["anthropic_api_key"] = self._api_key.text().strip()
        self.s["api_model"] = self._api_model.currentData()
        backend = "api" if on else ("auto" if self.s.get("backend") == "api" else self.s.get("backend", "auto"))
        self.s["backend"] = backend
        if getattr(self, "core", None) is not None:
            from emotion import LOCAL_STANCE_TRAINED
            self.core.affect.stance_text = backend == "api" or LOCAL_STANCE_TRAINED
        st.save(self.s)
        self._api_status.setText(f"saved - switching to {'Claude' if on else 'the local models'}...")

        def work():
            sc = getattr(self, "sidecar", None)            # set in _boot; Save may come first
            ok = sc is not None and sc.reload(backend)
            self._api_result = (f"Using {'Claude (' + self.s['api_model'] + ')' if on else 'the local models'}."
                                if ok else "Saved. The line service did not answer; it will use this on next start.")
        self._api_result = None
        threading.Thread(target=work, name="suitmk2_api_save", daemon=True).start()
        self._poll_api_result()

    def _import_memory(self) -> None:
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        import memory_store as ms
        path, _ = QFileDialog.getOpenFileName(self, "Import SuitMk2 memory", str(Path.home() / "Documents"),
                                              "Zip (*.zip)")
        if not path:
            return
        overwrite = QMessageBox.question(
            self, "Import memory", "Replace the current memory with this file?\n"
            "(A snapshot of the current memory is taken first.)") == QMessageBox.Yes
        if not overwrite:
            return
        try:
            ms.snapshot(st.DIR / "memory", self.s["pilot_id"])          # never lose the current one
            out = ms.import_pilot(path, st.DIR / "memory", overwrite=True)
            self._attach_tree()                 # the conversations in the file replace the ones that were here
            self.core and self.core._note(f"memory imported from {Path(path).name} -> {out}")
        except Exception as e:
            self.core and self.core._note(f"memory import REFUSED: {type(e).__name__}: {e}")

    def _set_volume(self, who: str, v: int) -> None:
        self.s[f"volume_{who}"] = v / 100.0
        st.save(self.s)
        self._vol_lbl[who].setText(f"{v}%")
        self.speech.set_level(who, v / 100.0)

    def _show_picture(self, act: str) -> None:
        never = self._pic_never[act].isChecked()
        self._pic_sl[act].setEnabled(not never)
        self._pic_lbl[act].setText("never" if never else pp.label(pp.STOPS[self._pic_sl[act].value()]))

    def _repace(self) -> None:
        if self.core is not None:
            self.core.pace.configure(self.s)      # takes effect on the eyes' next tick; nothing restarts

    def _set_picture_every(self, act: str, index: int) -> None:
        self.s[pp.every_key(act)] = float(pp.STOPS[max(0, min(len(pp.STOPS) - 1, int(index)))])
        st.save(self.s)
        self._show_picture(act)
        self._repace()

    def _set_picture_never(self, act: str, on: bool) -> None:
        self.s[pp.never_key(act)] = bool(on)
        st.save(self.s)
        self._show_picture(act)
        self._repace()

    def _set_eye_chattiness(self, v: int) -> None:
        self.s["eyes_chattiness"] = int(v)
        st.save(self.s)
        self._eye_chat_lbl.setText(LEVEL_NAMES[int(v)])
        if self.core is not None:
            self.core.set_eye_chattiness(int(v))

    def _set_chattiness(self, v: int) -> None:
        self.s["chattiness"] = int(v)
        st.save(self.s)
        self._chat_lbl.setText(LEVEL_NAMES[v])
        if self.core is not None:
            self.core.set_chattiness(v)

    def _set_fb_key(self, name: str, key: str, label: str) -> None:
        dlg = BindingCaptureDialog(self)
        if dlg.exec() and dlg.result is not None:
            if self.s.get("talk_key") == dlg.result.to_dict():
                self.core and self.core._note(f"{label}: that binding is already the talk key; not set")
                return
            self.s[key] = dlg.result.to_dict()
            st.save(self.s)
            self._fb_btn[name].setText(f"{label}: " + dlg.result.describe())
            self._fb_mon[name].start(dlg.result)          # start() stops the previous binding first

    def _toggle_mute(self, on: bool) -> None:
        self.s["muted"] = on
        self._apply_voice_gate()
        st.save(self.s)

    def _set_presence(self, value: str) -> None:
        self.s["presence"] = value
        st.save(self.s)

    def _test_voices(self) -> None:
        self.speech.say("Suit online. Vitals steady.", "elah", PRIORITY_EVENT)
        self.speech.say("And the ship, such as it is, remains at your service.", "montaigne", PRIORITY_EVENT)

    # -- lifecycle ----------------------------------------------------------------------------------------------------
    def handle_ipc_command(self, cmd: dict) -> None:
        t = cmd.get("type", "")
        if t == "show":
            self.showNormal()
            self.raise_()
            self.activateWindow()
        elif t == "hide":
            self.hide()
        elif t == "quit":
            self._quit()

    def _on_close(self) -> None:
        if self._standalone:
            self._quit()
        else:
            self.hide()               # the companion keeps running; the window is only a dashboard

    def _quit(self) -> None:
        # ONCE, AND THE GUARD HAS TO COVER THE QApplication.quit() IN THE finally TOO.
        # This method is wired from three places - handle_ipc_command("quit"), _on_close when standalone, and
        # suitmk2_companion_app.py:63 `app.aboutToQuit.connect(window._quit)` - and it ENDS by calling
        # QApplication.quit(). Measured on Qt 6.11: quit() re-emits aboutToQuit even when it is called from inside
        # that very emission, so those two lines were an unbounded loop: _quit -> quit() -> aboutToQuit -> _quit.
        # It ran 485 times per shutdown and died of `RecursionError: Stack overflow (used 2912 kB)`
        # (logs/suitmk2.crash.log, PIDs 36136 and 188024; and 485 `session_end` lines in that session's dream file).
        # Guarding only the teardown would leave the loop spinning on the finally, so the whole method is once-only.
        # That is safe because every path here is a quit INTENT: by the time a second call arrives, the first one has
        # already asked Qt to exit, so re-asking is redundant by construction.
        # ⚠ NOT fixed by removing the teardown warnings. The logging was never the cycle - a control with no logging
        # at all still re-entered 485 times - and stop()'s own comment says why those warnings must stay.
        if getattr(self, "_quitting", False):
            log.debug("suit window: _quit called again (aboutToQuit, IPC quit or the close button); "
                      "the teardown already ran, so this call does nothing")
            return
        self._quitting = True
        try:
            try:
                self.ears.shutdown()
                for m in self._fb_mon.values():
                    m.stop()
            except Exception:
                pass
            if self.core:
                self.core.stop()
            self.speech.close()
            if self.sidecar:
                self.sidecar.stop()   # only if we started it (and the local runtime, only if IT woke it)
            if self.setup is not None:
                try:
                    if self.setup.job is not None:
                        self.setup.job.cancel.set()      # a setup in flight stops at its next chunk; it resumes
                    self.setup.mgr.stop_if_ours()        # an `ollama serve` the setup job started
                except Exception:
                    pass
        finally:
            from PySide6.QtWidgets import QApplication
            QApplication.quit()


class SuitWindow(_SuitBody, SCWindow):
    """SuitMk2 in its own window (suitmk2_companion_app.py)."""

    # (what, text) about the held talk key: "listening", "released", "heard", "note", "error". Nobody listens in
    # this window; the shared window shows it on screen.
    pttState = Signal(str, str)
    pttChanged = Signal()

    def __init__(self, geometry, hotkey_text: str = "", cmd_file: Optional[str] = None) -> None:
        SCWindow.__init__(self, title="SuitMk2", width=geometry.w, height=geometry.h, min_w=420, min_h=360,
                          opacity=geometry.opacity, accent=ACCENT)
        self.restore_geometry_from_args(geometry.x, geometry.y, geometry.w, geometry.h, geometry.opacity)
        self._build(hotkey_text, cmd_file)


class SuitPanel(_SuitBody, QWidget):
    """SuitMk2 as one tab of the Toolbox Assistant window (tools/Assistant/toolbox_assistant_app.py).

    The same dashboard and the same companion core, with no title bar of its own. `mic` says whether this tab is the
    one listening when it is built; the window moves the microphone afterwards with mic_take / mic_release.
    """

    # (what, text) about the held talk key: "listening", "released", "heard", "note", "error". The window shows it
    # on screen (assistant/ptt_overlay.py), because the pilot holding the key is in the game, not looking here.
    pttState = Signal(str, str)
    # the talk key or the mic mode changed: the window checks the two tabs' keys again
    pttChanged = Signal()

    def __init__(self, parent: Optional[QWidget] = None, cmd_file: Optional[str] = None, mic: bool = True) -> None:
        QWidget.__init__(self, parent)
        self._mic_mine = bool(mic)
        self.content_layout = QVBoxLayout(self)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(0)
        self._build("", cmd_file, chrome=False)

    def _apply_voice_gate(self) -> None:
        """The companions speak only while the WINDOW this tab sits in is open, and the user has not muted them.

        Not this tab's own isVisible(): that goes False whenever the Assistant tab is the one showing, and the rule
        (J 2026-10-04) is about the tool being open, not about which tab is in front. A hidden window (preloaded by
        the launcher, closed with X, toggled off) is silent exactly as SuitWindow is, and lets the same one thing
        through: the answer to a question asked with the talk key."""
        _voice_gate(self.speech, bool(self.s.get("muted")), self.window().isVisible(),
                    getattr(self, "_ptt_pass", False))

    def host_visibility_changed(self) -> None:
        """Called by the window when it is shown or hidden. A tab that is not in front gets no show/hide event of its
        own when that happens, so the window has to say."""
        self._apply_voice_gate()

    def shutdown(self) -> None:
        """Launcher quit, or the window closing for good: the same once-only teardown as SuitWindow."""
        self._quit()

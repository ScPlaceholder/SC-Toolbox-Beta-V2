"""rare_line.py - Montaigne's MONT-AI-GN-3 line: once in about a hundred hours, the attendant shows (J, 2026-10-05).

J's backstory (elah-audio/_suit_preferences_J_2026-10-05.md, 23:18 and 23:25): Montaigne's real model number is
MONT-AI-GN-3, an attendant AI. In a bad accident his library of Montaigne and his own personality files crossed,
and he has believed he is the man ever since. J asked for an easter egg: a line "hidden deep inside his random
lines", heard by "only users who use him a ton ... maybe once out of 100 hours", in which the ship assistant
underneath shows for a second and then he is Montaigne again. No setting; it is meant as a surprise.

THE LINE is in data/canon_montaigne.json under "glitch", for J to edit, and is said word for word. No model sees it.

WHEN. Three numbers, all in HOURS OF USE as hours_aboard.py counts them (the window open, not muted, the pilot at
the controls), so a Suit left running hidden overnight earns nothing:

    MIN_HOURS        20   never before this many hours aboard, in all
    MIN_GAP_HOURS    20   never twice within this many hours of use
    MEAN_WAIT_HOURS  80   once it may be said, how long the wait is on average

  The arithmetic. Once eligible, every second of use carries the same small chance, 1 in 80 x 3600 = 1 in
  288,000. An opportunity is an ambient tick; at the normal dial a tick comes every 90 s of use, so the chance at
  one opportunity is 90 / 288,000 = 0.0003125, about 1 in 3,200, and 3,200 ticks x 90 s = 80 hours. Code does not
  hard-code the 90: it gives each opportunity the chance that the seconds of use since the last one have earned
  (1 - exp(-seconds / 288,000)), so the rate is the same at every chattiness dial, whose ticks run from 40 s to
  300 s, and a tick that was held (a fight, "not now") passes what it earned to the next, up to OWED_MAX_S.
  The wait is then followed by the 20-hour gap: 80 + 20 = 100 hours from one hearing to the next on average,
  and 20 + 80 = 100 hours to the first. tests/test_rare_line.py simulates 40,000 hours and counts.

  "On average" is all it is. Waits of this kind vary a great deal: about one wait in eight is shorter than 30
  hours of use (1 - exp(-10/80) = 0.12), and about one in twelve is longer than 220 (exp(-200/80) = 0.08).

HOW IT IS SAID. Through the path every unprompted line takes (CompanionCore.ambient_tick -> _consider -> the speak
gate -> _realize_one -> grounding -> speech), as an AMBIENT line of Montaigne's. So everything that holds an
unprompted line holds this one: Mute and the hidden window (speech refuses it), AFK, a fight, the PC overloaded
or hot, headroom not clear, "not now", the chattiness dial at silent, the companions still shaken. A line that
was chosen and then held is not lost: it waits, and is offered again at each tick until it is actually spoken.
Only then is it written down as said.

THE ONE EXEMPTION. The chat gate refuses a line in which Montaigne calls himself an assistant or offers help like
one, and this line does both: that is the point of it. problems() lifts those two checks, by name, for a text that
is EXACTLY the line the canon file holds at that moment, and for nothing else. Every other check still applies to
the exact line (length, markup, the five attachment moves, a product name), and a text that differs from it by
one character gets no exemption at all: it is refused for not being the line, and the full gate is run on it too.
Nothing in chat_contract knows this module exists, so a model that words the same sentence is still refused.

STATE. <pilot's memory>/rare_line.json: {"mont_ai_gn_3": {"said_at": unix time, "said_at_hours": hours aboard
then, "count": n}}. Missing = never said. Unreadable = treated as said just now, since the gap cannot be shown to
have passed. With no memory folder there is nowhere to keep it, and the line is never said.
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import chat_contract as cc

log = logging.getLogger("suitmk2.rare")
SCENARIO = "rare_line"
SPEAKER = "montaigne"
KEY = "mont_ai_gn_3"
STATE_NAME = "rare_line.json"
MIN_HOURS = 20.0
MIN_GAP_HOURS = 20.0
MEAN_WAIT_HOURS = 80.0
OWED_MAX_S = 1800.0            # the most a run of held ticks may pass on: half an hour of use, a chance of 0.6%
# The two checks of the chat gate this one line is excused from, by the names chat_contract gives them.
EXEMPT = (cc.CHARACTER[0][0], cc.CHARACTER[1][0])
NOT_THE_LINE = "not the rare line the canon file holds"


def entry() -> dict:
    e = cc.canon(SPEAKER).get("glitch")
    return e if isinstance(e, dict) else {}


def line() -> str:
    """The line as the canon file holds it now; "" when there is none."""
    text = entry().get("line")
    return text.strip() if isinstance(text, str) else ""


def problems(text: str) -> list[str]:
    """Why this text may not be said as the rare line; [] = it may. See THE ONE EXEMPTION above."""
    held = line()
    if not held:
        return ["the canon file holds no rare line"]
    if text != held:
        return [NOT_THE_LINE] + cc.chat_problems(SPEAKER, str(text or ""), "")
    # His own names and number are his to say; everything else the gate checks, it checks.
    return [f for f in cc.chat_problems(SPEAKER, text, text) if f not in EXEMPT]


def spec() -> Optional[dict]:
    """The line as a fixed-text spec the core can speak like any other unprompted line, or None when the file
    holds none or it does not pass. Its one claim is the model number and its expansion, which is where the
    grounding gate finds the 3."""
    text = line()
    if not text or problems(text):
        return None
    e, n = entry(), len(text.split())
    return {"id": f"rare:{KEY}", "scenario": SCENARIO, "speaker": SPEAKER, "fixed_text": text,
            "claims": [{"id": "C1", "predicate": "montaigne.model",
                        "value": f"{e.get('model', '')}: {e.get('expansion', '')}"}],
            "required_values": [], "length_words": [max(1, n - 3), n]}


class RareLine:
    """When the line may be said, and the record of when it was. hours: an hours_aboard.HoursAboard.
    path None = never (there is nowhere to keep the record)."""

    def __init__(self, path: Optional[Path], hours, rng: Optional[random.Random] = None,
                 now: Callable[[], float] = time.time):
        self.path = Path(path) if path else None
        self.hours, self.now = hours, now
        self.rng = rng or random.Random()
        self._lock = threading.Lock()
        self.armed = False                 # chosen, and waiting to be spoken
        self._owed = 0.0                   # seconds of eligible use not yet given their chance
        self.said_at: Optional[float] = None
        self.said_hours: Optional[float] = None
        self.count = 0
        self._unsaved = False              # the last write failed: nothing more is chosen until it succeeds
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            rec = json.loads(self.path.read_text(encoding="utf-8")).get(KEY) or {}
            if rec:
                self.said_at = float(rec["said_at"])
                self.said_hours = float(rec["said_at_hours"])
                self.count = int(rec.get("count") or 1)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:
            log.warning("rare line: %s could not be read (%s: %s); treated as said just now", self.path,
                        type(e).__name__, e)
            self.said_at, self.said_hours, self.count = self.now(), self.hours.hours, max(1, self.count)
        if self.said_hours is not None and self.said_hours > self.hours.hours:
            self.said_hours = self.hours.hours       # the hours were lost and restarted: the gap runs from now

    def eligible(self) -> bool:
        if self.path is None or self._unsaved:
            return False
        h = self.hours.hours
        return h >= MIN_HOURS and (self.said_hours is None or h - self.said_hours >= MIN_GAP_HOURS)

    def used(self, seconds: float) -> None:
        """This much use was just counted. While the line may be said, it earns its chance."""
        with self._lock:
            if self._unsaved:
                self._save()
            if seconds > 0 and not self.armed and self.eligible():
                self._owed = min(self._owed + float(seconds), OWED_MAX_S)

    def chance(self) -> float:
        """The chance at the next opportunity: what the use since the last one has earned."""
        return 1.0 - math.exp(-self._owed / (MEAN_WAIT_HOURS * 3600.0))

    def roll(self) -> bool:
        """An opportunity. True = the line is chosen and waits to be spoken (and stays so until said())."""
        with self._lock:
            if self.armed:
                return True
            if not self.eligible() or self._owed <= 0:
                return False
            p, self._owed = self.chance(), 0.0
            self.armed = self.rng.random() < p
            return self.armed

    def disarm(self) -> None:
        with self._lock:
            self.armed, self._owed = False, 0.0

    def said(self) -> None:
        """It was spoken. Written down at once; the gap starts here."""
        with self._lock:
            self.armed, self._owed = False, 0.0
            self.said_at, self.said_hours, self.count = self.now(), self.hours.hours, self.count + 1
            self._save()

    def _save(self) -> None:
        rec = {KEY: {"said_at": round(self.said_at or 0.0, 1), "said_at_hours": round(self.said_hours or 0.0, 4),
                     "count": self.count}}
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(rec), encoding="utf-8")
            os.replace(tmp, self.path)
            self._unsaved = False
        except OSError as e:
            self._unsaved = True
            log.warning("rare line: could not write %s (%s: %s); it will not be chosen again until this is written",
                        self.path, type(e).__name__, e)
        try:
            self.hours.save()              # the hours it was said at are no use without the hours themselves
        except Exception:
            log.exception("rare line: the hours could not be saved")

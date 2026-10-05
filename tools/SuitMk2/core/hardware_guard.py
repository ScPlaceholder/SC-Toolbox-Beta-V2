"""hardware_guard.py - the hard limit: the companions never cost the game its machine (J 2026-10-05).

J, asked whether quieting under load should be something a user can switch off: "Agreed. Our hardware monitoring
should prevent eyes or chat during important moments and if the card runs too hard disable them completely." And:
someone will run a 27B model beside Star Citizen on max graphics on a 1080 Ti with 16 GB of RAM; it must not crash
the game or overload the machine.

So nothing in this file reads a setting, and no setting, key or checkbox turns any of it off
(tests/test_hardware_guard.py scans the settings and the window for one).

WHAT THE MONITOR CAN READ (hw_monitor.py, the same counters Task Manager shows): processor load, free and total
system memory, graphics load, video memory in use and its total, and Star Citizen's own share of the graphics load.
It reads NO temperature: Windows has no counter for it without a vendor library, and none is added here. "Runs too
hard" therefore means what the monitor's own verdict means: headroom TIGHT, which is free system memory under 3 GB,
graphics load over 85 %, free video memory under 1 GB, or Star Citizen alone over 80 % of the card.

OverloadGuard: the eyes and the chat model already stand down at every TIGHT reading. This adds the second step:
TIGHT for OVERLOAD_AFTER_S without a break switches both OFF, and they stay off until the reading has been clear for
RECOVER_AFTER_S without a break. The pilot is told why once per switch-off, in the window, never out loud.

    python hardware_guard.py --selftest
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# How long TIGHT has to last before it is "the card stays overloaded" and not a spike. From what the monitor can
# measure: it samples every 2 s, calls TIGHT after 5 s over a limit, and needs 20 s back under every limit before it
# says OK again. So one spike (a shader compile, a jump, a city loading in) reads TIGHT for some tens of seconds.
# 120 s is six of those 20 s recovery windows failed in a row: a load that is not going away by itself.
OVERLOAD_AFTER_S = 120.0
# How long the reading must stay clear before eyes and chat come back. Three of the monitor's 20 s windows, so a
# card that dips under the limit for a moment between two heavy stretches does not switch them on and off.
RECOVER_AFTER_S = 60.0
CLEAR = ("OK", "ROOMY")
OFF_NOTICE = ("Eyes and chat are off: the PC has had no room to spare for {mins} (graphics load, video memory or "
              "system memory). They come back on their own when it has.")
BACK_NOTICE = "Eyes and chat are back: the PC has room to spare again."


def _span(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60} min" if s >= 60 and s % 60 == 0 else f"{s} s"


class OverloadGuard:
    """feed(reading) on a steady tick; .off says whether eyes and chat are switched off.

    reading is the monitor's verdict: "TIGHT", "OK", "ROOMY", or None when there is no reading at all (the model
    service is not answering). None is neither: it never switches anything off (nothing is known to be overloaded)
    and never switches anything back on (nothing is known to have recovered), and it breaks a run of either kind."""

    def __init__(self, overload_after_s: float = OVERLOAD_AFTER_S, recover_after_s: float = RECOVER_AFTER_S,
                 now: Callable[[], float] = time.time):
        self.overload_after_s, self.recover_after_s, self._now = float(overload_after_s), float(recover_after_s), now
        self.off = False
        self.trips = 0
        self._tight_since: Optional[float] = None
        self._clear_since: Optional[float] = None
        self._notice: Optional[str] = None

    def feed(self, reading: Optional[str]) -> bool:
        t = self._now()
        if reading == "TIGHT":
            self._clear_since = None
            if self._tight_since is None:
                self._tight_since = t
            if not self.off and t - self._tight_since >= self.overload_after_s:
                self.off = True
                self.trips += 1
                self._notice = OFF_NOTICE.format(mins=_span(self.overload_after_s))
        elif reading in CLEAR:
            self._tight_since = None
            if self._clear_since is None:
                self._clear_since = t
            if self.off and t - self._clear_since >= self.recover_after_s:
                self.off = False
                self._notice = BACK_NOTICE
        else:
            self._tight_since = self._clear_since = None
        return self.off

    def take_notice(self) -> Optional[str]:
        """What to tell the pilot, once: the text is handed out a single time per switch-off and per recovery."""
        n, self._notice = self._notice, None
        return n

    def status(self) -> str:
        return OFF_NOTICE.format(mins=_span(self.overload_after_s)) if self.off else ""


def _selftest() -> int:
    ok = True

    def case(name, cond):
        nonlocal ok
        ok &= bool(cond)
        print(("  PASS  " if cond else "  FAIL  ") + name)
    clock = [0.0]
    g = OverloadGuard(now=lambda: clock[0])

    def run(reading, seconds, step=5.0):
        for _ in range(int(seconds / step)):
            clock[0] += step
            g.feed(reading)
    g.feed("TIGHT")
    run("TIGHT", 60)
    run("OK", 10)
    case("a spike of a minute does not switch anything off", not g.off and g.take_notice() is None)
    run("TIGHT", 125)
    case("two minutes of TIGHT switches eyes and chat off", g.off and g.trips == 1)
    case("the pilot is told once", "no room to spare for 2 min" in (g.take_notice() or "") and g.take_notice() is None)
    run("OK", 30)
    run("TIGHT", 5)
    run("OK", 55)
    case("a dip under the limit does not switch them back on", g.off)
    run("OK", 10)
    case("a clear minute does", not g.off and g.take_notice() == BACK_NOTICE)
    run(None, 600)
    case("no reading at all switches nothing off", not g.off)
    print("hardware_guard selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)

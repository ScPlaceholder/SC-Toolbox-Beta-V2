"""hardware_guard.py - the hard limit: the companions never cost the game its machine.

Quieting under load is not something a user can switch off. The hardware monitoring
prevents eyes or chat during important moments and, if the card runs too hard, disables them completely. And:
someone will run a 27B model beside Star Citizen on max graphics on a 1080 Ti with 16 GB of RAM; it must not crash
the game or overload the machine.

So nothing in this file reads a setting, and no setting, key or checkbox turns any of it off
(tests/test_hardware_guard.py scans the settings and the window for one).

WHAT THE MONITOR CAN READ (hw_monitor.py, the same counters Task Manager shows): processor load, free and total
system memory, graphics load, video memory in use and its total, and Star Citizen's own share of the graphics load.
It reads NO temperature: Windows has no counter for it without a vendor library, and none is added here. "Runs too
hard" therefore means what the monitor's own verdict means: headroom TIGHT, which is free system memory under 3 GB,
graphics load over 85 %, free video memory under 1 GB, or Star Citizen alone over 80 % of the card.

TEMPERATURE (do not assume the card protects itself; a user may have flashed its firmware or lifted
its power limit, and that cannot be detected). So the Suit has a ceiling of its OWN, TEMP_CEILING_C, and never asks
the card what its limit is. A reading at or over the ceiling counts exactly as TIGHT does. A reading that is absent,
zero or nonsense is "cannot check": it is never "fine", it relaxes nothing, and the window says temperature is not
being watched.
TODAY NOTHING SUPPLIES A READING. read_gpu_temperature_c() returns None on every PC, because hw_monitor cannot read
one and nothing here runs a vendor tool. The ceiling and its tests are in place for the day a reader exists; until
then every PC is in the "cannot check" branch, and says so.

OverloadGuard: the eyes and the chat model already stand down at every TIGHT reading. This adds the second step:
TIGHT for OVERLOAD_AFTER_S without a break switches both OFF, and they stay off until the reading has been clear for
RECOVER_AFTER_S without a break. The pilot is told why once per switch-off, in the window, never out loud.

fits(): the check BEFORE a chat model is ever loaded. What the model needs is set against the video memory and the
system memory that are free at that moment; a model that does not fit is refused with the numbers in plain words. It
runs when a model is picked in the window and again whenever the talk path is about to use one (chat_models.py).
Nothing is loaded to find out: the need comes from the model's size on disk.

    python hardware_guard.py --selftest
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
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


# OUR ceiling for the graphics card's temperature, in degrees Celsius. It is a limit on the load the companions
# ADD, not a statement about any card: at or over it they take no picture and ask no chat model, whatever the card
# itself would tolerate. 80 is chosen to sit under where stock cards begin to throttle, which their makers publish
# as roughly the mid 80s to low 90s; those figures are from memory of the makers' pages and were not measured here.
# A card that runs Star Citizen at 80 or more by itself simply gets no pictures and no chat model while it does.
# Never replaced by a value the card reports, and not a setting.
TEMP_CEILING_C = 80.0
TEMP_SANE_C = (1.0, 150.0)       # a reading outside this is a broken sensor (0 is what a missing one often says)
HOT, NOT_HOT, CANNOT_CHECK = "HOT", "OK", "CANNOT_CHECK"
TEMP_NOT_WATCHED = "temperature is not being watched (this PC gives no reading)"


def read_gpu_temperature_c() -> Optional[float]:
    """The graphics card's temperature, or None when it cannot be read. It cannot be read: hw_monitor has no
    temperature counter, and reading one needs a vendor tool or library this project does not ship. Kept as the one
    place a reader would go."""
    return None


def temperature_state(reading) -> str:
    """HOT at or over TEMP_CEILING_C, OK under it, CANNOT_CHECK for anything that is not a believable reading
    (None, zero, a negative, NaN, text, a bool, a number over 150)."""
    if isinstance(reading, bool) or not isinstance(reading, (int, float)):
        return CANNOT_CHECK
    if reading != reading or not (TEMP_SANE_C[0] <= reading <= TEMP_SANE_C[1]):
        return CANNOT_CHECK
    return HOT if reading >= TEMP_CEILING_C else NOT_HOT


def with_temperature(reading: Optional[str], temperature) -> Optional[str]:
    """The monitor's verdict with the temperature folded in. HOT makes it TIGHT. OK and CANNOT_CHECK leave it
    exactly as it was: a temperature that is fine, or unknown, never turns a TIGHT into anything better."""
    return "TIGHT" if temperature_state(temperature) == HOT else reading


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


# ---- does a model fit -----------------------------------------------------------------------------------------
GB = 1024 ** 3
# Ollama's tag list gives a model's size ON DISK. That is a floor for the memory it takes, not the need: the weights
# load at about their file size, and the context and the working buffers come on top. 1.2 is that file plus a fifth.
# Not measured per model, on purpose (measuring means loading). The two figures this project has: each speaker's line
# model is 1.53 GB on disk and was measured at 1.83 GB of video memory (settings.py, speaker_residency), which is
# 1.2; gemma3:4b is 3.1 GB on disk and its cost is given as about 2.7 GB (chat_talker.py), which is under it.
NEED_FACTOR = 1.2
# What must stay free AFTER the model is in. The monitor's own lines for TIGHT (hw_monitor.EXIT_VRAM_FREE_GB_TO_TIGHT
# and EXIT_FREE_RAM_GB_TO_TIGHT): a model that would leave less than this would only be unloaded again at once.
VRAM_RESERVE_GB = 1.0
SYSTEM_RESERVE_GB = 3.0
# With no memory reading at all the check cannot run. Then nothing bigger than this on disk is accepted: room for
# the one model that has been measured here (gemma3:4b, 3.3 GB) and nothing larger. The headroom rule still refuses
# to use it while the monitor says TIGHT, and the monitor says TIGHT whenever it cannot read.
UNKNOWN_MAX_GB = 3.5


@dataclass
class FreeMemory:
    """What is free right now, in bytes; None = could not be read. System memory is the smaller of free RAM and
    free commit (RAM plus page file): a model that does not fit in video memory spills into RAM and then into the
    page file, and running out of commit is what crashes the game or freezes the PC."""
    vram_free: Optional[int] = None
    ram_free: Optional[int] = None
    commit_free: Optional[int] = None
    game_running: Optional[bool] = None

    def system_free(self) -> Optional[int]:
        known = [v for v in (self.ram_free, self.commit_free) if v is not None]
        return min(known) if known else None


def read_free_memory(interval: float = 0.25) -> FreeMemory:
    """One reading from hw_monitor (the counters Task Manager shows). Takes about `interval` seconds, so not on
    the Qt thread. Never raises: what cannot be read is None."""
    out = FreeMemory()
    try:
        import hw_monitor as hw
    except Exception:
        return out
    try:
        s = hw.sample_all(interval=interval)
        out.vram_free, out.ram_free, out.game_running = s.vram_free_bytes, s.ram_avail_bytes, s.sc_pid is not None
    except Exception:
        pass
    try:
        _, out.commit_free, _ = hw.read_commit_bytes()
    except Exception:
        pass
    return out


def need_bytes(size_on_disk: int) -> int:
    return int(size_on_disk * NEED_FACTOR)


def _gb(n: float) -> str:
    g = n / GB
    return f"{g:.0f} GB" if g >= 10 else f"{g:.1f} GB"


def fits(size_on_disk: Optional[int], free: Optional[FreeMemory]) -> tuple:
    """(True, why) when a model of this size on disk may be loaded with this much free; (False, why) when not.
    Unknown is never "fits": no size is a no, and no memory reading is a no for anything over UNKNOWN_MAX_GB."""
    if not isinstance(size_on_disk, (int, float)) or isinstance(size_on_disk, bool) or size_on_disk <= 0:
        return False, "Ollama does not say how big this model is, so it cannot be checked against this PC."
    need = need_bytes(size_on_disk)
    free = free if free is not None else FreeMemory()
    vram, system = free.vram_free, free.system_free()
    game = " with Star Citizen running" if free.game_running else ""
    if vram is None or system is None:
        what = "video memory" if system is not None else ("system memory" if vram is not None else "memory")
        if size_on_disk <= UNKNOWN_MAX_GB * GB:
            return True, (f"free {what} could not be read, so the check could not run; this model is small enough "
                          f"({_gb(size_on_disk)} on disk) to be allowed without it.")
        return False, (f"free {what} could not be read, so the check could not run, and without it only a model up "
                       f"to {UNKNOWN_MAX_GB} GB on disk is allowed. This one is {_gb(size_on_disk)}.")
    if need > vram - VRAM_RESERVE_GB * GB:
        return False, (f"this model needs about {_gb(need)} of video memory; {_gb(vram)} is free{game}, and "
                       f"{_gb(VRAM_RESERVE_GB * GB)} has to stay free for the game.")
    if need > system - SYSTEM_RESERVE_GB * GB:
        return False, (f"this model needs about {_gb(need)}; {_gb(system)} of system memory is free{game}, and "
                       f"{_gb(SYSTEM_RESERVE_GB * GB)} has to stay free.")
    return True, (f"needs about {_gb(need)}; {_gb(vram)} of video memory and {_gb(system)} of system memory are "
                  f"free{game}.")


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
    roomy = FreeMemory(vram_free=10 * GB, ram_free=20 * GB, commit_free=30 * GB)
    case("a 3.3 GB model fits a roomy PC", fits(int(3.3 * GB), roomy)[0])
    case("a 17 GB model does not fit 6 GB of free video memory",
         not fits(17 * GB, FreeMemory(vram_free=6 * GB, ram_free=40 * GB, commit_free=60 * GB))[0])
    case("nor a PC with the video memory but not the system memory",
         not fits(8 * GB, FreeMemory(vram_free=24 * GB, ram_free=5 * GB, commit_free=30 * GB))[0])
    case("unknown memory is not 'fits' for a big model", not fits(17 * GB, FreeMemory())[0] and fits(3 * GB, None)[0])
    case("unknown size is a no", not fits(None, roomy)[0])
    case("temperature: at the ceiling is HOT, under it is not", temperature_state(80) == HOT and temperature_state(79.9) == NOT_HOT)
    case("temperature: absent, zero and nonsense are 'cannot check'",
         [temperature_state(v) for v in (None, 0, -40, 900, "72", float("nan"), True)] == [CANNOT_CHECK] * 7)
    case("temperature: unknown never improves the verdict",
         with_temperature("TIGHT", None) == "TIGHT" and with_temperature("OK", 95) == "TIGHT" and with_temperature(None, 40) is None)
    case("nothing supplies a temperature today", read_gpu_temperature_c() is None)
    print("hardware_guard selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)

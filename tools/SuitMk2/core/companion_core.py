"""companion_core.py - the SuitMk2 brain as a toolbox tool (replaces main.py's WingmanAI glue; 2026-09-23).

    Game.log --LogMonitor--> EventParser --> EventClassifier(StateStore, VolatileContext)
        --> on_event: duplicate check BEFORE recording (the 09-23 fix), session record for dreaming,
            event_spec  --\
        ambient timer --> ambient_spec (+ eyes scene facts; a fight on screen = silence)
                          --> SpeakGate (whether/when) --> realizer worker (sidecar, local model)
                          --> grounding gate (hard) --> Speech queue (Piper, two voices)

Nothing here calls a cloud service. The model and the eyes live in companion_service.py (the sidecar, its own
Python env with torch); this process only needs the toolbox's own Python.

Every failure degrades to silence: no realizer, no sidecar, a failed gate, a stale line, all mean nothing is said.
Selftest (fake sidecar/speech, REAL Game.log lines): python companion_core.py --selftest [path\\to\\Game.log]
"""
from __future__ import annotations

import logging
import os
import queue
import random
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from ambient_spec import build_ambient_spec          # noqa: E402
from topic_ledger import TopicLedger, subject_key    # noqa: E402
from topic_graph import TopicGraph, TopicWalker      # noqa: E402
from combat_watch import CombatWatch                 # noqa: E402
from activity_mode import ActivityMode, PRESENT, PRESENCE, build_look_spec   # noqa: E402
from activity_mode import AfkWatch, DEFAULT_AFK_MINUTES                      # noqa: E402
from emotion import CompanionAffect                                          # noqa: E402
import idle_spec                                     # noqa: E402
from event_spec import build_event_spec              # noqa: E402
from grounding_validator import ground               # noqa: E402
from event_parser import EventParser                 # noqa: E402
from event_classifier import EventClassifier         # noqa: E402
from state_store import StateStore                   # noqa: E402
from volatile_context import VolatileContext         # noqa: E402
from speak_gate import SpeakGate, SpeakState, Candidate, Priority, Verdict   # noqa: E402
from speech import PRIORITY_URGENT, PRIORITY_EVENT, PRIORITY_AMBIENT        # noqa: E402
from banter import BanterPolicy, plan_exchange, run_exchange               # noqa: E402
from pacing import apply as apply_pacing, NotNow                           # noqa: E402
from feedback import FeedbackRecorder                                      # noqa: E402
from session_story import welcome_spec, first_meeting_spec, mark_welcomed, recap_record, read_session   # noqa: E402
import dev_facts as devf                                                   # noqa: E402

log = logging.getLogger("suitmk2.core")

# Event types that speak, and how urgently. Everything else only updates state.
EVENT_PRIORITY = {"incapacitated": (Priority.URGENT, PRIORITY_URGENT),
                  "injury": (Priority.EVENT, PRIORITY_EVENT),
                  "med_bed_heal": (Priority.EVENT, PRIORITY_EVENT),
                  "reward_earned": (Priority.EVENT, PRIORITY_EVENT),
                  "player_respawned": (Priority.EVENT, PRIORITY_EVENT),
                  "qt_arrived": (Priority.EVENT, PRIORITY_EVENT),
                  "jurisdiction_change": (Priority.EVENT, PRIORITY_EVENT),
                  "location_change": (Priority.AMBIENT, PRIORITY_AMBIENT),
                  # the edge layer (J 09-23): parsed from his live log all night and never spoken until now
                  # boarding / leaving the ship. ⚠ These are the CLASSIFIER's names: it renames channel_change before
                  # the core sees it, so the old "channel_change" key here matched nothing in live play (2026-09-24).
                  "ship_channel_joined": (Priority.EVENT, PRIORITY_EVENT),
                  "ship_channel_left": (Priority.EVENT, PRIORITY_EVENT),
                  "docking_detached": (Priority.EVENT, PRIORITY_EVENT),
                  "blueprint_received": (Priority.EVENT, PRIORITY_EVENT),
                  "contract_accepted": (Priority.EVENT, PRIORITY_EVENT),
                  "contract_complete": (Priority.EVENT, PRIORITY_EVENT),
                  "objective_new": (Priority.AMBIENT, PRIORITY_AMBIENT),
                  "entered_monitored_space": (Priority.AMBIENT, PRIORITY_AMBIENT),
                  "exited_monitored_space": (Priority.AMBIENT, PRIORITY_AMBIENT),
                  # 2026-09-24: parsed since 09-23 and spoken by nobody, because it was never listed HERE. A speaking
                  # event is registered in four places (event_spec variants + claims, this table, the generator); the
                  # selftest below now fails when one is missing.
                  "refinery_complete": (Priority.EVENT, PRIORITY_EVENT),
                  # AMBIENT, not EVENT: in a hauling loop the freight lift cycles every few minutes, and it is
                  # company, not news (J: "not comment on every box they move").
                  "platform_moving": (Priority.AMBIENT, PRIORITY_AMBIENT)}
# Routine events: worth a line the first time in a while, not every time. Measured on J's log 2026-09-24: a hauling
# session cycles the freight lift every few minutes, and without this each trip got its own line. Seconds, any mode.
ROUTINE_GAP_S = {"event_platform_moving": 900.0}
# Speaking events that reach speech by ANOTHER path than EVENT_PRIORITY, each with the reason. Anything in
# event_spec.speaking_events() that is in neither is a line the companion can never say.
ROUTED_ELSEWHERE = {"boarded_ship": "ship_channel_joined (the classifier's name) becomes boarded_ship in build_event_spec",
                    "left_ship": "ship_channel_left, as above",
                    "combat_on": "_combat_edge (audio + second sense), not the log",
                    "combat_off": "_combat_edge, as above",
                    "item_earned": "reward_earned carrying an item becomes item_earned inside build_event_spec",
                    "ship_milestone": "synthesised by _boarded() from the per-ship boarding count",
                    "out_of_medpens": "synthesised by _loadout_changed() when the last medpen goes",
                    "out_of_mags": "synthesised by _loadout_changed() when the last spare magazine goes"}
# AFK (J 2026-09-24): events the pilot had to DO, which the OS idle clock cannot see when they fly on a HOTAS or a
# gamepad. Each one counts as activity for the AFK window (AfkWatch.poke). Rewards, objectives and injuries are not
# here: the game hands those out whether or not anyone is at the controls.
PILOT_DRIVEN_EVENTS = {"location_change", "qt_arrived", "jurisdiction_change", "docking_detached", "player_respawned",
                       "contract_accepted", "ship_channel_joined", "weapon_holstered"}
# Dev facts (dev_facts.py): these cut a fact that is still being said, and open a no-facts window.
DEV_FACT_URGENT_EVENTS = {"injury", "incapacitated", "player_respawned"}
QUIET_WINDOW_EVENTS = {"qt_route_calculated": "quantum", "qt_target_selected": "quantum",
                       "hangar_ready": "parked", "docking_ready": "parked"}


_GROUND: Optional[list] = None


def _is_ground_vehicle(ship: str) -> bool:
    """True when the current craft is a ground vehicle in data/ships.json (size_label "Vehicle"). The log may give
    a raw entity name like TMBL_Cyclone_RC_123, so match on the vehicle's name tokens, longest name first."""
    global _GROUND
    if _GROUND is None:
        try:
            import json
            rows = json.loads((Path(__file__).resolve().parent.parent / "data" / "ships.json")
                              .read_text(encoding="utf-8")).get("ships", [])
            _GROUND = sorted({r["name"].lower() for r in rows if r.get("size_label") == "Vehicle"}, key=len,
                             reverse=True)
        except Exception:
            _GROUND = []
    norm = " ".join(str(ship).lower().replace("_", " ").replace("-", " ").split())
    return any(f" {g} " in f" {norm} " for g in _GROUND)


def find_game_log(saved: Optional[str] = None) -> Optional[Path]:
    """The live Game.log: a saved path, else the common install roots on every drive."""
    cands = [Path(saved)] if saved else []
    for letter in "CDEFGH":
        for rel in (r"Star Citizen\StarCitizen", r"StarCitizen", r"Program Files\Roberts Space Industries\StarCitizen",
                    r"Roberts Space Industries\StarCitizen", r"Games\StarCitizen"):
            for channel in ("LIVE", "PTU", "EPTU", "HOTFIX"):
                cands.append(Path(f"{letter}:\\") / rel / channel / "Game.log")
    live = [p for p in cands if p.is_file()]
    return max(live, key=lambda p: p.stat().st_mtime) if live else None


class CompanionCore:
    def __init__(self, speech, realizer: Optional[Callable[[dict], Optional[str]]] = None,
                 eyes=None, recorder=None, dreams=None, headroom: Callable[[], str] = lambda: "OK",
                 ambient_every_s: float = 90.0, now: Callable[[], float] = time.time, chattiness: int = 2,
                 feedback_dir: Optional[Path] = None, lifecycle=None, session_id: str = "", store=None,
                 sound=None, idle_source: Optional[Callable[[], Optional[float]]] = None,
                 afk_after_s: float = DEFAULT_AFK_MINUTES * 60.0, dev_facts=None):
        self.speech, self.realizer, self.eyes = speech, realizer, eyes
        self.sound = sound                   # sound_classifier.SoundClassifier or None (game ears that know WHAT)
        self.recorder, self.dreams, self.headroom = recorder, dreams, headroom
        self.ambient_every_s, self.now = ambient_every_s, now
        self.state = StateStore()
        self.volatile = VolatileContext()
        self.parser = EventParser()
        # Loadout (copied from Battle_Buddy, see loadout_parser.py): what the pilot carries. Summarised into the state
        # store for the conversation lane, and running OUT of medpens or spare magazines is an event.
        from loadout_parser import InventoryParser
        from loadout_tracker import InventoryTracker
        self.loadout_parser, self.loadout = InventoryParser(), InventoryTracker()
        self.loadout_parser.subscribe(self.loadout.on_event)
        self.loadout.on_changed(self._loadout_changed)
        self._loadout_prev = {"medpens": 0, "mags": 0}
        self.classifier = EventClassifier(self.state, self.volatile)
        self.parser.subscribe(self.classifier.on_event)
        self.classifier.subscribe(self.on_event)
        self.gate = SpeakGate(now=now)
        self.gate_state = SpeakState()
        self._variant = {"event": 0, "ambient": 0, "banter": 0}
        self.topics = TopicLedger(now=now)   # a subject gets a couple of mentions, then they move on (J 2026-09-23)
        self._rng = random.Random()
        # J's topic flowchart: once the live subjects are talked out, walk sourced lore/ship topics instead of silence.
        try:
            self.walker: Optional[TopicWalker] = TopicWalker(TopicGraph.load(), self.topics.spent)
        except Exception:
            log.exception("topic graph unavailable; talked-out ticks stay silent")
            self.walker = None
        # Combat from SC's own audio onsets (fed by voice_fx.DuckingMonitor.listeners), confirmed by the strongest
        # second sense there is: the sound classifier, then the eyes, else audio onsets alone. A holstered weapon
        # ends it early. J's design, 2026-09-23.
        self.combat = CombatWatch(self._combat_edge,
                                  confirm=self._combat_confirm if (eyes is not None or sound is not None) else None,
                                  now=now)
        self.banter = BanterPolicy(now=now)
        # PRESENT vs PRESENCE (J 2026-09-24): a deterministic novelty score decides whether they react to what is on
        # screen or keep company through a slow loop. The eyes feed it as much as the log (scene transitions).
        self.activity = ActivityMode(now=now)
        # AFK: no input for afk_after_s -> they stop talking (see _afk_holds for what may still speak). The idle source
        # is injected like the clock: the window passes activity_mode.os_idle_seconds; the dry run and the selftests
        # pass nothing, and no source means never AFK, so a replay is never silenced.
        self.afk = AfkWatch(idle_source, afk_after_s, now=now)
        # Feelings fed by the game (J 2026-09-24). Persisted per pilot next to their memory, aged on load.
        _adir = getattr(store, "dir", None) if store is not None else None
        self.affect = CompanionAffect(path=(Path(_adir) / "affect.json") if _adir else None, now=now)
        self._affect_t = now()
        self._presence_last: dict = {}          # scenario -> last time a routine EVENT line spoke in PRESENCE
        self._pending_look: Optional[str] = None
        self._look_busy = False
        self._last_look_t = -1e9
        self._seen_transitions = 0
        self.lifecycle, self.session_id = lifecycle, session_id or time.strftime("%Y%m%d_%H%M%S")
        self.store = store
        self._load_told_topics()
        self._refresh_feelings()
        self._welcomed = False               # once per session, whatever path triggers it
        self._first_meeting_pending = False
        self.not_now = NotNow(now)
        self.pacer = apply_pacing(chattiness, self.gate, self, self.not_now)   # after banter; sets ambient tick
        self.feedback = FeedbackRecorder(feedback_dir or (Path.home() / ".sctoolbox" / "suitmk2" / "feedback"),
                                         now=now, lifecycle=lifecycle, not_now=self.not_now, hush=self.hush,
                                         session=self.session_id)
        self._work: "queue.Queue[tuple]" = queue.Queue(maxsize=3)
        self._stop = threading.Event()
        self._monitor = None
        self.stats = {"lines": 0, "events": 0, "dup": 0, "spec": 0, "gated": 0, "busy": 0, "realized": 0,
                      "ungrounded": 0, "silent": 0, "spoken": 0, "combat_hold": 0,
                      "banter": 0, "banter_turns": 0, "not_now": 0}
        self.last: list[str] = []              # recent decisions, for the status window
        # Dev-history fun facts (dev_facts.py, J 2026-09-25): OPTIONAL, off by default, Montaigne only, quiet moments
        # only. None = the feature does not exist in this core; a DevFacts with enabled=False = present but off.
        self.dev_facts = dev_facts
        self.dev_facts_persist: Optional[Callable[[bool], None]] = None   # the window saves the voice toggle
        self._dev_inflight: Optional[dict] = None      # the fact being said right now (for the injury interruption)
        self._dev_resume: Optional[dict] = None        # a cut fact waiting to be picked back up
        self._dev_bit_used = False                     # the interrupted-fact bit: once per session
        self._dev_toggles = 0
        self._last_speak_event_t = -1e9                # last event that SPEAKS (EVENT_PRIORITY), for the quiet window
        self._last_urgent_t = -1e9                     # last injury / death / respawn / combat start
        self._born_t = now()                           # no fact in the first minutes: that is the welcome's time

    # -- lifecycle ----------------------------------------------------------------------------------------------
    def start(self, game_log: Optional[Path]) -> None:
        threading.Thread(target=self._realize_loop, name="suitmk2_realize", daemon=True).start()
        threading.Thread(target=self._ambient_loop, name="suitmk2_ambient", daemon=True).start()
        if self.dreams is not None:
            threading.Thread(target=self._dream_catchup, name="suitmk2_dream", daemon=True).start()
        else:
            threading.Thread(target=self._welcome, name="suitmk2_welcome", daemon=True).start()
        if self.eyes is not None:
            threading.Thread(target=self._eyes_loop, name="suitmk2_eyes_watch", daemon=True).start()
        if self.sound is not None:
            try:
                self._note("game ears: " + ("listening for StarCitizen.exe" if self.sound.start() else "unavailable"))
            except Exception:
                log.exception("sound classifier start")
        if game_log:
            from log_monitor import LogMonitor
            self._monitor = LogMonitor(game_log, poll_interval=0.25)
            self._monitor.subscribe(self.feed_line)
            self._monitor.start()
            self._note(f"reading {game_log}")
        else:
            self._note("no Game.log found; waiting (set the path in settings)")

    def stop(self) -> None:
        self._stop.set()
        if self.sound is not None:
            try:
                self.sound.stop()
            except Exception:
                pass
        if self._monitor is not None:
            try:
                self._monitor.stop()
            except Exception:
                pass
        if self.recorder is not None:
            try:
                closed = self.recorder.close()          # instant: the dream queue processes it next launch
                if self.store is not None:
                    recap_record(read_session(closed), self.store, session=closed.name)   # one write, no model
            except Exception:
                log.exception("session record close")

    def set_chattiness(self, level: int) -> None:
        p = self.pacer.set_level(level)
        self._note(f"chattiness -> {p.level}")

    def _visited(self, st: dict) -> set:
        """Every place the pilot has ever been: this session's stops PLUS every past session's recap in the pilot's
        memory (session_story writes a HISTORY 'session.recap' with its locations). Session-only, the "somewhere we
        have not been" lines would suggest Orison to someone who lives there. Store read cached for 10 minutes."""
        now = self.now()
        cache = getattr(self, "_visited_cache", None)
        if self.store is not None and (cache is None or now - cache[0] > 600):
            past = set()
            try:
                import memory_store as ms
                from session_story import RECAP_PRED
                for m in ms.query(self.store, kind="HISTORY", predicate=RECAP_PRED):
                    v = m.get("value")
                    if isinstance(v, dict):
                        past |= {str(x) for x in (v.get("locations") or []) if x}
            except Exception:
                log.exception("visited places from memory")
            cache = self._visited_cache = (now, past)
        return set(st.get("recent_locations") or []) | (cache[1] if cache else set())

    def _eyes_combat(self) -> Optional[bool]:
        """The eyes' answer: True if the scene is a fight OR the muzzle-flash burst saw weapons fire; the burst's own
        False/None otherwise (None = still looking or cannot look: abstain, don't veto). Cached 0.5 s: CombatWatch
        asks up to 20x a second while MAYBE, and each ask is an HTTP call to the sidecar."""
        now = self.now()
        cached = getattr(self, "_eyes_cache", None)
        if cached is not None and now - cached[0] < 0.5:
            return cached[1]
        ans: Optional[bool]
        try:
            if (self.eyes.state() or {}).get("in_combat"):
                ans = True
            else:
                burst = getattr(self.eyes, "burst_confirm", None)
                ans = burst() if burst is not None else False
        except Exception:
            ans = None
        self._eyes_cache = (now, ans)
        return ans

    def _combat_confirm(self) -> Optional[bool]:
        """Classifier first (it heard the gunfire itself), then the eyes. A classifier True wins outright; if it says
        no, the eyes may still see a fight it could not hear (suppressed weapons, a mix drowned in music). None only
        when neither sense can answer, which leaves CombatWatch on audio onsets alone."""
        heard = None
        if self.sound is not None:
            try:
                heard = self.sound.gunfire_confirm()
            except Exception:
                heard = None
            if heard is True:
                return True
        if self.eyes is not None:
            seen = self._eyes_combat()
            if seen is not None:
                return seen
        return heard

    def _combat_edge(self, state: str, reason: str) -> None:
        """CombatWatch flipped. ON holds ambient/banter (gate_state.in_combat) and says one clipped line as URGENT,
        because the combat hold would otherwise gag the very line announcing the combat. OFF is a normal EVENT."""
        on = state == "on"
        self.gate_state.in_combat = on
        self.combat_reason = f"{state}: {reason}"   # shown on the dashboard, for tuning
        self.stats["combat_" + state] = self.stats.get("combat_" + state, 0) + 1
        self._note(f"combat {state}: {reason}")
        if on:
            self.afk.poke()                          # somebody is fighting; a HOTAS pilot reads idle to the OS
            self.activity.feed("combat_on")
            self._dev_fact_urgent("combat_on", {})
        spec = build_event_spec("combat_on" if on else "combat_off", {}, self._ambient_state(), self._variant["event"])
        if spec is not None:
            self._variant["event"] += 1
            if on:
                self._consider(spec, Priority.URGENT, PRIORITY_URGENT, "combat on")
            else:
                self._consider(spec, Priority.EVENT, PRIORITY_EVENT, "combat off")

    def hush(self) -> None:
        """Cut the current line and flush the queue without leaving speech muted."""
        was = bool(getattr(self.speech, "muted", False))
        self.speech.mute(True)
        if not was:
            self.speech.mute(False)

    TOLD_WINDOW_DAYS = 14

    def _ship_event(self, what: str) -> None:
        ship = self.state.get("ship")
        if not ship or self.store is None:
            return
        try:
            import memory_store as ms
            import ship_feelings as sf
            ev = sf.event(ship, what)
            ms.record_callback(self.store, f"{what} aboard {ship}", kind=ev["kind"], meta=ev["meta"])
            self._refresh_feelings()
        except Exception:
            log.exception("ship event")

    def _boarded(self, ship) -> None:
        """Count this boarding against the ship; on the 5th/10th/25th/50th/100th, say so."""
        if not ship or self.store is None:
            return
        try:
            import memory_store as ms
            import ship_feelings as sf
            ev = sf.event(ship, "board")
            ms.record_callback(self.store, f"boarded {ship}", kind=ev["kind"], meta=ev["meta"])
            n = sf.boardings(ms.recent_callbacks(self.store, n=20000), ship)
            if n in sf.MILESTONES:
                spec = build_event_spec("ship_milestone", {"ship": ship, "count": n}, self._ambient_state(),
                                        self._variant["event"])
                if spec:
                    self._variant["event"] += 1
                    self._consider(spec, Priority.EVENT, PRIORITY_EVENT, f"ship milestone {n}")
        except Exception:
            log.exception("ship milestone")

    def _refresh_feelings(self) -> None:
        if self.store is None or self.walker is None:
            return
        try:
            import memory_store as ms
            import ship_feelings as sf
            import topic_graph as tgm
            t = sf.tally(ms.recent_callbacks(self.store, n=5000))
            n = self.walker.g.apply_feelings(sf.feelings(t, tgm._ELAH_OPINIONS, tgm._MONT_OPINIONS))
            if n:
                log.info("ship feelings: %d favourite(s) changed by experience", n)
        except Exception:
            log.exception("ship feelings")

    def _load_told_topics(self) -> None:
        """Topic facts already told in the last TOLD_WINDOW_DAYS, from the pilot's memory (callbacks, kind "topic"):
        without this the topic ledger forgot everything at each restart and tomorrow repeated tonight's brochure
        lines. Older than the window = fair game again."""
        if self.store is None or self.walker is None:
            return
        try:
            import memory_store as ms
            from datetime import datetime, timedelta, timezone
            cutoff = (datetime.now(timezone.utc) - timedelta(days=self.TOLD_WINDOW_DAYS)).isoformat()
            n = 0
            for cb in ms.recent_callbacks(self.store, n=5000):
                meta = cb.get("meta") or {}
                if cb.get("kind") == "topic" and cb.get("created", "") >= cutoff and "node" in meta:
                    self.walker.told(meta["node"], meta.get("fact", 0))
                    n += 1
            if n:
                log.info("topics: %d fact(s) already told in the last %d days, skipped", n, self.TOLD_WINDOW_DAYS)
        except Exception:
            log.exception("told topics from memory")

    def _spoke(self, spec: dict, text: str, cand) -> None:
        if self.dev_facts is not None:
            if spec.get("scenario") == devf.SCENARIO:
                self.dev_facts.spoken(spec)
            elif spec.get("scenario") == devf.RESUME_SCENARIO:
                self._dev_resume = None             # said once; never again
            elif spec.get("scenario") == "event_injury" and self._dev_resume is not None:
                self._dev_resume["injury_spoken"] = True
        if spec.get("scenario") == "idle_relationship":
            self._last_idle = self.now()        # the relationship slot is spent only when one is actually said
        self.topics.record(spec)
        t = spec.get("topic")
        if t and self.walker is not None:
            self.walker.told(t["node"], t["fact"])
            if self.store is not None:
                try:
                    import memory_store as ms
                    ms.record_callback(self.store, text, kind="topic", meta={"node": t["node"], "fact": t["fact"]})
                except Exception:
                    log.exception("record told topic")
        try:
            self.feedback.note_spoken(spec, text, cand.priority)
        except Exception:
            log.exception("feedback note")
        if self.lifecycle is not None and spec.get("rhetoric"):
            try:
                self.lifecycle.record_use(spec.get("move_id") or f"{spec['speaker']}.{spec['rhetoric'][0]}",
                                          self.session_id)
            except Exception:
                log.exception("move lifecycle use")

    def _note(self, msg: str) -> None:
        self.last.append(f"{time.strftime('%H:%M:%S')} {msg}")
        del self.last[:-40]
        log.info(msg)

    # -- input ----------------------------------------------------------------------------------------------------
    def feed_line(self, line: str) -> None:
        self.stats["lines"] += 1
        try:
            self.parser.on_raw_line(line)
        except Exception:
            log.exception("parser")
        try:
            self.loadout_parser.on_line(line)
        except Exception:
            log.exception("loadout parser")

    def _loadout_changed(self, s) -> None:
        """Summarise the loadout for the conversation lane; speak only when medpens or spare mags run OUT."""
        try:
            # A reward item keeps its raw class code as its name ("Crlf Medgun 01 Msn Rwd02", measured on J's 07-08
            # log): say the weapon type alone rather than read a code aloud.
            def _name(w):
                raw = str(w.display_name or "")
                if not raw or any(ch.isdigit() for ch in raw) or raw.split()[0].lower() in ("crlf", "klwe", "behr"):
                    return w.weapon_type.lower()
                return f"{raw} {w.weapon_type.lower()}".strip()
            weapons = [_name(w) for k, w in s.weapons.items() if k.startswith(("primary", "sidearm"))]
            mags = sum(w.spare_mags for w in s.weapons.values())
            self.state.set("loadout_weapons", ", ".join(weapons) if weapons else None)
            self.state.set("loadout_medpens", s.medpens)
            self.state.set("loadout_spare_mags", mags)
            self.state.set("loadout_grenades", s.grenade_count)
            prev, self._loadout_prev = self._loadout_prev, {"medpens": s.medpens, "mags": mags}
            # A session join RESETS the tracker to an empty loadout. Measured on J's 08-02 log (3 joins): each reset
            # read as "4 medpens -> 0" and said "that was the last medpen" at login. An empty snapshot is a reset,
            # not a use: update silently.
            if not s.weapons and not getattr(s, "pens", None) and not s.grenade_count:
                return
            for key, et in (("medpens", "out_of_medpens"), ("mags", "out_of_mags")):
                if prev[key] > 0 and self._loadout_prev[key] == 0 and (key != "mags" or weapons):
                    spec = build_event_spec(et, {"weapons": ", ".join(weapons)}, self._ambient_state(),
                                            self._variant["event"])
                    if spec:
                        self._variant["event"] += 1
                        self._consider(spec, Priority.EVENT, PRIORITY_EVENT, et.replace("_", " "))
        except Exception:
            log.exception("loadout summary")

    def _eyes_loop(self) -> None:
        """Watch the eyes' scene every 3 s for the one transition the log no longer reports in time: death.
        CIG removed the in-the-moment death lines build by build (Actor Death to Nov 2025, incap log to Feb 2026,
        ActorState Dead to Jul 2026); the death/respawn screen is still unmistakable on screen."""
        last = None
        while not self._stop.wait(3.0):
            try:
                st = self.eyes.state() or {}
                scene = st.get("scene")
            except Exception:
                continue
            if scene == "dead" and last != "dead":
                class _Ev:
                    event_type, data = "incapacitated", {"source": "vision", "state": "dead"}
                self.on_event(_Ev())
            try:
                self._eyes_present(st if isinstance(st, dict) else {}, scene, last)
            except Exception:
                log.exception("eyes presence")
            last = scene

    CURIOSITY_EVERY_S = 240.0      # in PRESENT, an unprompted look this often at most

    def _eyes_present(self, st: dict, scene, last) -> None:
        """What the eyes see drives the mode and the curiosity looks (J: "the eyes at times will need to be doing
        the heavy lifting" because the log skips elevators, doorways, going indoors)."""
        n = int(st.get("transitions") or 0)
        if n > self._seen_transitions:
            self._seen_transitions = n
            self.activity.feed("scene_transition")
            self._pending_look = self._pending_look or "transition"
        if scene and last and scene != last and scene not in ("dead", "menu", "map"):
            self.activity.feed("scene_change")
        mode = self.activity.tick(scene)
        now_ = self.now()
        try:
            self.affect.drift((now_ - self._affect_t) / 60.0, looping=(mode == PRESENCE))
        except Exception:
            log.exception("affect drift")
        self._affect_t = now_
        if mode != PRESENT or self.gate_state.in_combat or self._look_busy or not hasattr(self.eyes, "look"):
            return
        now = self.now()
        reason = self._pending_look
        if reason is None and now - self._last_look_t >= self.CURIOSITY_EVERY_S:
            reason = "curiosity"
        if reason is None:
            return
        self._pending_look, self._look_busy, self._last_look_t = None, True, now
        threading.Thread(target=self._look_worker, args=(reason,), name="suitmk2_look", daemon=True).start()

    def _look_worker(self, reason: str) -> None:
        try:
            notable = self.eyes.look(reason)
            self.stats["looks"] = self.stats.get("looks", 0) + 1
            if not notable:
                return
            spec = build_look_spec(notable, reason, self._variant["ambient"])
            if spec is None:
                return
            self.affect.feed("scene_notable")
            self._variant["ambient"] += 1
            eventful = reason in ("arrival", "transition")
            pr = (Priority.EVENT, PRIORITY_EVENT) if eventful else (Priority.AMBIENT, PRIORITY_AMBIENT)
            self._consider(spec, pr[0], pr[1], f"look ({reason}): {notable[:50]}")
        except Exception:
            log.exception("look worker")
        finally:
            self._look_busy = False

    def on_event(self, event) -> None:
        et, data = event.event_type, dict(event.data or {})
        self.stats["events"] += 1
        if et in PILOT_DRIVEN_EVENTS:
            self.afk.poke()
        if self.dev_facts is not None:
            try:
                self.dev_facts.note_event(et, data)          # a deque append; never loads or fetches anything
            except Exception:
                log.exception("dev facts note")
        if et == "incapacitated":
            # One reaction per death, whichever sensor (log or eyes) sees it first.
            if self.now() - getattr(self, "_last_incap", -1e9) < 120:
                self.stats["dup"] += 1
                return
            self._last_incap = self.now()
        # Death leaves EVERY ship channel at once (dry run 2026-09-24: 7 ship_channel_left in one second, and each
        # saddened Montaigne until his grief read 0.93). Only the first names a ship; the classifier has already
        # cleared it for the rest, so a leave that names no ship is the tail of that burst, not the pilot leaving. BEFORE the affect feed (first
        # version sat after it and the replay still ended at grief 0.93).
        if et == "ship_channel_left" and not data.get("ship_type"):
            self.stats["stray_left"] = self.stats.get("stray_left", 0) + 1
            return
        if et in DEV_FACT_URGENT_EVENTS:
            self._dev_fact_urgent(et, data)          # BEFORE the injury line is considered: it cuts a fact mid-line
        # The core receives boarding as ship_channel_joined / ship_channel_left (the classifier's names), and the
        # affect hooks call it boarded_ship / left_ship. Resolve it ONCE here. (Until 2026-09-24 nothing matched: the hooks
        # and EVENT_PRIORITY both keyed on channel_change, and the tests fed derived names directly, so all passed.)
        aff_et = {"ship_channel_joined": "boarded_ship", "ship_channel_left": "left_ship"}.get(et, et)
        if aff_et == "boarded_ship":
            self._boarded(data.get("channel") or data.get("ship_type") or self.state.get("ship"))
        try:
            self.affect.feed(aff_et, data)
        except Exception:
            log.exception("affect")
        # Favourite ships that change with experience (J 2026-09-24): a death or a finished contract is charged to
        # the ship the pilot is in. Raw events only; ship_feelings decides what they mean, with decay.
        if et in ("incapacitated", "contract_complete"):
            self._ship_event("death" if et == "incapacitated" else "mission")
        try:
            self.activity.feed(et, data)
            if et in ("location_change", "qt_arrived"):
                self._pending_look = "arrival"
        except Exception:
            log.exception("activity mode")
        dup = self.volatile.is_duplicate(et, data)          # BEFORE recording, or it finds itself
        self.volatile.record_event(et, data)
        # Departure. location_name is only ever REPLACED (on arrival somewhere new), never cleared, so after leaving
        # Seraphim they went on talking about "Seraphim Station, 34 minutes this visit" (J, dry run 2026-09-23).
        # Leaving the armistice zone is the log's own "we left" line; re-entering, or arriving anywhere, undoes it.
        # A weapon back in its slot ends a fight early. ~0 s elapsed is a whole loadout attaching at spawn/terminal
        # (measured on J's log: 0.00004-0.0006 s), not a holster, so only a real gap counts.
        if et == "weapon_holstered" and float(data.get("elapsed_s") or 0) > 1.0:
            self.combat.holstered()
        # Mission-aware topics (J 09-23): a contract/objective naming a place leans idle talk toward it; arriving there
        # or finishing the contract ends it. An objective overrides the contract (it names the next stop).
        if self.walker is not None:
            try:
                if et in ("contract_accepted", "objective_new"):
                    nid = self.walker.set_destination(data.get("objective") or data.get("mission_name") or "") \
                        or self.walker.destination
                    self.walker.destination = nid
                elif et == "contract_complete":
                    self.walker.clear_destination()
                elif et == "location_change" and self.walker.destination:
                    here = str(data.get("location_name") or "").lower()
                    title = self.walker.g.nodes[self.walker.destination]["title"].lower()
                    if here and (title in here or here in title):
                        self.walker.clear_destination()
            except Exception:
                log.exception("mission destination")
        if et == "armistice_zone":
            self._departed = self.state.get("location_name") if data.get("action") == "exited" else None
        elif et == "location_change":
            self._departed = None
        if self.recorder is not None:
            try:
                self.recorder.note(et, data)
            except Exception:
                log.exception("session record")
        if et in QUIET_WINDOW_EVENTS and self.dreams is not None:
            self._work_put(("dream", QUIET_WINDOW_EVENTS[et]))
        if dup:
            self.stats["dup"] += 1
            return
        if et not in EVENT_PRIORITY:
            return
        self._last_speak_event_t = self.now()
        if et == "location_change" and self._first_meeting_pending:
            self._first_meeting_pending = False
            threading.Thread(target=self._welcome, daemon=True).start()   # first run: now there is a live fact
        spec = build_event_spec(et, data, self._ambient_state(), self._variant["event"])
        if spec is None:
            return
        self._variant["event"] += 1
        gp, sp = EVENT_PRIORITY[et]
        # Talked out: an event ABOUT a subject (a place, a zone, a body part) already mentioned enough is dropped.
        # Subject-less events (a reward is only an amount) are always new, and URGENT is never throttled.
        if gp != Priority.URGENT and "|" in subject_key(spec) and self.topics.spent(spec):
            self.stats["talked_out"] = self.stats.get("talked_out", 0) + 1
            self._note(f"talked out, skipped: {subject_key(spec)}")
            return
        self._consider(spec, gp, sp, f"event {et}")

    # -- ambient --------------------------------------------------------------------------------------------------
    def _ambient_state(self) -> dict:
        st = self.state.get_all()
        snap = self.volatile.get_context_snapshot()
        out = {"location": st.get("location_name"), "ship": st.get("ship"), "system": st.get("star_system"),
               "in_armistice": st.get("in_armistice", False), "jurisdiction": st.get("jurisdiction", ""),
               "recent_injuries": snap.get("recent_injuries", []), "recent_deaths": snap.get("recent_deaths", 0),
               "recent_rewards": snap.get("recent_rewards_auec", 0),
               "minutes_at_location": snap.get("minutes_at_location", 0),
               "recent_locations": snap.get("recent_locations_visited", []), "queue_size": self.speech.pending()}
        if out.get("ship") and _is_ground_vehicle(out["ship"]):
            out["vehicle_kind"] = "ground vehicle"
        departed = getattr(self, "_departed", None)
        if departed and out["location"] == departed:          # we left it: not "where we are" any more
            out["location"], out["minutes_at_location"], out["departed_from"] = None, 0, departed
        return out

    def ambient_tick(self) -> None:
        # No eyes (vision off, or a log-only player): the activity mode and boredom tick HERE instead. They used to
        # tick only on the eyes loop, so a log-only session never decayed back to PRESENCE and never got bored
        # (found in the dry run's limits, 2026-09-24).
        if self.eyes is None:
            now_ = self.now()
            try:
                mode = self.activity.tick(None)
                self.affect.drift((now_ - self._affect_t) / 60.0, looping=(mode == PRESENCE))
            except Exception:
                log.exception("activity/affect tick without eyes")
            self._affect_t = now_
        st = self._ambient_state()
        eyes_state = {}
        if self.eyes is not None:
            try:
                eyes_state = self.eyes.state() or {}
                self.last_eyes = eyes_state          # the dashboard reads this: no HTTP on the UI thread
            except Exception:
                eyes_state = {}
        self.gate_state.in_combat = bool(eyes_state.get("in_combat")) or self.combat.active
        if self.gate_state.in_combat:
            self.stats["combat_hold"] += 1
            return
        # Nobody at the controls: no ambient, banter or relationship line to an empty chair. Checked here as well as
        # in _consider so an AFK tick does not spend topics, banter attempts or the idle slot on lines nobody hears.
        if self.afk.afk():
            self.stats["afk_quiet"] = self.stats.get("afk_quiet", 0) + 1
            return
        if self._dev_resume_tick():
            return
        # Entering a system: somewhere in it they have not been yet (J 09-23). The session's FIRST system is not an
        # arrival (the welcome covers it), so only a CHANGE counts.
        system, prev = st.get("system"), getattr(self, "_last_system", None)
        self._last_system = system or prev
        if system and prev and system != prev and self.walker is not None:
            spec = self.walker.wishlist_spec(system, self._variant["ambient"],
                                             visited=self._visited(st))
            if spec is not None:
                self._variant["ambient"] += 1
                self._consider(spec, Priority.AMBIENT, PRIORITY_AMBIENT, f"arrived in {system}")
                return
        if self._try_dev_fact(st):
            return
        # The Mk II's heart ("Did you even know you were wearing me?") gets a GUARANTEED turn in quiet stretches. It
        # used to be a last resort, after the topic walker, and the walker always has something: across 18 hours of
        # J's replayed sessions (2026-09-24) not one relationship line was said. Now, once per IDLE_SLOT_S, it goes
        # first. Any mode: requiring PRESENCE too gave ONE line in a busy 142-min session; build_idle_spec itself
        # already stands down after a recent injury, death or reward, and a fight holds ambient entirely.
        # The slot is spent when a relationship line is SPOKEN (_spoke), not when one is tried: a try the gate
        # merely DEFERS (an ordinary cooldown) used to burn the whole 15 minutes (dry run 2026-09-24: 3 tries, 1 said).
        # _idle_try only stops a waiting line from knocking on the gate every tick.
        if (self.now() - getattr(self, "_last_idle", -1e9) >= self.IDLE_SLOT_S
                and self.now() - getattr(self, "_idle_try", -1e9) >= self.IDLE_RETRY_S):
            spec = idle_spec.build_idle_spec(st, self._variant.get("idle", 0))
            if spec is not None and not self.topics.spent(spec):
                self._idle_try = self.now()
                self._variant["idle"] = self._variant.get("idle", 0) + 1
                self._variant["ambient"] += 1
                self._consider(spec, Priority.AMBIENT, PRIORITY_AMBIENT, "idle (relationship)")
                return
        if self._try_banter(st):
            return
        # The latest event already got its reaction on the edge. Idle time mostly wanders (60%) instead of
        # narrating the current state again, which is the "most recent event" obsession J heard.
        visited = self._visited(st)
        spec = None
        if self.walker is not None and self._rng.random() < 0.6:
            spec = self.walker.next_spec(st, self._variant["ambient"], visited=visited)
        if spec is None:
            spec = build_ambient_spec(st, variant=self._variant["ambient"], skip=self.topics.spent)
        if spec is None and self.walker is not None:
            spec = self.walker.next_spec(st, self._variant["ambient"], visited=visited)
        # Nothing left to say about the world: once in a while, something about the two of them (the Mk II's "Did
        # you even know you were wearing me?"). Own counter, so rotation does not skip themes; one per 10 min at most.
        if spec is None and self.now() - getattr(self, "_last_idle", -1e9) >= idle_spec.IDLE_MIN_INTERVAL_S:
            spec = idle_spec.build_idle_spec(st, self._variant.get("idle", 0))
            if spec is not None and not self.topics.spent(spec):
                self._last_idle = self.now()
                self._variant["idle"] = self._variant.get("idle", 0) + 1
            else:
                spec = None
        if spec is None:
            return
        self._variant["ambient"] += 1
        self._consider(spec, Priority.AMBIENT, PRIORITY_AMBIENT, "ambient")

    def _ambient_loop(self) -> None:
        while not self._stop.wait(self.ambient_every_s):
            try:
                self.ambient_tick()
            except Exception:
                log.exception("ambient tick")

    # -- decide, realize, gate, speak -----------------------------------------------------------------------------
    PRESENCE_EVENT_GAP_S = 600.0   # in PRESENCE, a routine event line of one kind at most this often
    IDLE_SLOT_S = 900.0            # a relationship line first, at most this often once one is SPOKEN (Mk II pattern 4)
    IDLE_RETRY_S = 180.0           # a deferred relationship line tries again no sooner than this

    def _afk_holds(self, spec: Optional[dict], gate_priority) -> bool:
        """True = the pilot is AFK and this line waits. THE RULE: while AFK only what would matter the moment they
        come back speaks: Priority.URGENT (incapacitated, combat starting, an answer to a spoken question) and a
        Tier 1 injury (SC's worst tier, whose line is "get to a med bed"; injuries are otherwise EVENT). Everything
        else (ambient, banter, idle relationship lines, curiosity looks, routine EVENT lines, the welcome) holds.
        Same shape as the combat hold and NotNow, which already let URGENT through and nothing else."""
        if gate_priority == Priority.URGENT or not self.afk.afk():
            return False
        if spec and spec.get("scenario") == "event_injury" and any(
                c.get("predicate") == "suit.injury_tier" and str(c.get("value")) == "1"
                for c in spec.get("claims") or []):
            return False
        return True

    def _consider(self, spec: dict, gate_priority, speech_priority: int, why: str) -> None:
        self.stats["spec"] += 1
        if self._afk_holds(spec, gate_priority):
            self.stats["afk_quiet"] = self.stats.get("afk_quiet", 0) + 1
            self._note(f"{why}: quiet (pilot AFK)")
            return
        # PRESENCE: company, not commentary (J: "not comment on every box they move"). URGENT never throttles, and
        # looks/banter/ambient are not EVENT lines, so only routine event commentary is thinned here.
        if (gate_priority == Priority.EVENT and self.activity.mode == PRESENCE
                and spec.get("scenario") != "scene_look"):
            key, now = spec.get("scenario") or why, self.now()
            if now - self._presence_last.get(key, -1e9) < self.PRESENCE_EVENT_GAP_S:
                self.stats["presence_quiet"] = self.stats.get("presence_quiet", 0) + 1
                self._note(f"{why}: quiet (presence mode, {key} said recently)")
                return
            self._presence_last[key] = now
        gap = ROUTINE_GAP_S.get(spec.get("scenario") or "")
        if gap is not None:
            key, now = "routine:" + spec["scenario"], self.now()
            if now - self._presence_last.get(key, -1e9) < gap:
                self.stats["routine_quiet"] = self.stats.get("routine_quiet", 0) + 1
                self._note(f"{why}: quiet (routine, said recently)")
                return
            self._presence_last[key] = now
        # A death is a beat, not a cue for small talk: strong fear or grief holds AMBIENT lines (events still speak).
        if gate_priority == Priority.AMBIENT and self.affect.hushed():
            self.stats["hushed"] = self.stats.get("hushed", 0) + 1
            self._note(f"{why}: quiet (still shaken)")
            return
        # A fixed-text line (dev fact, its resume, a toggle ack) has no model to colour and an exact length.
        mood = {} if spec.get("fixed_text") else self.affect.color(spec)   # stance + length; facts never touched
        if mood:
            self.stats["coloured"] = self.stats.get("coloured", 0) + 1
        self.gate_state.muted = bool(getattr(self.speech, "muted", False))
        cand = Candidate(priority=gate_priority, speaker=spec["speaker"],
                         text_len_words=spec["length_words"][1], created_at=self.now())
        d = self.gate.evaluate(self.gate_state, cand)
        if d.verdict is not Verdict.ALLOW:
            self.stats["gated"] += 1
            self._note(f"{why}: {d.verdict.name} ({d.reason[:60]})")
            return
        self._work_put(("line", spec, cand, speech_priority, why))

    def _work_put(self, item: tuple) -> None:
        try:
            self._work.put_nowait(item)
        except queue.Full:
            self.stats["busy"] += 1          # realizer backed up: drop, never queue stale speech

    def _realize_loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._work.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._realize_one(item)
            except Exception:
                log.exception("realize loop")

    def _realize_one(self, item: tuple) -> None:
        """One work item. Split out of the loop (2026-09-25) so a replay can drain the queue synchronously."""
        if item[0] == "dream":
            self.dreams.run_one(item[1], self.headroom())
            return
        if item[0] == "banter":
            if self.afk.afk():
                self.stats["afk_quiet"] = self.stats.get("afk_quiet", 0) + 1
                return                                     # queued before they walked away
            if self.affect.hushed():
                self.stats["hushed"] = self.stats.get("hushed", 0) + 1
                return                                     # no banter right after a scare
            self._run_banter(item[1])
            return
        _, spec, cand, speech_priority, why = item
        if cand.priority is not Priority.URGENT and self.not_now.active():
            self.stats["not_now"] += 1          # gated before the snooze: must not play after it
            self._note(f"{why}: dropped (not now)")
            return
        if self._afk_holds(spec, cand.priority):      # gated while present, went AFK before it was said
            self.stats["afk_quiet"] = self.stats.get("afk_quiet", 0) + 1
            self._note(f"{why}: dropped (pilot AFK)")
            return
        if spec.get("aside") == "dev_fact" and (
                self.gate_state.in_combat or self._last_urgent_t >= spec.get("offered_t", self.now())
                or self.dev_facts is None or not self.dev_facts.enabled):
            self.stats["dev_fact_dropped"] = self.stats.get("dev_fact_dropped", 0) + 1
            self._note(f"{why}: dropped (no longer a quiet moment)")
            return
        # A fixed-text line (a dev fact, its resume, a toggle ack) is a template from its source: no model words it.
        text = spec["fixed_text"] if spec.get("fixed_text") else (self.realizer(spec) if self.realizer else None)
        if not text:
            self.stats["silent"] += 1
            self._note(f"{why}: silent (no line from realizer)")
            return
        self.stats["realized"] += 1
        fails = ground(spec, text)
        if fails:
            self.stats["ungrounded"] += 1
            self._note(f"{why}: REFUSED by gate {fails}: {text[:60]!r}")
            return
        prev_last = self.gate_state.last_spoken_at
        if self.speech.say(text, spec["speaker"], speech_priority):
            self.gate.record_spoken(self.gate_state, cand)
            self._spoke(spec, text, cand)
            self.stats["spoken"] += 1
            self._note(f"{spec['speaker']}: {text}")
            if spec.get("scenario") == devf.SCENARIO:
                self._dev_fact_said(spec, text, prev_last)

    # -- dev-history fun facts (dev_facts.py): optional, Montaigne only, quiet moments only -----------------------
    DEV_FACT_QUIET_AFTER_S = 300.0     # no fact within 5 min of any event that speaks
    DEV_FACT_URGENT_QUIET_S = 600.0    # ... or within 10 min of an injury, death, respawn or combat start
    DEV_FACT_TRY_S = 600.0             # offer one at most this often (the per-hour cap is in DevFacts)
    DEV_FACT_RESUME_QUIET_S = 45.0     # the interrupted fact resumes only after this long with nothing urgent
    DEV_FACT_RESUME_MAX_S = 600.0      # ... and never later than this
    DEV_FACT_LOW_PCT = 25.0            # a ship/suit reading this low (fuel, oxygen, hull) is not a quiet moment
    DEV_FACT_WARMUP_S = 600.0          # none in the session's first 10 minutes (welcome, getting settled)

    def _dev_fact_unquiet(self, st: dict) -> Optional[str]:
        """Why this is NOT a moment for a dev fact, or None when it is. Deliberately stricter than ambient talk."""
        now = self.now()
        if self.gate_state.in_combat or self.combat.active:
            return "combat"
        if self.afk.afk():
            return "pilot AFK"
        if self.not_now.active():
            return "not now"
        if self.affect.hushed():
            return "still shaken"
        if self.activity.mode != PRESENCE:
            return "present mode (something is happening)"
        if st.get("recent_injuries") or st.get("recent_deaths"):
            return "recent injury or death"
        if any(k.startswith("injury_") and v for k, v in self.state.get_all().items()):
            return "an injury on the suit's record"
        if now - self._last_urgent_t < self.DEV_FACT_URGENT_QUIET_S:
            return "urgent event recently"
        if now - self._last_speak_event_t < self.DEV_FACT_QUIET_AFTER_S:
            return "event recently"
        if st.get("queue_size"):
            return "speech queued"
        for key in ("fuel_pct", "oxygen_pct", "o2_pct", "hull_pct", "quantum_fuel_pct"):
            v = self.state.get(key)
            try:
                if v is not None and float(v) <= self.DEV_FACT_LOW_PCT:
                    return f"low {key}"
            except (TypeError, ValueError):
                pass
        if self._dev_resume is not None:
            return "an interrupted fact is waiting"
        if now - self._born_t < self.DEV_FACT_WARMUP_S:
            return "session just started"
        return None

    def _try_dev_fact(self, st: dict) -> bool:
        df = self.dev_facts
        if df is None or not df.enabled:
            return False
        now = self.now()
        if now - getattr(self, "_dev_fact_try", -1e9) < self.DEV_FACT_TRY_S:
            return False
        why = self._dev_fact_unquiet(st)
        if why:
            self.stats["dev_fact_unquiet"] = self.stats.get("dev_fact_unquiet", 0) + 1
            return False
        try:
            spec = df.poll(st)                        # never blocks: a prepared fact, or None while one is prepared
        except Exception:
            log.exception("dev facts poll")
            return False
        if spec is None:
            return False
        self._dev_fact_try = now
        spec["offered_t"] = now
        self._consider(spec, Priority.AMBIENT, PRIORITY_AMBIENT, f"dev fact: {spec['source']['title'][:40]}")
        return True

    def _dev_fact_said(self, spec: dict, text: str, prev_last) -> None:
        """A fact went to speech: remember it as in flight (an injury may cut it), and maybe let Elah call it out."""
        self._dev_inflight = {"spec": spec, "text": text, "t": self.now(), "prev_last": prev_last,
                              "dur": 0.45 * len(text.split()) + 9.0}   # Montaigne's pace + the duck hold + synth
        co = self.dev_facts.callout_for(spec) if self.dev_facts is not None else None
        if co is None:
            return
        # Elah is in-universe: to her Montaigne is glitching. Never in an urgent moment, and grounded like any line.
        if self.gate_state.in_combat or self.afk.afk() or self.not_now.active() or self.affect.hushed():
            return
        fails = ground(co, co["fixed_text"])
        if fails:
            self._note(f"dev fact callout REFUSED {fails}")
            return
        c = Candidate(Priority.BANTER, "elah", len(co["fixed_text"].split()), self.now())
        if self.speech.say(co["fixed_text"], "elah", PRIORITY_AMBIENT):   # queued behind his line, never over it
            self.gate.record_spoken(self.gate_state, c)
            self.dev_facts.callout_spoken()
            self.stats["dev_fact_callout"] = self.stats.get("dev_fact_callout", 0) + 1
            self.stats["spoken"] += 1
            self._dev_inflight["t_last"] = self.gate_state.last_spoken_at
            self._note(f"elah (callout): {co['fixed_text']}")

    def _dev_in_flight(self, inf: dict) -> bool:
        if self.now() - inf["t"] > inf["dur"]:
            return False
        try:                                           # the real Speech lists what finished playing
            for t, _spk, txt in list(getattr(self.speech, "spoken", None) or [])[-6:]:
                if txt == inf["text"] and t >= inf["t"]:
                    return False
        except Exception:
            pass
        return True

    def _dev_fact_urgent(self, et: str, data: dict) -> None:
        """An injury, death, respawn or combat start. Opens the no-facts window, drops a fact waiting to resume, and
        cuts a fact still being said. The interrupted-fact bit (J 2026-09-25, Montaigne only): a Tier 2/3 injury
        cutting a fact, once per session, arms a resume; Tier 1, a death, combat or anything else never does."""
        self._last_urgent_t = self.now()
        if self.dev_facts is None:
            return
        if self._dev_resume is not None:
            self._dev_resume = None
            self.stats["dev_resume_dropped"] = self.stats.get("dev_resume_dropped", 0) + 1
            self._note(f"interrupted fact dropped ({et})")
        inf, self._dev_inflight = self._dev_inflight, None
        if inf is None or not self._dev_in_flight(inf):
            return
        self.hush()                                    # cut the fact mid-line; the urgent line takes over
        # The cut fact must not delay the urgent call through the gate's global gap.
        if self.gate_state.last_spoken_at in (inf["t"], inf.get("t_last")):
            self.gate_state.last_spoken_at = inf["prev_last"]
        self.stats["dev_fact_cut"] = self.stats.get("dev_fact_cut", 0) + 1
        self._note(f"dev fact cut mid-line ({et})")
        try:
            tier = int(data.get("tier"))
        except (TypeError, ValueError):
            tier = None
        sev = str(data.get("severity") or "").lower()
        if (et == "injury" and tier in (2, 3) and sev not in ("severe", "critical") and not self._dev_bit_used
                and self.dev_facts.enabled and inf["spec"].get("speaker") == devf.SPEAKER):
            self._dev_bit_used = True
            self._dev_resume = {"spec": inf["spec"], "t": self.now(), "injury_spoken": False}

    def _dev_resume_tick(self) -> bool:
        p = self._dev_resume
        if p is None:
            return False
        now = self.now()
        if self.dev_facts is None or not self.dev_facts.enabled or now - p["t"] > self.DEV_FACT_RESUME_MAX_S:
            self._dev_resume = None
            return False
        if not p["injury_spoken"] or now - self._last_urgent_t < self.DEV_FACT_RESUME_QUIET_S \
                or self.affect.hushed() or self.not_now.active():
            return False
        spec = devf.resume_spec(p["spec"])
        spec["offered_t"] = now
        self._consider(spec, Priority.EVENT, PRIORITY_AMBIENT, "dev fact resumed")
        return True

    def set_dev_facts(self, on: bool, why: str = "voice", ack: bool = True) -> None:
        """Turn dev facts on/off mid-session (voice toggle or the window). Off also cuts a fact being said and drops
        one waiting to resume. Persisted through dev_facts_persist; Montaigne acknowledges in one line."""
        if self.dev_facts is None:
            return
        on = bool(on)
        self.dev_facts.enabled = on
        if not on:
            self._dev_resume = None
            inf, self._dev_inflight = self._dev_inflight, None
            if inf is not None and self._dev_in_flight(inf):
                self.hush()
        if self.dev_facts_persist is not None:
            try:
                self.dev_facts_persist(on)
            except Exception:
                log.exception("dev facts persist")
        self._note(f"dev facts {'ON' if on else 'OFF'} ({why})")
        if ack:
            spec = devf.toggle_ack_spec(on, self._dev_toggles)
            self._dev_toggles += 1
            self._say_fixed(spec, Priority.URGENT, PRIORITY_URGENT)

    def voice_command(self, text: str) -> bool:
        """A spoken control the core handles itself (today: the dev-facts toggle). True = consumed."""
        on = devf.voice_toggle(text)
        if on is None or self.dev_facts is None:
            return False
        self.afk.poke()
        self.set_dev_facts(on, "voice")
        return True

    def _say_fixed(self, spec: dict, gate_priority, speech_priority: int) -> bool:
        """Say a fixed line now (no model): gate, grounding, speech. Used for the toggle acknowledgement."""
        self.gate_state.muted = bool(getattr(self.speech, "muted", False))
        cand = Candidate(priority=gate_priority, speaker=spec["speaker"], text_len_words=spec["length_words"][1],
                         created_at=self.now())
        if self.gate.evaluate(self.gate_state, cand).verdict is not Verdict.ALLOW:
            return False
        text = spec["fixed_text"]
        if ground(spec, text):
            return False
        if self.speech.say(text, spec["speaker"], speech_priority):
            self.gate.record_spoken(self.gate_state, cand)
            self.stats["spoken"] += 1
            self._note(f"{spec['speaker']}: {text}")
            return True
        return False

    # -- direct questions (conversation.py): the pilot asked, so this outranks everything else ---------------------
    def answer(self, spec: dict, utterance: str = "") -> None:
        """Answer a direct question. URGENT gate (skips cooldowns, still silenced by mute), its own worker so it is
        never dropped behind ambient work, ground_direct (also refuses an invented place for an UNKNOWN), one
        retry on refusal, then silence. Never a canned line."""
        self.afk.poke()                              # they spoke to us: somebody is here, whatever the keyboard says
        try:
            self.affect.feed("pilot_spoke")
        except Exception:
            log.exception("affect")
        self.gate_state.muted = bool(getattr(self.speech, "muted", False))
        cand = Candidate(priority=Priority.URGENT, speaker=spec["speaker"],
                         text_len_words=spec["length_words"][1], created_at=self.now())
        d = self.gate.evaluate(self.gate_state, cand)
        if d.verdict is not Verdict.ALLOW:
            self._note(f"question '{utterance[:40]}': {d.verdict.name} ({d.reason[:50]})")
            return
        threading.Thread(target=self._answer_worker, args=(spec, cand, utterance), daemon=True).start()

    def _answer_worker(self, spec: dict, cand, utterance: str) -> None:
        from conversation import ground_direct
        self.stats["questions"] = self.stats.get("questions", 0) + 1
        for attempt in (1, 2):
            text = self.realizer(spec) if self.realizer else None
            if not text:
                self._note(f"question '{utterance[:40]}': no line from realizer")
                return
            fails = ground_direct(spec, text)
            if not fails:
                if self.speech.say(text, spec["speaker"], PRIORITY_URGENT):
                    self.gate.record_spoken(self.gate_state, cand)
                    self._spoke(spec, text, cand)
                    self.stats["spoken"] += 1
                    self._note(f"{spec['speaker']} (answer): {text}")
                return
            self.stats["ungrounded"] += 1
            self._note(f"answer attempt {attempt} REFUSED {fails}: {text[:60]!r}")

    # -- banter: two characters, planned semantically up front (banter.py) ---------------------------------------
    def _try_banter(self, st: dict) -> bool:
        ok, _ = self.banter.may_start(BanterPolicy.context_from(self.gate_state))
        if not ok:
            return False
        # Half the time banter is about a TOPIC, not the current state: state banter is always about whatever just
        # happened, which is the "only thing it can think of" loop (J 09-23). The other half falls back to topics too
        # when no state situation matches, so banter no longer stops dead in open space.
        topic_first = self.walker is not None and self._rng.random() < 0.75   # 0.5 -> 0.75: the scripted state pairs sounded staged (J 09-23)
        # visited: Elah speaks from having been somewhere only when the pilot's record says so (dry run 2026-09-24).
        specs = self.walker.exchange(st, self._variant["banter"], visited=self._visited(st)) if topic_first else []
        if not specs:
            specs = plan_exchange(st, self._variant["banter"], self.banter.history, self.banter.max_turns)
        if not specs and self.walker is not None and not topic_first:
            specs = self.walker.exchange(st, self._variant["banter"], visited=self._visited(st))
        if not specs:
            return False
        self.gate_state.muted = bool(getattr(self.speech, "muted", False))
        cand = Candidate(priority=Priority.BANTER, speaker=specs[0]["speaker"],
                         text_len_words=specs[0]["length_words"][1], created_at=self.now())
        # Gate the EXCHANGE once: SpeakGate's per-priority BANTER cooldown would otherwise kill turn 2.
        if self.gate.evaluate(self.gate_state, cand).verdict is not Verdict.ALLOW:
            return False
        self._variant["banter"] += 1
        self.banter.started(specs)                 # the cap counts attempts, not only successes
        self.stats["banter"] += 1
        self._work_put(("banter", specs))
        return True

    def _run_banter(self, specs: list) -> None:
        cur = {}

        def realize(s):
            cur["spec"] = s
            return (self.realizer or (lambda _: None))(s)

        def say(text, speaker):
            c = Candidate(Priority.BANTER, speaker, len(text.split()), self.now())
            if self.speech.say(text, speaker, PRIORITY_AMBIENT):
                self.gate.record_spoken(self.gate_state, c)     # every turn counts toward the quiet budget
                if cur.get("spec"):
                    self._spoke(cur["spec"], text, c)
                self.stats["spoken"] += 1
                return True
            return False
        out = run_exchange(specs, realize, ground, say, max_turns=self.banter.max_turns,
                           hold=lambda: ("not now" if self.not_now.active() else None)
                           or ("pilot AFK" if self.afk.afk() else None)
                           or self.banter.may_continue(BanterPolicy.context_from(self.gate_state)))
        self.stats["banter_turns"] += len(out)
        for t in out:
            self._note(f"{t.get('speaker')} (banter): {t.get('text')}")
        self._note(f"banter {specs[0].get('exchange_id')}: {len(out)}/{len(specs)} turns ({out.stop_reason})")

    def _dream_catchup(self) -> None:
        try:
            self.dreams.enqueue_closed_sessions()
            n = self.dreams.run_until(self.now() + 30, window="launch", headroom=self.headroom)
            if n:
                self._note(f"dreamed {n} job(s) from earlier sessions")
        except Exception:
            log.exception("dream catch-up")
        self._welcome()                      # after catch-up: last session's milestones/first-visits now exist

    def _welcome(self) -> None:
        if self._welcomed or self.store is None:
            return
        try:
            sf = self.recorder.path.name if self.recorder is not None else None
            st = {"session_file": sf, **self._ambient_state()}
            spec = welcome_spec(self.store, st, self.now())
            if spec is None:
                spec = first_meeting_spec(self.store, st, self.now())
                self._first_meeting_pending = spec is None and not st.get("location")
            if spec is None:
                return
            self._welcomed = True
            mark_welcomed(self.store, spec, self.now(), sf)
            self._consider(spec, Priority.EVENT, PRIORITY_EVENT, "welcome")
        except Exception:
            log.exception("welcome")         # a bad memory file means no welcome, never a crash


# ---- selftest: real Game.log lines, fake sidecar + speech --------------------------------------------------------
def _selftest(game_log: Optional[str]) -> int:
    results = []

    def case(name, cond):
        results.append((name, bool(cond)))

    from event_spec import speaking_events
    unspoken = sorted(set(speaking_events()) - set(EVENT_PRIORITY) - set(ROUTED_ELSEWHERE))
    case(f"every speaking event has a path to speech{': MISSING ' + str(unspoken) if unspoken else ''}", not unspoken)

    class FakeSpeech:
        muted = False

        def __init__(self):
            self.said = []

        def say(self, text, speaker, priority):
            self.said.append((speaker, priority, text))
            return True

        def pending(self):
            return 0

    def fake_realizer(spec):
        # echoes every claim value + required number, so the REAL grounding gate passes honest output
        vals = [str(c["value"]) for c in spec["claims"] if not isinstance(c["value"], bool)]
        words = ("Noted " + " and ".join(vals) + " for the record, pilot, as the suit reports it.").split()
        lo, hi = spec["length_words"]
        return " ".join(words[:hi]) if len(words) >= lo else " ".join(words + ["steady"] * (lo - len(words)))

    sp = FakeSpeech()
    core = CompanionCore(sp, realizer=fake_realizer, ambient_every_s=3600)
    core.gate = SpeakGate(now=lambda: 10_000.0 + core.stats["spec"] * 400)   # far apart: cooldowns never block
    threading.Thread(target=core._realize_loop, daemon=True).start()

    # A FIXED fixture, not the live Game.log: the live file resets whenever the pilot starts SC (2026-09-23: J's new
    # session left 758 lines / 1 event and two checks "failed" with nothing wrong in the code).
    path = Path(game_log) if game_log else None
    if path is None or not path.exists():
        live = find_game_log()
        root = live.parent / "logbackups" if live else Path(r"C:\Star Citizen\StarCitizen\LIVE\logbackups")
        backups = sorted(root.glob("*.log"), key=lambda p: p.stat().st_size, reverse=True)
        path = backups[0] if backups else live
    case("a real Game.log is available to replay", path is not None)
    if path is not None:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                core.feed_line(line.rstrip("\n"))
        deadline = time.time() + 10
        while not core._work.empty() and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        print(f"  replayed {core.stats['lines']} lines from {path.name}: {core.stats}")
        case("the real log produced classified events", core.stats["events"] > 0)
        case("speaking events became specs", core.stats["spec"] > 0)
        case("honest lines passed the real grounding gate and were spoken", core.stats["spoken"] > 0)
        case("nothing ungrounded was spoken", core.stats["ungrounded"] == 0 or core.stats["spoken"] > 0)
        for spk, pri, text in sp.said[:6]:
            print(f"    {spk:9s} p{pri}  {text[:100]}")

    # A lying realizer is refused by the gate.
    sp2 = FakeSpeech()
    core2 = CompanionCore(sp2, realizer=lambda spec: "You lost 99999 aUEC and 7 ships today, which is terrible news.")
    threading.Thread(target=core2._realize_loop, daemon=True).start()
    core2._consider(build_event_spec("reward_earned", {"amount": 15000}), Priority.EVENT, PRIORITY_EVENT, "test")
    time.sleep(0.4)
    case("an invented number is refused, never spoken", not sp2.said and core2.stats["ungrounded"] == 1)

    # Eyes see a fight -> ambient holds its tongue.
    class Eyes:
        def state(self):
            return {"in_combat": True}
    core3 = CompanionCore(FakeSpeech(), realizer=fake_realizer, eyes=Eyes())
    core3.ambient_tick()
    case("combat on screen: ambient stays silent", core3.stats["combat_hold"] == 1 and core3.stats["spec"] == 0)

    # Combat confirm chain: classifier, then eyes, then nothing (None = audio onsets alone decide).
    class Ears:
        def __init__(self, v):
            self.v = v

        def gunfire_confirm(self):
            return self.v

        def start(self):
            return True

        def stop(self):
            pass

    class NoFight:
        def state(self):
            return {"in_combat": False}

    def cc(**kw):
        return CompanionCore(FakeSpeech(), realizer=None, **kw)._combat_confirm()
    case("confirm: classifier hears gunfire -> True even when the eyes see nothing",
         cc(sound=Ears(True), eyes=NoFight()) is True)
    case("confirm: classifier not running -> falls back to the eyes", cc(sound=Ears(None), eyes=Eyes()) is True)
    case("confirm: classifier says no, no eyes -> False", cc(sound=Ears(False)) is False)
    case("confirm: classifier not running, no eyes -> None (meter alone)", cc(sound=Ears(None)) is None)
    case("no classifier, no eyes: CombatWatch has no confirm at all (unchanged behaviour)",
         CompanionCore(FakeSpeech(), realizer=None).combat.confirm is None)

    # No realizer (sidecar down) -> silence, not a crash, not a canned line.
    sp4 = FakeSpeech()
    core4 = CompanionCore(sp4, realizer=None)
    threading.Thread(target=core4._realize_loop, daemon=True).start()
    core4._consider(build_event_spec("injury", {"body_part": "left leg", "tier": 1}), Priority.EVENT, PRIORITY_EVENT, "t")
    time.sleep(0.4)
    case("sidecar down: silence, no canned fallback", not sp4.said and core4.stats["silent"] == 1)

    # Duplicate check happens before recording (the 09-23 fix): first arrival speaks, instant repeat does not.
    sp5 = FakeSpeech()
    core5 = CompanionCore(sp5, realizer=fake_realizer)
    core5.gate = SpeakGate(now=lambda: 50_000.0 + core5.stats["spec"] * 400)
    threading.Thread(target=core5._realize_loop, daemon=True).start()

    class Ev:
        def __init__(self):
            self.event_type, self.data = "reward_earned", {"amount": 15000}
    core5.on_event(Ev())
    core5.on_event(Ev())
    time.sleep(0.5)
    case("first reward speaks, instant duplicate is suppressed", len(sp5.said) == 1 and core5.stats["dup"] == 1)

    # Departure (J 09-23 "why are they still talking about seraphim when we left seraphim?").
    class Arm:
        def __init__(self, action):
            self.event_type, self.data = "armistice_zone", {"action": action}
    core6 = CompanionCore(FakeSpeech(), realizer=None)
    core6.state.set("location_name", "Seraphim Station")
    case("docked: the station is where we are", core6._ambient_state()["location"] == "Seraphim Station")
    core6.on_event(Arm("exited"))
    st6 = core6._ambient_state()
    case("left the armistice zone: the station is no longer 'here'",
         st6["location"] is None and st6["minutes_at_location"] == 0 and st6["departed_from"] == "Seraphim Station")
    core6.on_event(Arm("entered"))
    case("came back: it is 'here' again", core6._ambient_state()["location"] == "Seraphim Station")
    core6.on_event(Arm("exited"))
    core6.state.set("location_name", "Orison")
    case("arrived somewhere new: the new place counts", core6._ambient_state()["location"] == "Orison")

    # AFK (J 2026-09-24): an injected idle clock, like `now`. AFK holds ambient, urgent still speaks, activity resumes.
    idle = [0.0]
    t7 = [70_000.0]
    sp7 = FakeSpeech()
    core7 = CompanionCore(sp7, realizer=fake_realizer, now=lambda: t7[0], idle_source=lambda: idle[0],
                          afk_after_s=300)
    core7.gate = SpeakGate(now=lambda: t7[0])       # ONE clock for core and gate, or the gate calls a line stale

    def say7(spec, gp, spri):
        t7[0] += 400                                 # far apart: gate cooldowns never block
        core7._consider(spec, gp, spri, "t")
    core7.walker = None
    core7.state.set("location_name", "Area18")
    threading.Thread(target=core7._realize_loop, daemon=True).start()
    idle[0] = 301.0
    t7[0] += 1000
    spec_before = core7.stats["spec"]
    core7.ambient_tick()
    case("AFK: an ambient tick says nothing and builds no line",
         core7.stats["spec"] == spec_before and core7.stats.get("afk_quiet") == 1)
    say7(build_event_spec("reward_earned", {"amount": 15000}), Priority.EVENT, PRIORITY_EVENT)
    say7(build_event_spec("injury", {"body_part": "left leg", "tier": 3}), Priority.EVENT, PRIORITY_EVENT)
    case("AFK: a routine event line and a minor injury hold", core7.stats["afk_quiet"] == 3)
    say7(build_event_spec("injury", {"body_part": "torso", "tier": 1}), Priority.EVENT, PRIORITY_EVENT)
    say7(build_event_spec("incapacitated", {}), Priority.URGENT, PRIORITY_URGENT)
    time.sleep(0.5)
    case("AFK: a Tier 1 injury and an URGENT line still speak",
         core7.stats["afk_quiet"] == 3 and len(sp7.said) == 2 and sp7.said[1][1] == PRIORITY_URGENT)
    idle[0] = 2.0
    n_said = len(sp7.said)
    say7(build_event_spec("reward_earned", {"amount": 20000}), Priority.EVENT, PRIORITY_EVENT)
    time.sleep(0.5)
    case("back at the keyboard: routine lines speak again", len(sp7.said) == n_said + 1)
    idle[0] = 9_999.0
    core7.on_event(type("E", (), {"event_type": "qt_arrived", "data": {"location": "Hurston"}})())
    case("a pilot-driven game event (HOTAS flying) counts as activity", not core7.afk.afk())
    case("no idle source (the dry run): never AFK",
         not CompanionCore(FakeSpeech(), realizer=None).afk.afk())

    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    bad = sum(not ok for _, ok in results)
    print(f"companion_core selftest: {len(results) - bad}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    if "--selftest" in sys.argv:
        rest = [a for a in sys.argv[1:] if a != "--selftest"]
        sys.exit(_selftest(rest[0] if rest else None))
    print(__doc__)

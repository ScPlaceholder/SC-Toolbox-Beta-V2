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
import npc_factions                                                        # noqa: E402
from refinery_tracker import RefineryTracker                               # noqa: E402
from bdl_tracker import BdlTracker                                         # noqa: E402
import manufacturers                                                       # noqa: E402
import place_flavour                                                       # noqa: E402
from contract_history import ContractHistory                               # noqa: E402
from settings import DEFAULTS as _SETTING_DEFAULTS                         # noqa: E402

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
                    "out_of_mags": "synthesised by _loadout_changed() when the last spare magazine goes",
                    "refinery_pickup": "synthesised by on_event on arrival where refinery_tracker has an open order",
                    "bdl_warning": "synthesised by feed_line from bdl_tracker (med pen use, an estimate)",
                    "bdl_clear": "synthesised by feed_line from bdl_tracker when the estimate falls back"}
# AFK (J 2026-09-24): events the pilot had to DO, which the OS idle clock cannot see when they fly on a HOTAS or a
# gamepad. Each one counts as activity for the AFK window (AfkWatch.poke). Rewards, objectives and injuries are not
# here: the game hands those out whether or not anyone is at the controls.
PILOT_DRIVEN_EVENTS = {"location_change", "qt_arrived", "jurisdiction_change", "docking_detached", "player_respawned",
                       "contract_accepted", "ship_channel_joined", "weapon_holstered"}
# Dev facts (dev_facts.py): these cut a fact that is still being said, and open a no-facts window.
DEV_FACT_URGENT_EVENTS = {"injury", "incapacitated", "player_respawned"}
QUIET_WINDOW_EVENTS = {"qt_route_calculated": "quantum", "qt_target_selected": "quantum",
                       "hangar_ready": "parked", "docking_ready": "parked"}
# Optional April-spec features (settings.py). The core reads only these keys from the settings it is handed; a core
# built with no settings (the selftests, the dry run) gets the shipped defaults.
FEATURE_KEYS = ("npc_faction_names", "refinery_tracker", "bdl_tracker", "manufacturer_flavour", "place_flavour",
                "contract_history")


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
    try:                                  # the launcher's shared install root, if linked
        from shared.sc_install import get_sc_root, newest_game_log
        shared = newest_game_log(get_sc_root())
        if shared:
            cands.append(Path(shared))
    except ImportError:
        pass
    for letter in "CDEFGH":
        for rel in (r"Star Citizen\StarCitizen", r"StarCitizen", r"Program Files\Roberts Space Industries\StarCitizen",
                    r"Roberts Space Industries\StarCitizen", r"Games\StarCitizen"):
            for channel in ("LIVE", "PTU", "EPTU", "HOTFIX"):
                cands.append(Path(f"{letter}:\\") / rel / channel / "Game.log")
    live = [p for p in cands if p.is_file()]
    return max(live, key=lambda p: p.stat().st_mtime) if live else None


class CompanionCore:
    # A question about the place is answered from its claims, not worded by the model (place_knowledge.answer_line
    # has the measurement behind that). True = let the model word it, two tries, with the planned line as the
    # fallback. Worth trying again with a larger model or the API backend; not with the 1.5B adapters.
    place_answers_from_model = False

    def __init__(self, speech, realizer: Optional[Callable[[dict], Optional[str]]] = None,
                 eyes=None, recorder=None, dreams=None, headroom: Callable[[], str] = lambda: "OK",
                 ambient_every_s: float = 90.0, now: Callable[[], float] = time.time, chattiness: int = 2,
                 feedback_dir: Optional[Path] = None, lifecycle=None, session_id: str = "", store=None,
                 sound=None, idle_source: Optional[Callable[[], Optional[float]]] = None,
                 afk_after_s: float = DEFAULT_AFK_MINUTES * 60.0, dev_facts=None,
                 features: Optional[dict] = None):
        self.speech, self.realizer, self.eyes = speech, realizer, eyes
        self.features = {k: (features or {}).get(k, _SETTING_DEFAULTS.get(k)) for k in FEATURE_KEYS}
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
        # Who the log says is around (npc_factions.py): kills, hits, hails and plain presence, with how much it said.
        self.threats = npc_factions.ThreatLog(now=now)
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
        # Every ship maker's brochure facts and both characters' takes join the graph (manufacturers.py). BEFORE the
        # told-topics load below, which addresses facts by index.
        if self.walker is not None and self.features.get("manufacturer_flavour"):
            try:
                manufacturers.merge_into(self.walker.g)
            except Exception:
                log.exception("manufacturer lore")
        if self.walker is not None and self.features.get("place_flavour"):
            try:
                place_flavour.merge_into(self.walker.g)      # Elah's re-checked April place lines, as her opinions
            except Exception:
                log.exception("place flavour")
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
        # Refinery orders across sessions (refinery_tracker.py): the log has no duration, only the "Completed at" notice
        # and its re-announcement at every login, so this remembers WHERE an order waits and reminds on arrival there.
        self.refinery = RefineryTracker(store, now=now) if self.features.get("refinery_tracker") else None
        # Simulated blood drug level (bdl_tracker.py): an ESTIMATE from med pens drawn and never seen again.
        self.bdl = BdlTracker() if self.features.get("bdl_tracker") else None
        # What kind of work the pilot does (contract_history.py), from contract titles, kept in the pilot's memory.
        self.contracts = ContractHistory(store, now=now) if self.features.get("contract_history") else None
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
        # LEFT BROAD DELIBERATELY, both of them, and the reason is the third step below rather than either of these.
        # stop() is a three-step teardown and the LAST step is the only one that persists anything: recorder.close()
        # plus recap_record() are what write this play session into the pilot's memory. An exception escaping from
        # the sound classifier or the log monitor would skip that write and silently lose the session - so every
        # earlier step has to be survivable whatever it raises. self.sound and self._monitor are also injectable
        # (the selftests pass fakes), so their close paths have no fixed exception set to narrow to.
        # What changes is that a failed teardown is now on the record, matching the recorder branch below, which has
        # always logged. Level: warning, not exception - a failure here costs a leaked thread, not the session.
        if self.sound is not None:
            try:
                self.sound.stop()
            except Exception as e:
                log.warning("companion stop: the sound classifier did not stop (%s: %s); its tap may still be "
                            "capturing", type(e).__name__, e, exc_info=True)
        if self._monitor is not None:
            try:
                self._monitor.stop()
            except Exception as e:
                log.warning("companion stop: the Game.log monitor did not stop (%s: %s); its poll thread may still "
                            "be running", type(e).__name__, e, exc_info=True)
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
        if self.features.get("npc_faction_names"):
            try:
                for s in npc_factions.scan_line(line, getattr(self.parser, "_local_name", None)):
                    self.threats.note(s)
            except Exception:
                log.exception("npc factions")
        if self.contracts is not None:
            try:
                self.contracts.on_line(line)             # <EndMission> ... Abandon: the one outcome with no HUD line
            except Exception:
                log.exception("contract history")
        if self.bdl is not None:
            try:
                evs = self.bdl.on_line(line, getattr(self.parser, "_local_name", None))
                if evs:
                    self._bdl_say(evs)
            except Exception:
                log.exception("bdl tracker")

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
        warned = False
        while not self._stop.wait(3.0):
            try:
                st = self.eyes.state() or {}
                scene = st.get("scene")
            except Exception as e:
                # LEFT BROAD DELIBERATELY: self.eyes is injected (RemoteEyes over HTTP, a local Eyes, or a fake in
                # the selftests), so there is no exception set to narrow to, and an escape would end this thread for
                # good - taking the death watcher with it permanently instead of for one tick.
                # But `continue` is NOT harmless, which is why it must not be silent: it also skips the `last = scene`
                # at the bottom, so a failure on the tick after a death loses the dead->respawn transition outright.
                # Per this method's own docstring that is the ONE event the game log no longer reports in time, so a
                # loop that quietly continues forever means the companion never notices the pilot dying again.
                if not warned:
                    warned = True
                    log.warning("eyes loop: cannot read the eyes' scene (%s: %s); the death/respawn screen will not "
                                "be noticed while this lasts", type(e).__name__, e, exc_info=True)
                continue
            warned = False
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
        if self.contracts is not None and et in ("contract_accepted", "contract_complete", "contract_failed"):
            self._contract_event(et, data)               # BEFORE the affect feed: it may carry an affect_scale
        try:
            self.affect.feed(aff_et, data)
        except Exception:
            log.exception("affect")
        # Favourite ships that change with experience (J 2026-09-24): a death or a finished contract is charged to
        # the ship the pilot is in. Raw events only; ship_feelings decides what they mean, with decay.
        if et in ("incapacitated", "contract_complete"):
            self._ship_event("death" if et == "incapacitated" else "mission")
        if self.bdl is not None and et in ("incapacitated", "player_respawned", "med_bed_heal"):
            self.bdl.reset(et)                          # a death, a new body, or the med bed clears the estimate
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
            if data.get("action") == "exited":
                self._departed = self.state.get("location_name")
            elif not getattr(self, "_departed_far", False):
                self._departed = None                       # back inside the zone we walked out of
        elif et == "qt_arrived" and self.state.get("location_name"):
            # A quantum jump ended: wherever this is, it is not the place the log last named (2026-10-05). Entering
            # an armistice zone after it is ANOTHER place's zone, so it no longer counts as coming back; only the
            # log naming a place does.
            self._departed, self._departed_far = self.state.get("location_name"), True
        elif et == "location_change":
            self._departed, self._departed_far = None, False
        if self.recorder is not None:
            try:
                self.recorder.note(et, data)
            except Exception:
                log.exception("session record")
        if et in QUIET_WINDOW_EVENTS and self.dreams is not None:
            self._work_put(("dream", QUIET_WINDOW_EVENTS[et]))
        if self.refinery is not None:
            self._refinery_event(et, data)
        if dup:
            self.stats["dup"] += 1
            return
        if et not in EVENT_PRIORITY:
            return
        self._last_speak_event_t = self.now()
        if et == "location_change" and self._first_meeting_pending:
            self._first_meeting_pending = False
            threading.Thread(target=self._welcome, daemon=True).start()   # first run: now there is a live fact
        if et == "refinery_complete" and self.refinery is not None:
            info = self.refinery.notice(data.get("location"))
            if info and info["suppressed"]:
                self._note(f"refinery notice for {info['station']}: quiet (the pickup reminder was just said)")
                return
            if info and info["known"]:
                data["refinery_known"], data["refinery_days_waiting"] = True, info["days_waiting"]
        if et == "incapacitated" and self.features.get("npc_faction_names") and data.get("killer"):
            f = npc_factions.resolve(data["killer"])          # <Actor Death> named who did it (builds to Nov 2025)
            if f:
                data["killer_faction"] = f["spoken"]
        spec = build_event_spec(et, data, self._ambient_state(), self._variant["event"])
        if spec is None:
            return
        if et == "ship_channel_joined" and self.features.get("manufacturer_flavour"):
            manufacturers.flavour_boarding(spec, data.get("channel") or data.get("ship_type") or "")
        self._variant["event"] += 1
        gp, sp = EVENT_PRIORITY[et]
        # Talked out: an event ABOUT a subject (a place, a zone, a body part) already mentioned enough is dropped.
        # Subject-less events (a reward is only an amount) are always new, and URGENT is never throttled.
        if gp != Priority.URGENT and "|" in subject_key(spec) and self.topics.spent(spec):
            self.stats["talked_out"] = self.stats.get("talked_out", 0) + 1
            self._note(f"talked out, skipped: {subject_key(spec)}")
            return
        self._consider(spec, gp, sp, f"event {et}")

    def _contract_event(self, et: str, data: dict) -> None:
        """Remember the contract's type and outcome; on completion, hand the line its milestone and the feeling its
        scale. Enriches `data` in place (the spec and the affect feed read the same dict)."""
        try:
            mid, title = data.get("mission_id"), str(data.get("mission_name") or "")
            if et == "contract_accepted":
                t = self.contracts.accepted(mid, title)
            elif et == "contract_failed":
                t = self.contracts.failure(mid, title)
            else:
                r = self.contracts.completed(mid, title)
                t = r["type"]
                data["affect_scale"] = r["affect_scale"]
                if r["milestone"]:
                    data["contract_milestone"] = r["milestone"]
                    data["contracts_of_type"], data["contracts_total"] = r["of_type"], r["total"]
            if t != "other":
                data["mission_type"] = t
        except Exception:
            log.exception("contract history")

    def _bdl_say(self, evs: list) -> None:
        """Say the most serious of the estimate's events from one log line (two bands can pass in one tick). The load
        is words, never a number; the one number is how many pens the log showed in the last few minutes."""
        doses = [e for e in evs if e["type"] == "dose"]
        order = ("caution", "danger", "critical")
        ev = max(doses, key=lambda e: order.index(e["band"])) if doses else evs[-1]
        et = "bdl_warning" if ev["type"] == "dose" else "bdl_clear"
        try:
            self.affect.feed(et)
        except Exception:
            log.exception("affect")
        spec = build_event_spec(et, ev, self._ambient_state(), self._variant["event"])
        if spec is None:
            return
        self._variant["event"] += 1
        if et == "bdl_clear":
            self._consider(spec, Priority.AMBIENT, PRIORITY_AMBIENT, "stim load estimate coming down")
        elif ev["band"] == "critical":
            self._consider(spec, Priority.URGENT, PRIORITY_URGENT, "stim load estimate critical")
        else:
            self._consider(spec, Priority.EVENT, PRIORITY_EVENT, f"stim load estimate {ev['band']}")

    def _refinery_event(self, et: str, data: dict) -> None:
        """Logins open the re-announcement window; arriving where an order waits reminds the pilot (once a session)."""
        try:
            if et == "join_pu":
                self.refinery.joined()
            else:
                self.refinery.sweep()
            if et not in ("location_change", "qt_arrived"):
                return
            r = self.refinery.arrived(data.get("location_name") or data.get("location") or "")
            if r is None:
                return
            spec = build_event_spec("refinery_pickup", {"location": r["station"], "days_waiting": r["days_waiting"]},
                                    self._ambient_state(), self._variant["event"])
            if spec is not None:
                self._variant["event"] += 1
                self._consider(spec, Priority.EVENT, PRIORITY_EVENT, f"refinery order waiting at {r['station']}")
        except Exception:
            log.exception("refinery tracker")

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
        if self.features.get("npc_faction_names"):
            th = self.threats.current()
            if th:
                out["threat_faction"], out["threat_role"] = th["spoken"], th["role"]
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
        except (TypeError, ValueError, KeyError, AttributeError) as e:
            # TypeError/ValueError: a `spoken` row that is not a (t, speaker, text) triple. KeyError: inf without
            # "text"/"t". True is the conservative answer - the companion assumes the dev fact is still playing and
            # holds its own line back - and the damage is bounded by the `inf["dur"]` check above, which returns
            # False once the fact's own duration has elapsed. So this is a real degradation (Elah's callout is
            # suppressed for up to one fact's length) rather than a permanent one, and it should still be visible:
            # a Speech implementation whose `spoken` shape drifted would otherwise degrade the callout timing
            # silently, forever.
            if not getattr(self, "_dev_inflight_warned", False):
                self._dev_inflight_warned = True
                log.warning("dev facts: cannot read what Speech has played (%s: %s); assuming the fact is still in "
                            "flight, so callouts will be held back", type(e).__name__, e, exc_info=True)
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
            # said by voice = the pilot asked for it, so the acknowledgement is an answer to him
            self._say_fixed(spec, Priority.URGENT, PRIORITY_URGENT, addressed=(why == "voice"))

    # -- answers to the pilot (J 2026-10-05) ---------------------------------------------------------------------
    # With the window hidden the speech is muted, and a question asked with the talk key is still answered: the
    # window opens an answer pass and Speech lets through a line said with addressed=True (speech.py). ONLY the
    # paths that answer something the pilot said use these two; every unprompted path still reads speech.muted and
    # calls speech.say() plainly, so it is refused while the window is hidden whatever the pass is doing.
    def _answer_muted(self) -> bool:
        asks = getattr(self.speech, "muted_for", None)
        return bool(asks(True)) if callable(asks) else bool(getattr(self.speech, "muted", False))

    def _say_answer(self, text: str, speaker: str, speech_priority: int) -> bool:
        if callable(getattr(self.speech, "muted_for", None)):
            return self.speech.say(text, speaker, speech_priority, addressed=True)
        return self.speech.say(text, speaker, speech_priority)      # a speech with no answer pass (tests)

    def voice_command(self, text: str) -> bool:
        """A spoken control the core handles itself (today: the dev-facts toggle). True = consumed."""
        on = devf.voice_toggle(text)
        if on is None or self.dev_facts is None:
            return False
        self.afk.poke()
        self.set_dev_facts(on, "voice")
        return True

    def _say_fixed(self, spec: dict, gate_priority, speech_priority: int, addressed: bool = False) -> bool:
        """Say a fixed line now (no model): gate, grounding, speech. Used for the toggle acknowledgement.
        addressed: the line answers something the pilot said (see _say_answer)."""
        self.gate_state.muted = self._answer_muted() if addressed else bool(getattr(self.speech, "muted", False))
        cand = Candidate(priority=gate_priority, speaker=spec["speaker"], text_len_words=spec["length_words"][1],
                         created_at=self.now())
        if self.gate.evaluate(self.gate_state, cand).verdict is not Verdict.ALLOW:
            return False
        text = spec["fixed_text"]
        if ground(spec, text):
            return False
        said = (self._say_answer(text, spec["speaker"], speech_priority) if addressed
                else self.speech.say(text, spec["speaker"], speech_priority))
        if said:
            self.gate.record_spoken(self.gate_state, cand)
            self.stats["spoken"] += 1
            self._note(f"{spec['speaker']}: {text}")
            return True
        return False

    # -- direct questions (conversation.py): the pilot asked, so this outranks everything else ---------------------
    def lane_state(self) -> dict:
        """What the conversation lane may answer from, right now: the trackers' own values, minus the place the
        pilot has left (see _departed). The window calls this for every transcript."""
        from conversation import lane_state_from_core
        return lane_state_from_core(self.state, self.volatile, departed=getattr(self, "_departed", None))

    def place_knowledge(self):
        """What each companion knows about a place, read from data this core already holds: the topic graph the
        walker talks from, and the dev-history pack. None when there is no graph (then a question about the place
        is answered with its name alone). Built once."""
        if getattr(self, "_place_knowledge", None) is None and self.walker is not None:
            from place_knowledge import PlaceKnowledge
            self._place_knowledge = PlaceKnowledge(
                self.walker.g, self.dev_facts.pack if self.dev_facts is not None else None)
        return getattr(self, "_place_knowledge", None)

    def answer(self, spec: dict, utterance: str = "") -> None:
        """Answer a direct question. URGENT gate (skips cooldowns, still silenced by mute), its own worker so it is
        never dropped behind ambient work, ground_direct (also refuses an invented place for an UNKNOWN), one
        retry on refusal, then silence. Never a canned line, with one exception since 2026-10-05: a question about
        the PLACE is answered from the spec's own claims (spec["fixed_text"], place_knowledge.answer_line) and
        not worded by the model, which loses the relation between the claims it is given. It needs no realizer,
        so it is answered with the model service down or the card busy. Still held to ground_direct."""
        self.afk.poke()                              # they spoke to us: somebody is here, whatever the keyboard says
        try:
            self.affect.feed("pilot_spoke")
        except Exception:
            log.exception("affect")
        self.gate_state.muted = self._answer_muted()        # an answer: see _say_answer
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

        def say(text: str, how: str) -> None:
            if self._say_answer(text, spec["speaker"], PRIORITY_URGENT):
                self.gate.record_spoken(self.gate_state, cand)
                self._spoke(spec, text, cand)
                self.stats["spoken"] += 1
                self._note(f"{spec['speaker']} ({how}): {text}")

        by_model = (self.place_answers_from_model and spec.get("place") and spec.get("aside") != "dev_fact"
                    and self.realizer is not None)
        if spec.get("fixed_text") and not by_model:
            # A place answer or a dev-history fact: said as planned, never worded by a model.
            fails = ground_direct(spec, spec["fixed_text"])
            if not fails:
                return say(spec["fixed_text"], "answer")
            self.stats["ungrounded"] += 1
            self._note(f"fixed answer REFUSED {fails}: {spec['fixed_text'][:60]!r}")
            return
        asked = spec
        if by_model:                                 # the model words it; the planned line is what it falls back to
            asked = dict(spec, fixed_text=None, length_words=spec.get("model_length_words") or spec["length_words"])
        for attempt in (1, 2):
            text = self.realizer(asked) if self.realizer else None
            if not text:
                self._note(f"question '{utterance[:40]}': no line from realizer")
                break
            fails = ground_direct(asked, text)
            if not fails:
                return say(text, "answer")
            self.stats["ungrounded"] += 1
            self._note(f"answer attempt {attempt} REFUSED {fails}: {text[:60]!r}")
        if by_model and not ground_direct(spec, spec["fixed_text"]):
            self.stats["answer_fallback"] = self.stats.get("answer_fallback", 0) + 1
            say(spec["fixed_text"], "answer, from the claims")

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


# ---- selftest: the optional April-spec features, each with a feature-OFF control ----------------------------------
def _capture(core) -> list:
    """Replace the gate with a recorder: the specs the core WOULD consider, in order."""
    got: list = []
    core._consider = lambda spec, *a, **k: got.append(spec)
    return got


def _claims(spec: Optional[dict]) -> dict:
    return {c["predicate"]: c["value"] for c in (spec or {}).get("claims", [])}


def _npc_case(FakeSpeech, on: bool) -> tuple:
    """A real ASD presence line (J's 2026-08-02 log), then a fight starts, then a real <Actor Death> line where an ASD
    grunt killed the pilot (J's 2025-08-21 log, whose incapacitated event carries the killer). -> (combat, death)."""
    core = CompanionCore(FakeSpeech(), realizer=None, features={"npc_faction_names": on})
    got = _capture(core)
    core.parser._local_name = "ProjectGegnome"
    core.feed_line(npc_factions.LINE_FIXTURES[5][3])
    core._combat_edge("on", "selftest")
    combat = next((s for s in got if s["scenario"] == "event_combat_on"), None)
    core.feed_line(npc_factions.LINE_FIXTURES[1][3])
    death = next((s for s in got if s["scenario"] == "event_incapacitated"), None)
    return _claims(combat), _claims(death)


def _feature_cases(FakeSpeech) -> list:
    out = []
    combat, death = _npc_case(FakeSpeech, True)
    out.append(("npc names: a fight next to ASD troops names them", combat.get("threat.faction") == "ASD troops"))
    out.append(("npc names: a death by an ASD grunt says who did it", death.get("threat.killed_by") == "ASD troops"))
    off_c, off_d = _npc_case(FakeSpeech, False)
    out.append(("npc names CONTROL: with the setting off, neither line names anyone (the checks above then fail)",
                "threat.faction" not in off_c and "threat.killed_by" not in off_d and "combat.state" in off_c))
    first, pickup, again = _refinery_case(FakeSpeech, True)
    out.append(("refinery: an order announced in one session is remembered in the next, and arriving there says so",
                pickup.get("refinery.location") == "HUR-L2 Faithful Dream Station"
                and pickup.get("refinery.days_waiting") == 2))
    out.append(("refinery: the login re-announcement right after the reminder is not said twice", again is None))
    warn = _bdl_case(FakeSpeech, True)
    import bdl_tracker as bt
    first = _claims(warn[0]) if warn else {}
    out.append(("bdl: J's real overdose gets a hedged stim warning (words, plus the pen count the log showed)",
                first.get("suit.stim_load_estimate") in {b[2] for b in bt.BANDS}
                and isinstance(first.get("suit.medpens_recent"), int)))
    n = first.get("suit.medpens_recent")
    honest = f"That is a lot of stims, {n} pens. Hold off."
    out.append(("bdl: grounding passes the pen count and refuses an invented level",
                warn and not ground(warn[0], honest) and ground(warn[0], f"BDL at 60 percent, {n} pens. Hold off.")))
    out.append(("bdl: it escalates to the critical wording before the game's own Overdose notice",
                any(_claims(w).get("suit.stim_load_estimate") == "far too many stims" for w in warn)))
    out.append(("bdl CONTROL: with the setting off the same pens say nothing", _bdl_case(FakeSpeech, False) == []))
    claims, stance, gatac = _maker_case(FakeSpeech, True)
    out.append(("makers: boarding J's Drake Ironclad names Drake Interplanetary and leans plain and short",
                claims.get("ship.manufacturer") == "Drake Interplanetary" and "plain and short" in stance))
    out.append(("makers: a maker the ship branch skipped (Gatac) has topic facts", gatac))
    oc, ostance, ogatac = _maker_case(FakeSpeech, False)
    out.append(("makers CONTROL: with the setting off, no maker claim, no cue, no Gatac node",
                "ship.manufacturer" not in oc and "plain and short" not in ostance and not ogatac
                and oc.get("ship.name") == "Drake Ironclad"))
    found, fails = _place_case(FakeSpeech, True)
    out.append(("places: Elah's re-checked Lorville line is in the walk and passes grounding", found and not fails))
    out.append(("places CONTROL: with the setting off the Lorville node has no such line",
                _place_case(FakeSpeech, False)[0] is False))
    c_on, pride_on = _contract_case(FakeSpeech, True)
    out.append(("contracts: the tenth bounty's completion line says so (type + count from the pilot's history)",
                c_on.get("mission.type") == "bounty" and c_on.get("contracts.of_type_completed") == 10))
    c_off, pride_off = _contract_case(FakeSpeech, False)
    out.append(("contracts: finishing the specialty lifts Elah's pride above the same event without history",
                pride_on > pride_off > 0))
    out.append(("contracts CONTROL: with the setting off, no type, no count", "mission.type" not in c_off
                and "contracts.of_type_completed" not in c_off and c_off.get("mission.status") == "complete"))
    off = _refinery_case(FakeSpeech, False)
    out.append(("refinery CONTROL: with the setting off there is no pickup reminder and the notice speaks as news",
                off[1] == {} and off[2] is not None and "refinery.still_waiting" not in _claims(off[2])))
    return out


def _refinery_case(FakeSpeech, on: bool) -> tuple:
    """J's 2026-04-03 login at HUR-L2, verbatim lines: {Join PU}, the station's location line, then the notice. The
    order was first announced two days earlier, in a previous session with the same pilot memory."""
    import tempfile
    import memory_store as ms
    import refinery_tracker as rt
    clock = [2_000_000.0]
    with tempfile.TemporaryDirectory() as d:
        store = ms.open_store(d, "pilot")
        c1 = CompanionCore(FakeSpeech(), realizer=None, now=lambda: clock[0], store=store,
                           features={"refinery_tracker": on})
        got1 = _capture(c1)
        c1.feed_line(rt.FIXTURE_NOTICE)
        first = next((s for s in got1 if s["scenario"] == "event_refinery_complete"), None)
        clock[0] += 2 * 86400 + 30
        c2 = CompanionCore(FakeSpeech(), realizer=None, now=lambda: clock[0], store=store,
                           features={"refinery_tracker": on})
        got2 = _capture(c2)
        for line in (rt.FIXTURE_JOIN, rt.FIXTURE_AT_STATION, rt.FIXTURE_NOTICE):
            clock[0] += 5
            c2.feed_line(line)
        pickup = next((s for s in got2 if s["scenario"] == "event_refinery_pickup"), None)
        again = next((s for s in got2 if s["scenario"] == "event_refinery_complete"), None)
    return first, _claims(pickup), again


# J's log "Game Build(12660092) 23 Sep 26 (19 52 37).log", verbatim (tail trimmed): boarding his Ironclad.
BOARD_IRONCLAD = ('<2026-09-24T01:15:39.827Z> [Notice] <SHUDEvent_OnNotification> Added notification "You have joined '
                  "channel 'Drake Ironclad : ProjectGegnome'.")


def _maker_case(FakeSpeech, on: bool) -> tuple:
    """Board J's Drake Ironclad (real line). -> (boarding claims, boarding stance, does Gatac have a topic node)."""
    core = CompanionCore(FakeSpeech(), realizer=None, features={"manufacturer_flavour": on})
    got = _capture(core)
    core.feed_line(BOARD_IRONCLAD)
    board = next((s for s in got if s["scenario"] == "event_boarded_ship"), None)
    gatac = core.walker is not None and bool((core.walker.g.nodes.get("maker_gatac_manufacture") or {}).get("facts"))
    return _claims(board), (board or {}).get("interpretation", {}).get("text", ""), gatac


def _place_case(FakeSpeech, on: bool) -> tuple:
    """At Lorville: does the walker have Elah's re-checked line there, and does it pass grounding as the walker
    would offer it? -> (line found, grounding failures)."""
    core = CompanionCore(FakeSpeech(), realizer=None, features={"place_flavour": on})
    if core.walker is None:
        return False, ["no walker"]
    g, w = core.walker.g, core.walker
    facts = (g.nodes.get("lorville") or {}).get("facts", [])
    i = next((k for k, f in enumerate(facts) if f.get("source", "").startswith("place_flavour")), None)
    if i is None:
        return False, []
    w._next_fact["lorville"] = i
    spec = w._spec("lorville", 0, set())
    return True, ground(spec, facts[i]["text"])


def _contract_case(FakeSpeech, on: bool) -> tuple:
    """A pilot with nine bounties and one cargo run behind them (their memory) finishes J's real bounty (18 Dec 2025
    line). -> (completion-line claims, Elah's pride right after)."""
    import tempfile
    import memory_store as ms
    import contract_history as chm
    clock = [3_000_000.0]
    with tempfile.TemporaryDirectory() as d:
        store = ms.open_store(d, "pilot")
        seed = chm.ContractHistory(store, now=lambda: clock[0])
        seed.completed("seed-cargo", "Rookie Rank - Direct Small Cargo Haul")
        for i in range(9):
            seed.completed(f"seed-{i}", "Verified Bounty: Someone")
        core = CompanionCore(FakeSpeech(), realizer=None, now=lambda: clock[0], store=store,
                             features={"contract_history": on})
        got = _capture(core)
        core.feed_line(chm.FIXTURE_BOUNTY_DONE)
        done = next((s for s in got if s["scenario"] == "event_contract_complete"), None)
        pride = core.affect.levels["elah"]["pride"]
    return _claims(done), pride


def _bdl_case(FakeSpeech, on: bool) -> list:
    """J's real 30 Mar 2026 overdose (bdl_tracker.MAR30, 16 pens in two minutes; the game said "Overdose" at 06:38:41
    and he went down at 06:38:58). -> the stim warnings the core would consider, in order."""
    import bdl_tracker as bt
    core = CompanionCore(FakeSpeech(), realizer=None, features={"bdl_tracker": on})
    got = _capture(core)
    for line in bt.mar30_lines():
        core.feed_line(line)
    return [s for s in got if s["scenario"] == "event_bdl_warning"]


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

    for name, ok in _feature_cases(FakeSpeech):
        results.append((name, ok))

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

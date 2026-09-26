"""settings.py - SuitMk2 tool settings at ~/.sctoolbox/suitmk2/settings.json (the toolbox's newest convention)."""
from __future__ import annotations

import json
import os
from pathlib import Path

DIR = Path.home() / ".sctoolbox" / "suitmk2"
PATH = DIR / "settings.json"

DEFAULTS = {
    "presence": "present",            # off | occasional | present | curious (eyes cadence; only while SC is focused)
    "vision_glance": False,           # local gemma3:4b glance when the fast eyes cannot tell (headroom-gated)
    "game_log": "",                   # blank = auto-detect
    "ambient_every_s": 90,
    "volume": 1.0,
    "volume_elah": 1.5,               # per-character level 0..2 (window sliders); >1 is limiter-protected boost
    "volume_montaigne": 1.5,
    "duck_scale": 0.8,                # level while SC is loud. Was 0.55: SC's bed is nearly always "loud", so
                                      # every line played at half volume (J 2026-09-23 "They sound really quiet")
    "muted": False,
    # Model service. Default backend "auto" runs the realizer through Ollama in the toolbox's own Python, so no
    # torch env is needed. model_python / adapters_dir matter ONLY for backend "hf" (a dev setup): empty by
    # default so no personal path ships (the installer's privacy check fails on home-directory paths).
    "model_python": "",
    "adapters_dir": "",
    # Voices ship inside the tool (build_installer copies elah/montaigne.onnx here); stock Piper voices otherwise.
    "voices_dir": str(Path(__file__).resolve().parent.parent / "voices"),
    "pilot_id": "pilot",
    "talk_key": None,                 # push-to-talk binding (InputBinding dict), set from the window
    "chattiness": 2,                  # 0 silent .. 4 very chatty (pacing.py)
    "afk_minutes": 5,                 # no keyboard/mouse input this long = AFK: only urgent lines speak until input
                                      # returns (activity_mode.AfkWatch; READS the OS idle clock, sends nothing). 0 = off
    "ducking": True,                  # wait/duck under Star Citizen's own dialogue (voice_fx)
    "sound_classifier": True,         # game ears: tap ONLY StarCitizen.exe's audio + YAMNet on CPU, the strongest
                                      # combat confirm (sound_classifier.py). Off/missing -> eyes, then meter only
    "backend": "auto",                # auto | ollama | hf | api | none (auto = Ollama suitmk2-* or realizer-* if installed)
    # API option (J 2026-09-24): backend "api" words each line with Claude instead of the local model; the local
    # models stay as the fallback. The key is the pilot's own; empty = use ANTHROPIC_API_KEY from the environment.
    "anthropic_api_key": "",
    "api_model": "claude-sonnet-5",
    # First attempt's sampling temperature (J 2026-09-24). 0.7 roughly doubles variety; 0.0 = greedy, the old
    # behaviour, where every line of a kind opened the same way. Retries always sample at 0.8.
    "first_temperature": 0.7,
    # First-run setup (J 2026-09-23): False = the Setup panel shows ONE "Set up Elah and Montaigne" click, then it is
    # hands-off. True = fully automatic, no click (installs the local runtime if absent and provisions both).
    "auto_setup": False,
    # Launch notice (J 2026-09-24): narrator-only disclaimer + the training-screenshot opt-in. notice_ack holds the
    # training_shots.NOTICE_VERSION the pilot last accepted; a newer wording shows once more.
    "notice_ack": 0,
    "keep_training_shots": False,     # OFF by default: only the launch notice's checkbox or the window turns it on
    # Dev-history fun facts (J 2026-09-25, core/dev_facts.py): OFF by default, because a real-world fact about how the
    # game was made breaks the fourth wall. On = Montaigne, in quiet moments only, says an aside from the dev-history
    # corpus ("Fun fact from the dev history: ..."), at most dev_facts_max_per_hour. Toggle by voice ("fun facts on" /
    # "fun facts off") or the window's checkbox; either one saves here.
    "dev_facts": False,
    "dev_facts_max_per_hour": 2,
    # April-spec ideas, built 2026-09-25 (J: "You can work through those"). Each is a small optional feature with its
    # own module and --selftest; every line it adds still goes through the speak gate, pacing and grounding.
    "npc_faction_names": True,        # NPC entity codes in the log -> "Nine Tails pirates" in fight/death lines
    "refinery_tracker": True,         # finished refinery orders remembered across sessions; reminder on arrival there
    "bdl_tracker": True,              # ESTIMATED blood drug level from med pen use -> a hedged "that's a lot of stims"
    "manufacturer_flavour": True,     # all 19 ship makers: brochure facts Montaigne quotes, both characters' takes,
                                      # and a per-maker lean on the boarding line (data/manufacturer_lore.json)
    "place_flavour": True,            # Elah's one-line impression of 35 places, April lines re-checked against
                                      # current data (data/place_flavour.json); only supported claims survive
    "contract_history": True,         # contract TYPES remembered across sessions: milestones in the completion line,
                                      # a little more pride finishing the pilot's specialty (contract_history.py)
}


def load() -> dict:
    s = dict(DEFAULTS)
    try:
        s.update(json.loads(PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    return s


def save(s: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s, indent=1), encoding="utf-8")
    os.replace(tmp, PATH)

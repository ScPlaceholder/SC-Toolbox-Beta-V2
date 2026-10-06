"""settings.py - SuitMk2 tool settings at ~/.sctoolbox/suitmk2/settings.json (the toolbox's newest convention)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import picture_pace                               # noqa: E402  stdlib only at import, like this module

DIR = Path.home() / ".sctoolbox" / "suitmk2"
PATH = DIR / "settings.json"

# The talk key of a SuitMk2 that has never had one set: Scroll Lock (J 2026-10-05: each tool has a push-to-talk key
# of its own, with a default). The same dict is SUIT_DEFAULT in shared/ptt_keys.py, which also says why that key;
# this module cannot import it (the model service loads settings in a process without the toolbox on its path), and
# shared/tests/test_ptt_keys.py holds the two equal. kind/code/label/joy_index are voice_in.input_devices.InputBinding.
DEFAULT_TALK_KEY = {"kind": "keyboard", "code": 145, "label": "SCROLL_LOCK", "joy_index": 0}

# CONVERSATION MEMORY (tree_memory.py, "Christmas Tree Storage", J 2026-10-05). True = what the pilot says to the
# companions with the talk key or the Suit's open mic, and what they say back, is kept as TEXT in
# <memory>/<pilot>/tree/ on this PC until the pilot clears it (the Suit tab says so, beside "Forget conversations").
# Never audio, never the Assistant's traffic. This one constant is the default for a settings file that has never
# chosen: set it to False to make keeping conversations opt-in.
RECORD_CONVERSATIONS_DEFAULT = True

DEFAULTS = {
    # J 2026-10-05: "a disable companions checkbox which keeps them from running for people who don't want them or
    # have potato computers". False = NOT RUNNING, which is more than muted: no model service is started or woken, no
    # model is loaded, there are no eyes, the game log is not read, no voice is loaded and the talk key is not
    # watched. The window still opens and says they are off.
    "companions_enabled": True,
    "presence": "present",            # off | occasional | present | curious (eyes cadence; only while SC is focused)
    "vision_glance": False,           # local gemma3:4b glance when the fast eyes cannot tell (headroom-gated)
    # HOW OFTEN THE EYES TAKE A PICTURE, by what the pilot is doing (J 2026-10-05, picture_pace.py). A picture is a
    # frame handed to the vision model; the frame compare that "presence" sets is not one. Per activity: seconds
    # between pictures (5 s .. 120 min, put in range on load) and a never flag. The keys are
    #   eyes_salvage_every_s 300        eyes_salvage_never False
    #   eyes_mining_every_s 300         eyes_mining_never False
    #   eyes_combat_mission_every_s 120 eyes_combat_mission_never False
    #   eyes_sandbox_every_s 300        eyes_sandbox_never False     (everything else)
    #   eyes_mining_capture_cooldown_s 90   a capture from the mining reader may ask for a picture early, at most
    #                                       this often (nothing feeds it yet)
    # The interval is one more condition on a picture, never a replacement for the others: only with Star Citizen in
    # front, never with headroom TIGHT or in a fight, inside the hourly cap below.
    **picture_pace.defaults(),
    # Talk about what the eyes saw: 0 never .. 4 no wait of its own (pacing.EYE_TALK_GAP_S; 2 = one such line per
    # 240 s at most, as before there was a dial). Separate from how often they look.
    "eyes_chattiness": 2,
    # NO CEILING IS CHOSEN FOR THE PILOT (J 2026-10-05: someone will run a 27B model and want companions that never
    # shut up, "our system should make that possible"). These were constants in eyes.py; they are settings with the
    # same values, so nothing changes until someone sets them. Not in the window; read when the model service starts.
    #   eyes_pictures_per_hour  None = what "presence" gives (occasional 4, present 12, curious 30); a number = that
    #                           many pictures in any hour, with no upper limit
    #   eyes_look_gap_s         seconds between two deliberate looks (20)
    # What is NOT a setting, here or anywhere: looking only while Star Citizen is the window in front, no picture and
    # no chat model while headroom is TIGHT or a fight is on, and both switched off while the PC stays overloaded
    # (hardware_guard.py).
    "eyes_pictures_per_hour": None,
    "eyes_look_gap_s": 20.0,
    # THE EYES' SHIP REFERENCE (core/eyes_reference.py, EYES_REFERENCE.md). None = the built-in address of the
    # public reference site; a string = that address instead; "" = OFFLINE, nothing is ever requested. With an
    # address, the Suit asks the site for ONE small file (index.json, about 22 kB) when the companions start, at
    # most once a day, and sends nothing about the pilot, the PC or the game. No table is fetched at start-up.
    # Nothing recognises a ship with it yet (eyes_reference.RECOGNITION_CONNECTED is False). Not in the window;
    # read when the model service starts.
    "eyes_reference_url": None,
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
    "talk_key": None,                 # push-to-talk binding (InputBinding dict), set from the window; load()
                                      # gives DEFAULT_TALK_KEY to a settings file that has none
    "talk_mode": "push",              # "push" (hold the talk key) | "always" (mic open) - J 2026-09-26
    "chattiness": 2,                  # 0 silent .. 4 very chatty (pacing.py)
    "afk_minutes": 5,                 # no keyboard/mouse input this long = AFK: only urgent lines speak until input
                                      # returns (activity_mode.AfkWatch; READS the OS idle clock, sends nothing). 0 = off
    "ducking": True,                  # wait/duck under Star Citizen's own dialogue (voice_fx)
    "sound_classifier": True,         # game ears: tap ONLY StarCitizen.exe's audio + YAMNet on CPU, the strongest
                                      # combat confirm (sound_classifier.py). Off/missing -> eyes, then meter only
    "backend": "auto",                # auto | ollama | hf | api | none (auto = Ollama suitmk2-* or realizer-* if installed)
    # VRAM residency of the two speaker models (J 2026-09-26). The local backend holds ONE Ollama model per SPEAKER,
    # measured at 1.83 GB of VRAM each (~2.7 GB of commit per llama-server), so keeping both warm costs 3.7 GB before
    # Star Citizen has asked for anything.
    #   "evict" - only the speaker being asked stays resident; asking the OTHER one releases the idle one first
    #             (keep_alive 0). The companion's floor is ONE model.
    #   "both"  - both stay warm. Faster when the speakers alternate, and only sane with the VRAM to spare.
    # Default "evict" on J's own argument: "it would be better to unload Elah because not everyone will have a card
    # that's beefy enough to do both" - this ships to 6 GB cards, not just to a 4070. The price is a cold load
    # whenever the speaker changes: measured 2.19s wall (ollama load_duration 2.14s) against 0.05s warm, and J:
    # "2 seconds is not a painful response time to wait for a response when talking to an ai".
    # ⚠ 2.19s was measured on an IDLE card. With the game resident it will be worse and nobody has that number yet,
    #   so do not quote 2.19s as the cost under load; it is a floor.
    "speaker_residency": "evict",     # evict | both
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
    "remember_conversations": RECORD_CONVERSATIONS_DEFAULT,   # see RECORD_CONVERSATIONS_DEFAULT above
    # Fact lines (J 2026-10-05, core/fact_lines.py): OFF by default, and not in the window yet; J hears the lines
    # first. On = while conversations are being kept (the key above), the THING a sentence names is counted: the
    # ship the pilot flies, their kit, a place they go, something they want. Never the sentence. Now and then, where
    # an ordinary unprompted line would have been said anyway, a companion says a light line about one of those
    # things, word for word from data/fact_lines.json: at most one in 30 minutes, and one thing once a week. Off =
    # nothing is counted and nothing is said. "Forget conversations" deletes the counts either way. Read when the
    # companions start.
    "fact_banter": False,
    # Free talk (J 2026-10-05, core/chat_talker.py): OFF by default. On = an ordinary remark, a greeting, or a question
    # about the companion itself is worded by the local model named in chat_model (J's choice: gemma3:4b), then cut
    # and checked in code before it is spoken. A question the Suit can answer from what it knows, and a question it
    # cannot answer at all, are both answered exactly as with chat off. Chat cannot be on with no chat model: load()
    # turns it off and chat_on() says no. A settings file from before these two keys has neither and is chat off,
    # which is what it always did.
    "chat": False,
    "chat_model": "",                 # the Ollama model that words free talk, e.g. "gemma3:4b"; empty = none chosen
    # The window now sets both: a drop-down of the models Ollama has on this PC and a checkbox. A model is only ever
    # written here by the window after it passed the fit check (chat_models.choose); one typed in by hand is checked
    # the same way before each use, and not used if it does not fit.
    # The conversation the chat model is shown (were constants in chat_talker.py; the same values, and no ceiling):
    "chat_thread_exchanges": 6,       # exchanges kept per companion
    "chat_thread_ends_after_s": 600.0,   # the pilot silent this long: the conversation is over and is dropped
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


def chat_on(s: dict) -> bool:
    """Free talk is on: chat is true AND a chat model is named. Either one alone is off."""
    return s.get("chat") is True and bool(str(s.get("chat_model") or "").strip())


def load() -> dict:
    s = dict(DEFAULTS)
    try:
        s.update(json.loads(PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    # A file from before the default existed has "talk_key": null (or no such key). Both mean "never set".
    if not isinstance(s.get("talk_key"), dict) or s["talk_key"].get("code") is None:
        s["talk_key"] = dict(DEFAULT_TALK_KEY)
    if not chat_on(s):
        s["chat"] = False                         # chat with no chat model named is off, and the file is told so
    _clean_eyes(s)
    try:
        s["chat_thread_exchanges"] = max(1, int(s.get("chat_thread_exchanges")))
    except (TypeError, ValueError):
        s["chat_thread_exchanges"] = DEFAULTS["chat_thread_exchanges"]
    try:
        ends = float(s.get("chat_thread_ends_after_s"))
        s["chat_thread_ends_after_s"] = ends if ends > 0.0 else DEFAULTS["chat_thread_ends_after_s"]
    except (TypeError, ValueError):
        s["chat_thread_ends_after_s"] = DEFAULTS["chat_thread_ends_after_s"]
    return s


def _clean_eyes(s: dict) -> None:
    """Put the eyes' keys in range, in place. Anything unreadable becomes its default; nothing else is touched."""
    picture_pace.clean(s)                         # the per-activity intervals: 5 s .. 120 min
    s["companions_enabled"] = s.get("companions_enabled") is not False
    try:
        s["eyes_chattiness"] = max(0, min(4, int(s.get("eyes_chattiness"))))
    except (TypeError, ValueError):
        s["eyes_chattiness"] = DEFAULTS["eyes_chattiness"]
    try:
        n = s.get("eyes_pictures_per_hour")
        s["eyes_pictures_per_hour"] = None if n is None or isinstance(n, bool) or int(n) < 1 else int(n)
    except (TypeError, ValueError):
        s["eyes_pictures_per_hour"] = None
    try:
        gap = float(s.get("eyes_look_gap_s"))
        s["eyes_look_gap_s"] = gap if gap >= 0.0 else DEFAULTS["eyes_look_gap_s"]      # NaN fails this too
    except (TypeError, ValueError):
        s["eyes_look_gap_s"] = DEFAULTS["eyes_look_gap_s"]


def save(s: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s, indent=1), encoding="utf-8")
    os.replace(tmp, PATH)

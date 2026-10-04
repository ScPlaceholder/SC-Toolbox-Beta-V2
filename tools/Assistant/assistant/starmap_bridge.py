"""The Assistant's side of the Star Map: voice lives here, the map takes orders.

Until 2026-10-04 the Star Map had its own ears (skills/Starmap/starmap/voice/):
its own microphone capture, its own Whisper, its own mic mode. With that mode
saved as "Always on", opening the map - on its own, or as a tab of the
Everything Finder - armed the mic. J asked for voice-to-text to live in ONE
place, here. Three jobs follow from that, and this module is all three:

1. :func:`command_text` - decide whether something the pilot said is meant for
   the map ("navigate to Area 18", "zoom in", "star map, show Hurston").
2. :func:`send_command` - relay it to whichever process is showing a Star Map
   (the standalone tool, else the Everything Finder), wait for its answer, and
   keep listening for the in-game route macro's later narration. The map does
   not speak any more; this process says the answer in its own voice, which is
   also the only voice its ears know to ignore.
3. :func:`migrate_starmap_voice` - carry the pilot's saved Star Map mic
   settings over once, without overriding anything already chosen here.

Pure helpers where possible: no Qt, and :func:`send_command` takes its IPC bus
and temp folder as arguments so tests never touch a real process.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from typing import Callable, List, Optional

log = logging.getLogger(__name__)

#: Skill ids that can show a Star Map, in order of preference: the standalone
#: tool first, then the Everything Finder (whose Star Map tab opens on demand).
TARGETS = (("starmap", "the Star Map"), ("everything_finder", "the Everything Finder"))

#: Must match starmap/commands.py REPLY_PREFIX / REPLY_SUFFIX: the map only
#: answers into a temp-folder file with this shape.
REPLY_PREFIX = "sc_toolbox_reply_"
REPLY_SUFFIX = ".jsonl"

REPLY_TIMEOUT_S = 6.0      # the map polls its command file every 0.5 s; an Everything
                           # Finder may also have to build its Star Map tab first
FOLLOW_S = 120.0           # how long to keep listening for the route macro's narration
_POLL_S = 0.1

NOT_OPEN = "The Star Map isn't open. Say open the star map, then ask again."


# ── 1. is this utterance for the map? ────────────────────────────────────────

_CLEAN = re.compile(r"[^\w\s]")

#: "star map, <anything>" hands <anything> to the map's own router, which makes
#: every map command reachable ("star map, commodities", "star map, help").
_EXPLICIT = re.compile(r"^(?:please )?(?:on |tell |ask )?(?:the )?(?:star ?map)\b(?: to)? (.+)$")

#: Said with nothing in front. Each is anchored to the START of the utterance:
#: "what is the best trade route to Pyro" must not become "route to Pyro".
_DIRECT = [re.compile(p) for p in (
    r"^(?:please )?(?:set (?:the |a )?route to|navigate to|set (?:a )?course to|"
    r"plot (?:a )?course to) \S.*$",
    r"^(?:please )?route to \S.*$",
    r"^(?:please )?clear (?:the )?route$",
    r"^(?:please )?zoom (?:in|out)$",
    r"^(?:please )?(?:go )?back to (?:the )?galaxy$",
    r"^(?:please )?take me home$",
    r"^(?:please )?(?:open|close|toggle|show|hide) (?:the |my )?(?:grocery|shopping)(?: list)?$",
)]


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", _CLEAN.sub(" ", (text or "").lower())).strip()


def command_text(utterance: str) -> str:
    """The Star Map command in *utterance*, or "" when it is not one."""
    t = _clean(utterance)
    if not t:
        return ""
    # Anchored at the start, so "open the star map" (launch_tool's business) and a
    # bare "star map" never match: only "star map, <something>" does.
    m = _EXPLICIT.match(t)
    if m:
        return m.group(1).strip()
    for rx in _DIRECT:
        if rx.match(t):
            return t[len("please "):] if t.startswith("please ") else t
    return ""


# ── 2. relay a command and hear the answer ───────────────────────────────────

_follow_lock = threading.Lock()
_follow_gen = 0


def _complete_lines(path: str, offset: int):
    """(parsed JSON objects, new offset) for the complete lines after *offset*."""
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            chunk = fh.read()
    except OSError:
        return [], offset
    end = chunk.rfind(b"\n")
    if end < 0:
        return [], offset                     # a line still being written
    out = []
    for raw in chunk[:end].split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out, offset + end + 1


def _remove(path: str) -> None:
    for p in (path, path + ".lock"):
        try:
            os.remove(p)
        except OSError:
            pass


def _follow(path: str, offset: int, ident: str, gen: int,
            on_say: Optional[Callable[[str], None]], seconds: float) -> None:
    """Keep reading the reply file for narration; stop when superseded or timed out."""
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            with _follow_lock:
                if gen != _follow_gen:
                    return
            objs, offset = _complete_lines(path, offset)
            for obj in objs:
                line = ""
                if isinstance(obj.get("say"), str):
                    line = obj["say"]
                elif obj.get("id") == ident and isinstance(obj.get("reply"), str):
                    line = obj["reply"]          # the answer arrived after we stopped waiting
                if line and on_say is not None:
                    try:
                        on_say(line)
                    except Exception:            # a speech failure must not kill the follower
                        log.warning("starmap bridge: could not pass on %r", line, exc_info=True)
            time.sleep(_POLL_S * 3)
    finally:
        _remove(path)


def send_command(text: str, bus=None, on_say: Optional[Callable[[str], None]] = None,
                 timeout: float = REPLY_TIMEOUT_S, follow_s: float = FOLLOW_S,
                 tmp_dir: Optional[str] = None) -> dict:
    """Send one map command and wait (bounded) for the map's answer.

    Returns ``{"reply", "understood", "target", "command"}`` plus one of
    ``"not_running": True`` (no process is showing a Star Map; nothing was sent)
    or ``"timed_out": True`` (sent, no answer yet - a late one is still passed
    to *on_say*). Narration the map produces afterwards goes to *on_say* too.
    """
    global _follow_gen
    if bus is None:
        from . import ipc_bus as bus
    text = (text or "").strip()
    target = next(((sid, label) for sid, label in TARGETS if bus.is_running(sid)), None)
    if target is None:
        return {"command": text, "not_running": True, "understood": False, "reply": NOT_OPEN}
    sid, label = target

    ident = uuid.uuid4().hex[:12]
    path = os.path.join(tmp_dir or tempfile.gettempdir(),
                        "%s%d_%s%s" % (REPLY_PREFIX, os.getpid(), ident, REPLY_SUFFIX))
    try:
        open(path, "w", encoding="utf-8").close()
    except OSError as exc:
        log.warning("starmap bridge: cannot create the reply file %s: %s", path, exc)
        path = ""
    with _follow_lock:
        _follow_gen += 1                     # retires the previous command's follower
        gen = _follow_gen

    if not bus.send(sid, {"type": "map_command", "text": text, "id": ident,
                          "reply_file": path}):
        if path:
            _remove(path)
        return {"command": text, "not_running": True, "understood": False, "reply": NOT_OPEN}

    result = {"command": text, "target": label, "understood": False, "reply": ""}
    offset, answered = 0, False
    deadline = time.monotonic() + timeout
    while path and time.monotonic() < deadline and not answered:
        objs, offset = _complete_lines(path, offset)
        for obj in objs:
            if obj.get("id") == ident and "reply" in obj:
                result["understood"] = bool(obj.get("ok"))
                result["reply"] = str(obj.get("reply") or "")
                answered = True
            elif isinstance(obj.get("say"), str) and on_say is not None:
                on_say(obj["say"])
        if not answered:
            time.sleep(_POLL_S)
    if not answered:
        result["timed_out"] = True
    if path:
        if follow_s > 0:
            threading.Thread(target=_follow, args=(path, offset, ident, gen, on_say, follow_s),
                             daemon=True, name="StarmapReplies").start()
        else:
            _remove(path)
    return result


# ── 3. the one-time move of the Star Map's mic settings ──────────────────────

MIGRATED_KEY = "starmap_voice_migrated"
_MODES = {"push": "Push-to-talk", "always": "Always on"}
_MODELS = ("tiny.en", "base.en", "small.en", "medium.en")


def starmap_state_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".sctoolbox", "starmap", "starmap_state.json")


def load_starmap_state(path: Optional[str] = None) -> dict:
    try:
        with open(path or starmap_state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _convert_binding(b) -> Optional[dict]:
    """A Star Map mic binding as an Assistant one, or None when it has no equivalent.

    The Star Map stored a keyboard key as a Windows virtual-key number; the
    Assistant stores pynput's name for it. Only the keys whose mapping is
    certain are converted (letters, digits, F1-F12, side mouse buttons).
    Joystick and gamepad buttons have no equivalent here yet."""
    if not isinstance(b, dict):
        return None
    kind, code = str(b.get("kind") or ""), b.get("code")
    if kind == "mouse" and isinstance(code, str) and code.startswith("Button.") \
            and code not in ("Button.left", "Button.right"):
        return {"kind": "mouse", "code": code}
    if kind == "keyboard":
        try:
            vk = int(code)
        except (TypeError, ValueError):
            return None
        if 65 <= vk <= 90 or 48 <= vk <= 57:
            return {"kind": "key", "code": chr(vk).lower()}
        if 112 <= vk <= 123:
            return {"kind": "key", "code": "f%d" % (vk - 111)}
    return None


def _describe_binding(b) -> str:
    if isinstance(b, dict):
        return str(b.get("label") or "%s %s" % (b.get("kind"), b.get("code")))
    return ""


def migrate_starmap_voice(state: dict, starmap_state: dict, today: str = "") -> List[str]:
    """Fold the Star Map's saved mic settings into the Assistant's *state*, once.

    The rule: a setting the Assistant ALREADY has is the pilot's own choice for
    this tool and is kept; a Star Map setting only fills a gap. Whatever was
    not applied is said out loud in the returned notices, never dropped
    silently, and the Star Map's original values are recorded under
    ``starmap_voice_migrated`` either way. The Star Map's state file is only
    read, never written.

    Mutates *state*; returns the lines to show the pilot (empty when there was
    nothing to carry over or it has already been done)."""
    if state.get(MIGRATED_KEY):
        return []
    ears = starmap_state.get("ears") if isinstance(starmap_state.get("ears"), dict) else {}
    voice = starmap_state.get("voice") if isinstance(starmap_state.get("voice"), dict) else {}
    record = {"at": today, "found": bool(ears or voice)}
    notes: List[str] = []
    if not (ears or voice):
        state[MIGRATED_KEY] = record          # never used the map's voice: nothing to say
        return notes

    mode = str(ears.get("mode") or "")
    mode = mode if mode in _MODES else ""
    model = str(ears.get("model") or "")
    binding = ears.get("binding")
    record.update({"mode": mode, "model": model, "binding": binding,
                   "replies": voice.get("replies")})

    notes.append("Star Map voice has moved here: the map no longer opens the mic.")

    if mode:
        mine = state.get("mic_mode")
        if mine not in _MODES:
            state["mic_mode"] = mode
            record["mode_applied"] = True
            notes.append("Mic mode carried over from the Star Map: %s." % _MODES[mode])
        elif mine != mode:
            record["mode_applied"] = False
            notes.append("The Star Map was set to %s; I am keeping this window's %s. "
                         "Click %s if you want that here too."
                         % (_MODES[mode], _MODES[mine], _MODES[mode]))

    if model in _MODELS and not state.get("whisper_model"):
        state["whisper_model"] = model

    if binding:
        mine = state.get("binding")
        if not (isinstance(mine, dict) and mine.get("code")):
            conv = _convert_binding(binding)
            if conv is not None:
                state["binding"] = conv
                notes.append("Mic key carried over from the Star Map: %s."
                             % _describe_binding(binding))
            else:
                notes.append("The Star Map's mic trigger (%s) cannot be used here; "
                             "click Set Mic Key and press a keyboard key."
                             % _describe_binding(binding))

    if "voice_replies" not in state and isinstance(voice.get("replies"), bool):
        state["voice_replies"] = voice["replies"]

    state[MIGRATED_KEY] = record
    return notes

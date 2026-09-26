"""character_voice.py - Elah / Montaigne voices for the tools outside SuitMk2 (Starmap, Assistant).

J, 2026-09-26: the map and the assistant spoke through the stock Windows voice. "Elah as default but
allow players to pick." So the choice is one shared setting (``tool_voice`` in
~/.sctoolbox/shared_settings.json, beside the Star Citizen folder) and one speak queue every tool can use.

    mouth = CharacterMouth()      # same interface as the old SAPI Mouth: speak(text), stop(), available()
    mouth.speak("Route plotted")  # queued, returns immediately; lines never overlap

The voice is read on EVERY line, so changing it in the launcher's Settings applies without restarting a tool.

Voices, in order: SuitMk2's trained elah.onnx / montaigne.onnx (they ship in tools/SuitMk2/voices), else the
stock Piper voices they were fine-tuned from (fetched once into ~/.cache/piper, as SuitMk2 does), else the
Windows voice. A failure never silences a tool: any error on the Piper path falls through to Windows speech.
It lives in shared/ on purpose - the Starmap and Assistant must not import from another tool's folder
(that is how the DPS Calculator lost its scunpacked adapter in 2.4.0).
"""
from __future__ import annotations

import io
import logging
import os
import subprocess
import sys
import threading
import urllib.request
import wave
from pathlib import Path
from queue import Empty, Queue
from typing import Optional

log = logging.getLogger(__name__)

VOICES = ("elah", "montaigne", "windows")
LABELS = {"elah": "Elah", "montaigne": "Montaigne", "windows": "Windows voice"}
DEFAULT = "elah"
SETTING_KEY = "tool_voice"

# Kept in step with tools/SuitMk2/core/speech.py (STOCK, DELIVERY) so a character sounds the same everywhere.
_HF = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
STOCK = {
    "elah": ("en_US-lessac-medium", "en/en_US/lessac/medium/en_US-lessac-medium"),
    "montaigne": ("en_GB-alan-medium", "en/en_GB/alan/medium/en_GB-alan-medium"),
}
DELIVERY = {"elah": {"length_scale": 0.95, "noise_scale": 0.6, "noise_w_scale": 0.7},
            "montaigne": {"length_scale": 1.12, "noise_scale": 0.667, "noise_w_scale": 0.8}}

_ROOT = Path(__file__).resolve().parent.parent
VOICES_DIR = _ROOT / "tools" / "SuitMk2" / "voices"
_SAPI_RATE, _SAPI_VOLUME, _SAPI_TIMEOUT = 2, 90, 30


# -- who they are ------------------------------------------------------------------------------------------------
# J, 2026-09-26: "let's give them personality." Taken from SuitMk2's pair_realizer.API_SYSTEM (the sheet a
# base model gets when it is not fine-tuned on the characters), so both tools describe the same two people.
CHARACTER = {
    "elah": ("You are ELAH, the pilot's suit AI: dry, brief, confident, warm under the edge, practical. "
             "She has been to these places with the pilot and never takes a brochure as a source."),
    "montaigne": ("You are MONTAIGNE, the ship's AI, slightly broken, who believes he is the essayist Michel de "
                  "Montaigne: digressive, self-deprecating, gently sceptical, fond of the pilot. He knows ship "
                  "specifications firsthand, but knows places only from travel brochures and commercials, which "
                  "he quotes with complete faith."),
}


def persona_prompt(voice: Optional[str] = None) -> str:
    """Text to append to a tool's system prompt so it speaks as the chosen character; '' for the Windows voice.
    The personality is in HOW things are said. Every rule already in the prompt still binds."""
    v = voice or get_voice()
    if v not in CHARACTER:
        return ""
    return ("\n\nCHARACTER: " + CHARACTER[v] + " Speak as this character, in their voice. Every rule above "
            "still binds: stay just as short, keep every name and number exactly, and add no facts, advice or "
            "places of your own. The personality is in how you say it, not in what you add. Use contractions. "
            "No stage directions, no quotation marks, no speaker label.")


# Fixed lines the Star Map speaks, per character. None = the plain wording (Windows voice, or unknown pick).
LINES = {
    "navigate": {
        "elah": ["Plotting {dest}.", "{dest}. On it.", "Course set for {dest}."],
        "montaigne": ["{dest}, then. The brochures speak very highly of it.",
                      "Setting a course for {dest}. I have read wonderful things.",
                      "To {dest}. I am told the views are unforgettable."],
        None: ["Navigate to {dest}"],
    },
    "unknown": {
        "elah": ["{name}? Not on my charts. Try again?", "Never heard of {name}."],
        "montaigne": ["{name}... I confess no brochure of mine mentions it.",
                      "I cannot place {name}. Perhaps I misheard; it happens more than I admit."],
        None: ["Unknown destination: {name}"],
    },
    "which": {
        "elah": ["Which one? {options}."],
        "montaigne": ["There are several, and I would not presume. {options}?"],
        None: ["Which one? {options}"],
    },
}


def line(key: str, voice: Optional[str] = None, **fields) -> str:
    """One of the Star Map's fixed lines, in the chosen character's words."""
    import random
    v = voice or get_voice()
    pool = LINES[key].get(v) or LINES[key][None]
    return random.choice(pool).format(**fields)


# -- the setting -------------------------------------------------------------------------------------------------
def get_voice() -> str:
    """The player's pick: 'elah' (default), 'montaigne' or 'windows'."""
    try:
        from shared import sc_install
        v = str(sc_install._load().get(SETTING_KEY) or DEFAULT).lower()
    except Exception:
        v = DEFAULT
    return v if v in VOICES else DEFAULT


def set_voice(voice: str) -> str:
    voice = str(voice).lower()
    if voice not in VOICES:
        raise ValueError(f"unknown voice {voice!r}; expected one of {VOICES}")
    from shared import sc_install
    data = sc_install._load()
    data[SETTING_KEY] = voice
    sc_install._save(data)
    return voice


# -- synthesis ---------------------------------------------------------------------------------------------------
_voices: dict = {}
_voices_lock = threading.Lock()


def _stock_path(speaker: str, cache: Path) -> Path:
    name, rel = STOCK[speaker]
    onnx, meta = cache / f"{name}.onnx", cache / f"{name}.onnx.json"
    if not (onnx.exists() and meta.exists()):
        cache.mkdir(parents=True, exist_ok=True)
        for url, dest in ((_HF + rel + ".onnx", onnx), (_HF + rel + ".onnx.json", meta)):
            tmp = dest.with_suffix(dest.suffix + ".part")
            urllib.request.urlretrieve(url, tmp)
            tmp.replace(dest)
    return onnx


def _piper_voice(speaker: str):
    with _voices_lock:
        if speaker not in _voices:
            from piper import PiperVoice  # type: ignore
            trained = VOICES_DIR / f"{speaker}.onnx"
            if trained.exists() and trained.with_suffix(".onnx.json").exists():
                path = trained
            else:
                path = _stock_path(speaker, Path.home() / ".cache" / "piper")
            _voices[speaker] = PiperVoice.load(str(path))
            log.info("character_voice: %s = %s", speaker, path.name)
        return _voices[speaker]


def synthesize(text: str, speaker: str):
    """(float32 mono audio, sample rate) for one line in a character's voice."""
    import numpy as np
    voice = _piper_voice(speaker)
    try:
        from piper.config import SynthesisConfig  # type: ignore
        cfg = SynthesisConfig(**DELIVERY[speaker])
    except Exception:
        cfg = None
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        if cfg is not None:
            voice.synthesize_wav(text, wf, syn_config=cfg)
        else:
            voice.synthesize_wav(text, wf)
    buf.seek(0)
    with wave.open(buf, "rb") as wf:
        audio = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        return audio, wf.getframerate()


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


# -- the speak queue ---------------------------------------------------------------------------------------------
class CharacterMouth:
    """Drop-in for the Starmap / Assistant SAPI Mouth: one queue, lines never overlap."""

    def __init__(self, volume: float = 0.9) -> None:
        self.volume = volume
        self._q: "Queue[Optional[str]]" = Queue()
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self.last_voice: Optional[str] = None       # which path actually spoke the last line (tests, status)
        threading.Thread(target=self._run, daemon=True, name="CharacterMouth").start()

    @staticmethod
    def available() -> bool:
        return sys.platform == "win32" or _piper_importable()

    def speak(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            self._q.put(text)

    def stop(self) -> None:
        """Drain the queue and cut the current line."""
        try:
            while True:
                self._q.get_nowait()
        except Empty:
            pass
        try:
            import sounddevice as sd  # type: ignore
            sd.stop()                 # ignore_errors=True by default, so this call itself does not raise
        except ImportError as exc:
            # No sounddevice means no Piper line was ever playing: there is nothing to cut. A normal state on
            # a Windows-voice-only install, so debug rather than a warning on every stop().
            log.debug("character_voice: no sounddevice to stop (%s)", exc)
        except OSError as exc:
            log.warning("character_voice: sounddevice could not load PortAudio (%s); "
                        "a Piper line may still be playing", exc)
        self._kill()

    def close(self) -> None:
        self.stop()
        self._q.put(None)

    # -- worker ------------------------------------------------------------------------------------------------
    def _run(self) -> None:
        while True:
            text = self._q.get()
            if text is None:
                return
            voice = get_voice()
            if voice != "windows":
                try:
                    audio, sr = synthesize(text, voice)
                    import sounddevice as sd  # type: ignore
                    sd.play(audio * self.volume, sr)
                    sd.wait()
                    self.last_voice = voice
                    continue
                except Exception:
                    log.exception("character_voice: %s voice failed; using the Windows voice", voice)
            self._sapi(text)
            self.last_voice = "windows"

    def _sapi(self, text: str) -> None:
        if sys.platform != "win32":
            return
        cmd = ("Add-Type -AssemblyName System.Speech; "
               "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
               f"$s.Rate = {_SAPI_RATE}; $s.Volume = {_SAPI_VOLUME}; "
               f"$s.Speak({_ps_quote(text)}); $s.Dispose()")
        try:
            with self._lock:
                self._proc = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._proc.wait(timeout=_SAPI_TIMEOUT)
        except Exception:
            log.exception("character_voice: Windows voice failed")
        finally:
            with self._lock:
                self._proc = None

    def _kill(self) -> None:
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError as exc:
                # The usual case is the benign race: the speech process exited between poll() and kill().
                log.debug("character_voice: could not kill the speech process (%s)", exc)


def _piper_importable() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("piper") is not None
    except (ImportError, ValueError) as exc:
        # find_spec raises ImportError when piper is present but broken, and ValueError when it is imported
        # yet has no __spec__. Both mean "cannot use Piper", which is a fallback to the Windows voice.
        log.debug("character_voice: piper is not usable (%s)", exc)
        return False

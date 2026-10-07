"""speech.py - local two-voice speech for the SuitMk2 toolbox tool.

Replaces WingmanAI's TTS (speech_dispatcher.py was the one WingmanAI-coupled module). Everything is local:
Piper ONNX voices through `piper-tts` (bundled phonemizer, Windows wheels) and `sounddevice` for playback, both
already in the toolbox's Python.

  Speech(voices_dir).say(text, speaker="elah"|"montaigne", priority=...)   -> queued, returns immediately

Rules:
  * ONE playback thread and a priority queue: lines never overlap (Mining_Signals' speak() overlaps).
  * Higher priority first; within a priority, oldest first. A line older than its max_age is dropped, not spoken
    late: a stale "you are hurt" after the heal is worse than silence.
  * Voices: <voices_dir>/elah.onnx and montaigne.onnx (fine-tuned) when present, else the stock Piper base voices
    they were fine-tuned from (lessac / alan), fetched once into ~/.cache/piper like Mining_Signals does. Swapping a
    trained voice in is dropping a file in; reload() picks it up.
  * A VOICE OF THE PLAYER'S OWN. custom_voices={speaker: path of a Piper .onnx} (the window's voice picker) is
    tried first for that speaker. A file that is missing, has no .onnx.json beside it, or will not load never
    costs a line: the speaker uses the voice above, and voice_problem(speaker) says in plain words which file
    and what was wrong. The character (voice_fx), the delivery and the level are applied to it as to any voice.
    set_custom_voice() then reload() applies a new choice without a restart.
  * mute() stops the current line and clears the queue.
  * ANSWERS. A line said with addressed=True is an answer to something the pilot asked with the
    talk key. While muted, such a line is still spoken if allow_addressed(True) was called; every other line is
    refused exactly as before. This is how SuitMk2 stays silent with its window hidden (the companions talk only
    while it is launched) and still answers a push-to-talk question asked from the game. The window
    decides when (ui/suit_window.py _voice_gate); nothing here opens the pass by itself, and it starts closed.
  * A SEQUENCE. say_sequence(items, speaker, ...) queues ONE item made of words, bursts of static
    and gaps: [("say", text) | ("static", seconds) | ("gap", seconds)]. It was added for Montaigne's rare line,
    after "Brzzz" was read out as letters: static has to be an audio clip, not text. It is one queue item, so
    every rule above holds for it exactly as for a line: priority, max age, mute stopping it and clearing it,
    the hidden-window gate, the wait for the game's own dialogue, one playback thread, never over another line.
    The words are synthesised by the same synth and given the same character and level as any line; the static is
    made here (static_burst) and is given neither: it is set to 0.8 of the level the first words came out at, so
    a character turned down takes its static down with it. One limiter pass over the whole. Without voice_fx
    (no scipy) the static is the same stuttered noise UNFILTERED and at half that level, and it still plays.
"""
from __future__ import annotations

import heapq
import io
import itertools
import json
import logging
import threading
import time
import urllib.request
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

try:                                    # imported up front: scipy's first import is ~0.5-1 s, which must not
    import voice_fx                     # land on the first spoken line
except Exception as _e:                 # no scipy / pycaw -> voices simply stay clean, nothing else changes
    voice_fx = None
    log.warning("speech: voice_fx unavailable, voices stay clean: %s", _e)

PRIORITY_URGENT, PRIORITY_EVENT, PRIORITY_AMBIENT = 0, 1, 2
MAX_AGE_S = {PRIORITY_URGENT: 20.0, PRIORITY_EVENT: 30.0, PRIORITY_AMBIENT: 60.0}

_HF = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
STOCK = {
    "elah": ("en_US-lessac-medium", "en/en_US/lessac/medium/en_US-lessac-medium"),
    "montaigne": ("en_GB-alan-medium", "en/en_GB/alan/medium/en_GB-alan-medium"),
}
# Per-character delivery. Montaigne is unhurried; Elah is brisk. (length_scale > 1 = slower)
DELIVERY = {"elah": {"length_scale": 0.95, "noise_scale": 0.6, "noise_w_scale": 0.7},
            "montaigne": {"length_scale": 1.12, "noise_scale": 0.667, "noise_w_scale": 0.8}}
# In-world character per speaker (voice_fx presets: "clean" | "default" | "heavy").
FX_PRESET = {"elah": "default", "montaigne": "default"}
# How long a NEW line may wait for Star Citizen's own dialogue to finish, by priority. Urgent barely waits.
HOLD_MAX_S = {PRIORITY_URGENT: 1.0, PRIORITY_EVENT: 4.0, PRIORITY_AMBIENT: 8.0}


@dataclass(order=True)
class _Item:
    priority: int
    seq: int
    created: float = field(compare=False)
    text: str = field(compare=False)
    speaker: str = field(compare=False)
    addressed: bool = field(compare=False, default=False)     # an answer to the pilot; see allow_addressed
    sequence: Optional[tuple] = field(compare=False, default=None)     # say_sequence: words, static and gaps


# The static of a sequence. The numbers are those of the rendering approved by ear:
# noise band-passed 350 to 3600 Hz, a random stutter of short bursts, coarse
# amplitude steps, a quiet 120 Hz square buzz under it, 6 ms fades, 0.8 of the voice's speech level. Seeded, so
# the same burst is the same every time.
STATIC_BAND_HZ = (350.0, 3600.0)
STATIC_LEVEL = 0.8                 # of the level the sequence's first words came out at
STATIC_LEVEL_UNFILTERED = 0.4      # without scipy the noise is full-band and harsher: half as loud
STATIC_SEEDS = (3, 7, 11)          # the first three bursts of a sequence, as approved; later ones go on by fours
MAX_SEQUENCE_SOUND_S = 5.0


def static_seed(n: int) -> int:
    """The seed of a sequence's n-th burst of static (0 first)."""
    return STATIC_SEEDS[n] if n < len(STATIC_SEEDS) else STATIC_SEEDS[-1] + 4 * (n - len(STATIC_SEEDS) + 1)


def static_burst(seconds: float, sr: int, seed: int, level: float):
    """(float32 samples, filtered?) for one burst of static of this length, at `level` RMS x STATIC_LEVEL when
    it could be band-passed and x STATIC_LEVEL_UNFILTERED when it could not (no voice_fx, so no scipy)."""
    import numpy as np
    r = np.random.default_rng(seed)
    n = max(0, int(seconds * sr))
    if n == 0:
        return np.zeros(0, dtype=np.float32), False
    x = r.standard_normal(n).astype(np.float32)
    filtered = False
    if voice_fx is not None and n:
        try:
            from scipy.signal import butter, sosfilt  # type: ignore
            x = sosfilt(butter(4, list(STATIC_BAND_HZ), btype="band", fs=sr, output="sos"), x)
            filtered = True
        except Exception as e:
            log.warning("speech: static could not be band-passed, playing it unfiltered: %s: %s", type(e).__name__, e)
    env = np.zeros(n, dtype=np.float32)
    i = 0
    while i < n:                                   # stutter: short bursts with shorter gaps
        on, off = int(sr * r.uniform(0.025, 0.09)), int(sr * r.uniform(0.008, 0.035))
        env[i:i + on] = r.uniform(0.55, 1.0)
        i += max(1, on + off)
    x = x * env
    x = np.round(x / (np.abs(x).max() + 1e-9) * 12) / 12            # coarse steps: a digital edge on the noise
    hum = 0.25 * np.sign(np.sin(2 * np.pi * 120 * np.arange(n) / sr)).astype(np.float32)   # a low buzz under it
    x = (x + hum * env).astype(np.float32)
    f = int(0.006 * sr)
    if 0 < 2 * f <= n:
        ramp = np.linspace(0, 1, f, dtype=np.float32)
        x[:f] *= ramp
        x[-f:] *= ramp[::-1]
    rms = float(np.sqrt(np.mean(x ** 2))) if n else 0.0
    gain = (STATIC_LEVEL if filtered else STATIC_LEVEL_UNFILTERED) * float(level) / (rms + 1e-9)
    return (x * gain).astype(np.float32), filtered


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


# What Piper reads from a voice's .onnx.json when it loads the voice. A file without these is not one.
_VOICE_JSON_NEEDS = ("audio", "espeak", "num_symbols", "num_speakers", "phoneme_id_map")


def check_voice_file(path) -> tuple:
    """Can this file be tried as a voice? (True, "") or (False, why), the why in plain words that name the file.
    It looks at the file and reads the small .onnx.json beside it, and nothing else: no audio, no model, no Qt.
    A file that passes can still fail to load (a .onnx that is not a voice); Speech catches that and says so in
    the same way. shared/character_voice.py has a shorter copy of this for the Assistant."""
    text = str(path or "").strip()
    if not text:
        return False, "no file was chosen"
    p = Path(text)
    if not p.is_file():
        return False, f"{p} was not found"
    if p.suffix.lower() != ".onnx":
        return False, f"{p.name} is not a Piper voice file (.onnx)"
    meta = Path(str(p) + ".json")
    try:
        if p.stat().st_size == 0:
            return False, f"{p.name} is an empty file"
        if not meta.is_file():
            return False, f"{meta.name} was not found beside {p.name}"
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return False, f"{meta.name} could not be read ({type(e).__name__})"
    if (not isinstance(data, dict) or any(k not in data for k in _VOICE_JSON_NEEDS)
            or not isinstance(data["audio"], dict) or "sample_rate" not in data["audio"]
            or not isinstance(data["espeak"], dict) or "voice" not in data["espeak"]):
        return False, f"{meta.name} is not the settings file of a Piper voice"
    return True, ""


def list_voice_files(folder) -> list:
    """The voice files in a folder that pass check_voice_file, by name. A folder that is not there has none."""
    try:
        found = sorted(Path(folder).glob("*.onnx"), key=lambda p: p.name.lower())
    except OSError as e:
        log.warning("speech: the voices folder %s could not be read: %s", folder, e)
        return []
    return [p for p in found if check_voice_file(p)[0]]


class Speech:
    def __init__(self, voices_dir: Path, cache_dir: Optional[Path] = None, volume: float = 0.9,
                 synth: Optional[Callable[[str, str], tuple]] = None,
                 play: Optional[Callable[[object, int], None]] = None,
                 now: Callable[[], float] = time.time,
                 fx_presets: Optional[dict] = None, ducker=None,
                 custom_voices: Optional[dict] = None, load: Optional[Callable[[str], object]] = None):
        """synth/play are injectable for tests: synth(text, speaker) -> (float32 audio, sr); play(audio, sr).
        fx_presets: {speaker: voice_fx preset}; default FX_PRESET. ducker: a started voice_fx.DuckingMonitor, or
        None to never wait/duck (tests, or no pycaw). custom_voices: {speaker: path of the player's own voice
        file, or ""}. load: load(path) -> a Piper voice; the tests' stand-in for loading a model."""
        self.voices_dir, self.cache = Path(voices_dir), cache_dir or (Path.home() / ".cache" / "piper")
        self.volume, self.now = volume, now
        self._voices: dict[str, object] = {}
        self._voice_src: dict[str, str] = {}
        self.custom_voices: dict[str, str] = {k: str(v or "").strip() for k, v in (custom_voices or {}).items()}
        self._problems: dict[str, str] = {}                 # speaker -> why its custom voice is not in use
        self._load = load or self._piper_load
        self._voice_lock = threading.Lock()                 # one load at a time: preload() and a line both ask
        self._voice_gen = 0                                 # goes up at reload(); see _voice
        self._synth = synth or self._piper_synth
        self._play = play or self._sd_play
        self._q: list[_Item] = []
        self._seq = itertools.count()
        self._cv = threading.Condition()
        self._muted = False
        self._addressed_ok = False                          # while muted: may an answer to the pilot be spoken?
        self._current: Optional[_Item] = None               # the line being played, for mute() to judge
        self._stop = threading.Event()
        self.spoken: list[tuple[float, str, str]] = []     # (time, speaker, text) for the status window
        self.dropped_stale = 0
        self.fx_presets = dict(FX_PRESET if fx_presets is None else fx_presets)
        # Per-character level, 0..2 (the window's sliders). Applied at render, BEFORE the limiter, so a boost past
        # 100% gets louder instead of clipping. In the first dry run they sounded really quiet.
        self.levels: dict[str, float] = {"elah": 1.0, "montaigne": 1.0}
        self.ducker = ducker
        self.held_s = 0.0                                   # total time spent waiting for the game (status window)
        self._abort = threading.Event()                     # stops a streamed (duckable) line on mute()
        self._thread = threading.Thread(target=self._loop, name="suitmk2_speech", daemon=True)
        self._thread.start()

    # -- voices -------------------------------------------------------------------------------------------------
    def voice_source(self, speaker: str) -> str:
        """'custom' / 'trained' / 'stock' / 'not loaded' (for the status window)."""
        return self._voice_src.get(speaker, "not loaded")

    def set_custom_voice(self, speaker: str, path) -> None:
        """The player's own voice file for a speaker, or "" for the built-in voice. reload() applies it."""
        self.custom_voices[speaker] = str(path or "").strip()

    def voice_problem(self, speaker: str) -> str:
        """"" while the speaker has the voice that was asked for. Otherwise why its custom voice is not the one
        in use, in plain words that name the file; the speaker is then on its built-in voice."""
        return self._problems.get(speaker, "")

    def preload(self) -> None:
        """Load both voices now (on the speech thread's behalf, from any thread) so the dashboard shows them loaded
        and neither character's first line pays the ~2 s model load. Failures are logged, never raised."""
        for spk in STOCK:
            try:
                self._voice(spk)
            except Exception as e:
                log.warning("speech: preload %s failed: %s", spk, e)

    def reload(self) -> None:
        self._voice_gen += 1
        self._voices.clear()
        self._voice_src.clear()
        self._problems.clear()

    @staticmethod
    def _piper_load(path: str):
        from piper import PiperVoice  # type: ignore
        return PiperVoice.load(str(path))

    def _voice(self, speaker: str):
        with self._voice_lock:
            while True:
                voice = self._voices.get(speaker)
                if voice is not None:
                    return voice
                gen = self._voice_gen
                voice, src, name, problem = self._open_voice(speaker)
                if gen != self._voice_gen:                  # reload() ran while this loaded: the choice may have
                    continue                                # changed, so what was loaded is not kept
                self._voices[speaker], self._voice_src[speaker] = voice, src
                if problem:
                    self._problems[speaker] = problem
                else:
                    self._problems.pop(speaker, None)
                log.info("speech: %s voice = %s (%s)", speaker, name, src)
                return voice

    def _open_voice(self, speaker: str):
        """(voice, 'custom' | 'trained' | 'stock', file name, problem). The player's own file first, when one is
        chosen. If it cannot be used the built-in voice is loaded in its place and `problem` says why; a custom
        voice is never allowed to cost the speaker its voice."""
        custom, problem = self.custom_voices.get(speaker, ""), ""
        if custom:
            ok, problem = check_voice_file(custom)
            if ok:
                try:
                    return self._load(custom), "custom", Path(custom).name, ""
                except Exception as e:
                    problem = f"{Path(custom).name} could not be loaded as a voice"
                    log.warning("speech: %s could not be loaded: %s: %s", custom, type(e).__name__, e)
            log.warning("speech: %s is on its built-in voice: %s", speaker, problem)
        trained = self.voices_dir / f"{speaker}.onnx"
        if trained.exists() and trained.with_suffix(".onnx.json").exists():
            path, src = trained, "trained"
        else:
            path, src = _stock_path(speaker, self.cache), "stock"
        return self._load(str(path)), src, path.name, problem

    def _piper_synth(self, text: str, speaker: str):
        import numpy as np
        voice = self._voice(speaker)
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

    def set_fx(self, speaker: str, preset: str) -> None:
        """Per-speaker character: "clean" | "default" | "heavy" (settings window)."""
        self.fx_presets[speaker] = preset

    def set_level(self, speaker: str, level: float) -> None:
        """Per-character volume, 0.0..2.0; takes effect from the next line rendered."""
        self.levels[speaker] = max(0.0, min(2.0, float(level)))

    def _level(self, audio, sr: int, speaker: str):
        """Scale one line by its character's level; above 1.0 the look-ahead limiter keeps peaks under the ceiling."""
        g = self.levels.get(speaker, 1.0)
        try:
            import numpy as np
            if not isinstance(audio, np.ndarray) or g == 1.0:
                return audio
            y = audio.astype(np.float32) * g
            if g > 1.0:
                y = voice_fx._limit(y, sr) if voice_fx is not None else np.clip(y, -0.95, 0.95)
            return y.astype(np.float32)
        except Exception as e:
            log.warning("speech: level %s failed, playing unscaled: %s: %s", speaker, type(e).__name__, e)
            return audio

    def _character(self, audio, sr: int, speaker: str):
        """voice_fx after synth, before play. Never allowed to cost a line: on any failure, speak it clean."""
        preset = self.fx_presets.get(speaker, "clean")
        if preset == "clean" or voice_fx is None:
            return audio
        try:
            import numpy as np
            if not isinstance(audio, np.ndarray):
                return audio                                # injected test synth
            return voice_fx.apply(audio, sr, speaker, preset)
        except Exception as e:
            log.warning("speech: fx %s/%s failed, speaking clean: %s: %s", speaker, preset, type(e).__name__, e)
            return audio

    def _sd_play(self, audio, sr: int) -> None:
        import sounddevice as sd  # type: ignore
        if self.ducker is None:
            sd.play(audio * self.volume, sr)
            sd.wait()
            return
        # Streamed so the gain can follow the game WHILE the line plays: target = volume * volume_scale(),
        # smoothed per block (~50 ms time constant) so a duck is a fade, not a click.
        import numpy as np
        data = np.ascontiguousarray(audio, dtype=np.float32).reshape(-1)
        pos, gain, done = [0], [self.volume * self.ducker.volume_scale()], threading.Event()
        blk = max(256, sr // 50)                            # 20 ms blocks
        coef = 1.0 - np.exp(-blk / (0.05 * sr))

        def cb(out, frames, _t, _status):
            if self._abort.is_set():
                raise sd.CallbackAbort
            target = self.volume * self.ducker.volume_scale()
            g0, g1 = gain[0], gain[0] + (target - gain[0]) * coef
            gain[0] = g1
            chunk = data[pos[0]: pos[0] + frames]
            n = len(chunk)
            out[:n, 0] = chunk * np.linspace(g0, g1, n, dtype=np.float32) if n else chunk
            out[n:, 0] = 0.0
            pos[0] += n
            if n < frames:
                raise sd.CallbackStop

        self._abort.clear()
        with sd.OutputStream(samplerate=sr, channels=1, dtype="float32", blocksize=blk, callback=cb,
                             finished_callback=done.set):
            done.wait(len(data) / sr + 5.0)

    # -- queue ----------------------------------------------------------------------------------------------------
    def say(self, text: str, speaker: str = "elah", priority: int = PRIORITY_AMBIENT,
            addressed: bool = False) -> bool:
        """addressed=True: this line answers something the pilot asked (see allow_addressed)."""
        if self._refuses(addressed) or not text or speaker not in STOCK:
            return False
        with self._cv:
            heapq.heappush(self._q, _Item(priority, next(self._seq), self.now(), text, speaker, bool(addressed)))
            self._cv.notify()
        return True

    def say_sequence(self, items, speaker: str = "montaigne", priority: int = PRIORITY_AMBIENT,
                     addressed: bool = False, text: str = "") -> bool:
        """Queue a prepared sequence as ONE line of this speaker's: items are ("say", words), ("static", seconds)
        and ("gap", seconds), played in order. Refused exactly when say() would refuse a line, and also when the
        sequence has no words or an item that is not one of the three. text: what the status window shows for it;
        by default its words."""
        seq = []
        try:
            for kind, value in items:
                if kind == "say" and isinstance(value, str) and value.strip():
                    seq.append(("say", value.strip()))
                elif kind in ("static", "gap") and 0 < float(value) <= MAX_SEQUENCE_SOUND_S:
                    seq.append((kind, float(value)))
                else:
                    return False
        except (TypeError, ValueError):
            return False
        words = " ".join(v for k, v in seq if k == "say")
        if self._refuses(addressed) or not words or speaker not in STOCK:
            return False
        with self._cv:
            heapq.heappush(self._q, _Item(priority, next(self._seq), self.now(), str(text or words), speaker,
                                          bool(addressed), tuple(seq)))
            self._cv.notify()
        return True

    def _render_sequence(self, seq, speaker: str):
        """(audio, sr) for a sequence. The words are synthesised, given their character and their level, one item
        at a time, as a line is; the static is generated at 0.8 of the level the first words came out at; then
        everything is joined and limited once. With a synth that does not return arrays (the tests') the result
        is the list of parts in order, the words' audio as the synth gave it and ("static" | "gap", seconds) for
        the rest, and play() is handed that list."""
        import numpy as np
        voiced, sr = {}, 22050
        for i, (kind, value) in enumerate(seq):
            if kind == "say":
                audio, sr = self._synth(value, speaker)
                voiced[i] = self._level(self._character(audio, sr, speaker), sr, speaker)
        if not all(isinstance(a, np.ndarray) for a in voiced.values()):
            return [voiced[i] if kind == "say" else (kind, value) for i, (kind, value) in enumerate(seq)], sr
        first = next(iter(voiced.values()))
        try:
            ref = voice_fx.speech_rms(first, sr) if voice_fx is not None else 0.0
        except Exception:
            ref = 0.0
        if not ref and len(first):
            ref = float(np.sqrt(np.mean(first.astype(np.float32) ** 2)))
        parts, bursts = [], 0
        for i, (kind, value) in enumerate(seq):
            if kind == "say":
                parts.append(voiced[i].astype(np.float32).reshape(-1))
            elif kind == "gap":
                parts.append(np.zeros(int(value * sr), dtype=np.float32))
            else:
                try:
                    burst, _ = static_burst(value, sr, static_seed(bursts), ref)
                except Exception as e:           # never allowed to cost the line: silence of the same length
                    log.warning("speech: static failed, a gap in its place: %s: %s", type(e).__name__, e)
                    burst = np.zeros(int(value * sr), dtype=np.float32)
                bursts += 1
                parts.append(burst)
        out = np.concatenate(parts).astype(np.float32)
        try:
            out = voice_fx._limit(out, sr) if voice_fx is not None else out
        except Exception as e:
            log.warning("speech: the sequence could not be limited, clipping it: %s: %s", type(e).__name__, e)
        return np.clip(out, -1.0, 1.0).astype(np.float32), sr

    def _refuses(self, addressed: bool) -> bool:
        """Not muted: nothing is refused. Muted: everything is, except an answer while answers are allowed."""
        return self._muted and not (addressed and self._addressed_ok)

    def muted_for(self, addressed: bool) -> bool:
        """Would a line be refused right now? muted_for(False) is `muted`; muted_for(True) asks for an answer."""
        return self._refuses(bool(addressed))

    def allow_addressed(self, on: bool) -> None:
        """While muted, let answers to the pilot through (True) or refuse them like everything else (False).
        Changes nothing while not muted. Closing it drops an answer that is queued or being spoken."""
        self._addressed_ok = bool(on)
        if not on and self._muted:
            self._drop_refused()

    def _drop_refused(self) -> None:
        """Clear every queued line that may no longer be spoken, and stop the current one if it is such a line."""
        with self._cv:
            self._q = [i for i in self._q if not self._refuses(i.addressed)]
            heapq.heapify(self._q)
            cur = self._current
        if cur is not None and not self._refuses(cur.addressed):
            return                                          # an answer that is still allowed keeps playing
        self._abort.set()
        try:
            import sounddevice as sd  # type: ignore
            sd.stop()
        except Exception:
            pass

    def pending(self) -> int:
        with self._cv:
            return len(self._q)

    def mute(self, on: bool = True) -> None:
        self._muted = on
        if on:
            self._drop_refused()

    @property
    def muted(self) -> bool:
        return self._muted

    def _next(self) -> Optional[_Item]:
        with self._cv:
            while not self._q and not self._stop.is_set():
                self._cv.wait(0.5)
            if self._stop.is_set():
                return None
            item = heapq.heappop(self._q)
        if self.now() - item.created > MAX_AGE_S[item.priority]:
            self.dropped_stale += 1
            return _Item(item.priority, item.seq, item.created, "", item.speaker)   # skipped, not spoken late
        return item

    def _loop(self) -> None:
        while not self._stop.is_set():
            item = self._next()
            if item is None:
                break
            if not item.text or self._refuses(item.addressed):
                continue
            try:
                if item.sequence is not None:
                    audio, sr = self._render_sequence(item.sequence, item.speaker)
                else:
                    audio, sr = self._synth(item.text, item.speaker)
                    audio = self._level(self._character(audio, sr, item.speaker), sr, item.speaker)
                if self.ducker is not None and not self._refuses(item.addressed):
                    # The game is talking: wait for it (bounded by priority), then re-check staleness -
                    # a line that waited past its max_age is dropped like any other stale line.
                    waited = self.ducker.wait_clear(HOLD_MAX_S[item.priority],
                                                    abort=lambda: self._refuses(item.addressed)
                                                    or self._stop.is_set())
                    self.held_s += waited
                    if self.now() - item.created > MAX_AGE_S[item.priority]:
                        self.dropped_stale += 1
                        continue
                if not self._refuses(item.addressed):
                    self._current = item
                    try:
                        self._play(audio, sr)
                    finally:
                        self._current = None
                    self.spoken.append((self.now(), item.speaker, item.text))
                    del self.spoken[:-50]
            except Exception as e:           # one bad line (or a missing voice) must not kill the voice thread
                log.warning("speech: %s line failed: %s: %s", item.speaker, type(e).__name__, e)

    def close(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        self.mute(True)


VOICE_JSON_EXAMPLE = {"audio": {"sample_rate": 16000}, "espeak": {"voice": "en-us"}, "num_symbols": 256,
                      "num_speakers": 1, "phoneme_id_map": {"_": [0]}}


def write_fake_voice(folder: Path, name: str, meta=VOICE_JSON_EXAMPLE, body: bytes = b"not a real model") -> Path:
    """For the tests: <folder>/<name>.onnx with `body` in it and, unless meta is None, its .onnx.json (a dict is
    written as JSON, a str as it is). Nothing here is a model; the tests give Speech a load() of their own."""
    folder.mkdir(parents=True, exist_ok=True)
    onnx = folder / f"{name}.onnx"
    onnx.write_bytes(body)
    if meta is not None:
        Path(str(onnx) + ".json").write_text(meta if isinstance(meta, str) else json.dumps(meta), encoding="utf-8")
    return onnx


def _selftest_voices(case, tmp: Path) -> None:
    """The player's own voice file: the check, the fallback and what is said about it. Everything is under `tmp`;
    no model is loaded, nothing is downloaded and nothing is played."""
    global voice_fx
    builtin, mine, cache = tmp / "builtin", tmp / "mine", tmp / "cache"
    for spk in STOCK:                                       # the voices that ship, so nothing is ever fetched
        write_fake_voice(builtin, spk)
    good = write_fake_voice(mine, "my_voice")
    no_json = write_fake_voice(mine, "no_json", meta=None)
    bad_json = write_fake_voice(mine, "bad_json", meta="{ this is not json")
    other_json = write_fake_voice(mine, "other_json", meta={"name": "something else"})
    empty = write_fake_voice(mine, "empty", body=b"")
    wont_load = write_fake_voice(mine, "wont_load")
    not_onnx = mine / "song.mp3"
    not_onnx.write_bytes(b"x")
    gone = mine / "gone.onnx"

    case("check: a .onnx with its .onnx.json beside it passes", check_voice_file(good) == (True, ""))
    why = {p.name: check_voice_file(p) for p in (gone, no_json, bad_json, other_json, empty, not_onnx)}
    case("check: every unusable file is refused", all(ok is False for ok, _ in why.values()))
    case("check: a missing file is named in full", why["gone.onnx"][1] == f"{gone} was not found")
    case("check: a missing .onnx.json is named",
         why["no_json.onnx"][1] == "no_json.onnx.json was not found beside no_json.onnx")
    case("check: an unreadable .onnx.json is named", why["bad_json.onnx"][1].startswith("bad_json.onnx.json could not"))
    case("check: a .json that is not a voice's is named",
         why["other_json.onnx"][1] == "other_json.onnx.json is not the settings file of a Piper voice")
    case("check: an empty file and a file that is not .onnx are named",
         why["empty.onnx"][1] == "empty.onnx is an empty file" and "song.mp3 is not" in why["song.mp3"][1])
    case("check: no choice is not a voice, and never an error", check_voice_file("") == (False, "no file was chosen")
         and check_voice_file(None)[0] is False)
    case("the folder lists only the voices that pass, by name",
         [p.name for p in list_voice_files(mine)] == ["my_voice.onnx", "wont_load.onnx"]
         and list_voice_files(tmp / "no such folder") == [])

    loaded, played, fx = [], [], []

    class FakeVoice:
        def __init__(self, path):
            self.path = str(path)

        def synthesize_wav(self, text, wf, syn_config=None):
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x10" * 160)

    def fake_load(path):
        loaded.append(str(path))
        if Path(path).name == "wont_load.onnx":
            raise RuntimeError("not a model")
        return FakeVoice(path)

    class FakeFx:                                           # stands where voice_fx stands: records, changes nothing
        @staticmethod
        def apply(audio, sr, speaker, preset):
            fx.append((speaker, preset))
            return audio

        @staticmethod
        def _limit(y, sr):
            return y

    def line(s, speaker):
        """Say one line and wait for it; True when it was played (so the speaker was not silent)."""
        n = len(played)
        s.say("hello", speaker, PRIORITY_EVENT)
        for _ in range(100):
            if len(played) > n:
                return True
            time.sleep(0.02)
        return False

    def make(**custom):
        return Speech(builtin, cache_dir=cache, play=lambda audio, sr: played.append((len(audio), sr)),
                      load=fake_load, custom_voices=custom)

    real_fx, voice_fx = voice_fx, FakeFx
    made = []
    try:
        s = make()
        made.append(s)
        case("default: with no custom voice the built-in voice speaks",
             line(s, "elah") and s.voice_source("elah") == "trained" and s.voice_problem("elah") == ""
             and loaded == [str(builtin / "elah.onnx")])

        del loaded[:], fx[:]
        s = make(elah=str(good))
        made.append(s)
        case("a valid custom file is the voice that is loaded and speaks",
             line(s, "elah") and loaded == [str(good)] and s.voice_source("elah") == "custom"
             and s.voice_problem("elah") == "" and played[-1] == (160, 16000))
        case("the other speaker keeps its built-in voice",
             line(s, "montaigne") and s.voice_source("montaigne") == "trained"
             and loaded == [str(good), str(builtin / "montaigne.onnx")])
        case("the character is applied to a custom voice exactly as to a built-in one",
             fx == [("elah", "default"), ("montaigne", "default")])

        for bad, said in ((gone, f"{gone} was not found"),
                          (no_json, "no_json.onnx.json was not found beside no_json.onnx"),
                          (wont_load, "wont_load.onnx could not be loaded as a voice")):
            del loaded[:]
            s = make(elah=str(bad))
            made.append(s)
            case(f"{bad.name}: the line is still spoken, with the built-in voice, and the reason is given",
                 line(s, "elah") and s.voice_source("elah") == "trained" and s.voice_problem("elah") == said
                 and loaded[-1] == str(builtin / "elah.onnx"))

        del loaded[:]
        s.set_custom_voice("elah", str(good))               # s is on its built-in voice (wont_load); change it
        case("a new choice is not used before reload()", line(s, "elah") and s.voice_source("elah") == "trained")
        s.reload()
        case("reload() applies a new choice without a restart",
             s.voice_source("elah") == "not loaded" and s.voice_problem("elah") == "" and line(s, "elah")
             and s.voice_source("elah") == "custom"
             and s.voice_problem("elah") == "" and loaded == [str(good)])
        s.set_custom_voice("elah", "")
        s.reload()
        case("choosing the built-in voice again goes back to it",
             line(s, "elah") and s.voice_source("elah") == "trained" and s.voice_problem("elah") == "")
    finally:
        voice_fx = real_fx
        for s in made:
            s.close()


def _selftest() -> int:
    import tempfile
    results = []

    def case(name, cond):
        results.append((name, bool(cond)))

    with tempfile.TemporaryDirectory(prefix="suitmk2_speech_selftest_") as tmp:
        _selftest_voices(case, Path(tmp))

    played, clock = [], [0.0]
    gate = threading.Event()

    def fake_synth(text, speaker):
        return (f"{speaker}:{text}", 22050)

    def fake_play(audio, sr):
        gate.wait(2)
        played.append(audio)

    s = Speech(Path("."), synth=fake_synth, play=fake_play, now=lambda: clock[0])
    s.say("ambient one", "elah", PRIORITY_AMBIENT)          # starts playing (blocks on gate)
    time.sleep(0.2)
    s.say("ambient two", "montaigne", PRIORITY_AMBIENT)
    s.say("you are hurt", "elah", PRIORITY_EVENT)           # jumps ahead of 'ambient two'
    gate.set()
    time.sleep(0.5)
    case("lines never overlap and keep order within the queue", played[:1] == ["elah:ambient one"])
    case("an event line jumps ahead of queued ambient", played[1:3] == ["elah:you are hurt", "montaigne:ambient two"])

    gate.clear()
    s.say("old news", "elah", PRIORITY_EVENT)
    clock[0] += 120                                          # it waited two minutes in the queue
    gate.set()
    time.sleep(0.4)
    case("a stale line is dropped, not spoken late", "elah:old news" not in played and s.dropped_stale >= 1)

    s.mute(True)
    case("muted: say() refuses", s.say("anything", "elah") is False)
    s.mute(False)
    case("unknown speaker refused", s.say("hi", "ghost") is False)

    def bad_synth(text, speaker):
        raise RuntimeError("voice file missing")
    s2 = Speech(Path("."), synth=bad_synth, play=fake_play, now=lambda: clock[0])
    s2.say("x", "elah", PRIORITY_EVENT)
    time.sleep(0.3)
    s2.say("y", "elah", PRIORITY_EVENT)
    time.sleep(0.3)
    case("a failing line does not kill the voice thread", s2._thread.is_alive())
    # A sequence (words, static, gaps) is ONE line: one play, in order, and refused like a line when muted.
    del played[:]
    gate.set()
    seq = [("static", 0.5), ("gap", 0.07), ("say", "letters"), ("static", 0.2), ("say", "where was I")]
    case("a sequence is accepted and a sequence with no words is not",
         s.say_sequence(seq, "montaigne", PRIORITY_AMBIENT, text="[static] letters [static] where was I") is True
         and s.say_sequence([("static", 0.5)], "montaigne") is False)
    time.sleep(0.4)
    case("a sequence is played once, its parts in order",
         played == [[("static", 0.5), ("gap", 0.07), "montaigne:letters", ("static", 0.2), "montaigne:where was I"]])
    case("the status window shows it as one line", s.spoken[-1][1:] == ("montaigne", "[static] letters [static] where was I"))
    s.mute(True)
    case("muted: say_sequence refuses", s.say_sequence(seq, "montaigne") is False)
    s.mute(False)
    try:
        import numpy as np
        a, _f = static_burst(0.55, 22050, static_seed(0), 0.1)
        b, _f2 = static_burst(0.55, 22050, static_seed(0), 0.1)
        rms = float(np.sqrt(np.mean(a ** 2)))
        case("static: the stored length, the same every time, at its level (0.8 band-passed, 0.4 unfiltered)",
             len(a) == int(0.55 * 22050) and np.array_equal(a, b)
             and abs(rms - (STATIC_LEVEL if _f else STATIC_LEVEL_UNFILTERED) * 0.1) < 0.002)
    except ImportError:
        case("static: numpy is there to make it", False)
    s.close(); s2.close()
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    bad = sum(not ok for _, ok in results)
    print(f"speech selftest: {len(results) - bad}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())

"""speech.py - local two-voice speech for the SuitMk2 toolbox tool (2026-09-23).

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
  * mute() stops the current line and clears the queue.
  * ANSWERS (J 2026-10-05). A line said with addressed=True is an answer to something the pilot asked with the
    talk key. While muted, such a line is still spoken if allow_addressed(True) was called; every other line is
    refused exactly as before. This is how SuitMk2 stays silent with its window hidden ("only have the AI's talk
    while it is launched", J 2026-10-04) and still answers a push-to-talk question asked from the game. The window
    decides when (ui/suit_window.py _voice_gate); nothing here opens the pass by itself, and it starts closed.
"""
from __future__ import annotations

import heapq
import io
import itertools
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


class Speech:
    def __init__(self, voices_dir: Path, cache_dir: Optional[Path] = None, volume: float = 0.9,
                 synth: Optional[Callable[[str, str], tuple]] = None,
                 play: Optional[Callable[[object, int], None]] = None,
                 now: Callable[[], float] = time.time,
                 fx_presets: Optional[dict] = None, ducker=None):
        """synth/play are injectable for tests: synth(text, speaker) -> (float32 audio, sr); play(audio, sr).
        fx_presets: {speaker: voice_fx preset}; default FX_PRESET. ducker: a started voice_fx.DuckingMonitor, or
        None to never wait/duck (tests, or no pycaw)."""
        self.voices_dir, self.cache = Path(voices_dir), cache_dir or (Path.home() / ".cache" / "piper")
        self.volume, self.now = volume, now
        self._voices: dict[str, object] = {}
        self._voice_src: dict[str, str] = {}
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
        # 100% gets louder instead of clipping. J's dry run 2026-09-23: "They sound really quiet".
        self.levels: dict[str, float] = {"elah": 1.0, "montaigne": 1.0}
        self.ducker = ducker
        self.held_s = 0.0                                   # total time spent waiting for the game (status window)
        self._abort = threading.Event()                     # stops a streamed (duckable) line on mute()
        self._thread = threading.Thread(target=self._loop, name="suitmk2_speech", daemon=True)
        self._thread.start()

    # -- voices -------------------------------------------------------------------------------------------------
    def voice_source(self, speaker: str) -> str:
        """'trained' / 'stock' / 'not loaded' (for the status window)."""
        return self._voice_src.get(speaker, "not loaded")

    def preload(self) -> None:
        """Load both voices now (on the speech thread's behalf, from any thread) so the dashboard shows them loaded
        and neither character's first line pays the ~2 s model load. Failures are logged, never raised."""
        for spk in STOCK:
            try:
                self._voice(spk)
            except Exception as e:
                log.warning("speech: preload %s failed: %s", spk, e)

    def reload(self) -> None:
        self._voices.clear()
        self._voice_src.clear()

    def _voice(self, speaker: str):
        if speaker not in self._voices:
            from piper import PiperVoice  # type: ignore
            trained = self.voices_dir / f"{speaker}.onnx"
            if trained.exists() and trained.with_suffix(".onnx.json").exists():
                path, src = trained, "trained"
            else:
                path, src = _stock_path(speaker, self.cache), "stock"
            self._voices[speaker] = PiperVoice.load(str(path))
            self._voice_src[speaker] = src
            log.info("speech: %s voice = %s (%s)", speaker, path.name, src)
        return self._voices[speaker]

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


def _selftest() -> int:
    results = []

    def case(name, cond):
        results.append((name, bool(cond)))

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
    s.close(); s2.close()
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    bad = sum(not ok for _, ok in results)
    print(f"speech selftest: {len(results) - bad}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())

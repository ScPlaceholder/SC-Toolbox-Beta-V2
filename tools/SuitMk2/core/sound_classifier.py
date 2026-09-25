"""sound_classifier.py - game ears that know WHAT they hear (J approved 2026-09-23).

    StarCitizen.exe --(bin/sc_audio_tap.exe: Windows per-PROCESS loopback, 16 kHz mono f32le)--> this thread
        --> 0.975 s windows, hop ~0.49 s --> numpy log-mel (YAMNet's own frontend) --> YAMNet ONNX on CPU
        --> {gunfire, explosion, engine, music, speech, other} scores --> recent(seconds) / gunfire_confirm()

Why a separate process tap: process loopback hears ONLY the target's process tree, so voice chat, Discord, and
the companion's own voices can never be classified as "the game". (combat_watch's meter was already per-process;
this keeps that property while hearing the actual waveform.)

Model: YAMNet (AudioSet, 521 classes), Qualcomm AI Hub float ONNX, ~15 MB, MIT; provenance + hashes in
models/yamnet/SOURCE.json. The graph takes a [1,1,96,64] log-mel patch and returns logits, so the frontend is
computed here and a sigmoid applied; both were checked against Google's own graph (see SOURCE.json).

CPU discipline (memory and cores are shared with the game): one ORT thread, no memory arena, a window whose RMS is
below SILENCE_RMS is scored as silence without running the model, and a missing StarCitizen.exe is re-checked every
RETRY_S with psutil instead of spawning anything.

Failure policy: every public method returns a neutral value (None / zeros / False) and never raises into the
caller. No model, no tap binary, no onnxruntime, no game: the classifier simply is not running, and
gunfire_confirm() says None ("I cannot tell"), which CombatWatch already treats as "no second opinion".

Selftest (no Star Citizen needed; plays ~6 s of QUIET audio on the default device for the tap tests):
    python sound_classifier.py --selftest [--no-play]
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

log = logging.getLogger("suitmk2.sound")

ROOT = Path(__file__).resolve().parent.parent
TAP_EXE = ROOT / "bin" / "sc_audio_tap.exe"
MODEL_DIR = ROOT / "models" / "yamnet"

RATE = 16_000
WINDOW = 15_600                 # 0.975 s: YAMNet's 96 frames of 10 ms + the 25 ms tail
HOP = 7_800                     # ~0.49 s: each moment is seen by two windows, so a shot on an edge is not lost
SILENCE_RMS = 3e-4              # ~ -70 dBFS: below this the model is not run
HISTORY_S = 30.0
RETRY_S = 10.0                  # how often to look for the game when it is not running
CONFIRM_WINDOW_S = 2.0
CONFIRM_THRESHOLD = 0.5         # gunfire group
EXPLOSION_THRESHOLD = 0.65      # explosion group is broader ("Boom"): a diesel backfire in the CC0 engine clip
                                # scores 0.53-0.55 once normalised, a real battle 1.00
NORM_PEAK = 0.8                 # each window is peak-normalised to this before the model...
NORM_MAX_GAIN = 100.0           # ...with at most +40 dB. See classify() for why.
LIVE_S = 3.0                    # no window scored in this long = not running (tap stalled or gone)

GROUPS = ("gunfire", "explosion", "engine", "music", "speech", "other")
# AudioSet display names (models/yamnet/labels.txt). Resolved by NAME at load, so an index shift cannot silently
# re-map a group. "Cap gun" is in gunfire on purpose: a real recorded pistol (tests_audio/gunshots_8_pd.wav) scores
# Cap gun 0.89 over Gunshot 0.78, and game weapons are designed sounds, further still from field recordings.
GROUP_CLASSES = {
    "gunfire": ["Gunshot, gunfire", "Machine gun", "Fusillade", "Artillery fire", "Cap gun"],
    "explosion": ["Explosion", "Boom"],
    "engine": ["Vehicle", "Motor vehicle (road)", "Car", "Engine", "Light engine (high frequency)",
               "Medium engine (mid frequency)", "Heavy engine (low frequency)", "Engine starting", "Idling",
               "Accelerating, revving, vroom", "Aircraft", "Aircraft engine", "Jet engine", "Propeller, airscrew",
               "Helicopter", "Fixed-wing aircraft, airplane", "Race car, auto racing", "Truck", "Motorcycle"],
    "music": ["Music", "Musical instrument", "Ambient music", "Electronic music", "Soundtrack music",
              "Background music", "Orchestra", "Synthesizer"],
    "speech": ["Speech", "Child speech, kid speaking", "Conversation", "Narration, monologue", "Speech synthesizer",
               "Shout", "Yell", "Whispering"],
}


def _hidden_startupinfo():
    """Toolbox convention: STARTUPINFO + SW_HIDE, NEVER CREATE_NO_WINDOW (segfaults PySide6 on py3.14)."""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0          # SW_HIDE
    return si


class _Frontend:
    """YAMNet's log-mel: 25 ms periodic Hann, 10 ms hop, 512-pt FFT magnitude, 64 HTK-mel bands 125-7500 Hz,
    log(mel + 0.001). 96 frames -> one [96, 64] patch."""

    def __init__(self, np):
        self.np = np
        hz2mel = lambda f: 1127.0 * np.log1p(f / 700.0)       # noqa: E731
        spec_mel = hz2mel(np.linspace(0, RATE / 2, 257))
        edges = np.linspace(hz2mel(125.0), hz2mel(7500.0), 66)
        w = np.zeros((257, 64), np.float32)
        for i in range(64):
            lo, c, hi = edges[i:i + 3]
            w[:, i] = np.maximum(0, np.minimum((spec_mel - lo) / (c - lo), (hi - spec_mel) / (hi - c)))
        w[0, :] = 0
        self.mel = w
        self.win = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(400) / 400)).astype(np.float32)
        self.idx = np.arange(400)[None, :] + 160 * np.arange(96)[:, None]

    def patch(self, x):
        np = self.np
        spec = np.abs(np.fft.rfft(x[self.idx] * self.win, 512))
        return np.log(spec @ self.mel + 0.001).astype(np.float32)[None, None]


class SoundClassifier:
    def __init__(self, target: str = "StarCitizen.exe", pid: Optional[int] = None,
                 tap_exe: Path = TAP_EXE, model_dir: Path = MODEL_DIR, threshold: float = CONFIRM_THRESHOLD,
                 now=time.time):
        self.target, self.pid, self.tap_exe, self.model_dir = target, pid, Path(tap_exe), Path(model_dir)
        self.threshold, self.now = threshold, now
        self._hist: deque = deque()                 # (t, {group: score})
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self._sess = self._np = self._front = None
        self._groups: dict = {}                     # group -> index array
        self._other = None
        self._last_scored = 0.0
        self.status = "stopped"
        self.stats = {"windows": 0, "silent": 0, "inferred": 0, "infer_ms_last": 0.0, "infer_ms_avg": 0.0,
                      "infer_ms_max": 0.0, "tap_starts": 0}

    # -- model --------------------------------------------------------------------------------------------------
    def load(self) -> bool:
        """Load numpy + onnxruntime + the model. False (never raises) when any piece is missing."""
        if self._sess is not None:
            return True
        try:
            import numpy as np
            import onnxruntime as ort
            labels = (self.model_dir / "labels.txt").read_text(encoding="utf-8").splitlines()
            so = ort.SessionOptions()
            so.intra_op_num_threads = 1
            so.inter_op_num_threads = 1
            so.enable_cpu_mem_arena = False
            so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            so.log_severity_level = 3
            sess = ort.InferenceSession(str(self.model_dir / "yamnet.onnx"), so, providers=["CPUExecutionProvider"])
            pos = {name.strip(): i for i, name in enumerate(labels)}
            groups, used = {}, set()
            for g, names in GROUP_CLASSES.items():
                idx = [pos[n] for n in names if n in pos]
                missing = [n for n in names if n not in pos]
                if missing:
                    log.warning("sound classifier: %s classes not in labels: %s", g, missing)
                groups[g] = np.array(idx, dtype=np.int64)
                used.update(idx)
            self._other = np.array([i for i in range(len(labels)) if i not in used], dtype=np.int64)
            self._np, self._front, self._groups = np, _Frontend(np), groups
            self._input = sess.get_inputs()[0].name
            self._sess = sess
            return True
        except Exception as e:
            log.warning("sound classifier unavailable: %s", e)
            self.status = f"no model ({type(e).__name__})"
            return False

    def classify(self, wave) -> Optional[dict]:
        """One 0.975 s window (float32 mono 16 kHz, >= WINDOW samples) -> {group: score 0..1}, or None."""
        if not self.load():
            return None
        try:
            np = self._np
            x = np.nan_to_num(np.asarray(wave, dtype=np.float32)[:WINDOW], nan=0.0, posinf=0.0, neginf=0.0)
            x = np.clip(x, -1.0, 1.0)
            if len(x) < WINDOW:
                x = np.pad(x, (0, WINDOW - len(x)))
            if float(np.sqrt(np.mean(x * x))) < SILENCE_RMS:
                self.stats["silent"] += 1
                return {g: 0.0 for g in GROUPS}
            # YAMNet is badly level-sensitive (log(mel + 0.001) has an absolute floor): the PD gunshot clip scores
            # gunfire 0.90 as recorded and 0.02 at -12 dB. What we capture is AFTER SC's in-game volume and its
            # Windows mixer volume, so the pilot's volume knob would decide the class. Peak-normalising each window
            # made every test clip score the same from 0 dB down to -30 dB (measured 2026-09-23).
            x = x * min(NORM_PEAK / max(float(np.abs(x).max()), 1e-9), NORM_MAX_GAIN)
            t0 = time.perf_counter()
            logits = self._sess.run(None, {self._input: self._front.patch(x)})[0][0]
            ms = (time.perf_counter() - t0) * 1000
            s = self.stats
            s["inferred"] += 1
            s["infer_ms_last"] = ms
            s["infer_ms_max"] = max(s["infer_ms_max"], ms) if s["inferred"] > 3 else s["infer_ms_max"]  # skip warm-up
            s["infer_ms_avg"] += (ms - s["infer_ms_avg"]) / min(s["inferred"], 50)
            p = 1.0 / (1.0 + np.exp(-np.clip(logits, -30.0, 30.0)))
            out = {g: float(p[idx].max()) if len(idx) else 0.0 for g, idx in self._groups.items()}
            out["other"] = float(p[self._other].max())
            return out
        except Exception as e:
            log.warning("sound classify failed: %s", e)
            return None

    # -- lifecycle ----------------------------------------------------------------------------------------------
    def start(self) -> bool:
        """Start the listening thread. False (never raises) when the tap or model is missing."""
        try:
            if self._thread is not None and self._thread.is_alive():
                return True
            if not self.tap_exe.is_file():
                self.status = "no tap binary"
                log.warning("sound classifier: %s missing", self.tap_exe)
                return False
            if not self.load():
                return False
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="suitmk2_sound", daemon=True)
            self._thread.start()
            return True
        except Exception as e:
            log.warning("sound classifier start failed: %s", e)
            return False

    def stop(self) -> None:
        try:
            self._stop.set()
            self._kill_tap()
            if self._thread is not None:
                self._thread.join(timeout=2.0)
            self.status = "stopped"
        except Exception:
            pass

    @property
    def running(self) -> bool:
        """Listening AND recently scored a window: the only state in which an answer means anything."""
        return (self._thread is not None and self._thread.is_alive() and self._proc is not None
                and self.now() - self._last_scored <= LIVE_S)

    # -- answers ------------------------------------------------------------------------------------------------
    def recent(self, seconds: float = CONFIRM_WINDOW_S) -> dict:
        """Max score per group over the last `seconds`. All zeros when nothing was heard (or not running)."""
        out = {g: 0.0 for g in GROUPS}
        try:
            cut = self.now() - seconds
            with self._lock:
                rows = [sc for t, sc in self._hist if t >= cut]
            for sc in rows:
                for g in GROUPS:
                    out[g] = max(out[g], sc.get(g, 0.0))
        except Exception:
            pass
        return out

    def is_combat(self, scores: dict) -> bool:
        return scores.get("gunfire", 0.0) >= self.threshold or scores.get("explosion", 0.0) >= EXPLOSION_THRESHOLD

    def gunfire_confirm(self) -> Optional[bool]:
        """CombatWatch confirm: True if gunfire (>= threshold) or an explosion (>= EXPLOSION_THRESHOLD) was heard
        within ~2 s, False if the game is being heard and it is not that, None if not running (no opinion)."""
        try:
            if not self.running:
                return None
            return self.is_combat(self.recent(CONFIRM_WINDOW_S))
        except Exception:
            return None

    # -- the listening thread -----------------------------------------------------------------------------------
    def _target_alive(self) -> bool:
        if self.pid is not None:
            return True
        try:
            import psutil
            want = self.target.lower()
            return any((p.info.get("name") or "").lower() == want for p in psutil.process_iter(["name"]))
        except Exception:
            return True                             # cannot tell: let the tap itself decide

    def _spawn_tap(self) -> Optional[subprocess.Popen]:
        args = [str(self.tap_exe)] + (["--pid", str(self.pid)] if self.pid is not None else ["--name", self.target])
        try:
            p = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 startupinfo=_hidden_startupinfo(), bufsize=0)
            self.stats["tap_starts"] += 1
            return p
        except Exception as e:
            log.warning("sound tap spawn failed: %s", e)
            return None

    def _kill_tap(self) -> None:
        p, self._proc = self._proc, None
        if p is not None:
            try:
                p.kill()
                p.wait(timeout=2)
            except Exception:
                pass

    def _run(self) -> None:
        np = self._np
        while not self._stop.is_set():
            if not self._target_alive():
                self.status = f"waiting for {self.target}"
                self._stop.wait(RETRY_S)
                continue
            self._proc = self._spawn_tap()
            if self._proc is None:
                self.status = "tap failed to start"
                self._stop.wait(RETRY_S)
                continue
            self.status = "listening"
            buf = np.zeros(0, dtype=np.float32)
            carry = b""
            stream = self._proc.stdout
            try:
                while not self._stop.is_set():
                    raw = stream.read(HOP * 4)
                    if not raw:
                        break                       # tap exited: target gone, or capture failed
                    # An unbuffered pipe read can end mid-sample; carry the tail, or every later float is misaligned
                    # garbage (the first live selftest read NaN/1e38 "audio" exactly this way).
                    raw = carry + raw
                    n = len(raw) // 4 * 4
                    carry = raw[n:]
                    buf = np.concatenate([buf, np.frombuffer(raw[:n], dtype="<f4")])
                    while len(buf) >= WINDOW:
                        sc = self.classify(buf[:WINDOW])
                        buf = buf[HOP:]
                        if sc is None:
                            continue
                        t = self.now()
                        self.stats["windows"] += 1
                        with self._lock:
                            self._hist.append((t, sc))
                            while self._hist and t - self._hist[0][0] > HISTORY_S:
                                self._hist.popleft()
                        self._last_scored = t
            except Exception as e:
                log.warning("sound listen loop: %s", e)
            rc = self._proc.poll() if self._proc is not None else None
            self._kill_tap()
            if self._stop.is_set():
                break
            if self.pid is not None:
                self.status = f"target pid {self.pid} gone (tap rc={rc})"
                break                               # a fixed pid never comes back
            self.status = f"tap ended (rc={rc}); retrying"
            self._stop.wait(RETRY_S)


# ---- selftest ----------------------------------------------------------------------------------------------------
def _read_wav(path: Path):
    import wave
    import numpy as np
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == RATE and w.getnchannels() == 1 and w.getsampwidth() == 2, path
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0


def _write_wav(path: Path, x, rate: int = RATE) -> None:
    import wave
    import numpy as np
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def _file_scores(sc: SoundClassifier, x) -> dict:
    """Max per group over every hop-spaced window of a clip, plus the per-window rows."""
    rows = [sc.classify(x[s:s + WINDOW]) for s in range(0, max(1, len(x) - WINDOW + 1), HOP)]
    rows = [r for r in rows if r]
    return {g: max((r[g] for r in rows), default=0.0) for g in GROUPS}, rows


_PLAYER = ("import sys, time, winsound\n"
           "winsound.PlaySound(sys.argv[1], winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)\n"
           "time.sleep(float(sys.argv[2]))\n"
           "winsound.PlaySound(None, 0)\n")


def _selftest(play: bool = True) -> int:
    import numpy as np
    import tempfile
    results = []

    def case(name, cond, detail=""):
        results.append((name, bool(cond)))
        print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))

    fmt = lambda d: " ".join(f"{g}={d[g]:.2f}" for g in GROUPS)       # noqa: E731
    audio = ROOT / "tests_audio"

    # 0. fails closed: no model / no tap -> not running, None, zeros, no exception.
    dead = SoundClassifier(model_dir=ROOT / "nope", tap_exe=ROOT / "nope.exe")
    case("missing pieces: start() is False, confirm is None, recent is zeros",
         dead.start() is False and dead.gunfire_confirm() is None and not any(dead.recent(5).values()))
    dead.stop()

    sc = SoundClassifier()
    case("model loads (onnxruntime CPU)", sc.load())
    if not sc.load():
        print("sound_classifier selftest: cannot continue without the model")
        return 1
    case("idle classifier: gunfire_confirm is None (not running)", sc.gunfire_confirm() is None)

    # 1. real recordings -> groups.
    gun, gun_rows = _file_scores(sc, _read_wav(audio / "gunshots_8_pd.wav"))
    print(f"    gunshots_8_pd.wav     max {fmt(gun)}")
    case("recorded gunshots -> gunfire >= 0.5", gun["gunfire"] >= 0.5, f"gunfire={gun['gunfire']:.2f}")
    case("recorded gunshots: gunfire beats engine and music",
         gun["gunfire"] > gun["engine"] and gun["gunfire"] > gun["music"])
    eng, eng_rows = _file_scores(sc, _read_wav(audio / "detroit62_engine_cc0.wav"))
    print(f"    detroit62_engine.wav  max {fmt(eng)}")
    case("recorded diesel engine -> engine >= 0.5", eng["engine"] >= 0.5, f"engine={eng['engine']:.2f}")
    case("recorded diesel engine would NOT confirm combat", not sc.is_combat(eng),
         f"gunfire={eng['gunfire']:.2f} explosion={eng['explosion']:.2f}")
    quiet, _ = _file_scores(sc, _read_wav(audio / "gunshots_8_pd.wav") * 0.03)
    case("the same gunshots 30 dB quieter still -> gunfire >= 0.5 (volume knob does not decide the class)",
         quiet["gunfire"] >= 0.5, f"gunfire={quiet['gunfire']:.2f}")
    war, _ = _file_scores(sc, _read_wav(audio / "war_sounds_cc0_8s.wav"))
    print(f"    war_sounds_8s.wav     max {fmt(war)}")
    case("battle recording -> gunfire and explosion >= 0.5", war["gunfire"] >= 0.5 and war["explosion"] >= 0.5)
    silent = sc.classify(np.zeros(WINDOW, np.float32))
    case("digital silence is scored zero without running the model", silent and not any(silent.values()))

    # 2. confirm logic on a fed history (no tap).
    clk = [1000.0]
    fake = SoundClassifier(now=lambda: clk[0])
    fake._thread, fake._proc = threading.current_thread(), object()      # pretend: listening
    fake._last_scored = clk[0]
    fake._hist.append((clk[0] - 0.5, {**{g: 0.0 for g in GROUPS}, "gunfire": 0.8}))
    case("fed gunfire 0.8 half a second ago -> confirm True", fake.gunfire_confirm() is True)
    clk[0] += 3.0
    fake._last_scored = clk[0]
    case("same shot 3.5 s ago -> confirm False (listening, nothing now)", fake.gunfire_confirm() is False)
    clk[0] += LIVE_S + 1
    case("no window scored for > LIVE_S -> None (stalled tap is no opinion)", fake.gunfire_confirm() is None)
    fake._thread = fake._proc = None

    if not play:
        print("  SKIP  tap tests (--no-play)")
    elif not TAP_EXE.is_file():
        case("bin/sc_audio_tap.exe exists", False)
    else:
        tmp = Path(tempfile.mkdtemp(prefix="suitmk2_tap_"))
        t = np.arange(RATE * 2) / RATE
        _write_wav(tmp / "a440.wav", 0.005 * np.sin(2 * np.pi * 440 * t))   # ~ -46 dBFS: barely audible
        _write_wav(tmp / "b1000.wav", 0.005 * np.sin(2 * np.pi * 1000 * t))
        si = _hidden_startupinfo()

        def player(wav, secs):
            return subprocess.Popen([sys.executable, "-c", _PLAYER, str(wav), str(secs)], startupinfo=si)

        def tap_seconds(pid, secs):
            p = subprocess.Popen([str(TAP_EXE), "--pid", str(pid)], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, startupinfo=si)
            raw = p.stdout.read(int(RATE * secs) * 4)
            p.kill()
            p.wait()
            return np.frombuffer(raw, dtype="<f4")

        def band(x, f0):
            sp = np.abs(np.fft.rfft(x * np.hanning(len(x))))
            fr = np.fft.rfftfreq(len(x), 1 / RATE)
            return float(sp[(fr > f0 - 15) & (fr < f0 + 15)].sum())

        # 3. isolation: two processes at once, tap each, each hears only itself.
        a, b = player(tmp / "a440.wav", 3.5), player(tmp / "b1000.wav", 3.5)
        time.sleep(0.8)
        xa, xb = tap_seconds(a.pid, 1.0), tap_seconds(b.pid, 1.0)
        a.wait(), b.wait()
        ra = band(xa, 440) / max(band(xa, 1000), 1e-9)
        rb = band(xb, 1000) / max(band(xb, 440), 1e-9)
        print(f"    tap A: {len(xa)} samples peak {np.abs(xa).max():.4f}  440/1000 Hz energy ratio {ra:.0f}x")
        print(f"    tap B: {len(xb)} samples peak {np.abs(xb).max():.4f}  1000/440 Hz energy ratio {rb:.0f}x")
        case("tap A captured its own 440 Hz tone (real audio, not silence)",
             len(xa) == RATE and np.abs(xa).max() > 0.001)
        case("tap A did NOT capture process B's simultaneous 1000 Hz tone (>= 100x)", ra >= 100, f"{ra:.0f}x")
        case("control: tap B hears B's tone and not A's (>= 100x)", rb >= 100 and np.abs(xb).max() > 0.001,
             f"{rb:.0f}x")

        # 4. the whole chain: classifier tapping a player of gunshots while another process plays the engine.
        gsrc = _read_wav(audio / "gunshots_8_pd.wav")
        _write_wav(tmp / "gun.wav", gsrc[int(0.9 * RATE):int(3.9 * RATE)] * 0.25)   # ~ -12 dB of the recording
        _write_wav(tmp / "eng.wav", _read_wav(audio / "detroit62_engine_cc0.wav")[int(2.9 * RATE):int(5.9 * RATE)]
                   * 0.25)
        g, e = player(tmp / "gun.wav", 3.2), player(tmp / "eng.wav", 3.2)
        live = SoundClassifier(pid=g.pid)
        live.start()
        t0 = time.time()
        while time.time() - t0 < 3.0:
            time.sleep(0.1)
        heard, conf, st = live.recent(3.0), live.gunfire_confirm(), dict(live.stats)
        live.stop()
        g.wait(), e.wait()
        print(f"    live chain (tapping the gunshot player): {fmt(heard)}  confirm={conf}  "
              f"windows={st['windows']} infer avg {st['infer_ms_avg']:.1f} ms")
        case("live: tap -> classifier hears gunfire from the tapped process", heard["gunfire"] >= 0.5 and conf is True)
        case("live: the other process's engine did not leak in (engine < 0.5)", heard["engine"] < 0.5,
             f"engine={heard['engine']:.2f}")
        case("stop(): thread and tap are gone", not live.running and live._proc is None)

    bad = sum(not ok for _, ok in results)
    print(f"sound_classifier selftest: {len(results) - bad}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if "--selftest" in sys.argv:
        sys.exit(_selftest(play="--no-play" not in sys.argv))
    print(__doc__)

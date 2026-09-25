"""voice_fx.py - in-world character for the SuitMk2 companion voices, plus ducking under Star Citizen (2026-09-23).

Two jobs, both pure CPU, no models, nothing beyond numpy / scipy / pycaw+comtypes+psutil (all already in the
toolbox Python):

1. apply(audio, sr, speaker, preset) -> audio
     Elah       = HELMET COMMS. Band 300-3400 Hz: a Chebyshev-II high-pass (flat above ~330 Hz, -30 dB by 250 Hz)
                  because Piper lessac's F0 body sits at 150-300 Hz and a gentle Butterworth left it only 5 dB down,
                  and a 3rd-order Butterworth top (rolled, not telephone-brickwall, which is where harshness lives),
                  a small 2.2 kHz presence lift so consonants survive the band, light tanh
                  saturation BEFORE the filter (so its harmonics are filtered too), a 13 ms helmet-shell slap and a
                  fainter 26 ms second reflection, and a very light band-limited noise floor that only exists while
                  she is talking (a keyed comms channel, not hiss under silence).
     Montaigne  = SHIP SPEAKERS. Wider band (160-5500 Hz), a small metallic room (one short feedback comb for
                  the ring of a steel cabin + four early-reflection taps), and a very subtle "semi-broken AI" grit:
                  a 47 Hz ring-mod at 6% mix and a 12-bit crush blended at 15%. He is damaged, not a robot.
   Every non-bypass preset ends in the same two stages: loudness-normalise the SPEECH frames (silence is excluded
   so padding cannot skew it) to TARGET_RMS, then a look-ahead hard limiter to CEILING. Output is float32 mono and
   is LONGER than the input by fx_tail_samples() so reflections are not chopped.
   Presets: "clean" (true bypass, returns a float32 copy), "default", "heavy".

2. DuckingMonitor - how loud is Star Citizen RIGHT NOW, so a line can wait / play quieter while the game talks.
   Feasibility finding (measured, see _selftest): WASAPI exposes a per-SESSION peak meter
   (IAudioMeterInformation queried from IAudioSessionControl), and pycaw+comtypes are installed, so this reads
   StarCitizen.exe's OWN output level - no capture stream, no loopback, no audio data copied, and crucially it
   does NOT hear our own companion voice (python.exe is a different session), so it cannot duck under itself.
   Sessions are enumerated on EVERY active render device, not just the default, because SC is often pointed at a
   headset that is not the Windows default.
   Limitation: the meter is the session's post-volume PEAK, and SC is one session - it cannot tell dialogue from
   engine noise / gunfire / music. The threshold therefore has to sit above SC's ambient bed; tune it with
   `python voice_fx.py --probe StarCitizen.exe` while flying. mode="others" (every session except our own
   process) exists as a fallback; mode="endpoint" (whole-device meter) is the last resort and DOES include our
   own voice, so the caller must not consult it while it is playing.

Run: python voice_fx.py            -> selftest (renders fx_*.wav for listening, plays NOTHING)
     python voice_fx.py --probe [ProcessName.exe] [seconds]  -> print live session peaks, plays nothing
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from functools import lru_cache
from typing import Callable, Iterable, Optional

import numpy as np
from scipy import signal
from scipy.ndimage import minimum_filter1d, uniform_filter1d

log = logging.getLogger(__name__)
# comtypes logs every COM Release at DEBUG, and the toolbox's catch-all log runs at DEBUG: the meter poll produced
# ~47 lines/s, 99.98% of suitmk2.crash.log, rotating every real line out within minutes (dry run 2026-09-23).
logging.getLogger("comtypes").setLevel(logging.WARNING)

TARGET_RMS = 0.12            # ~ -18.4 dBFS over speech frames
CEILING = 0.95               # ~ -0.45 dBFS after the limiter
_GATE_REL_DB = -40.0         # a 10 ms frame is "speech" when within 40 dB of the loudest frame

# ---------------------------------------------------------------------------------------------------------------
# presets
# ---------------------------------------------------------------------------------------------------------------
_P = {
    "elah": {
        "default": dict(hp=250.0, hp_order=6, hp_atten=30.0, lp=3400.0, lp_order=3, presence=(2200.0, 2.0, 1.0),
                        drive=1.8, sat_mix=0.45, taps=((13.0, 0.16), (26.5, 0.06)), comb=None,
                        ring=None, crush=None, noise_db=-50.0),
        "heavy": dict(hp=300.0, hp_order=6, hp_atten=35.0, lp=3000.0, lp_order=3, presence=(2000.0, 3.0, 1.0),
                      drive=3.0, sat_mix=0.75, taps=((14.0, 0.24), (28.5, 0.10)), comb=None,
                      ring=None, crush=(10, 0.25), noise_db=-42.0),
    },
    "montaigne": {
        "default": dict(hp=160.0, hp_order=2, lp=5500.0, lp_order=2, presence=(3000.0, 1.0, 0.8),
                        drive=1.3, sat_mix=0.30, taps=((7.3, 0.18), (11.9, 0.14), (17.1, 0.10), (23.7, 0.07)),
                        comb=(4.1, 0.42, 0.10), ring=(47.0, 0.06), crush=(12, 0.15), noise_db=-56.0),
        "heavy": dict(hp=200.0, hp_order=2, lp=4500.0, lp_order=2, presence=(2800.0, 1.5, 0.8),
                      drive=1.8, sat_mix=0.45, taps=((7.3, 0.24), (11.9, 0.19), (17.1, 0.14), (23.7, 0.10)),
                      comb=(4.1, 0.60, 0.18), ring=(47.0, 0.14), crush=(10, 0.30), noise_db=-50.0),
    },
}
PRESETS = ("clean", "default", "heavy")


def _params(speaker: str, preset: str) -> Optional[dict]:
    if preset == "clean":
        return None
    by = _P.get(speaker)
    if by is None:
        return None                       # unknown speaker: bypass rather than guess a character
    return by.get(preset, by["default"])


def fx_tail_samples(sr: int, speaker: str, preset: str = "default") -> int:
    """How many samples apply() appends so the reflections ring out (0 for bypass)."""
    p = _params(speaker, preset)
    if p is None:
        return 0
    longest = max([d for d, _ in p["taps"]] + [p["comb"][0] * 6 if p["comb"] else 0.0])
    return int(round((longest + 30.0) / 1000.0 * sr))


# ---------------------------------------------------------------------------------------------------------------
# DSP building blocks (coefficients cached per sample rate)
# ---------------------------------------------------------------------------------------------------------------
@lru_cache(maxsize=32)
def _band_sos(sr: int, hp: float, hp_order: int, lp: float, lp_order: int, presence: tuple,
              hp_atten: Optional[float] = None) -> np.ndarray:
    """hp_atten set -> Chebyshev II high-pass whose STOPBAND edge is `hp` (flat passband, steep skirt: removes
    the F0 body below the comms band without dulling anything above it); else Butterworth with -3 dB at `hp`."""
    nyq = sr / 2.0
    if hp_atten:
        parts = [signal.cheby2(hp_order, hp_atten, hp / nyq, "highpass", output="sos")]
    else:
        parts = [signal.butter(hp_order, hp / nyq, "highpass", output="sos")]
    if lp < nyq * 0.98:
        parts.append(signal.butter(lp_order, lp / nyq, "lowpass", output="sos"))
    f0, gain_db, q = presence
    if gain_db and f0 < nyq * 0.9:        # RBJ peaking biquad
        a_ = 10 ** (gain_db / 40.0)
        w0 = 2 * np.pi * f0 / sr
        alpha = np.sin(w0) / (2 * q)
        b = np.array([1 + alpha * a_, -2 * np.cos(w0), 1 - alpha * a_])
        a = np.array([1 + alpha / a_, -2 * np.cos(w0), 1 - alpha / a_])
        parts.append(signal.tf2sos(b / a[0], a / a[0]))
    return np.vstack(parts)


def _frame_env(x: np.ndarray, sr: int) -> tuple[np.ndarray, int]:
    """10 ms RMS envelope per frame, and the frame length."""
    hop = max(1, int(sr * 0.010))
    n = len(x) // hop
    if n == 0:
        return np.zeros(1, np.float32), hop
    fr = x[: n * hop].reshape(n, hop)
    return np.sqrt(np.mean(fr * fr, axis=1) + 1e-12), hop


def _speech_mask(env: np.ndarray) -> np.ndarray:
    peak = float(env.max()) if env.size else 0.0
    if peak <= 1e-6:
        return np.zeros_like(env, dtype=bool)
    return env > peak * 10 ** (_GATE_REL_DB / 20.0)


def _gate_curve(x: np.ndarray, sr: int, total: int) -> np.ndarray:
    """0..1 per-sample curve that is open while there is speech: 5 ms attack, ~120 ms release."""
    env, hop = _frame_env(x, sr)
    open_ = _speech_mask(env).astype(np.float32)
    rel = 12                                              # frames (~120 ms) of hang/release
    held = np.convolve(open_, np.ones(rel, np.float32), mode="full")[: len(open_) + rel]
    held = np.clip(held, 0.0, 1.0)
    curve = np.repeat(held, hop)
    if len(curve) < total:
        curve = np.pad(curve, (0, total - len(curve)))
    curve = curve[:total]
    k = max(1, int(sr * 0.005))
    return uniform_filter1d(curve, k, mode="nearest").astype(np.float32)


def _limit(x: np.ndarray, sr: int, ceiling: float = CEILING) -> np.ndarray:
    """Look-ahead hard limiter. Gain = min over +-1 ms, then averaged over the same window: the average of values
    that are each <= the needed gain at this sample is itself <= it, so no sample can overshoot."""
    a = np.abs(x)
    if a.max() <= ceiling:
        return x
    g = np.minimum(1.0, ceiling / np.maximum(a, 1e-9)).astype(np.float32)
    w = max(3, int(sr * 0.002) | 1)
    g = minimum_filter1d(g, w, mode="nearest")
    g = uniform_filter1d(g, w, mode="nearest")
    return np.clip(x * g, -ceiling, ceiling)


def _normalize(x: np.ndarray, sr: int, target: float = TARGET_RMS) -> np.ndarray:
    env, _ = _frame_env(x, sr)
    m = _speech_mask(env)
    if not m.any():
        return x
    rms = float(np.sqrt(np.mean(env[m] ** 2)))
    return x * (target / max(rms, 1e-9))


def speech_rms(x: np.ndarray, sr: int) -> float:
    """RMS over speech frames only (what _normalize targets); exposed for tests and tuning."""
    env, _ = _frame_env(np.asarray(x, np.float32), sr)
    m = _speech_mask(env)
    return float(np.sqrt(np.mean(env[m] ** 2))) if m.any() else 0.0


_RNG_SEED = 0x5117  # deterministic noise: the same line renders the same way twice


# ---------------------------------------------------------------------------------------------------------------
# public
# ---------------------------------------------------------------------------------------------------------------
def apply(audio: np.ndarray, sr: int, speaker: str, preset: str = "default") -> np.ndarray:
    """Give a clean Piper render its in-world character. Pure; float32 mono in, float32 mono out."""
    x = np.asarray(audio, dtype=np.float32).reshape(-1)
    p = _params(speaker, preset)
    if p is None or x.size == 0:
        return x.copy()
    n = x.size
    tail = fx_tail_samples(sr, speaker, preset)
    pk = float(np.abs(x).max())
    if pk < 1e-6:
        return np.zeros(n + tail, np.float32)
    x = x * (0.9 / pk)
    t = np.arange(n, dtype=np.float32) / sr

    if p["ring"]:                                          # "broken AI": a whisper of ring-mod
        hz, mix = p["ring"]
        x = x * (1.0 - mix) + x * np.sin(2 * np.pi * hz * t, dtype=np.float32) * mix
    if p["crush"]:                                         # quantisation grit, blended in, never alone
        bits, mix = p["crush"]
        q = float(2 ** (bits - 1))
        x = x * (1.0 - mix) + (np.round(x * q) / q) * mix
    d, mix = p["drive"], p["sat_mix"]                       # light saturation before the band
    x = x * (1.0 - mix) + (np.tanh(d * x) / np.tanh(d)) * mix

    sos = _band_sos(sr, p["hp"], p["hp_order"], p["lp"], p["lp_order"], p["presence"], p.get("hp_atten"))
    y = np.zeros(n + tail, np.float32)
    y[:n] = signal.sosfilt(sos, x).astype(np.float32)
    dry = y.copy()

    if p["comb"]:                                          # steel-cabin ring: short feedback comb
        ms, fb, mix = p["comb"]
        dl = max(1, int(sr * ms / 1000.0))
        a = np.zeros(dl + 1, np.float32); a[0] = 1.0; a[dl] = -fb
        y = y + signal.lfilter([1.0], a, dry).astype(np.float32) * mix
    for ms, g in p["taps"]:                                # helmet slap / early reflections
        dl = int(sr * ms / 1000.0)
        y[dl:] += dry[:-dl] * g

    if p["noise_db"] is not None:                          # keyed channel noise: only while talking
        rng = np.random.default_rng(_RNG_SEED)
        nz = signal.sosfilt(sos, rng.standard_normal(n + tail).astype(np.float32)).astype(np.float32)
        nz *= (10 ** (p["noise_db"] / 20.0)) / (float(np.sqrt(np.mean(nz * nz))) + 1e-9)
        y += nz * _gate_curve(dry[:n], sr, n + tail) * float(np.sqrt(np.mean(dry[:n] ** 2)) / TARGET_RMS + 1e-9)

    y = _normalize(y, sr)
    return _limit(y, sr).astype(np.float32)


# ---------------------------------------------------------------------------------------------------------------
# ducking
# ---------------------------------------------------------------------------------------------------------------
class SessionMeter:
    """Peak (0..1) of the WASAPI render sessions belonging to named processes, across every active render device.

    mode="process"  : sessions whose process name is in `names` (case-insensitive). DEFAULT: StarCitizen.exe.
    mode="others"   : every session except this process's own (hears Discord/Spotify too).
    mode="endpoint" : the whole-device meter of every active render device (INCLUDES our own voice).
    COM objects are thread-affine: create and use one instance per thread."""

    REFRESH_S = 5.0                                   # re-enumerate sessions (SC rarely changes session)

    def __init__(self, names: Iterable[str] = ("StarCitizen.exe",), mode: str = "process"):
        self.names = {s.lower() for s in names}
        self.mode = mode
        self._meters: list = []        # [(label, IAudioMeterInformation)]
        self._last_refresh = -1e9
        self._pid_names: dict[int, str] = {}
        self.own_pid = os.getpid()
        self.found: list[str] = []     # labels of what is currently being metered
        self.error: Optional[str] = None

    def _pname(self, pid: int) -> str:
        if pid not in self._pid_names:
            try:
                import psutil
                self._pid_names[pid] = psutil.Process(pid).name()
            except Exception:
                self._pid_names[pid] = ""
        return self._pid_names[pid]

    def refresh(self) -> None:
        import comtypes
        from pycaw.pycaw import IAudioMeterInformation, IAudioSessionControl2, IAudioSessionManager2
        from pycaw.pycaw import AudioUtilities
        meters, found = [], []
        try:
            enum = AudioUtilities.GetDeviceEnumerator()
            coll = enum.EnumAudioEndpoints(0, 1)          # eRender, DEVICE_STATE_ACTIVE
            for i in range(coll.GetCount()):
                dev = coll.Item(i)
                if self.mode == "endpoint":
                    m = dev.Activate(IAudioMeterInformation._iid_, comtypes.CLSCTX_ALL, None)
                    meters.append((f"device{i}", m.QueryInterface(IAudioMeterInformation)))
                    found.append(f"device{i}")
                    continue
                mgr = dev.Activate(IAudioSessionManager2._iid_, comtypes.CLSCTX_ALL, None)
                mgr = mgr.QueryInterface(IAudioSessionManager2)
                se = mgr.GetSessionEnumerator()
                for j in range(se.GetCount()):
                    ctl = se.GetSession(j)
                    if ctl is None:
                        continue
                    pid = ctl.QueryInterface(IAudioSessionControl2).GetProcessId()
                    if pid == 0:
                        continue                          # system sounds
                    name = self._pname(pid)
                    if self.mode == "process" and name.lower() not in self.names:
                        continue
                    if self.mode == "others" and pid == self.own_pid:
                        continue
                    meters.append((f"{name}:{pid}@dev{i}", ctl.QueryInterface(IAudioMeterInformation)))
                    found.append(f"{name}:{pid}@dev{i}")
            self.error = None
        except Exception as e:                            # a device vanishing mid-enumeration is normal
            self.error = f"{type(e).__name__}: {e}"
        self._meters, self.found = meters, found
        self._last_refresh = time.monotonic()

    def peak(self) -> float:
        if time.monotonic() - self._last_refresh > self.REFRESH_S:
            self.refresh()
        best, stale = 0.0, False
        for _, m in self._meters:
            try:
                best = max(best, float(m.GetPeakValue()))
            except Exception:
                stale = True
        if stale:
            self._last_refresh = -1e9                     # re-enumerate next poll
        return min(1.0, max(0.0, best))

    def close(self) -> None:
        self._meters = []


class DuckingMonitor:
    """Tracks the game's level over the last second and answers two questions for the playback thread:
         should_hold(now)  -> the game was loud for >= loud_min_s of the last window_s: wait before a NEW line
         volume_scale(now) -> duck_scale while the game is loud (plus release_s hang), else 1.0
       level_fn is injectable for tests: level_fn() -> 0..1. Otherwise start() spawns a 20 Hz COM poll thread."""

    def __init__(self, names: Iterable[str] = ("StarCitizen.exe",), mode: str = "process",
                 threshold: float = 0.08, window_s: float = 1.0, loud_min_s: float = 0.3,
                 duck_scale: float = 0.55, release_s: float = 0.4, poll_hz: float = 20.0,
                 level_fn: Optional[Callable[[], float]] = None,
                 now: Callable[[], float] = time.monotonic):
        self.names, self.mode = tuple(names), mode
        self.threshold, self.window_s, self.loud_min_s = threshold, window_s, loud_min_s
        self.duck_scale, self.release_s = duck_scale, release_s
        self.period = 1.0 / min(max(poll_hz, 1.0), 20.0)  # 20 Hz max, by contract
        self.now = now
        self._level_fn = level_fn
        self._hist: deque[tuple[float, float]] = deque()
        self._lock = threading.Lock()
        self._last = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._local = threading.local()
        self.found: list[str] = []
        self.error: Optional[str] = None
        # Other readers of the same meter (combat_watch: gunfire is a train of sharp onsets in SC's own output).
        # Called outside the lock, after every reading; a listener that raises is dropped from this reading only.
        self.listeners: list[Callable[[float, float], None]] = []

    # -- level source ---------------------------------------------------------------------------------------------
    def _meter(self) -> SessionMeter:
        m = getattr(self._local, "meter", None)
        if m is None:
            m = self._local.meter = SessionMeter(self.names, self.mode)
        return m

    def game_level(self) -> float:
        """Current 0..1 peak. From the poll thread's last reading when running, else a direct one-shot read."""
        if self._thread and self._thread.is_alive():
            return self._last
        if self._level_fn is not None:
            return float(self._level_fn())
        m = self._meter()
        v = m.peak()
        self.found, self.error = m.found, m.error
        return v

    def sample(self, level: Optional[float] = None, t: Optional[float] = None) -> float:
        """Record one reading (the poll thread calls this; tests call it with explicit level/t)."""
        if level is None:
            level = float(self._level_fn()) if self._level_fn else self._meter().peak()
        t = self.now() if t is None else t
        with self._lock:
            self._last = level
            self._hist.append((t, level))
            while self._hist and self._hist[0][0] < t - self.window_s - 0.5:
                self._hist.popleft()
        for fn in list(self.listeners):
            try:
                fn(level, t)
            except Exception:
                log.debug("duck listener failed", exc_info=True)
        return level

    # -- decisions -------------------------------------------------------------------------------------------------
    def loud_seconds(self, now: Optional[float] = None) -> float:
        now = self.now() if now is None else now
        with self._lock:
            h = [s for s in self._hist if s[0] > now - self.window_s and s[0] <= now]
        total, cap = 0.0, 2.0 * self.period
        for i, (t, lv) in enumerate(h):
            nxt = h[i + 1][0] if i + 1 < len(h) else now
            if lv >= self.threshold:
                total += min(max(nxt - t, 0.0), cap)
        return total

    def should_hold(self, now: Optional[float] = None) -> bool:
        return self.loud_seconds(now) >= self.loud_min_s

    def volume_scale(self, now: Optional[float] = None) -> float:
        now = self.now() if now is None else now
        with self._lock:
            recent = [lv for t, lv in self._hist if now - self.release_s <= t <= now]
        return self.duck_scale if any(lv >= self.threshold for lv in recent) else 1.0

    def wait_clear(self, max_wait_s: float, abort: Callable[[], bool] = lambda: False) -> float:
        """Block while should_hold(), up to max_wait_s or until abort(). Returns seconds waited."""
        t0 = time.monotonic()
        while self.should_hold() and not abort() and time.monotonic() - t0 < max_wait_s:
            time.sleep(self.period)
        return time.monotonic() - t0

    # -- thread ----------------------------------------------------------------------------------------------------
    def start(self) -> "DuckingMonitor":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="suitmk2_duck", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        co = False
        if self._level_fn is None:
            try:
                import comtypes
                comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
                co = True
            except Exception as e:
                # RPC_E_CHANGED_MODE (-2147417850): COM is already initialised on this thread (comtypes' import does it,
                # single-threaded). The meter works under that too (verified with StarCitizen.exe live), so not an error.
                log.debug("duck: CoInitializeEx: %s (COM already initialised; meter unaffected)", e)
        try:
            nxt = time.monotonic()
            while not self._stop.is_set():
                try:
                    self.sample()
                    if self._level_fn is None:
                        m = self._meter()
                        self.found, self.error = m.found, m.error
                except Exception as e:                    # the monitor must never take the voice down
                    self.error = f"{type(e).__name__}: {e}"
                nxt += self.period
                self._stop.wait(max(0.0, nxt - time.monotonic()))
        finally:
            m = getattr(self._local, "meter", None)
            if m is not None:
                m.close()
                self._local.meter = None
            if co:
                import comtypes
                import gc
                gc.collect()
                comtypes.CoUninitialize()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(2.0)

    def status(self) -> dict:
        return {"level": round(self._last, 3), "hold": self.should_hold(), "scale": self.volume_scale(),
                "metering": list(self.found), "error": self.error, "mode": self.mode}


# ---------------------------------------------------------------------------------------------------------------
# selftest - renders WAVs, plays NOTHING
# ---------------------------------------------------------------------------------------------------------------
_SAMPLES = r"C:\Users\prjgn\Projects\elah-audio\voice_lab\engine_samples"


def _synthetic_speech(sr: int, secs: float = 5.0, f0: float = 180.0, seed: int = 1) -> np.ndarray:
    """Glottal-ish pulse train through three formants, syllable-gated, with pauses and a broadband 'sss'."""
    rng = np.random.default_rng(seed)
    n = int(sr * secs)
    t = np.arange(n) / sr
    vib = f0 * (1 + 0.04 * np.sin(2 * np.pi * 5 * t))
    phase = np.cumsum(vib / sr)
    src = (np.mod(phase, 1.0) < 0.12).astype(np.float32) - 0.12
    y = np.zeros(n, np.float32)
    for fc, bw, g in ((700, 110, 1.0), (1220, 130, 0.6), (2600, 200, 0.35)):
        b, a = signal.iirpeak(fc / (sr / 2), fc / bw)
        y += g * signal.lfilter(b, a, src).astype(np.float32)
    hiss = signal.sosfilt(signal.butter(4, 4500 / (sr / 2), "highpass", output="sos"),
                          rng.standard_normal(n)).astype(np.float32) * 0.15
    syl = (np.sin(2 * np.pi * 3.2 * t) > -0.2).astype(np.float32)
    syl[int(sr * 1.6): int(sr * 2.1)] = 0.0                           # a pause
    y = y * syl + hiss * (np.sin(2 * np.pi * 0.9 * t) > 0.7)
    y += 0.02 * signal.lfilter([1.0], [1, -0.97], rng.standard_normal(n)).astype(np.float32) * 0.02  # low rumble
    return (y / np.abs(y).max() * 0.7).astype(np.float32)


def _band_ratio_db(x: np.ndarray, sr: int, lo: float, hi: float) -> float:
    """Energy OUTSIDE [lo, hi] relative to energy inside it, dB."""
    X = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    f = np.fft.rfftfreq(len(x), 1 / sr)
    inside = X[(f >= lo) & (f <= hi)].sum()
    outside = X[(f < lo) | (f > hi)].sum()
    return 10 * np.log10(outside / inside + 1e-20)


def _selftest() -> int:
    import soundfile as sf
    results = []

    def case(name, ok, detail=""):
        results.append((name, bool(ok)))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))

    sources = []
    for spk, f0, seed in (("elah", 200.0, 1), ("montaigne", 110.0, 2)):
        sources.append((f"synthetic_{spk}", spk, _synthetic_speech(22050, 5.0, f0, seed), 22050))
    for spk in ("elah", "montaigne"):
        p = os.path.join(_SAMPLES, f"toolbox_{spk}_stock.wav")
        if os.path.exists(p):
            a, sr = sf.read(p, dtype="float32")
            sources.append((f"piper_{spk}", spk, a if a.ndim == 1 else a.mean(axis=1), sr))
        else:
            print(f"  (no real render at {p}; synthetic only for {spk})")

    for label, spk, a, sr in sources:
        for preset in PRESETS:
            out = apply(a, sr, spk, preset)
            tag = f"{label}/{preset}"
            case(f"{tag}: length", len(out) == len(a) + fx_tail_samples(sr, spk, preset),
                 f"{len(a)} -> {len(out)}")
            case(f"{tag}: float32, finite", out.dtype == np.float32 and np.isfinite(out).all())
            if preset == "clean":
                case(f"{tag}: bypass is bit-exact", np.array_equal(out, a.astype(np.float32)))
                continue
            case(f"{tag}: no clipping", np.abs(out).max() <= CEILING + 1e-6, f"peak {np.abs(out).max():.3f}")
            r = speech_rms(out, sr)
            err = 20 * np.log10(r / TARGET_RMS)
            case(f"{tag}: speech RMS within 1.5 dB of target", abs(err) <= 1.5, f"{err:+.2f} dB")
            if spk == "elah":
                before = _band_ratio_db(a, sr, 300, 3400)
                after = _band_ratio_db(out, sr, 300, 3400)
                case(f"{tag}: out-of-band (<300, >3400) energy down >= 12 dB", before - after >= 12.0,
                     f"{before:+.1f} -> {after:+.1f} dB rel. in-band, drop {before - after:.1f}")
            else:
                before = _band_ratio_db(a, sr, 160, 5500)
                after = _band_ratio_db(out, sr, 160, 5500)
                case(f"{tag}: out-of-band (<160, >5500) reduced", before - after >= 6.0,
                     f"drop {before - after:.1f} dB")
            if label.startswith("piper") or label.startswith("synthetic"):
                name = f"fx_{label}_{preset}.wav"
                sf.write(os.path.join(_SAMPLES, name), out, sr, subtype="PCM_16")

    # timing: 5 s clip, warm, median of 20
    a = _synthetic_speech(22050, 5.0)
    for spk in ("elah", "montaigne"):
        for preset in ("default", "heavy"):
            apply(a, 22050, spk, preset)
            ts = []
            for _ in range(20):
                t0 = time.perf_counter(); apply(a, 22050, spk, preset); ts.append(time.perf_counter() - t0)
            med = sorted(ts)[len(ts) // 2] * 1000
            case(f"timing {spk}/{preset} 5 s @22.05k < 30 ms", med < 30.0,
                 f"median {med:.1f} ms, max {max(ts) * 1000:.1f} ms")

    # silence / empty / unknown speaker
    case("all-zero input stays silent", np.abs(apply(np.zeros(22050, np.float32), 22050, "elah")).max() == 0)
    case("empty input -> empty", apply(np.zeros(0, np.float32), 22050, "elah").size == 0)
    case("unknown speaker bypasses", np.array_equal(apply(a, 22050, "ghost"), a))

    # ducking logic with a fake clock + level source
    clk = [100.0]
    d = DuckingMonitor(level_fn=lambda: 0.0, now=lambda: clk[0])
    for _ in range(20):
        d.sample(0.0, clk[0]); clk[0] += 0.05
    case("duck: quiet game -> no hold, scale 1.0", not d.should_hold() and d.volume_scale() == 1.0)
    for _ in range(4):                                     # 200 ms of loud: below the 300 ms bar
        d.sample(0.5, clk[0]); clk[0] += 0.05
    case("duck: 200 ms loud -> no hold yet, but ducked", not d.should_hold() and d.volume_scale() == 0.55,
         f"loud {d.loud_seconds():.2f}s")
    for _ in range(4):                                     # 400 ms loud total
        d.sample(0.5, clk[0]); clk[0] += 0.05
    case("duck: 400 ms loud in last 1 s -> hold", d.should_hold(), f"loud {d.loud_seconds():.2f}s")
    for _ in range(9):                                     # 450 ms quiet: duck released (release 0.4 s)
        d.sample(0.0, clk[0]); clk[0] += 0.05
    case("duck: released after 400 ms of quiet", d.volume_scale() == 1.0)
    case("duck: still holding (loud 400 ms is inside the 1 s window)", d.should_hold(),
         f"loud {d.loud_seconds():.2f}s")
    for _ in range(12):
        d.sample(0.0, clk[0]); clk[0] += 0.05
    case("duck: hold clears once the loud burst leaves the window", not d.should_hold())
    d.sample(0.5, clk[0]); clk[0] += 5.0
    case("duck: a single stale sample cannot count for 5 s (gap capped)", d.loud_seconds() == 0.0)
    clk[0] = 200.0
    d2 = DuckingMonitor(level_fn=lambda: 0.9, now=lambda: clk[0])
    for _ in range(20):
        d2.sample(0.9, clk[0]); clk[0] += 0.05
    t0 = time.monotonic()
    waited = d2.wait_clear(0.3)
    case("duck: wait_clear honours max_wait while the game never stops", 0.25 <= waited <= 0.6,
         f"waited {waited:.2f}s")
    waited = d2.wait_clear(5.0, abort=lambda: True)
    case("duck: wait_clear aborts immediately (mute/stop)", waited < 0.1)

    bad = sum(not ok for _, ok in results)
    print(f"voice_fx selftest: {len(results) - bad}/{len(results)} passed")
    print(f"WAVs: {_SAMPLES}\\fx_*.wav")
    return 1 if bad else 0


def _probe(names: list[str], secs: float) -> int:
    """Real, read-only meter probe. Plays nothing."""
    import psutil
    for name in names:
        mode = "process"
        mon = DuckingMonitor(names=[name], mode=mode).start()
        proc = psutil.Process()
        cpu0 = proc.cpu_times()
        t0 = time.perf_counter()
        peaks, n = [], 0
        while time.perf_counter() - t0 < secs:
            time.sleep(0.05)
            peaks.append(mon.game_level()); n += 1
        cpu1 = proc.cpu_times()
        st = mon.status()
        mon.stop()
        cpu = (cpu1.user + cpu1.system - cpu0.user - cpu0.system) / secs * 100
        print(f"  {name:22s} sessions={st['metering'] or 'NONE FOUND'}  peak max={max(peaks):.3f} "
              f"mean={np.mean(peaks):.3f}  hold={st['hold']} scale={st['scale']}  err={st['error']}  "
              f"proc CPU {cpu:.1f}% over {secs:.0f}s")
    return 0


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if len(sys.argv) > 1 and sys.argv[1] == "--probe":
        names = [a for a in sys.argv[2:] if not a.replace(".", "").isdigit()] or ["StarCitizen.exe"]
        secs = next((float(a) for a in sys.argv[2:] if a.replace(".", "").isdigit()), 3.0)
        sys.exit(_probe(names, secs))
    sys.exit(_selftest())

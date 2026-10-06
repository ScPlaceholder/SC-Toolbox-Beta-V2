"""eyes.py - the companion's sight, cheapest first (ARCHITECTURE.md "Eyes"; J 2026-09-23: "have the eyes run every
few seconds on the screen to produce a feeling of actual presence" + "wire in the vision api to filter pipeline").

Every tick (presence mode sets the cadence: occasional 10s / present 3s / curious 2s):

    StarCitizen.exe foreground?  --no-->  nothing captured, nothing kept          (privacy: never the desktop)
        | yes
    grab one frame, shrink to a thumbnail (in memory only; the only frames ever written are glanced ones, and only
    if the pilot opted in to training shots: training_shots.py, via on_glance)
        |
    CHANGE GATE (64-bit difference hash)  --static-->  keep the current scene, zero further work
        | changed
    SCENE CLASSIFIER (online nearest-neighbour over 32x18 thumbnails)  --confident-->  scene
        | unsure
    VISION GLANCE (LOCAL gemma3:4b via Ollama, headroom-gated, hourly cap) -> scene + "notable" + LABELS THE CLASSIFIER
        |                                                (every glance makes future glances less necessary:
        |                                                 the same distillation idea as the realizer)
    Observation -> eyes.state() -> the facts the realizer is handed. Vision never writes words for Elah;
    it only supplies scene/context, and the grounding gate still refuses any number it did not come from.

Headroom TIGHT (hw_monitor) stretches the cadence to occasional AND forbids the glance: the glance runs a vision
model on the player's own GPU, so it only happens when the game is leaving room. Nothing ever leaves the PC.

Selftest (no screen, no model, no GPU): python eyes.py --selftest
Live peek (captures only if SC is foreground; no model call): python eyes.py --peek
"""
from __future__ import annotations

import base64
import ctypes
import io
import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

_LOG = logging.getLogger("suitmk2.eyes")
_WARNED: set = set()


def _warn_once(key: str, msg: str, *args) -> None:
    """Warn the FIRST time this failure is seen, then drop to debug.

    Everything in this module runs on a 2-10 s tick, so an unconditional warning on a permanent fault (no ctypes,
    an unreadable scene memory) is thousands of identical lines. The first one carries all the information; the
    repeats only teach the reader to filter the logger out, which is how a real fault goes quiet again.
    """
    first = key not in _WARNED
    _WARNED.add(key)
    (_LOG.warning if first else _LOG.debug)(msg, *args)


SCENES = ("menu", "hangar", "on_foot", "cockpit", "quantum", "combat", "landing", "mining", "trading", "map",
          "dead", "other")   # "dead": death / incapacitated / respawn screen (CIG keeps cutting death lines from Game.log)
CADENCE_S = {"occasional": 10.0, "present": 3.0, "curious": 2.0}
# Pictures (glances and looks together) in any one hour, by presence. A DEFAULT, not a ceiling: the settings key
# eyes_pictures_per_hour replaces it with any number the pilot likes (Eyes(per_hour=...)).
GLANCES_PER_HOUR = {"occasional": 4, "present": 12, "curious": 30}
GLANCE_MIN_GAP_S = 45.0      # between two routine glances, until the core has set a pace (Eyes.set_pace)
CHANGE_BITS = 10             # of 64 dHash bits; below this the frame is "the same scene"
# SCENE TRANSITIONS the log never records (J 2026-09-24: "going down an elevator and going indoors ... the eyes will
# need to be doing the heavy lifting"). A frame far from the ROLLING average of recent frames is a new place, not a
# camera turn. ⚠ 0.22 is a first guess on the _dist scale (near-duplicates < 0.12), NOT tuned on real play yet.
TRANSITION_DIST = 0.22
TRANSITION_EMA = 0.15        # weight of each new changed frame in the rolling baseline
TRANSITION_MIN_GAP_S = 20.0
LOOK_MIN_GAP_S = 20.0        # a deliberate look (Eyes.look) waits this long after the last one. The default of
                             # the settings key eyes_look_gap_s (Eyes(look_gap_s=...)), not a floor
HOLDS = ("", "combat", "overload", "hot")   # why no picture may be taken at all right now (Eyes.set_pace)
THUMB = (32, 18)             # classifier features: 32x18 RGB = 1728 bytes per example
SEED = Path(__file__).resolve().parent.parent / "data" / "eyes_seed.json"   # companion_design/seed_eyes.py
MAX_PER_LABEL = 40
MIN_PER_LABEL = 3            # a label with fewer examples cannot be predicted (never guess from one frame)
CONFIDENT_MARGIN = 0.15      # nearest other-label distance must exceed nearest same-label by this fraction
SC_PROCESS = "StarCitizen.exe"


# ---- privacy gate: only ever look at the game ---------------------------------------------------------------
def foreground_process_name() -> Optional[str]:
    """Executable name of the foreground window's process, or None. Windows only; anything else -> None,
    which the caller treats as 'not the game' (so a non-Windows host never captures)."""
    try:
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(len(buf))
            if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return None
            return os.path.basename(buf.value)
        finally:
            kernel32.CloseHandle(h)
    except AttributeError as e:
        # ctypes.windll does not exist off Windows. That is the documented "anything else -> None" case and the
        # normal answer on a Linux/macOS host, so it stays quiet - but at debug, not nowhere.
        _LOG.debug("eyes: no Windows foreground-window API (%s); never capturing", e)
        return None
    except (OSError, ValueError, ctypes.ArgumentError) as e:
        # ★ ABSENCE BECOMES A VALUE. A WinError from any of these four calls returns None, and None is the caller's
        # reading for "the game is not in front" - so a broken privacy gate makes the eyes BLIND FOR THE WHOLE
        # SESSION and it looks exactly like a pilot who is alt-tabbed. No scene, no glance, no death detection, and
        # eyes.state() reports a perfectly healthy "not looking". The fallback is the safe direction (it never
        # captures the desktop), which is why this must be narrowed and logged rather than changed.
        # ValueError and ctypes.ArgumentError are listed so that nothing ctypes can raise escapes a call site that
        # previously caught everything: this runs on the tick thread and an escape would kill the eyes outright.
        _warn_once("foreground", "eyes: cannot read the foreground window (%s: %s); treating it as 'not the game', "
                                 "so the eyes will not look at all", type(e).__name__, e)
        return None


def grab_primary_screen():
    """One frame of the primary monitor as a PIL image (RGB). Imported lazily: mss/PIL are only needed live."""
    import mss
    from PIL import Image
    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[1])
        return Image.frombytes("RGB", shot.size, shot.rgb)


# ---- change gate -------------------------------------------------------------------------------------------
def dhash(img) -> int:
    """64-bit difference hash: 9x8 grayscale, compare horizontal neighbours. Robust to HUD flicker and
    compression noise, sensitive to the scene actually changing."""
    g = img.convert("L").resize((9, 8))
    px = list(g.get_flattened_data() if hasattr(g, "get_flattened_data") else g.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


# ---- scene classifier: learns from glances, predicts only when it has earned it -----------------------------
def thumb_features(img) -> bytes:
    return img.convert("RGB").resize(THUMB).tobytes()


def _dist(a: bytes, b: bytes) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5 / (len(a) ** 0.5 * 255)


class SceneClassifier:
    """Nearest-neighbour over tiny thumbnails. No training job, no model file: examples arrive as labelled
    glances (or log-derived hints) and are kept per label, capped. Portable: a few hundred KB of JSON."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path
        self.examples: dict[str, deque] = {}
        # No memory of this player's own yet: start from the shipped seed (data/eyes_seed.json, labelled offline from
        # real SC screenshots). Without it the eyes classified ZERO scenes in the 09-23 dry run: they only learn from
        # glances, and glances are forbidden while SC owns the GPU. The player's own file replaces the seed on save.
        # Seed FIRST, then the player's own memory on top: each label keeps its newest MAX_PER_LABEL, so what this
        # player's eyes learn gradually displaces the seed. (First version used the seed only when no own file
        # existed, and the dry run had already saved an EMPTY own file, so the seed never loaded on J's machine.)
        for src in ([SEED] if path is not None and SEED.exists() else []) + ([path] if path and path.exists() else []):
            try:
                data = json.loads(src.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                # OSError: the file vanished between exists() and read, or is locked. ValueError: truncated or
                # non-UTF-8 JSON (UnicodeDecodeError and JSONDecodeError both subclass it).
                # ★ ABSENCE BECOMES A VALUE, and this exact failure has already cost a day: an eyes_seed.json that
                # does not parse leaves self.examples empty, predict() then returns (None, ...) for every frame,
                # and the eyes classify ZERO scenes - reported as "unsure", which is also what a genuinely novel
                # screen looks like. The comment above records the previous version of this bug being found by
                # reasoning about a dry run rather than by any log line. Now the file says so itself.
                _warn_once(f"scene_memory:{src}", "eyes: cannot read the scene memory %s (%s: %s); those examples "
                                                  "are missing and scenes will read as 'unsure'",
                           src, type(e).__name__, e)
                continue
            for label, rows in data.items():
                q = self.examples.setdefault(label, deque(maxlen=MAX_PER_LABEL))
                q.extend(base64.b64decode(r) for r in rows)

    def learn(self, feats: bytes, label: str) -> None:
        if label not in SCENES:
            return
        self.examples.setdefault(label, deque(maxlen=MAX_PER_LABEL)).append(feats)

    def predict(self, feats: bytes) -> tuple[Optional[str], float]:
        """(label, confidence 0..1) or (None, 0) when no label has MIN_PER_LABEL examples or the nearest two
        labels are too close to call. Never guesses."""
        best: dict[str, float] = {}
        for label, rows in self.examples.items():
            if len(rows) >= MIN_PER_LABEL:
                best[label] = min(_dist(feats, r) for r in rows)
        if not best:
            return None, 0.0
        ranked = sorted(best.items(), key=lambda kv: kv[1])
        top, d1 = ranked[0]
        if len(ranked) == 1:
            return (top, 0.5) if d1 < 0.12 else (None, 0.0)   # one known label: only claim near-duplicates
        d2 = ranked[1][1]
        margin = (d2 - d1) / max(d2, 1e-9)
        if margin < CONFIDENT_MARGIN:
            return None, margin
        return top, min(1.0, margin)

    def save(self) -> None:
        if self.path:
            data = {k: [base64.b64encode(r).decode() for r in v] for k, v in self.examples.items()}
            self.path.write_text(json.dumps(data), encoding="utf-8")


# ---- the vision glance: a LOCAL vision model (J 2026-09-23: "the vision API for suitmk2 needs to be entirely local")
# ⛔ NO POSITIONS, EVER (J 2026-09-24): "If I made the companion AI be able to target track ... it could be too easy to
# turn into a bot that plays the game for you ... That's no longer a narration tool but an aimbot with no control
# output." The eyes may say WHAT is on screen, never WHERE: no coordinates, boxes, bearings or offsets in the glance
# schema, in state(), in an Observation, or in a burst result (the muzzle-flash check counts flashes and discards
# where they were). A narrator needs a noun, not a point; a point is the one field an aimbot needs.
# The selftest fails if any exposed field name looks positional (POSITION_WORDS).
POSITION_WORDS = ("x", "y", "pos", "position", "coord", "bbox", "box", "rect", "bearing", "azimuth", "offset",
                  "target", "aim", "cursor", "pixel", "where", "location_px", "center", "centre")

GLANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "scene": {"type": "string", "enum": list(SCENES)},
        "confidence": {"type": "number"},
        "notable": {"type": "string"},
    },
    "required": ["scene", "confidence", "notable"],
    "additionalProperties": False,
}
GLANCE_PROMPT = ("This is one frame of the game Star Citizen, seen by the player's suit AI. Classify the scene "
                 f"as one of: {', '.join(SCENES)} ('dead' = the player's death, incapacitated or respawn screen). In 'notable', say in at most 12 words what a companion would "
                 "notice, with NO numbers, names or text read off the HUD (those come from other sensors). "
                 "If unsure, lower the confidence rather than guessing. In 'notable', if you cannot tell what "
                 "something is, SAY so ('a creature, or maybe a body') instead of leaving it out; describe the "
                 "place's look and mood too (lighting, ruin, water, crowds).")


class LocalGlance:
    """callable(jpeg_bytes) -> {"scene","confidence","notable"}, from a vision model on THIS PC (Ollama on 127.0.0.1).
    Nothing leaves the machine. Default gemma3:4b (vision-capable, ~3 GB, already used by the realizer bench).
    keep_alive is short so the model gives its VRAM back to the game seconds after a glance; the caller only glances
    when headroom is not TIGHT (see Eyes._may_glance), because this now costs the player's own GPU."""

    def __init__(self, model: str = "gemma3:4b", url: str = "http://127.0.0.1:11434", timeout: float = 60.0,
                 keep_alive: str = "20s"):
        self.model, self.url, self.timeout, self.keep_alive = model, url.rstrip("/"), timeout, keep_alive

    def __call__(self, jpeg: bytes) -> dict:
        import urllib.request
        body = {"model": self.model, "stream": False, "keep_alive": self.keep_alive, "format": GLANCE_SCHEMA,
                "options": {"temperature": 0},
                "messages": [{"role": "user", "content": GLANCE_PROMPT,
                              "images": [base64.standard_b64encode(jpeg).decode("ascii")]}]}
        req = urllib.request.Request(self.url + "/api/chat", data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(json.loads(r.read())["message"]["content"])


def glance_crop(img, width: int = 768) -> bytes:
    """Downscaled JPEG of the whole frame (the center carries the action, the edges carry the HUD; the scene
    needs both). ~40-80 KB: cheap to send, useless to anyone who intercepts it."""
    w, h = img.size
    small = img.convert("RGB").resize((width, max(1, int(h * width / w))))
    buf = io.BytesIO()
    small.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


# ---- weapons fire: a muzzle-flash burst check (CPU only, no model) -------------------------------------------
# combat_watch hears a train of sharp onsets and asks "do you SEE it?". The glance cannot answer while the game owns
# the GPU, so this is the cheap answer: on request, grab a short burst (~12 frames at ~8 Hz = 1.5 s), shrink each to
# 160x90, and count FLASHES: a compact patch that jumps bright between two consecutive frames and is gone again within
# the next two. Gunfire flickers; a HUD pulse ramps; a scene cut or explosion moves the WHOLE frame.
#
# Channel: HSV "V" = max(R,G,B). A saturated orange muzzle flash (255,160,40) is V=255 but only ~L=170 in luminance,
# so V catches "bright OR saturated-and-bright" in one plane without a second pass.
#
# Per consecutive pair (prev -> cur):
#   1. GLOBAL gate: |mean(cur) - mean(prev)| > FLASH_GLOBAL_SHIFT -> skip the pair (scene cut / exposure / explosion
#      wash). Below that, the mean shift is SUBTRACTED so a small global brightening cannot fake a local jump.
#   2. APPEAR mask: pixels that rose >= FLASH_DV (after compensation) AND are now >= FLASH_V.
#   3. SIZE gate: unweighted area <= FLASH_MAX_FRAC (bigger = a cut or a nearby explosion, not a muzzle), and
#      centre-weighted area >= FLASH_MIN_WFRAC (weight 1.0 at centre falling to 0.4 in the corners: your own muzzle
#      and the crosshair fight live in the middle, tracers/impacts anywhere, HUD furniture at the edges).
#   4. VANISH: within the next FLASH_VANISH_FRAMES frames, >= FLASH_VANISH_FRAC of those pixels drop back by
#      >= FLASH_DV. Only then is it a flash. A light that comes on and STAYS on (landing lights, a door) is not.
# flash_score = number of such flashes; burst_check says True at >= FLASH_EVENTS (a gun is a TRAIN, so one flash
# alone - a glint, a single spark - does not confirm).
FLASH_RES = (160, 90)
FLASH_DV = 60                # rise (and later fall) in V, 0..255, after global compensation
FLASH_V = 180                # the flash pixel itself must end up at least this bright
FLASH_GLOBAL_SHIFT = 20.0    # mean-V change between frames above which the pair is a cut, not a flash
FLASH_MAX_FRAC = 0.15        # appear area above this share of the frame = too big to be a muzzle
FLASH_MIN_WFRAC = 0.0008     # centre-weighted share: ~12 px of 14,400 at centre, ~29 px in a corner
FLASH_CENTRE_FLOOR = 0.4     # corner weight
FLASH_VANISH_FRAMES = 2
FLASH_VANISH_FRAC = 0.5
FLASH_EVENTS = 2             # flashes in one burst to say "weapons fire visible"
BURST_FRAMES = 12
BURST_HZ = 8.0
BURST_MIN_FRAMES = 4         # fewer usable frames than this = could not look (None), never "no fire"
BURST_RESULT_TTL_S = 3.0     # burst_confirm() reuses a result this fresh instead of grabbing again

_flash_weight = None         # lazily built L image: centre weight map at FLASH_RES


def _flash_weights():
    global _flash_weight
    if _flash_weight is None:
        from PIL import Image
        w, h = FLASH_RES
        cx, cy = (w - 1) / 2, (h - 1) / 2
        px = []
        for y in range(h):
            for x in range(w):
                r = min(1.0, (((x - cx) / cx) ** 2 + ((y - cy) / cy) ** 2) ** 0.5 / 2 ** 0.5)
                px.append(int(round(255 * (1.0 - (1.0 - FLASH_CENTRE_FLOOR) * r))))
        img = Image.new("L", FLASH_RES)
        img.putdata(px)
        _flash_weight = img
    return _flash_weight


def _vplane(img):
    """Frame -> 160x90 V plane (HSV value = max(R,G,B)). BOX downscale averages, so a flash must cover roughly one
    output cell (~12x12 px at 1080p) to survive at full strength; the full frame is not kept."""
    from PIL import Image
    box = getattr(Image, "Resampling", Image).BOX
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img.resize(FLASH_RES, box).convert("HSV").getchannel(2)


def _above(img, thr: int):
    return img.point(lambda v: 255 if v >= thr else 0)


def _score_planes(vs) -> float:
    from PIL import ImageChops, ImageStat
    if len(vs) < 2:
        return 0.0
    weight = _flash_weights()
    n_px = FLASH_RES[0] * FLASH_RES[1]
    means = [ImageStat.Stat(v).mean[0] for v in vs]
    flashes = 0
    for i in range(1, len(vs)):
        shift = means[i] - means[i - 1]
        if abs(shift) > FLASH_GLOBAL_SHIFT:
            continue                                                   # scene cut / whole-frame wash
        rise = ImageChops.subtract(vs[i], vs[i - 1], 1.0, int(round(-shift)))
        appear = ImageChops.multiply(_above(rise, FLASH_DV), _above(vs[i], FLASH_V))
        n = appear.histogram()[255]
        if n == 0 or n > FLASH_MAX_FRAC * n_px:
            continue
        wfrac = ImageStat.Stat(ImageChops.multiply(appear, weight)).sum[0] / 255.0 / n_px
        if wfrac < FLASH_MIN_WFRAC:
            continue
        for j in range(i + 1, min(len(vs), i + 1 + FLASH_VANISH_FRAMES)):
            back = means[j] - means[i]
            fall = ImageChops.subtract(vs[i], vs[j], 1.0, int(round(back)))
            gone = ImageChops.multiply(appear, _above(fall, FLASH_DV)).histogram()[255]
            if gone >= FLASH_VANISH_FRAC * n:
                flashes += 1
                break
    return float(flashes)


def flash_score(frames) -> float:
    """Number of transient flashes (appear, then vanish within FLASH_VANISH_FRAMES) in a sequence of frames (PIL
    images, any size). >= FLASH_EVENTS reads as weapons fire. Pure function, for tests and offline tuning."""
    return _score_planes([_vplane(f) for f in frames])


# ---- the loop ----------------------------------------------------------------------------------------------
@dataclass
class Observation:
    t: float
    looked: bool                 # False: game not foreground (nothing captured)
    changed: bool = False
    scene: Optional[str] = None
    confidence: float = 0.0
    source: str = "none"         # none | carried | classifier | glance
    notable: str = ""


@dataclass
class EyesStats:
    ticks: int = 0
    not_foreground: int = 0
    static: int = 0
    classified: int = 0
    glances: int = 0
    glance_errors: int = 0
    over_budget: int = 0
    learned: int = 0
    bursts: int = 0              # burst_check calls that got to look
    burst_fire: int = 0          # ...of which saw weapons fire
    burst_blind: int = 0         # burst_check returned None (not foreground, stopped, capture failed)
    errors: list = field(default_factory=list)


class Eyes:
    def __init__(self, presence: str = "present",
                 grab: Callable[[], object] = grab_primary_screen,
                 foreground: Callable[[], Optional[str]] = foreground_process_name,
                 glance: Optional[Callable[[bytes], dict]] = None,
                 headroom: Callable[[], str] = lambda: "OK",
                 now: Callable[[], float] = time.time,
                 classifier: Optional[SceneClassifier] = None,
                 on_glance: Optional[Callable[[bytes, dict], object]] = None,
                 per_hour: Optional[int] = None, look_gap_s: float = LOOK_MIN_GAP_S):
        if presence not in CADENCE_S:
            raise ValueError(f"presence must be one of {tuple(CADENCE_S)}")
        # The pilot's own numbers (settings eyes_pictures_per_hour / eyes_look_gap_s). None = what presence gives.
        self.per_hour = None if per_hour is None else max(1, int(per_hour))
        self.look_gap_s = max(0.0, float(look_gap_s))
        # The pace, set by the core as the pilot's activity changes (picture_pace.py). Until it is set the eyes
        # behave as they did before there was one. interval: seconds between two pictures the eyes take of their own
        # accord. never: no picture of their own accord at all. hold: no picture of any kind, a pilot's "look at
        # that" included, because a fight is on or the PC is overloaded.
        self._pace_set = False
        self._pace_interval = GLANCE_MIN_GAP_S
        self._pace_never = False
        self._hold = ""
        self.presence, self._grab, self._fg, self._glance = presence, grab, foreground, glance
        self._headroom, self._now = headroom, now
        self.clf = classifier or SceneClassifier()
        self.stats = EyesStats()
        self._prev_hash: Optional[int] = None
        self._glance_times: deque = deque()
        self.scene: Optional[str] = None
        self.scene_since: Optional[float] = None
        self.notable = ""
        self._stop = threading.Event()
        self._burst_lock = threading.Lock()      # one burst at a time; the scene loop is never blocked by it
        self._confirm_lock = threading.Lock()
        self._burst_result: Optional[tuple] = None   # (monotonic time, True/False/None) for burst_confirm
        self._burst_thread: Optional[threading.Thread] = None
        self.last_burst: dict = {}
        self._baseline: Optional[list] = None       # rolling average thumbnail, for transitions
        self.transition_at: Optional[float] = None
        self.transitions = 0
        self.notable_at: Optional[float] = None
        self.notable_reason = ""
        self._last_look: Optional[float] = None
        # Called with (the JPEG the model saw, what it said) after every successful glance or look. The app hands it
        # training_shots.ShotStore.keep, which keeps nothing unless the pilot opted in.
        self._on_glance = on_glance

    def _kept(self, jpeg: bytes, reason: str, g: dict) -> None:
        if self._on_glance is None:
            return
        try:
            self._on_glance(jpeg, {"reason": reason, "scene": g.get("scene"), "confidence": g.get("confidence"),
                                   "notable": g.get("notable")})
        except Exception as e:                       # keeping a shot must never cost the companion its eyes
            self.stats.errors.append(f"keep: {type(e).__name__}: {e}"[:200])

    def cadence(self) -> float:
        return CADENCE_S["occasional"] if self._headroom() == "TIGHT" else CADENCE_S[self.presence]

    def set_pace(self, interval_s: Optional[float] = None, never: bool = False, hold: str = "") -> None:
        """The core says how often a picture may be taken for what the pilot is doing now, and whether any may be.
        One more condition on a picture, never fewer: game in front, headroom and the hourly cap are checked as
        before. interval_s None leaves the interval as it was."""
        if interval_s is not None:
            self._pace_interval = max(0.0, float(interval_s))
        self._pace_never = bool(never)
        self._hold = hold if hold in HOLDS else "overload"     # an unknown reason to stop is still a reason to stop
        self._pace_set = True

    def _cap(self) -> int:
        return GLANCES_PER_HOUR[self.presence] if self.per_hour is None else self.per_hour

    def _may_glance(self, t: float) -> bool:
        if self._glance is None or self._headroom() == "TIGHT":   # local model = the player's GPU
            return False
        if self._hold or self._pace_never:
            return False
        while self._glance_times and t - self._glance_times[0] > 3600:
            self._glance_times.popleft()
        if len(self._glance_times) >= self._cap():
            return False
        gap = self._pace_interval if self._pace_set else GLANCE_MIN_GAP_S
        return not self._glance_times or t - self._glance_times[-1] >= gap

    def _set_scene(self, scene: Optional[str], t: float) -> None:
        if scene and scene != self.scene:
            self.scene, self.scene_since = scene, t

    def tick(self) -> Observation:
        t = self._now()
        self.stats.ticks += 1
        if (self._fg() or "").lower() != SC_PROCESS.lower():
            self.stats.not_foreground += 1
            return Observation(t, looked=False, scene=self.scene, source="carried")
        img = self._grab()
        h = dhash(img)
        changed = self._prev_hash is None or hamming(h, self._prev_hash) >= CHANGE_BITS
        self._prev_hash = h
        if not changed:
            self.stats.static += 1
            return Observation(t, looked=True, changed=False, scene=self.scene, source="carried")
        feats = thumb_features(img)
        self._track_transition(feats, t)
        label, conf = self.clf.predict(feats)
        if label:
            self.stats.classified += 1
            self._set_scene(label, t)
            return Observation(t, True, True, label, conf, "classifier")
        if not self._may_glance(t):
            if self._glance is not None:
                self.stats.over_budget += 1
            return Observation(t, True, True, self.scene, 0.0, "carried")
        self._glance_times.append(t)
        try:
            jpeg = glance_crop(img)
            g = self._glance(jpeg)
            scene, conf, notable = g.get("scene"), float(g.get("confidence", 0)), str(g.get("notable", ""))[:120]
        except Exception as e:
            self.stats.glance_errors += 1
            self.stats.errors.append(f"{type(e).__name__}: {e}"[:200])
            return Observation(t, True, True, self.scene, 0.0, "carried")
        self.stats.glances += 1
        self._kept(jpeg, "glance", g)
        if scene in SCENES and conf >= 0.6:
            self.clf.learn(feats, scene)          # the glance teaches the free layer
            self.stats.learned += 1
            self._set_scene(scene, t)
            self.notable = notable
            self.notable_at, self.notable_reason = t, "glance"
        return Observation(t, True, True, scene, conf, "glance", notable)

    def _track_transition(self, feats: bytes, t: float) -> bool:
        """Compare this frame to the rolling baseline; a big jump is a TRANSITION (new room, elevator, indoors)."""
        if self._baseline is None or len(self._baseline) != len(feats):
            self._baseline = [float(x) for x in feats]
            return False
        n = len(feats)
        d = (sum((x - y) ** 2 for x, y in zip(feats, self._baseline)) ** 0.5) / (n ** 0.5 * 255)
        a = TRANSITION_EMA
        self._baseline = [(1 - a) * b + a * x for b, x in zip(self._baseline, feats)]
        if d >= TRANSITION_DIST and (self.transition_at is None or t - self.transition_at >= TRANSITION_MIN_GAP_S):
            self.transition_at = t
            self.transitions += 1
            self._baseline = [float(x) for x in feats]      # the new place IS the baseline now
            return True
        return False

    def look(self, reason: str = "curiosity") -> Optional[str]:
        """A DELIBERATE glance on a hook (J 2026-09-24: "on certain hooks as well as curiosity it should use the eyes
        and see what is going on and comment on it"). Skips the classifier, because the point is a DESCRIPTION, not a
        label. Same guards as any glance: game in front, headroom not TIGHT, hourly budget, plus LOOK_MIN_GAP_S.
        -> the 'notable' text (what is on screen, may carry uncertainty) or None if it did not or could not look."""
        t = self._now()
        if (self._fg() or "").lower() != SC_PROCESS.lower():
            return None
        # A fight or an overloaded PC: no picture, whoever asks. "Never" for this activity: none of the eyes' own
        # accord, but the pilot saying "look at that" is the pilot's accord.
        if self._hold or (self._pace_never and reason != "pilot_asked"):
            return None
        if self._last_look is not None and t - self._last_look < self.look_gap_s:
            return None
        # A routine glance may have JUST described this frame (a transition usually triggers one): reuse it rather
        # than spend another look on the same view.
        if self.notable and self.notable_at is not None and t - self.notable_at <= 10.0:
            self._last_look = t
            self.notable_reason = reason
            return self.notable
        # NOT _may_glance(): its GLANCE_MIN_GAP_S (45 s) is for ROUTINE glances and would refuse exactly the look a
        # transition asks for, seconds after the routine glance. Keep the protections that matter: GPU headroom and
        # the hourly budget. LOOK_MIN_GAP_S above spaces the deliberate looks themselves.
        if self._glance is None or self._headroom() == "TIGHT":
            return None
        while self._glance_times and t - self._glance_times[0] > 3600:
            self._glance_times.popleft()
        if len(self._glance_times) >= self._cap():
            return None
        self._last_look = t
        self._glance_times.append(t)
        try:
            jpeg = glance_crop(self._grab())
            g = self._glance(jpeg)
        except Exception as e:
            self.stats.glance_errors += 1
            self.stats.errors.append(f"look: {type(e).__name__}: {e}"[:200])
            return None
        self.stats.glances += 1
        self._kept(jpeg, reason, g)
        notable = str(g.get("notable", "")).strip()[:160]
        if notable:
            self.notable, self.notable_at, self.notable_reason = notable, t, reason
        scene = g.get("scene")
        if scene in SCENES and float(g.get("confidence", 0)) >= 0.6:
            self._set_scene(scene, t)
        return notable or None

    def state(self) -> dict:
        """What the rest of the companion reads. Facts only, each with its provenance."""
        t = self._now()
        return {"scene": self.scene,
                "scene_minutes": None if self.scene_since is None else round((t - self.scene_since) / 60, 1),
                "scene_notable": self.notable,
                "notable_age_s": None if self.notable_at is None else round(t - self.notable_at, 1),
                "notable_reason": self.notable_reason,
                "transition_age_s": None if self.transition_at is None else round(t - self.transition_at, 1),
                "transitions": self.transitions,
                # seconds since the model was last handed a frame (a glance or a look), for the core's pace
                "picture_age_s": round(t - self._glance_times[-1], 1) if self._glance_times else None,
                "pictures_held": self._hold,
                "in_combat": self.scene == "combat",
                "eyes_source": "vision"}

    # -- weapons-fire burst (asked for by combat_watch; never runs on its own) ------------------------------------
    def burst_check(self, timeout_s: float = 2.0, frames: int = BURST_FRAMES, hz: float = BURST_HZ) -> Optional[bool]:
        """BLOCKING, up to timeout_s: grab a short fast burst and say whether weapons fire is visible.
        True / False, or None = could not look (eyes stopped, SC not foreground, capture failed, too few frames, or
        another burst already running past the timeout). Never raises. Frames are shrunk to 160x90 V planes the
        moment they are grabbed and dropped when this returns: nothing is kept, nothing is written.
        Thread-safe with run(): it does not touch the scene loop's state, and only one burst runs at a time."""
        try:
            if self._stop.is_set():
                self.stats.burst_blind += 1
                return None
            deadline = time.monotonic() + timeout_s
            if not self._burst_lock.acquire(timeout=max(0.0, timeout_s)):
                self.stats.burst_blind += 1
                return None
            try:
                period = 1.0 / max(hz, 0.1)
                planes, work = [], 0.0
                start = time.monotonic()
                while len(planes) < frames and time.monotonic() < deadline and not self._stop.is_set():
                    if (self._fg() or "").lower() != SC_PROCESS.lower():
                        break                      # privacy gate, re-checked every frame: never the desktop
                    img = self._grab()
                    t0 = time.perf_counter()
                    planes.append(_vplane(img))
                    work += time.perf_counter() - t0
                    del img
                    wait = start + len(planes) * period - time.monotonic()
                    if wait > 0 and len(planes) < frames:
                        self._stop.wait(min(wait, max(0.0, deadline - time.monotonic())))
                if len(planes) < BURST_MIN_FRAMES:
                    self.stats.burst_blind += 1
                    self.last_burst = {"result": None, "frames": len(planes)}
                    return None
                t0 = time.perf_counter()
                score = _score_planes(planes)
                work += time.perf_counter() - t0
                fire = score >= FLASH_EVENTS
                self.stats.bursts += 1
                self.stats.burst_fire += fire
                self.last_burst = {"result": fire, "score": score, "frames": len(planes),
                                   "ms_per_frame": round(1000 * work / len(planes), 2),
                                   "span_s": round(time.monotonic() - start, 2)}
                return fire
            finally:
                self._burst_lock.release()
        except Exception as e:  # a confirm must never take combat_watch down with it
            _LOG.warning("eyes: burst check failed (%s: %s); no visual opinion on weapons fire", type(e).__name__, e)
            try:
                self.stats.burst_blind += 1
                self.stats.errors.append(f"burst {type(e).__name__}: {e}"[:200])
            except (AttributeError, TypeError) as book:
                # Only the BOOKKEEPING is guarded here, not the burst: self.stats is a dataclass, so the ways this
                # can fail are a renamed/absent field or errors having been replaced by a non-list. An escape from
                # inside an except block would defeat the outer guard entirely and take combat_watch down with it,
                # which is what the comment above is protecting against - so it stays guarded, and narrowly.
                # The real failure (e) is already logged on the line above, which is the point: the record of the
                # burst failure no longer depends on the stats object being intact.
                _LOG.warning("eyes: could not record the burst failure in stats (%s: %s)",
                             type(book).__name__, book)
            return None

    def burst_confirm(self) -> Optional[bool]:
        """NON-BLOCKING confirm for CombatWatch. Returns a burst result younger than BURST_RESULT_TTL_S if there is
        one; otherwise starts a burst in the background (if none is running) and returns None for now.
        Why not hand burst_check to CombatWatch directly: CombatWatch calls confirm() INSIDE its lock on every meter
        reading (~20 Hz) while in MAYBE, so a 1.5 s blocking burst would stall the audio feed thread."""
        try:
            with self._confirm_lock:
                r = self._burst_result
                if r is not None and time.monotonic() - r[0] <= BURST_RESULT_TTL_S:
                    return r[1]
                if self._burst_thread is None or not self._burst_thread.is_alive():
                    def work():
                        res = self.burst_check()
                        with self._confirm_lock:
                            self._burst_result = (time.monotonic(), res)
                    self._burst_thread = threading.Thread(target=work, name="eyes-burst", daemon=True)
                    self._burst_thread.start()
            return None
        except (RuntimeError, OSError) as e:
            # RuntimeError: Thread.start() when the process is out of threads or already shutting down (and a
            # re-entrant acquire of _confirm_lock, were the lock ever made non-reentrant). OSError: the OS refusing
            # a new thread.
            # ★ ABSENCE BECOMES A VALUE, and this is the worst-disguised one in the file: None is this method's
            # NORMAL, most common return - "no answer yet, a burst is running". So a confirm that can never start a
            # burst returns the same None forever and CombatWatch waits for a second opinion that will never come,
            # with stats.bursts frozen at whatever it was. Called at ~20 Hz while in MAYBE, hence warn-once.
            _warn_once("burst_confirm", "eyes: cannot start a burst check (%s: %s); CombatWatch will keep reading "
                                        "'no answer yet' and never get one", type(e).__name__, e)
            return None

    def run(self, on_obs: Callable[[Observation], None] = lambda o: None) -> threading.Thread:
        def loop():
            while not self._stop.is_set():
                try:
                    on_obs(self.tick())
                except Exception as e:  # a bad frame must not blind the companion for good
                    self.stats.errors.append(f"tick {type(e).__name__}: {e}"[:200])
                self._stop.wait(self.cadence())
        th = threading.Thread(target=loop, name="eyes", daemon=True)
        th.start()
        return th

    def stop(self) -> None:
        self._stop.set()


# ---- selftest ----------------------------------------------------------------------------------------------
def _selftest() -> int:
    from PIL import Image, ImageDraw
    results = []

    def case(name, cond):
        results.append((name, bool(cond)))

    def frame(kind, jitter=0):
        img = Image.new("RGB", (320, 180), {"hangar": (40, 40, 60), "quantum": (10, 10, 90),
                                            "combat": (140, 30, 20)}[kind])
        d = ImageDraw.Draw(img)
        if kind == "hangar":
            d.rectangle([20 + jitter, 100, 300, 170], fill=(120, 120, 120))
        elif kind == "quantum":
            for i in range(0, 320, 40):
                d.line([(160, 90), (i + jitter, 0)], fill=(200, 200, 255), width=3)
        else:
            d.ellipse([100 + jitter, 40, 220 + jitter, 150], fill=(255, 200, 0))
        return img

    clock = [0.0]
    fg = ["StarCitizen.exe"]
    shown = [frame("hangar")]
    calls = []

    def fake_glance(jpeg):
        calls.append(len(jpeg))
        return {"scene": answer[0], "confidence": 0.9, "notable": "pilot is standing near the ship"}
    answer = ["hangar"]

    eyes = Eyes(presence="present", grab=lambda: shown[0], foreground=lambda: fg[0], glance=fake_glance,
                now=lambda: clock[0])

    # 1. Privacy: not the game -> nothing captured.
    fg[0] = "chrome.exe"
    grabbed = []
    e_priv = Eyes(grab=lambda: grabbed.append(1) or shown[0], foreground=lambda: "chrome.exe", now=lambda: 0.0)
    o = e_priv.tick()
    case("not foreground: nothing captured", not o.looked and not grabbed)
    fg[0] = "StarCitizen.exe"

    # 2. First frame changes, classifier empty -> one glance, which labels the scene and teaches the classifier.
    o = eyes.tick()
    case("unknown scene triggers a glance", o.source == "glance" and o.scene == "hangar" and len(calls) == 1)
    case("glance teaches the classifier", eyes.stats.learned == 1)

    # 3. Same frame -> change gate stops everything.
    clock[0] += 3
    o = eyes.tick()
    case("static frame: no glance, scene carried", o.source == "carried" and not o.changed and len(calls) == 1)

    # 4. Budget: a new scene 10s later is within the 45s gap -> no glance.
    shown[0] = frame("quantum"); answer[0] = "quantum"; clock[0] += 10
    o = eyes.tick()
    case("glance gap enforced", len(calls) == 1 and eyes.stats.over_budget == 1)

    # 5. Teach three examples per scene (as glances would over a session), then the classifier answers alone.
    for kind in ("hangar", "quantum", "combat"):
        for j in range(3):
            eyes.clf.learn(thumb_features(frame(kind, jitter=j * 4)), kind)
    shown[0] = frame("combat", jitter=6); clock[0] += 60
    before = len(calls)
    o = eyes.tick()
    case("learned scene classified with no API call", o.source == "classifier" and o.scene == "combat"
         and len(calls) == before)
    case("combat scene reaches state()", eyes.state()["in_combat"] is True)

    # 6. Hourly cap: 'present' allows 12; the 13th inside the hour is refused.
    e_cap = Eyes(presence="present", grab=lambda: capf[0], foreground=lambda: "StarCitizen.exe",
                 glance=lambda j: {"scene": "other", "confidence": 0.2, "notable": ""}, now=lambda: capc[0])
    capf, capc = [None], [0.0]
    import random
    rng = random.Random(1)
    for i in range(14):
        capf[0] = Image.frombytes("RGB", (64, 36), bytes(rng.randrange(256) for _ in range(64 * 36 * 3)))
        capc[0] = i * 50.0
        e_cap.tick()
    case("hourly glance cap holds", e_cap.stats.glances == GLANCES_PER_HOUR["present"])

    # 7. Low-confidence glance is NOT learned (a guess must not become training data).
    case("low-confidence glance not learned", e_cap.stats.learned == 0)

    # 8. No provider (not opted in) -> never glances, never crashes; change still detected.
    e_off = Eyes(grab=lambda: shown[0], foreground=lambda: "StarCitizen.exe", now=lambda: 0.0)
    o = e_off.tick()
    case("no API: changed but no glance", o.changed and o.source == "carried" and e_off.stats.glances == 0)

    # 9. Provider error -> counted, scene carried, no exception.
    def broken(j):
        raise TimeoutError("api down")
    e_err = Eyes(grab=lambda: shown[0], foreground=lambda: "StarCitizen.exe", glance=broken, now=lambda: 0.0)
    o = e_err.tick()
    case("glance error is survivable", e_err.stats.glance_errors == 1 and o.source == "carried")

    # 10. TIGHT headroom stretches cadence to 'occasional'.
    e_t = Eyes(presence="curious", headroom=lambda: "TIGHT")
    case("TIGHT slows the eyes", e_t.cadence() == CADENCE_S["occasional"])
    tight_calls = []
    e_tg = Eyes(grab=lambda: shown[0], foreground=lambda: "StarCitizen.exe", headroom=lambda: "TIGHT",
                glance=lambda j: tight_calls.append(1) or {"scene": "hangar", "confidence": 0.9, "notable": ""},
                now=lambda: 0.0)
    e_tg.tick()
    case("TIGHT forbids the local glance (it would cost the game's GPU)", not tight_calls)

    # 10b. The pace (picture_pace.py) and the pilot's own numbers. A pace only ever adds a refusal.
    def paced(**kw):
        n, c = [], [0.0]
        e = Eyes(presence="occasional", grab=lambda: shown[0], foreground=lambda: "StarCitizen.exe", now=lambda: c[0],
                 glance=lambda j: n.append(1) or {"scene": "hangar", "confidence": 0.9, "notable": "a ship"}, **kw)
        return e, n, c
    e_p, n_p, c_p = paced(per_hour=100000, look_gap_s=0.0)
    e_p.set_pace(5.0)
    for _ in range(40):
        c_p[0] += 11.0
        e_p.look("interval")
    case("a pilot's own cap and gap: 40 looks in 440 s where 'occasional' allows 4 an hour", len(n_p) == 40)
    e_p.set_pace(5.0, hold="combat")
    c_p[0] += 60
    case("a hold stops every picture, a pilot's 'look at that' too",
         e_p.look("pilot_asked") is None and e_p.look("interval") is None and len(n_p) == 40)
    e_p.set_pace(5.0, never=True)
    c_p[0] += 60
    case("never: none of the eyes' own accord, but the pilot may still ask",
         e_p.look("interval") is None and e_p.look("pilot_asked") == "a ship" and len(n_p) == 41)
    e_t2, n_t2, c_t2 = paced(per_hour=100000, look_gap_s=0.0, headroom=lambda: "TIGHT")
    e_t2.set_pace(5.0)
    c_t2[0] += 60
    e_t2.tick()
    case("TIGHT forbids the picture at any pace and any cap", e_t2.look("interval") is None and not n_t2)
    case("state() says how old the last picture is", e_p.state()["picture_age_s"] == 0.0
         and e_t2.state()["picture_age_s"] is None)

    # 11. Classifier persists and reloads (portable memory).
    import tempfile
    p = Path(tempfile.mkdtemp()) / "scenes.json"
    eyes.clf.path = p
    eyes.clf.save()
    again = SceneClassifier(p)
    case("classifier round-trips to disk", again.predict(thumb_features(frame("combat", 6)))[0] == "combat")

    # 12. The glance payload is small.
    case("glance JPEG under 150 KB", max(calls) < 150_000)

    # 13-19. Weapons-fire burst detector, on synthetic cockpit frames (320x180, shrunk to 160x90 inside).
    def cockpit(flash=None, lift=0, pulse=0, marker_x=40):
        img = Image.new("RGB", (320, 180), (18, 22, 34))                       # space
        d = ImageDraw.Draw(img)
        d.ellipse([230, 20, 300, 90], fill=(150, 130, 110))                    # a planet (static, bright-ish)
        d.polygon([(0, 180), (60, 60), (80, 60), (30, 180)], fill=(70, 72, 80))    # canopy struts
        d.polygon([(320, 180), (260, 60), (240, 60), (290, 180)], fill=(70, 72, 80))
        d.rectangle([0, 150, 320, 180], fill=(45, 48, 55))                     # dashboard
        d.rectangle([20, 20, 70, 26], fill=(0, 150 + pulse, 170 + pulse))      # HUD bar (can pulse)
        d.rectangle([marker_x, 160, marker_x + 12, 166], fill=(0, 220, 230))   # HUD marker (can slide)
        d.ellipse([155, 85, 165, 95], outline=(0, 230, 230))                   # crosshair
        if flash:
            x, y, r = flash
            d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 190, 70))       # saturated orange muzzle flash
        if lift:
            img = img.point(lambda v: min(255, v + lift))
        return img

    static = [cockpit()] * 12
    s_static = flash_score(static)
    case(f"burst: static cockpit is not fire (score {s_static:.0f})", s_static < FLASH_EVENTS)

    on = {2, 3, 6, 9, 10}                                   # a flash for one or two frames, repeated
    firing = [cockpit(flash=(165, 100, 12) if i in on else None) for i in range(12)]
    s_fire = flash_score(firing)
    case(f"burst: repeated centre flash is fire (score {s_fire:.0f})", s_fire >= FLASH_EVENTS)

    one = [cockpit(flash=(165, 100, 12) if i == 5 else None) for i in range(12)]
    s_one = flash_score(one)
    case(f"burst: ONE flash alone is not a gun (score {s_one:.0f})", s_one < FLASH_EVENTS)

    cut = [cockpit(lift=90 if i % 3 == 1 else (12 if i % 3 == 2 else 0)) for i in range(12)]   # jumps + a small lift
    s_cut = flash_score(cut)
    case(f"burst: whole-frame brightness jumps are not fire (score {s_cut:.0f})", s_cut < FLASH_EVENTS)
    # The uniform jump above is cancelled by mean compensation alone. This one needs the GLOBAL and SIZE gates
    # (mutation-checked 2026-09-23: it scores 3 only when both are removed): a repeated explosion wash over ~1/3 of
    # the frame, which is too big to be a muzzle.
    boom = [cockpit(flash=(160, 90, 110) if i in on else None) for i in range(12)]
    s_boom = flash_score(boom)
    case(f"burst: repeated large explosion wash is not a muzzle (score {s_boom:.0f})", s_boom < FLASH_EVENTS)

    hud = [cockpit(pulse=min(80, 8 * i), marker_x=40 + 2 * i) for i in range(12)]   # gradual pulse + slow slide
    s_hud = flash_score(hud)
    case(f"burst: slow HUD animation is not fire (score {s_hud:.0f})", s_hud < FLASH_EVENTS)

    stays = [cockpit(flash=(165, 100, 12) if i >= 4 else None) for i in range(12)]   # a light that comes on, stays
    s_stay = flash_score(stays)
    case(f"burst: a light that turns on and stays on is not fire (score {s_stay:.0f})", s_stay < FLASH_EVENTS)

    grabbed_b = []
    e_nf = Eyes(grab=lambda: grabbed_b.append(1) or static[0], foreground=lambda: "chrome.exe")
    case("burst: SC not foreground -> None, nothing captured", e_nf.burst_check(timeout_s=0.5) is None and not grabbed_b)

    seq = iter(firing * 3)
    e_b = Eyes(grab=lambda: next(seq), foreground=lambda: "StarCitizen.exe")
    r_fire = e_b.burst_check(timeout_s=2.0, hz=200)
    seq2 = iter(static * 3)
    e_s = Eyes(grab=lambda: next(seq2), foreground=lambda: "StarCitizen.exe")
    r_static = e_s.burst_check(timeout_s=2.0, hz=200)
    case(f"burst_check end to end: firing True, static False ({r_fire}/{r_static})", r_fire is True and r_static is False)

    e_dead = Eyes(grab=lambda: static[0], foreground=lambda: "StarCitizen.exe")
    e_dead.stop()
    def bad_grab():
        raise OSError("capture failed")
    e_bad = Eyes(grab=bad_grab, foreground=lambda: "StarCitizen.exe")
    case("burst: stopped eyes / failing capture -> None, no raise",
         e_dead.burst_check(0.5) is None and e_bad.burst_check(0.5) is None)

    seq3 = iter(firing * 3)
    e_c = Eyes(grab=lambda: next(seq3), foreground=lambda: "StarCitizen.exe")
    first = e_c.burst_confirm()                              # starts a background burst, returns at once
    e_c._burst_thread.join(5)
    case("burst_confirm: None while looking, then the cached answer", first is None and e_c.burst_confirm() is True)

    # Cost per frame at real capture sizes (shrink + share of the pair analysis). Printed, not asserted.
    for size in ((1920, 1080), (2560, 1440)):
        big = [cockpit(flash=(165, 100, 12) if i in on else None).resize(size) for i in range(12)]
        t0 = time.perf_counter()
        planes = [_vplane(b) for b in big]
        t1 = time.perf_counter()
        _score_planes(planes)
        t2 = time.perf_counter()
        print(f"  cost @ {size[0]}x{size[1]}: shrink {1000 * (t1 - t0) / 12:.2f} ms/frame, "
              f"score {1000 * (t2 - t1) / 12:.2f} ms/frame (grab not included)")

    # NO POSITIONS: nothing the eyes expose may carry a screen location (see POSITION_WORDS).
    import dataclasses
    import re as _re
    exposed = set(GLANCE_SCHEMA["properties"]) | set(eyes.state()) | {f.name for f in dataclasses.fields(Observation)}
    exposed |= {"result", "score", "frames", "ms_per_frame", "span_s"} | set(eyes.last_burst)
    def positional(name):
        parts = _re.split(r"[_\W]+", name.lower())
        return any(w in parts or (len(w) > 3 and w in name.lower()) for w in POSITION_WORDS)
    leaks = sorted(n for n in exposed if positional(n))
    case(f"no exposed field carries a screen position ({len(exposed)} checked){': ' + str(leaks) if leaks else ''}",
         not leaks)
    case("the position check itself catches a leak", positional("target_x") and positional("bbox")
         and positional("aim_offset") and not positional("scene_notable"))

    # Training shots: every successful glance/look hands (jpeg, what it said) to on_glance; a broken hook costs nothing.
    kept = []
    e_keep = Eyes(grab=lambda: frame("hangar"), foreground=lambda: "StarCitizen.exe", now=lambda: 0.0,
                  glance=lambda j: {"scene": "hangar", "confidence": 0.9, "notable": "a ship, or maybe a crate"},
                  on_glance=lambda jpeg, meta: kept.append((jpeg[:2], meta)))
    e_keep.tick()
    case("on_glance gets the JPEG the model saw + what it said",
         len(kept) == 1 and kept[0][0] == b"\xff\xd8" and kept[0][1]["reason"] == "glance"
         and kept[0][1]["notable"] == "a ship, or maybe a crate")
    e_boom = Eyes(grab=lambda: frame("hangar"), foreground=lambda: "StarCitizen.exe", now=lambda: 0.0,
                  glance=lambda j: {"scene": "hangar", "confidence": 0.9, "notable": "x"},
                  on_glance=lambda jpeg, meta: 1 / 0)
    o = e_boom.tick()
    case("a failing on_glance never costs the eyes their scene", o.scene == "hangar" and e_boom.scene == "hangar")

    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    bad = sum(not ok for _, ok in results)
    print(f"eyes selftest: {len(results) - bad}/{len(results)} passed")
    return 1 if bad else 0


def _peek() -> int:
    """Live, local only: is SC foreground, and how fast is one capture + hash + thumbnail on this PC?"""
    fg = foreground_process_name()
    print(f"foreground: {fg}")
    if (fg or "").lower() != SC_PROCESS.lower():
        print("Star Citizen is not the foreground window: nothing captured (by design).")
        return 0
    t0 = time.perf_counter()
    img = grab_primary_screen()
    t1 = time.perf_counter()
    h = dhash(img); f = thumb_features(img)
    t2 = time.perf_counter()
    print(f"frame {img.size}: grab {1000 * (t1 - t0):.1f} ms, hash+thumb {1000 * (t2 - t1):.1f} ms, "
          f"glance payload {len(glance_crop(img)) // 1024} KB (not sent)")
    return 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--peek" in sys.argv:
        sys.exit(_peek())
    print(__doc__)

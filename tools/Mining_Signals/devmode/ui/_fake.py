"""In-memory stand-in for ``devmode.api`` so the UI can be built, tested
and screenshotted before (or without) the real backend.

It implements every function in the Dev Mode interface contract with
plausible data: ~60 synthetic crops that look like the in-game signal
panel (dark teal box, location pin, a value such as ``12,810``), a mix of
consensus / single-reader / imported proposals (some of them wrong, as
real imported file names are), glyph thumbnails (real "capture" ones and a
few rendered from the game font, source "font"), a simulated PyTorch
install and a simulated training run.

Selected by ``devmode.ui.backend`` when ``SC_DEVMODE_FAKE=1`` or when the
real backend cannot be imported. Nothing it does touches the real OCR
models or the user's real Dev Mode data: its files live in a temp folder
(or under ``SC_DEVMODE_ROOT`` when that is set).

Knobs (env): SC_DEVMODE_FAKE_TORCH=1 starts with the engine "installed";
SC_DEVMODE_FAKE_DELAY=<seconds per progress step> slows long calls down so
progress bars are visible (default 0.03; tests pass delay=0).
"""

from __future__ import annotations

import json
import os
import random
import re
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageDraw, ImageFont

Progress = Optional[Callable[[float, str], None]]

KINDS = ("signal", "signal_inv", "signal_rgb", "signal_rgb_inv", "hud", "hud_rgb")

_REPO_ROOT = Path(__file__).resolve().parents[4]
_FONT_CANDIDATES = (
    _REPO_ROOT / "shared" / "qt" / "fonts" / "Electrolize-Regular.ttf",
    _REPO_ROOT / "tools" / "Mining_Signals" / "jura.ttf",
)

_STOCK_ACC = {"signal": 0.912, "signal_inv": 0.884, "signal_rgb": 0.861,
              "signal_rgb_inv": 0.842, "hud": 0.955, "hud_rgb": 0.931}
# How a freshly trained candidate compares with stock, per kind. "hud" is
# deliberately worse so the UI's "Activate disabled unless better" path shows.
_TRAIN_DELTA = {"signal": 0.031, "signal_inv": 0.022, "signal_rgb": 0.064,
                "signal_rgb_inv": 0.018, "hud": -0.012, "hud_rgb": 0.009}

_MISREAD = {"0": "8", "1": "7", "3": "8", "5": "6", "6": "5", "7": "1", "8": "3", "9": "8",
            "2": "7", "4": "1"}


def _font(size: int):
    for p in _FONT_CANDIDATES:
        if p.is_file():
            try:
                return ImageFont.truetype(str(p), size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def _fmt(n: int) -> str:
    return f"{n:,}"


def _misread(value: str, rng: random.Random) -> str:
    idx = [i for i, c in enumerate(value) if c.isdigit()]
    if not idx:
        return value
    i = rng.choice(idx)
    return value[:i] + _MISREAD[value[i]] + value[i + 1:]


def _digits(s: Optional[str]) -> str:
    return re.sub(r"[^0-9.%]", "", s or "")


def draw_signal(value: str, kind: str, rng: random.Random) -> Image.Image:
    """A crop that looks like the scanner's signal panel."""
    w, h = 217, 100
    img = Image.new("RGB", (w, h), (6, 10, 13))
    d = ImageDraw.Draw(img)
    for _ in range(rng.randint(4, 12)):           # dust / stars
        x, y = rng.randrange(w), rng.randrange(h)
        c = rng.randint(90, 220)
        d.point((x, y), fill=(c, c, c))
    font = _font(22)
    tw = int(d.textlength(value, font=font))
    bx0, by0 = 40 + rng.randint(-4, 4), 28 + rng.randint(-3, 3)
    bx1, by1 = bx0 + 44 + tw + 12, by0 + 38
    d.rounded_rectangle((bx0, by0, bx1, by1), radius=4, fill=(20, 46, 52),
                        outline=(58, 118, 126), width=1)
    # location pin glyph
    px, py = bx0 + 22, by0 + 15
    d.ellipse((px - 8, py - 9, px + 8, py + 7), fill=(92, 196, 255))
    d.polygon([(px - 7, py + 2), (px + 7, py + 2), (px, py + 16)], fill=(255, 150, 60))
    d.ellipse((px - 3, py - 4, px + 3, py + 2), fill=(20, 46, 52))
    d.line((px - 9, py + 19, px + 9, py + 19), fill=(255, 96, 150), width=2)
    d.text((bx0 + 40, by0 + 7), value, font=font, fill=(196, 232, 238))
    if kind.endswith("_inv"):
        img = Image.eval(img, lambda v: 255 - v)
    if kind in ("signal", "signal_inv"):
        img = img.convert("L").convert("RGB")
    return img


def draw_hud(value: str, kind: str, rng: random.Random) -> Image.Image:
    w, h = 180, 36
    img = Image.new("RGB", (w, h), (8, 12, 16))
    d = ImageDraw.Draw(img)
    colour = (120, 235, 180) if kind == "hud_rgb" else (225, 230, 235)
    d.text((8 + rng.randint(0, 6), 7), value, font=_font(19), fill=colour)
    if kind == "hud":
        img = img.convert("L").convert("RGB")
    return img


def draw_font_glyph(ch: str, rng: random.Random, kind: str) -> Image.Image:
    """A crisp, white-padded tile, the way the real font renderer's look."""
    img = Image.new("RGB", (28, 28), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle((2, 2, 25, 25), fill=(30 + rng.randint(0, 20), 44, 48))
    d.text((14, 14), ch, font=_font(rng.randint(20, 24)), anchor="mm",
           fill=(230, 245, 240 - rng.randint(0, 30)))
    if kind.endswith("_inv"):
        img = Image.eval(img, lambda v: 255 - v)
    return img


def draw_glyph(ch: str, rng: random.Random, kind: str) -> Image.Image:
    img = Image.new("L", (28, 28), rng.randint(0, 25))
    d = ImageDraw.Draw(img)
    f = _font(rng.randint(19, 23))
    d.text((14 + rng.randint(-2, 2), 14 + rng.randint(-2, 2)), ch, font=f,
           fill=rng.randint(190, 255), anchor="mm")
    if kind.endswith("_inv"):
        img = Image.eval(img, lambda v: 255 - v)
    return img.convert("RGB")


class FakeBackend:
    """Implements the devmode.api contract in memory."""

    is_fake = True

    def __init__(self, root: Optional[str] = None, n_captures: int = 60,
                 torch_installed: Optional[bool] = None,
                 delay: Optional[float] = None, seed: int = 7):
        base = root or os.environ.get("SC_DEVMODE_ROOT")
        if base:
            self._root = Path(base) / "fake_backend"
        else:
            self._root = Path(tempfile.mkdtemp(prefix="sc_devmode_fake_"))
        (self._root / "captures").mkdir(parents=True, exist_ok=True)
        (self._root / "glyphs").mkdir(parents=True, exist_ok=True)
        (self._root / "models").mkdir(parents=True, exist_ok=True)
        if delay is None:
            try:
                delay = float(os.environ.get("SC_DEVMODE_FAKE_DELAY", "0.03"))
            except ValueError:
                delay = 0.03
        self.delay = max(0.0, delay)
        if torch_installed is None:
            torch_installed = os.environ.get("SC_DEVMODE_FAKE_TORCH") == "1"
        self._torch = bool(torch_installed)
        self._rng = random.Random(seed)
        self._lock = threading.RLock()
        self._captures: dict[str, dict] = {}
        self._order: list[str] = []
        self._glyphs: dict[str, dict] = {}
        self._capture_on = False
        # Measured: Furore matches the HUD, no bundled font matches the scanner.
        self._fonts = {"hud": {"font": "furore", "lookalike_share": 0.15},
                       "signal": {"font": None, "lookalike_share": 0.15}}
        self._candidates: dict[str, dict] = {}      # kind -> {"path", "acc"}
        self._active: dict[str, dict] = {}          # kind -> {"path", "acc"}
        self._next = 1
        self.calls: list[tuple] = []                # (name, *args) — for tests
        self._seed_captures(n_captures)
        self._seed_glyphs()

    # ── internals ──────────────────────────────────────────────────────
    def _log(self, *call) -> None:
        with self._lock:
            self.calls.append(call)

    def _sleep(self, steps: int = 1) -> None:
        if self.delay:
            time.sleep(self.delay * steps)

    def _new_id(self, prefix: str = "cap") -> str:
        with self._lock:
            i = self._next
            self._next += 1
        return f"{prefix}_{i:05d}"

    def _save(self, img: Image.Image, sub: str, ident: str) -> str:
        path = self._root / sub / f"{ident}.png"
        img.save(path)
        return str(path)

    def _public(self, c: dict) -> dict:
        return {k: (dict(v) if isinstance(v, dict) else v)
                for k, v in c.items() if not k.startswith("_")}

    def _seed_captures(self, n: int) -> None:
        rng = self._rng
        kinds = (["signal_rgb"] * 5 + ["signal"] * 3 + ["signal_rgb_inv", "signal_inv"]
                 + ["hud_rgb"] * 2 + ["hud"])
        for i in range(n):
            kind = rng.choice(kinds)
            if kind.startswith("hud"):
                if rng.random() < 0.5:
                    truth = f"{rng.randint(1, 95)}.{rng.randint(0, 99):02d}%"
                else:
                    truth = _fmt(rng.randint(800, 48000))
                img = draw_hud(truth, kind, rng)
            else:
                truth = _fmt(rng.choice([1790, 3585, 4250, 7080, 8420, 12810, 14340,
                                         16600, 19020, 21510, 25300, 31440])
                             * rng.choice([1, 1, 1, 2]) // 1)
                img = draw_signal(truth, kind, rng)
            cid = self._new_id()
            r = rng.random()
            engines: dict[str, Optional[str]]
            if r < 0.40:
                src = "consensus"
                engines = {"onnx": _digits(truth), "tesseract": _digits(truth),
                           "paddle": _digits(truth) if rng.random() < 0.8 else _digits(_misread(truth, rng))}
                proposed = truth
            elif r < 0.62:
                src = "reader"
                read = truth if rng.random() < 0.8 else _misread(truth, rng)
                engines = {"onnx": _digits(read), "tesseract": None, "paddle": None}
                proposed = read
            elif r < 0.85:
                src = "import"
                proposed = truth if rng.random() < 0.8 else _misread(truth, rng)
                engines = {}
            else:
                src = None
                proposed = None
                engines = {"onnx": None, "tesseract": None, "paddle": None}
            self._captures[cid] = {
                "id": cid, "kind": kind,
                "image_path": self._save(img, "captures", cid),
                "proposed": proposed, "proposal_source": src, "engines": engines,
                "status": "proposed" if proposed else "unlabeled", "label": None,
                "_truth": truth,
            }
            self._order.append(cid)
        # A few already done so the stats and export screens have content.
        done = [c for c in self._order if self._captures[c]["proposed"]][:14]
        for cid in done[:12]:
            c = self._captures[cid]
            c["status"], c["label"] = "confirmed", c["_truth"]
        for cid in done[12:]:
            self._captures[cid]["status"] = "rejected"
        for cid in self._order[-3:]:
            c = self._captures[cid]
            if c["status"] == "unlabeled":
                c["status"], c["label"] = "confirmed", c["_truth"]

    def _seed_glyphs(self) -> None:
        rng = self._rng
        counts = {"0": (82, 6), "1": (74, 4), "2": (41, 9), "3": (19, 7), "4": (55, 3),
                  "5": (12, 11), "6": (37, 2), "7": (66, 5), "8": (28, 14), "9": (9, 3),
                  ",": (48, 0)}
        for ch, (appr, pend) in counts.items():
            for j in range(appr + pend):
                self._add_glyph("signal_rgb", ch, "approved" if j < appr else "pending", "capture")
        # The HUD has a matching bundled font (Furore); the scanner does not,
        # so only HUD glyphs have font renders. (approved, pending):
        hud = {"0": (60, 4), "1": (51, 3), "2": (22, 5), "3": (14, 2), "4": (35, 1),
               "5": (18, 6), "6": (27, 2), "7": (31, 0), "8": (24, 3), "9": (6, 2),
               ".": (44, 0), "%": (38, 1)}
        # '9' was topped up from the font; '5' has a fresh batch waiting for review.
        hud_font = {"9": (40, 6), "5": (0, 12)}
        for ch, (appr, pend) in hud.items():
            for j in range(appr + pend):
                self._add_glyph("hud_rgb", ch, "approved" if j < appr else "pending", "capture")
        for ch, (appr, pend) in hud_font.items():
            for j in range(appr + pend):
                self._add_glyph("hud_rgb", ch, "approved" if j < appr else "pending", "font")

    def _add_glyph(self, kind: str, ch: str, status: str, source: str) -> str:
        gid = self._new_id("gly")
        draw = draw_font_glyph if source == "font" else draw_glyph
        path = self._save(draw(ch, self._rng, kind), "glyphs", gid)
        with self._lock:
            self._glyphs[gid] = {
                "id": gid, "kind": kind, "char": ch, "image_path": path, "status": status,
                "source": source, "font": "furore.otf" if source == "font" else None,
                "capture_id": "" if source == "font" else "cap_seed",
            }
        return gid

    # ── paths ──────────────────────────────────────────────────────────
    def dev_root(self) -> Path:
        return self._root

    # ── capture ────────────────────────────────────────────────────────
    def capture_enabled(self) -> bool:
        return self._capture_on

    def set_capture_enabled(self, on: bool) -> None:
        self._log("set_capture_enabled", bool(on))
        self._capture_on = bool(on)

    def capture_stats(self) -> dict:
        with self._lock:
            total = 0
            for c in self._captures.values():
                try:
                    total += os.path.getsize(c["image_path"])
                except OSError:
                    continue
            return {"count": len(self._captures), "bytes": total, "cap_bytes": 500 * 1024 * 1024}

    def add_capture(self, image, kind: str, engines: dict, meta: dict) -> str:
        cid = self._new_id()
        vals = [v for v in engines.values() if v]
        agree = len(vals) >= 2 and len(set(vals)) == 1
        proposed = vals[0] if agree else (vals[0] if len(vals) == 1 else None)
        src = "consensus" if agree else ("reader" if proposed else None)
        with self._lock:
            self._captures[cid] = {
                "id": cid, "kind": kind, "image_path": self._save(image, "captures", cid),
                "proposed": proposed, "proposal_source": src, "engines": dict(engines),
                "status": "proposed" if proposed else "unlabeled", "label": None,
                "_truth": proposed or "",
            }
            self._order.append(cid)
        return cid

    def import_folder(self, path: str, kind: str, progress: Progress = None) -> int:
        self._log("import_folder", path, kind)
        files = sorted(Path(path).glob("*.png"))
        n = 0
        for i, f in enumerate(files):
            m = re.match(r".*_([0-9][0-9,.]*|none)$", f.stem)
            value = m.group(1) if m and m.group(1) != "none" else None
            try:
                img = Image.open(f).convert("RGB")
            except OSError:
                continue
            cid = self._new_id()
            with self._lock:
                self._captures[cid] = {
                    "id": cid, "kind": kind, "image_path": self._save(img, "captures", cid),
                    "proposed": value, "proposal_source": "import" if value else None,
                    "engines": {}, "status": "proposed" if value else "unlabeled",
                    "label": None, "_truth": value or "",
                }
                self._order.append(cid)
            n += 1
            if progress and (i % 5 == 0 or i == len(files) - 1):
                progress((i + 1) / max(1, len(files)), f"{i + 1}/{len(files)} {f.name}")
                self._sleep()
        return n

    # ── labels ─────────────────────────────────────────────────────────
    def list_captures(self, status: Optional[str] = None, kind: Optional[str] = None,
                      limit: int = 200, offset: int = 0) -> list[dict]:
        self._sleep()
        with self._lock:
            rows = [self._captures[c] for c in self._order]
            if status:
                rows = [r for r in rows if r["status"] == status]
            if kind:
                rows = [r for r in rows if r["kind"] == kind]
            return [self._public(r) for r in rows[offset:offset + limit]]

    def confirm(self, capture_id: str, label: str) -> None:
        self._log("confirm", capture_id, label)
        with self._lock:
            c = self._captures[capture_id]
            c["status"], c["label"] = "confirmed", label

    def reject(self, capture_id: str) -> None:
        self._log("reject", capture_id)
        with self._lock:
            c = self._captures[capture_id]
            c["status"], c["label"] = "rejected", None

    def label_stats(self, kind: Optional[str] = None) -> dict:
        out = {"unlabeled": 0, "proposed": 0, "confirmed": 0, "rejected": 0}
        with self._lock:
            for c in self._captures.values():
                if kind is None or c["kind"] == kind:
                    out[c["status"]] += 1
        return out

    # ── glyphs ─────────────────────────────────────────────────────────
    def extract_glyphs(self, kind: str, progress: Progress = None) -> int:
        self._log("extract_glyphs", kind)
        with self._lock:
            src = [c for c in self._captures.values()
                   if c["kind"] == kind and c["status"] == "confirmed"]
        made = 0
        for i, c in enumerate(src):
            for ch in (c["label"] or ""):
                if ch == " ":
                    continue
                self._add_glyph(kind, ch, "pending", "capture")
                made += 1
            if progress:
                progress((i + 1) / max(1, len(src)), f"capture {i + 1}/{len(src)}")
            self._sleep()
        if progress and not src:
            progress(1.0, "no confirmed captures for this kind yet")
        return made

    def list_glyphs(self, kind: str, char: Optional[str] = None,
                    status: Optional[str] = None, source: Optional[str] = None) -> list[dict]:
        with self._lock:
            rows = [g for g in self._glyphs.values() if g["kind"] == kind]
            if char is not None:
                rows = [g for g in rows if g["char"] == char]
            if status:
                rows = [g for g in rows if g["status"] == status]
            if source:
                rows = [g for g in rows if g["source"] == source]
            return [{k: v for k, v in g.items() if k != "kind"} for g in rows]

    def approve_glyph(self, glyph_id: str) -> None:
        self._log("approve_glyph", glyph_id)
        with self._lock:
            self._glyphs[glyph_id]["status"] = "approved"

    def reject_glyph(self, glyph_id: str) -> None:
        self._log("reject_glyph", glyph_id)
        with self._lock:
            self._glyphs[glyph_id]["status"] = "rejected"

    def glyph_stats(self, kind: str) -> dict:
        """Totals plus the same counts per source ("capture" = real, "font")."""
        def zero():
            return {"approved": 0, "pending": 0, "rejected": 0}
        out: dict[str, dict] = {}
        with self._lock:
            for g in self._glyphs.values():
                if g["kind"] != kind:
                    continue
                s = out.setdefault(g["char"], {**zero(), "capture": zero(), "font": zero()})
                s[g["status"]] += 1
                s[g["source"]][g["status"]] += 1
        return dict(sorted(out.items()))

    def region_font(self, kind_or_family: str) -> dict:
        fam = kind_or_family if kind_or_family in self._fonts else (
            "signal" if kind_or_family.startswith("signal") else "hud")
        font = self._fonts[fam]["font"]
        where = "the scanner (signal) panel" if fam == "signal" else "the mining HUD"
        reason = (f"No matching font bundled for {where}: none of the bundled fonts matches "
                  "its real glyphs, and a wrong font would teach the wrong shapes."
                  if font is None else f"{where} renders in {font}")
        return {"family": fam, "font": font, "lookalike_share": self._fonts[fam]["lookalike_share"],
                "available": ["furore", "jura", "orbitron", "quantico"], "reason": reason}

    def set_region_font(self, family: str, font: Optional[str],
                        lookalike_share: Optional[float] = None) -> dict:
        self._log("set_region_font", family, font)
        if family not in self._fonts:
            raise ValueError("family must be 'signal' or 'hud'")
        self._fonts[family]["font"] = font
        if lookalike_share is not None:
            self._fonts[family]["lookalike_share"] = float(lookalike_share)
        return self.region_font(family)

    def render_font_glyphs(self, kind: str, char: str, count: int,
                           progress: Progress = None) -> int:
        """Like devmode.fontglyphs: a region without a matching font refuses,
        colour kinds refuse until real glyphs of that kind are approved;
        renders queue as pending, source "font"."""
        self._log("render_font_glyphs", kind, char, count)
        if not 1 <= int(count) <= 2000:
            raise ValueError("count must be between 1 and 2000")
        setting = self.region_font(kind)
        if setting["font"] is None:
            raise RuntimeError(setting["reason"])
        if "rgb" in kind:
            with self._lock:
                real = any(g["kind"] == kind and g["source"] == "capture"
                           and g["status"] == "approved" for g in self._glyphs.values())
            if not real:
                raise RuntimeError(
                    f"{kind} is a colour kind and has no approved REAL glyphs yet, so the game's "
                    "colours are unknown. Approve a few real glyphs first.")
        for i in range(int(count)):
            self._add_glyph(kind, char, "pending", "font")
            if progress and (i % 10 == 0 or i == count - 1):
                progress((i + 1) / count, f"rendering {char!r} {i + 1}/{count}")
                self._sleep()
        return int(count)

    # ── synth ──────────────────────────────────────────────────────────
    def synth_seeds(self, kind: str) -> dict:
        out = {}
        for ch, s in self.glyph_stats(kind).items():
            real, font = s["capture"]["approved"], s["font"]["approved"]
            out[ch] = {"real": real, "font": font, "stock": 0 if real or font else 1}
        return out

    def generate_synth(self, kind: str, per_class: int, progress: Progress = None) -> dict:
        self._log("generate_synth", kind, per_class)
        stats = self.glyph_stats(kind)
        out = {}
        items = list(stats.items())
        for i, (ch, s) in enumerate(items):
            out[ch] = max(0, int(per_class) - s["approved"])
            if progress:
                progress((i + 1) / max(1, len(items)), f"class '{ch}': +{out[ch]}")
            self._sleep(2)
        return out

    # ── engine ─────────────────────────────────────────────────────────
    def torch_status(self) -> dict:
        if self._torch:
            return {"installed": True, "version": "2.8.0+cpu",
                    "location": str(self._root / "pyenv")}
        return {"installed": False, "version": None, "location": None}

    def install_torch(self, progress: Progress = None) -> None:
        self._log("install_torch")
        steps = [(0.05, "Resolving torch CPU wheel"), (0.15, "Downloading torch (0/196 MB)"),
                 (0.45, "Downloading torch (88/196 MB)"), (0.75, "Downloading torch (196/196 MB)"),
                 (0.88, "Unpacking into pyenv"), (0.97, "Verifying import"), (1.0, "Installed")]
        for frac, msg in steps:
            if progress:
                progress(frac, msg)
            self._sleep(8)
        self._torch = True

    # ── train / bench / activate ───────────────────────────────────────
    def train(self, kind: str, progress: Progress = None) -> str:
        self._log("train", kind)
        if not self._torch:
            raise RuntimeError("PyTorch is not installed (Engine step)")
        epochs = 12
        for e in range(epochs):
            if progress:
                progress((e + 1) / epochs, f"epoch {e + 1}/{epochs}  loss {1.9 / (e + 1.5):.3f}")
            self._sleep(4)
        path = self._root / "models" / f"{kind}_candidate.onnx"
        path.write_bytes(b"fake-onnx")
        base = self._active.get(kind, {}).get("acc", _STOCK_ACC[kind])
        with self._lock:
            self._candidates[kind] = {"path": str(path),
                                      "acc": round(min(0.995, base + _TRAIN_DELTA[kind]), 3)}
        return str(path)

    def _bench(self, kind: str, acc: float, model: str) -> dict:
        rng = random.Random(f"{kind}{model}")
        n = 48 + len(kind) * 3
        chars = "0123456789" + ("%." if kind.startswith("hud") else ",")
        per_class = {c: round(max(0.0, min(1.0, acc + rng.uniform(-0.08, 0.05))), 3) for c in chars}
        return {"accuracy": acc, "n": n, "per_class": per_class, "model": model}

    def benchmark(self, kind: str, model_path: Optional[str] = None) -> dict:
        self._log("benchmark", kind, model_path)
        self._sleep(3)
        with self._lock:
            if model_path is None:
                act = self._active.get(kind)
                if act:
                    return self._bench(kind, act["acc"], act["path"])
                return self._bench(kind, _STOCK_ACC[kind], f"stock/{kind}.onnx")
            cand = self._candidates.get(kind)
            acc = cand["acc"] if cand and cand["path"] == model_path else _STOCK_ACC[kind]
            return self._bench(kind, acc, model_path)

    def compare(self, kind: str) -> dict:
        current = self.benchmark(kind)
        cand = self._candidates.get(kind)
        candidate = self.benchmark(kind, cand["path"]) if cand else None
        better = bool(candidate and candidate["accuracy"] > current["accuracy"])
        return {"current": current, "candidate": candidate, "better": better}

    def activate(self, kind: str) -> bool:
        self._log("activate", kind)
        cmp = self.compare(kind)
        if not cmp["better"]:
            return False
        with self._lock:
            self._active[kind] = self._candidates.pop(kind)
        return True

    def revert(self, kind: str) -> None:
        self._log("revert", kind)
        with self._lock:
            self._active.pop(kind, None)

    def active_model_path(self, kind: str) -> Optional[str]:
        a = self._active.get(kind)
        return a["path"] if a else None

    # ── export ─────────────────────────────────────────────────────────
    def export_preview(self) -> list[dict]:
        out = []
        with self._lock:
            for cid in self._order:
                c = self._captures[cid]
                if c["status"] != "confirmed":
                    continue
                reads = [v for v in c["engines"].values() if v]
                if c["proposed"] and _digits(c["proposed"]) != _digits(c["label"]):
                    reason = "corrected"
                elif not reads and not c["proposed"]:
                    reason = "no_read"
                elif any(v != _digits(c["label"]) for v in reads):
                    reason = "misread"
                else:
                    continue
                try:
                    size = os.path.getsize(c["image_path"])
                except OSError:
                    size = 0
                out.append({"file": f"crops/{cid}.png", "kind": c["kind"],
                            "reason": reason, "size": size})
        return out

    def export_zip(self, dest_dir: str) -> str:
        self._log("export_zip", dest_dir)
        preview = self.export_preview()
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / f"mining_signals_failures_{time.strftime('%Y%m%d_%H%M%S')}.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            lines = []
            for row in preview:
                cid = Path(row["file"]).stem
                c = self._captures[cid]
                z.write(c["image_path"], row["file"])
                lines.append(json.dumps({"file": row["file"], "kind": c["kind"],
                                         "label": c["label"], "reason": row["reason"],
                                         "engines": c["engines"]}))
            z.writestr("labels.jsonl", "\n".join(lines) + "\n")
            z.writestr("settings.json", json.dumps({
                "game_resolution": "2560x1440", "hud_colour": None,
                "toolbox_version": "fake", "active_model_hashes": {},
                "counts": self.label_stats()}, indent=2))
        return str(path)

"""Glyph gate — make Glyph Review quarantine decisions binding for
trainers and extractors.

The user's review tool moves rejected tiles into ``_quarantine``
folders (2,600+ curated rejections), but nothing downstream consulted
them: ``training_data_blacklist`` (the extractor's reject store) held a
single file, and the trainers read class folders raw — so re-extraction
and augmentation kept re-introducing rejected junk (user-caught: merged
"26"/"20" tiles living in digit classes).

``training_data_blacklist`` cannot simply be filled with the quarantine:
that folder doubles as the ICON LOCATOR's template set
(``_locate_icon_via_blacklist_match`` slide-matches every file in it),
so flooding it would make blurry digits match junk refs and the icon
mask would start eating leading digits. The quarantine therefore gets
its own pHash reference store, loaded here.

API (all polarity-agnostic 8x8 average-hash, identical to the
extractor's ``_phash``):

    is_quarantine_lookalike(img_or_path, thr=0.88) -> bool
    filter_clean(paths, thr=0.88, label="") -> list   # kept paths
    refs_count() -> int

Env kill-switch: ``SC_TRAIN_NO_GATE=1`` disables the gate entirely
(filter_clean returns its input; is_quarantine_lookalike -> False).

Hashes are cached to ``models/_quarantine_hashes.npy`` and rebuilt
automatically when the quarantine file count changes.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional, Union

import numpy as np
from PIL import Image

_MODULE_DIR = Path(__file__).resolve().parent
_TOOL_DIR = _MODULE_DIR.parent

# Quarantine roots: this tree + the WingmanAI staging tree (same dual-
# tree convention as training_registry.resolve_staging_dir).
_WINGMAN_TOOL_DIR = Path(
    r"C:\Users\prjgn\AppData\Roaming\ShipBit\WingmanAI\custom_skills"
    r"\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
)
_QUARANTINE_PARENTS = [
    "training_data_user_panel",
    "training_data_user_sig",
    "training_data_user_panel_rgb",
    "training_data_user_sig_rgb",
]

_CACHE_NPY = _MODULE_DIR / "models" / "_quarantine_hashes8.npy"
_CACHE_NPY16 = _MODULE_DIR / "models" / "_quarantine_hashes16.npy"
_CACHE_STAMP = _MODULE_DIR / "models" / "_quarantine_hashes.count"

# TWO-STAGE matching. A single 8x8 hash captures SHAPE: the quarantine
# is mostly QUALITY-rejects of normal digit shapes, so at 0.88 it
# "matches" 79% of the entire corpus (every clean 0 resembles some
# blurry rejected 0). Stage 1 (8x8, >= _THR_COARSE) finds candidates;
# stage 2 (16x16 = 256 bit, >= _THR_FINE) confirms only NEAR-DUPLICATES
# — re-extractions and augmented copies of the rejected tiles — which
# is what "the user already rejected this" actually means.
_THR_COARSE = 0.90
_THR_FINE = 0.95
_THR_DEFAULT = _THR_FINE  # public thr parameter maps to the fine stage
_refs: Optional[np.ndarray] = None    # (N, 64) uint8
_refs16: Optional[np.ndarray] = None  # (N, 256) uint8


def _gate_disabled() -> bool:
    return bool(os.environ.get("SC_TRAIN_NO_GATE"))


def _phash_bits(img: Image.Image, size: int = 8) -> np.ndarray:
    small = np.asarray(
        img.convert("L").resize((size, size), Image.BILINEAR),
        dtype=np.float32,
    )
    return (small > small.mean()).astype(np.uint8).reshape(size * size)


def _quarantine_files() -> list:
    files: list = []
    seen_dirs = set()
    for tool in (_TOOL_DIR, _WINGMAN_TOOL_DIR):
        for parent in _QUARANTINE_PARENTS:
            qd = tool / parent / "_quarantine"
            key = str(qd).lower()
            if key in seen_dirs or not qd.is_dir():
                continue
            seen_dirs.add(key)
            for root, _dirs, names in os.walk(qd):
                for fn in names:
                    if fn.lower().endswith(".png"):
                        files.append(Path(root) / fn)
    return files


def _load_refs() -> "tuple[np.ndarray, np.ndarray]":
    global _refs, _refs16
    if _refs is not None and _refs16 is not None:
        return _refs, _refs16
    files = _quarantine_files()
    n = len(files)
    # cache hit?
    try:
        if (_CACHE_NPY.is_file() and _CACHE_NPY16.is_file()
                and _CACHE_STAMP.is_file()):
            if int(_CACHE_STAMP.read_text().strip()) == n:
                a8 = np.load(_CACHE_NPY)
                a16 = np.load(_CACHE_NPY16)
                if (a8.ndim == 2 and a8.shape[1] == 64
                        and a16.ndim == 2 and a16.shape[1] == 256
                        and len(a8) == len(a16)):
                    _refs, _refs16 = a8, a16
                    return _refs, _refs16
    except Exception:
        pass
    h8, h16 = [], []
    for p in files:
        try:
            im = Image.open(p)
            h8.append(_phash_bits(im, 8))
            h16.append(_phash_bits(im, 16))
        except Exception:
            continue
    _refs = np.stack(h8) if h8 else np.zeros((0, 64), dtype=np.uint8)
    _refs16 = (np.stack(h16) if h16
               else np.zeros((0, 256), dtype=np.uint8))
    try:
        _CACHE_NPY.parent.mkdir(parents=True, exist_ok=True)
        np.save(_CACHE_NPY, _refs)
        np.save(_CACHE_NPY16, _refs16)
        _CACHE_STAMP.write_text(str(n))
    except Exception:
        pass
    return _refs, _refs16


def refs_count() -> int:
    return 0 if _gate_disabled() else int(len(_load_refs()[0]))


# Provenance rules. Tiles the user HAND-KEPT in Glyph Review (the
# reviewed staging sets) are explicit decisions — the gate must never
# second-guess them with similarity matching (a frame-adjacent twin of
# a rejected tile can be a deliberate keep). For those, only an EXACT
# hash duplicate of a rejected tile (a re-mint of the rejected content
# itself) is dropped. DERIVED data — re-extracted datasets and
# augmented/auto-promoted files that never passed human review — gets
# full near-duplicate matching.
_DERIVED_PATH_MARKERS = (
    "_aspect_28x20", "_aspect_40x28",
    "training_data_hud_glyphs_extracted",
)
_DERIVED_NAME_PREFIXES = ("aug_", "auto_", "synth_")


def _is_derived(path_hint: Optional[str]) -> bool:
    if not path_hint:
        return False  # unknown provenance -> treat as hand-kept (safe)
    s = str(path_hint).replace("\\", "/").lower()
    if any(m in s for m in _DERIVED_PATH_MARKERS):
        return True
    name = s.rsplit("/", 1)[-1]
    return name.startswith(_DERIVED_NAME_PREFIXES)


def is_quarantine_lookalike(
    img: Union[str, Path, Image.Image, np.ndarray],
    thr: float = _THR_DEFAULT,
    near: Optional[bool] = None,
) -> bool:
    """True if the image matches a quarantined tile under the
    provenance-appropriate rule.

    near=True  -> two-stage near-duplicate (8x8 candidates >=
                  _THR_COARSE, confirmed by 16x16 >= thr)
    near=False -> exact duplicate only (both hashes identical)
    near=None  -> inferred from the path (derived data -> near;
                  hand-kept/unknown -> exact)

    Hamming via ``!=`` — uint8 subtraction would wrap (0-1 -> 255)
    and silently deflate similarity."""
    if _gate_disabled():
        return False
    refs8, refs16 = _load_refs()
    if not len(refs8):
        return False
    path_hint = img if isinstance(img, (str, Path)) else None
    if near is None:
        near = _is_derived(path_hint)
    if isinstance(img, (str, Path)):
        img = Image.open(img)
    elif isinstance(img, np.ndarray):
        img = Image.fromarray(img.astype(np.uint8))
    h8 = _phash_bits(img, 8)
    sim8 = 1.0 - (refs8 != h8).mean(axis=1)
    if near:
        cand = np.flatnonzero(sim8 >= _THR_COARSE)
        if not cand.size:
            return False
        h16 = _phash_bits(img, 16)
        sim16 = 1.0 - (refs16[cand] != h16).mean(axis=1)
        return bool(sim16.max() >= thr)
    # exact mode: identical at BOTH resolutions
    cand = np.flatnonzero(sim8 >= 1.0)
    if not cand.size:
        return False
    h16 = _phash_bits(img, 16)
    return bool((refs16[cand] == h16).all(axis=1).any())


def filter_clean(
    paths: Iterable,
    thr: float = _THR_DEFAULT,
    label: str = "",
) -> list:
    """Return only paths that do NOT match the quarantine. Logs a count
    when anything is dropped (stdout — trainers run interactively)."""
    paths = list(paths)
    if _gate_disabled() or not paths:
        return paths
    refs = _load_refs()
    if not len(refs):
        return paths
    kept, dropped = [], 0
    for p in paths:
        try:
            if is_quarantine_lookalike(p, thr=thr):
                dropped += 1
                continue
        except Exception:
            pass  # unreadable -> let the trainer's own loader decide
        kept.append(p)
    if dropped:
        tag = f" [{label}]" if label else ""
        print(f"[glyph_gate]{tag} dropped {dropped}/{len(paths)} "
              f"quarantine-lookalike tiles (thr={thr})")
    return kept

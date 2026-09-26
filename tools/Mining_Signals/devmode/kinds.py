"""Region kinds, capture families, glyph pools, label normalisation.

Backbone is ``ocr.training_registry`` (six trainable kinds). Two derived
groupings mirror what the registry already encodes:

* FAMILY — which captures a kind learns from. All four ``signal*`` kinds
  register the same source (``training_data_panels/user_*/region2``), so a
  signal capture confirmed once trains all four. ``hud`` / ``hud_rgb`` share
  HUD captures.
* POOL — where extracted glyphs live. Kinds that share a registry
  ``glyph_staging_dir`` share a pool (``signal`` + ``signal_inv`` share the
  grey pool; the ``_inv`` twin inverts at load time, exactly as
  ``scripts/train_for_region.py`` does).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

KINDS = ("signal", "signal_inv", "signal_rgb", "signal_rgb_inv", "hud", "hud_rgb")

# Characters a human can TYPE as a label, per family. '@' (the signal icon
# class) is a model class but never part of a typed value; ',' is stripped
# because the runtime re-inserts thousands separators.
TYPED_CHARS = {"signal": "0123456789", "hud": "0123456789.%"}

_CLASS_DIR = {".": "dot", "%": "pct", "@": "icon", ",": "comma"}


class UnknownKindError(ValueError):
    pass


def registry():
    paths.ensure_import_paths()
    from ocr import training_registry  # noqa: WPS433 — lazy on purpose
    return training_registry


def check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise UnknownKindError(f"unknown region kind {kind!r}; expected one of {KINDS}")
    return kind


def family(kind: str) -> str:
    check_kind(kind)
    return "signal" if kind.startswith("signal") else "hud"


def kinds_in_family(fam: str) -> list[str]:
    return [k for k in KINDS if family(k) == fam]


def is_rgb(kind: str) -> bool:
    return "rgb" in check_kind(kind).split("_")


def is_inv(kind: str) -> bool:
    return "inv" in check_kind(kind).split("_")


def pool(kind: str) -> str:
    """Glyph pool name = the registry staging dir's folder name."""
    spec = registry().get(check_kind(kind))
    return Path(spec.glyph_staging_dir).name


def spec(kind: str):
    return registry().get(check_kind(kind))


def stock_model_path(kind: str) -> Path:
    return Path(spec(kind).model_path)


def class_dirname(ch: str) -> str:
    return _CLASS_DIR.get(ch, ch)


def classes_for(kind: str) -> str:
    """Class order of the SHIPPED model for ``kind``.

    Taken from the model's JSON sidecar (``charClasses``) because the
    registry can lag the model (``signal_rgb_inv`` registers 10 classes but
    ships 11 with the icon). Falls back to the registry label set."""
    sp = spec(kind)
    side = Path(sp.model_path).with_suffix(".json")
    if side.is_file():
        try:
            cc = json.loads(side.read_text(encoding="utf-8")).get("charClasses")
        except (OSError, ValueError) as exc:
            log.warning("devmode: unreadable sidecar %s: %s", side, exc)
            cc = None
        if isinstance(cc, str) and cc:
            return cc
    return sp.label_set


def normalize_value(kind_or_family: str, value) -> str | None:
    """Normalise an engine/reader/import value. Returns None when the value
    is empty or contains characters the family cannot label (engine noise)."""
    fam = kind_or_family if kind_or_family in TYPED_CHARS else family(kind_or_family)
    if value is None:
        return None
    s = str(value).strip()
    for junk in (",", " ", " ", " ", " "):
        s = s.replace(junk, "")
    if not s or s.lower() == "none":
        return None
    allowed = TYPED_CHARS[fam]
    if any(ch not in allowed for ch in s):
        return None
    return s


def normalize_label(kind: str, label: str) -> str:
    """Like normalize_value but raises: a human label must be valid."""
    out = normalize_value(kind, label)
    if out is None:
        raise ValueError(
            f"label {label!r} is not valid for {family(kind)} captures "
            f"(allowed characters: {TYPED_CHARS[family(kind)]!r}, commas/spaces ignored)"
        )
    return out

"""Training-source / model registry — the single source of truth for
"which crops train which model for which scan region".

Every scan region in the toolbox (signal, mining HUD, refinery,
commodity terminal, …) gets one ``RegionSpec`` entry below. The spec
declares:

  * ``training_sources``  — directories whose contents are the
    ONLY allowed inputs to that region's training pipeline. Mixing
    sources between regions is forbidden and the loader functions
    enforce it programmatically.
  * ``model_path``        — where the trained classifier for this
    region lives on disk. The runtime scanner loads from this path;
    the trainer writes to it. Never shared across regions.
  * ``label_set``         — full character vocabulary. Anything
    outside this set in a label is treated as a labeling error.
  * ``capture_label_glob`` / ``capture_image_glob`` — filename
    patterns used to find labeled (image, JSON) pairs inside
    ``training_sources``.
  * ``font_height_px``    — expected on-screen glyph height range.
    Used at training time to reject mis-sized crops and at inference
    time to validate that the live region matches what the model
    was trained on.
  * ``polarity``          — "white_on_dark" or "dark_on_light".
    Drives the polarity-canonicalization step in preprocessing so
    the trained model only ever sees one polarity.
  * ``valid_value_range`` — optional (lo, hi) plausibility bounds
    used by the validator after OCR.

Two hard rules baked into the loader API:

  1. ``get_training_sources(region_kind)`` returns ONLY the
     directories registered for that kind. Callers cannot pass an
     arbitrary path; they MUST go through the registry.
  2. ``assert_path_belongs_to(region_kind, path)`` raises if a
     given file/dir lives outside that region's registered sources.
     Use this in trainers as a tripwire.

Add a new region: append a ``RegionSpec`` entry below + write its
trainer + add the model file. Nothing else needs to change.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Module-level paths — every other path the registry advertises is
# derived from these so a checkout / install relocation just works.
_MODULE_DIR = Path(__file__).resolve().parent
_TOOL_DIR = _MODULE_DIR.parent  # tools/Mining_Signals/
_MODELS_DIR = _MODULE_DIR / "models"
_PANELS_ROOT = _TOOL_DIR / "training_data_panels"


@dataclass(frozen=True)
class RegionSpec:
    """Immutable contract for one scan region's training + inference."""

    kind: str
    description: str
    training_sources: tuple[Path, ...]
    capture_image_glob: str
    capture_label_glob: str
    label_field: str  # which JSON key holds the ground-truth label
    label_set: str
    model_path: Path
    font_height_px: tuple[int, int]
    polarity: str  # "white_on_dark" | "dark_on_light"
    # Where the per-character 28×28 staging crops live AFTER extraction
    # from raw captures. The trainer reads from here; the extractor
    # writes here. Always per-kind, never shared.
    glyph_staging_dir: Path = Path()
    valid_value_range: Optional[tuple[float, float]] = None

    # Per-region quality thresholds (samples per class). Used by the
    # coverage HUD and the trainer's "ready to train?" check.
    floor_per_class: int = 30
    working_per_class: int = 60
    solid_per_class: int = 150

    def expand_sources(self) -> list[Path]:
        """Return every concrete directory this spec covers right now.
        Globs in ``training_sources`` (e.g. ``user_*/region2``) are
        resolved against the live filesystem each call so newly
        created capture sessions are picked up automatically."""
        out: list[Path] = []
        for src in self.training_sources:
            src_str = str(src)
            if any(ch in src_str for ch in ("*", "?", "[")):
                # Glob — split into base + pattern relative to base.
                # We assume the glob is rooted at an absolute path with
                # the wildcard somewhere downstream.
                parts = src.parts
                # Find the first part containing a wildcard
                wild_idx = next(
                    (i for i, p in enumerate(parts)
                     if any(ch in p for ch in ("*", "?", "["))),
                    None,
                )
                if wild_idx is None:
                    if src.is_dir():
                        out.append(src)
                    continue
                base = Path(*parts[:wild_idx])
                pattern = str(Path(*parts[wild_idx:]))
                if base.is_dir():
                    out.extend(p for p in base.glob(pattern) if p.is_dir())
            else:
                if src.is_dir():
                    out.append(src)
        return out


# ─────────────────────────────────────────────────────────────
# THE REGISTRY
# ─────────────────────────────────────────────────────────────
#
# Add new regions at the end. Existing entries are stable contracts —
# changing their `kind` or `model_path` will silently invalidate every
# caller that hard-coded the old value.

_REGISTRY: dict[str, RegionSpec] = {

    "signal": RegionSpec(
        kind="signal",
        description=(
            "Mining signal scanner — large signature numbers like "
            "'14,160' rendered in the SC scanner overlay. White-on-"
            "dark, ~24 px glyph height."
        ),
        training_sources=(
            _PANELS_ROOT / "user_*" / "region2",
        ),
        capture_image_glob="cap_*.png",
        capture_label_glob="cap_*.json",  # excludes *.boxes.json by suffix check
        label_field="value",
        # NOTE: ',' is intentionally NOT in the label_set. The
        # extractor strips commas from the typed label before glyph
        # extraction (see extract_region2_glyphs) because the runtime
        # re-inserts thousand-separators based on digit count rather
        # than classifying them as a glyph. Including ',' here would
        # leave the class at zero forever and block training.
        label_set="0123456789",
        model_path=_MODELS_DIR / "model_signal_cnn.onnx",
        font_height_px=(20, 30),
        polarity="white_on_dark",
        glyph_staging_dir=_TOOL_DIR / "training_data_user_sig",
        valid_value_range=(1000.0, 35000.0),
    ),

    "hud": RegionSpec(
        kind="hud",
        description=(
            "Mining HUD panel — mass / resistance / instability / "
            "mineral name rows. Colored text on dark background, "
            "smaller glyphs (~28-32 px)."
        ),
        training_sources=(
            _PANELS_ROOT / "user_*" / "region1",
            _TOOL_DIR / "training_data_user_panel",
        ),
        capture_image_glob="cap_*.png",
        capture_label_glob="cap_*.json",
        label_field="mass",  # primary label; trainer also reads other fields
        label_set="0123456789.%",
        model_path=_MODELS_DIR / "model_hud_cnn.onnx",
        font_height_px=(26, 36),
        polarity="white_on_dark",  # after polarity canonicalization
        glyph_staging_dir=_TOOL_DIR / "training_data_user_panel",
        valid_value_range=None,  # depends on field; per-field rules in validate.py
    ),

    # Pending-review staging area: auto-labeled HUD glyphs collected
    # from legacy pools (training_data/, training_data_clean/,
    # digit_reservoir/, archived snapshots) that the user is sorting
    # through manually. Same label set + visual conventions as "hud"
    # so review_glyphs.py shows it in the same UI; only the
    # glyph_staging_dir differs. Files in here are NOT used by the
    # trainer until the user moves them via promote_reviewed.py.
    "pending_hud": RegionSpec(
        kind="pending_hud",
        description=(
            "Pending-review staging for HUD glyphs auto-labeled by "
            "the OCR engines (3-engine consensus, single-engine solo, "
            "or legacy raw harvest). Sort with review_glyphs.py — "
            "click trash, hit 'Move to quarantine'. Survivors get "
            "promoted into training_data_user_panel/ via "
            "scripts/promote_reviewed.py."
        ),
        training_sources=(
            _TOOL_DIR / "training_data_pending_review",
        ),
        capture_image_glob="cap_*.png",
        capture_label_glob="cap_*.json",
        label_field="mass",
        label_set="0123456789.%",
        model_path=_MODELS_DIR / "model_hud_cnn.onnx",  # promoted to here eventually
        font_height_px=(26, 36),
        polarity="white_on_dark",
        glyph_staging_dir=_TOOL_DIR / "training_data_pending_review",
        valid_value_range=None,
    ),

    # Future:
    # "refinery": RegionSpec(...) — when refinery alphabet model arrives
    # "commodity_terminal": RegionSpec(...) — same
}


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

class RegistryError(LookupError):
    """Raised when a caller asks for a region kind that doesn't exist
    or passes a path that doesn't belong to the kind it claimed."""


def list_kinds() -> list[str]:
    """All registered region kinds, in declaration order."""
    return list(_REGISTRY)


def get(kind: str) -> RegionSpec:
    """Look up a region spec. Raises RegistryError on unknown kind so
    typos show up as crashes at the call site, not as silent fallback
    to the wrong model."""
    spec = _REGISTRY.get(kind)
    if spec is None:
        raise RegistryError(
            f"Unknown region kind {kind!r}. "
            f"Known kinds: {sorted(_REGISTRY)}"
        )
    return spec


def get_training_sources(kind: str) -> list[Path]:
    """Resolved list of source directories for a region kind. Glob
    patterns are expanded against the live filesystem."""
    return get(kind).expand_sources()


def get_model_path(kind: str) -> Path:
    """Path to the trained ONNX model for a region kind. Caller is
    responsible for handling the case where the file doesn't exist
    yet (e.g. fall back to a generic shipped model)."""
    return get(kind).model_path


def assert_path_belongs_to(kind: str, path: os.PathLike | str) -> None:
    """Tripwire: raise RegistryError if ``path`` is not inside one of
    ``kind``'s registered training source directories.

    Use at the top of trainers and label-export tools to guarantee
    the only thing they touch is the corpus they were invoked for.
    """
    p = Path(path).resolve()
    sources = [s.resolve() for s in get(kind).expand_sources()]
    for src in sources:
        try:
            p.relative_to(src)
        except ValueError:
            continue
        else:
            return
    raise RegistryError(
        f"Path {p} does not belong to region kind {kind!r}. "
        f"Allowed sources: {[str(s) for s in sources] or '(none yet)'}"
    )


def find_kind_for_path(path: os.PathLike | str) -> Optional[str]:
    """Reverse lookup — return the kind whose training sources contain
    ``path``, or None if no kind owns it. Useful for sanity-checking
    arbitrary tools (\"this file you opened belongs to the 'signal'
    corpus\")."""
    p = Path(path).resolve()
    for kind, spec in _REGISTRY.items():
        for src in spec.expand_sources():
            try:
                p.relative_to(src.resolve())
            except ValueError:
                continue
            else:
                return kind
    return None

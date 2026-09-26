"""Dev Mode public API (the contract the UI codes against).

All functions are synchronous and safe to call from a worker thread; long
ones take ``progress: Callable[[float, str], None] | None``.

Contract (see devmode_brief.md) plus a few additive extras, marked EXTRA.

Label discipline: engine consensus, a live reader value, or an imported
filename value is only ever a PROPOSAL. Glyph extraction, training and
benchmarking read nothing but human-confirmed labels. Benchmarks run on a
deterministic held-out split (devmode.split) that is never trained on.

Region kinds and families: a capture has a kind, but ``kind=`` filters on
labels/captures select its FAMILY — a confirmed signal capture serves all
four signal kinds; a HUD capture serves hud and hud_rgb.

Errors raised (all subclasses of built-ins, safe to show to users):
  ValueError                  bad kind / label / status
  captures.ScreenshotRefusedError (ValueError)  screen-sized image given to add_capture
  captures.CapFullError (RuntimeError)          confirmed captures alone fill the cap
  engine.TorchInstallError (RuntimeError)       pip missing / pip failed
  train.TorchMissingError / NotEnoughDataError / TrainError (RuntimeError)
  bench.BenchmarkError (RuntimeError)
  fontglyphs.NeedRealGlyphsError / NoMatchingFontError / FontRenderError (RuntimeError)
  KeyError                    unknown capture / glyph id

Glyph sources: every glyph is ``"capture"`` (cut from a confirmed capture)
or ``"font"`` (render_font_glyphs). Font glyphs are TRAINING-ONLY; the
benchmark scores held-out captures and never reads glyphs.
"""
from __future__ import annotations

from .activate import activate, active_model_path, compare, revert
from .bench import BenchmarkError, benchmark
from .captures import (CapFullError, ScreenshotRefusedError, add_capture, capture_enabled,
                       capture_stats, import_folder, set_capture_cap, set_capture_enabled)
from .engine import TorchInstallError, install_torch, torch_status
from .export import export_preview, export_zip, set_game_info
from .fontglyphs import (FontRenderError, NeedRealGlyphsError, NoMatchingFontError, region_font,
                         render_font_glyphs, set_region_font)
from .glyphs import approve_glyph, extract_glyphs, glyph_stats, list_glyphs, reject_glyph
from .kinds import KINDS
from .labels import confirm, get_capture, label_stats, list_captures, reject
from .paths import dev_root
from .synth import generate_synth, synth_seeds
from .train import NotEnoughDataError, TorchMissingError, TrainError, train

__all__ = [
    # paths
    "dev_root",
    # capture
    "capture_enabled", "set_capture_enabled", "capture_stats", "add_capture", "import_folder",
    "set_capture_cap",                      # EXTRA
    # labels
    "list_captures", "confirm", "reject", "label_stats",
    "get_capture",                          # EXTRA
    # glyphs
    "extract_glyphs", "list_glyphs", "approve_glyph", "reject_glyph", "glyph_stats",
    "render_font_glyphs",                   # EXTRA (2026-09-25): glyphs from the game font
    "region_font", "set_region_font",       # EXTRA: which bundled font a region family renders in
    # synth
    "generate_synth",
    "synth_seeds",                          # EXTRA: per class real / font / stock seeds
    # engine
    "torch_status", "install_torch",
    # train / bench / activate
    "train", "benchmark", "compare", "activate", "revert", "active_model_path",
    # export
    "export_preview", "export_zip",
    "set_game_info",                        # EXTRA
    # constants / errors (EXTRA)
    "KINDS", "CapFullError", "ScreenshotRefusedError", "TorchInstallError", "TorchMissingError",
    "NotEnoughDataError", "TrainError", "BenchmarkError", "FontRenderError", "NeedRealGlyphsError",
    "NoMatchingFontError",
]

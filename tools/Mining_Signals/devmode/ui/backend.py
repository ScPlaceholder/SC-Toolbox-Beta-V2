"""The single place the UI gets its backend from.

``load_backend()`` returns an object exposing the Dev Mode interface
contract (``devmode.api`` as a module, or a ``FakeBackend`` instance) and,
when it is the fake, the reason why — so the window can show a banner
saying the data is not real. Swapping backends means changing only this
file.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger(__name__)

# Every function the UI calls. Used to refuse a half-implemented backend
# up front rather than crash on the first click that reaches a gap.
CONTRACT = (
    "dev_root", "capture_enabled", "set_capture_enabled", "capture_stats", "add_capture",
    "import_folder", "list_captures", "confirm", "reject", "label_stats",
    "extract_glyphs", "list_glyphs", "approve_glyph", "reject_glyph", "glyph_stats",
    "generate_synth", "torch_status", "install_torch", "train", "benchmark",
    "compare", "activate", "revert", "active_model_path", "export_preview", "export_zip",
    "render_font_glyphs", "synth_seeds", "region_font",
)


@dataclass
class BackendHandle:
    api: Any
    fake_reason: Optional[str] = None     # None = the real backend

    @property
    def is_fake(self) -> bool:
        return self.fake_reason is not None


def missing_functions(api: Any) -> list[str]:
    return [name for name in CONTRACT if not callable(getattr(api, name, None))]


def load_backend(force_fake: Optional[bool] = None) -> BackendHandle:
    from ._fake import FakeBackend

    if force_fake or (force_fake is None and os.environ.get("SC_DEVMODE_FAKE") == "1"):
        return BackendHandle(FakeBackend(), "SC_DEVMODE_FAKE=1 (demo data)")
    try:
        from .. import api  # devmode.api — Agent A's backend
    except ImportError as exc:
        log.warning("Dev Mode backend not importable, using demo data: %s", exc, exc_info=True)
        return BackendHandle(FakeBackend(), f"the training backend could not be loaded ({exc})")
    gaps = missing_functions(api)
    if gaps:
        log.warning("Dev Mode backend is missing %s, using demo data", gaps)
        return BackendHandle(FakeBackend(), "the training backend is incomplete (missing "
                                            + ", ".join(gaps) + ")")
    return BackendHandle(api, None)

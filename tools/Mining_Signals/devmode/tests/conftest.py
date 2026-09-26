from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

TOOL_DIR = Path(__file__).resolve().parents[2]      # tools/Mining_Signals
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def dev_root(tmp_path, monkeypatch):
    root = tmp_path / "devroot"
    monkeypatch.setenv("SC_DEVMODE_ROOT", str(root))
    from devmode import bench
    bench._sessions.clear()
    bench._tiles.clear()
    yield root


def render_value(text: str, *, size=(160, 44), bg=(18, 28, 36), fg=(215, 240, 250),
                 font_px: int = 26, jitter: int = 0) -> Image.Image:
    """A small HUD-like crop: pale text on a dark background."""
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=font_px)
    except TypeError:                    # Pillow < 10.1
        font = ImageFont.load_default()
    x = 10 + jitter
    for ch in text:
        d.text((x, 8), ch, fill=fg, font=font)
        x += int(font_px * 0.75)
    return img


def tiny(color=(10, 20, 30), size=(60, 30)) -> Image.Image:
    return Image.new("RGB", size, color)

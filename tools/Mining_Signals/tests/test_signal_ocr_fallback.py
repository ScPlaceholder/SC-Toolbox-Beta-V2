"""Regression tests for dependency-free signal OCR fallbacks."""

from __future__ import annotations

import os
import sys
import unittest

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ocr.sc_ocr import api  # noqa: E402


def _seven_segment_digit(digit: str) -> np.ndarray:
    """Build a simple boolean glyph resembling segmented SC numerals."""
    segments = {
        "0": "abcefd",
        "1": "cf",
        "2": "acdeg",
        "3": "acdfg",
        "4": "bcfg",
        "5": "abdfg",
        "6": "abdefg",
        "7": "acf",
        "8": "abcdefg",
        "9": "abcdfg",
    }[digit]
    g = np.zeros((21, 13), dtype=bool)
    if "a" in segments:
        g[0:3, 3:10] = True
    if "b" in segments:
        g[2:10, 0:3] = True
    if "c" in segments:
        g[2:10, 10:13] = True
    if "d" in segments:
        g[18:21, 3:10] = True
    if "e" in segments:
        g[11:19, 0:3] = True
    if "f" in segments:
        g[11:19, 10:13] = True
    if "g" in segments:
        g[9:12, 3:10] = True
    return g


def _synthetic_signal_crop(text: str) -> Image.Image:
    """Build a tight icon+number crop with optional comma separators."""
    row_h = 25
    parts: list[np.ndarray] = []
    icon = np.zeros((row_h, 22), dtype=bool)
    icon[3:22, 2:20] = True
    parts.append(icon)
    gap = np.zeros((row_h, 8), dtype=bool)
    parts.append(gap)
    for ch in text:
        if ch == ",":
            comma = np.zeros((row_h, 4), dtype=bool)
            comma[17:21, 1:3] = True
            parts.append(comma)
        else:
            glyph = np.zeros((row_h, 13), dtype=bool)
            glyph[2:23, :] = _seven_segment_digit(ch)
            parts.append(glyph)
        parts.append(np.zeros((row_h, 2), dtype=bool))
    row = np.concatenate(parts, axis=1)
    canvas = np.zeros((45, row.shape[1] + 8), dtype=np.uint8) + 70
    canvas[10:10 + row_h, 4:4 + row.shape[1]][row] = 240
    return Image.fromarray(canvas, mode="L").convert("RGB")


class TestSignalOcrFallback(unittest.TestCase):
    def setUp(self) -> None:
        api._reset_signal_consensus()

    def test_geometry_classifier_covers_all_digits(self) -> None:
        for digit in "0123456789":
            with self.subTest(digit=digit):
                self.assertEqual(api._signal_digit_from_geometry(_seven_segment_digit(digit)), digit)

    def test_synthetic_tight_crops_cover_all_digits_and_ignore_commas(self) -> None:
        cases = {
            "23,456": 23456,
            "17,890": 17890,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                gray = np.asarray(_synthetic_signal_crop(text)).max(axis=2).astype("uint8")
                self.assertEqual(api._signal_read_tight_local(gray), expected)

    def test_tight_crop_fallback_preserves_narrow_leading_digits(self) -> None:
        gray = np.asarray(_synthetic_signal_crop("11,700")).max(axis=2).astype("uint8")

        self.assertEqual(api._signal_read_tight_local(gray), 11700)


if __name__ == "__main__":
    unittest.main()

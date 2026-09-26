"""The seven Dev Mode step pages, in rail order."""

from .engine import EnginePage
from .capture import CapturePage
from .label import LabelPage
from .glyphs import GlyphsPage
from .synth import SynthPage
from .train import TrainPage
from .export import ExportPage

PAGE_CLASSES = (EnginePage, CapturePage, LabelPage, GlyphsPage, SynthPage, TrainPage, ExportPage)

__all__ = ["PAGE_CLASSES", "EnginePage", "CapturePage", "LabelPage", "GlyphsPage",
           "SynthPage", "TrainPage", "ExportPage"]

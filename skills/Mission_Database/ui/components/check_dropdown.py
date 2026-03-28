"""Reusable multi-select checkbox dropdown — thin wrapper around SCFuzzyMultiCheck."""
from __future__ import annotations
from typing import Callable, Dict, List, Optional, Sequence, Set

from PySide6.QtWidgets import QWidget

from shared.qt.fuzzy_multi_check import SCFuzzyMultiCheck


def check_dropdown(
    parent_layout,
    section_label_fn: Callable,
    section_name: str,
    values: Sequence[str],
    on_change: Callable,
    searchable: bool = False,
    visible_fn: Optional[Callable[[], Set[str]]] = None,
) -> SCFuzzyMultiCheck:
    """Create an SCFuzzyMultiCheck widget and add it to the parent layout.

    Args:
        parent_layout: QLayout to add the widget into
        section_label_fn: callable(layout, text) to create a section label
        section_name: label text
        values: list of string options
        on_change: callback when selection changes
        searchable: unused (SCFuzzyMultiCheck always supports search)
        visible_fn: unused (SCFuzzyMultiCheck shows all items)

    Returns:
        SCFuzzyMultiCheck widget
    """
    section_label_fn(parent_layout, section_name)
    multi = SCFuzzyMultiCheck(label="All", items=values)
    multi.selection_changed.connect(lambda _: on_change())
    parent_layout.addWidget(multi)
    return multi

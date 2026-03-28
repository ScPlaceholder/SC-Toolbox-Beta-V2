"""Virtual-scroll card grid — true virtualized scrolling.

Only creates QWidget cards for visible rows + a small buffer.
As the user scrolls, off-screen cards are recycled and refilled
with new data. This keeps widget count low even for 1000+ items.
"""

from __future__ import annotations
import logging
from typing import Any, Callable, List, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget, QScrollBar,
)

from shared.qt.theme import P

log = logging.getLogger(__name__)

# ── Card widget ──────────────────────────────────────────────────────────────

_CARD_SS = f"""
    QFrame#mcard {{
        background-color: {P.bg_secondary};
        border: 1px solid {P.border};
        border-radius: 3px;
    }}
    QFrame#mcard:hover {{
        border-color: {P.accent};
    }}
"""


class MissionCard(QFrame):
    """Lightweight card frame for a single grid item.

    Content is populated externally via ``set_data()``.
    """

    def __init__(self, on_click: Optional[Callable] = None, parent=None):
        super().__init__(parent)
        self.setObjectName("mcard")
        self.setStyleSheet(_CARD_SS)
        self.setCursor(Qt.PointingHandCursor)

        self._data = None
        self._index = 0
        self._on_click = on_click

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(8, 6, 8, 6)
        self._layout.setSpacing(3)

        self._title_lbl = QLabel(self)
        self._title_lbl.setStyleSheet(
            f"color: {P.fg}; font-weight: bold; font-size: 11pt; background: transparent;"
        )
        self._title_lbl.setWordWrap(True)
        self._layout.addWidget(self._title_lbl)

        self._tags_row = QHBoxLayout()
        self._tags_row.setContentsMargins(0, 0, 0, 0)
        self._tags_row.setSpacing(4)
        self._layout.addLayout(self._tags_row)

        self._desc_lbl = QLabel(self)
        self._desc_lbl.setStyleSheet(
            f"color: {P.fg_dim}; font-size: 9pt; background: transparent;"
        )
        self._desc_lbl.setWordWrap(True)
        self._desc_lbl.setMaximumHeight(40)
        self._layout.addWidget(self._desc_lbl)

        self._bottom_lbl = QLabel(self)
        self._bottom_lbl.setStyleSheet(
            f"color: {P.fg_dim}; font-size: 9pt; background: transparent;"
        )
        self._layout.addWidget(self._bottom_lbl)

        self._layout.addStretch(1)

    # ── Public API ───

    def set_click_data(self, data, index: int, fn: Optional[Callable] = None):
        """Store the data/index for click callback and optionally override fn."""
        self._data = data
        self._index = index
        if fn is not None:
            self._on_click = fn

    def set_title(self, text: str):
        self._title_lbl.setText(text)

    def set_description(self, text: str):
        self._desc_lbl.setText(text)

    def set_bottom(self, text: str):
        self._bottom_lbl.setText(text)

    def clear_tags(self):
        while self._tags_row.count():
            item = self._tags_row.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def add_tag(self, text: str, bg_color: str = P.accent, fg_color: str = P.bg_primary):
        tag = QLabel(text, self)
        tag.setStyleSheet(
            f"background-color: {bg_color}; color: {fg_color};"
            f" padding: 1px 5px; border-radius: 2px; font-size: 9pt;"
            f" font-weight: bold;"
        )
        tag.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._tags_row.addWidget(tag)

    def set_data(self, title: str, initials: str, faction_name: str,
                 tags: list, reward_text: str, reward_color: str,
                 extra: str = ""):
        """Populate the card with mission/location data."""
        self.clear_tags()
        self._title_lbl.setText(title)

        # Faction badge + name
        self._desc_lbl.setText(f"[{initials}] {faction_name}")

        # Tags: list of (text, bg, fg, is_bold)
        for tag_data in tags:
            text, bg, fg = tag_data[0], tag_data[1], tag_data[2]
            self.add_tag(text, bg_color=bg, fg_color=fg)

        # Reward line
        self._bottom_lbl.setText(reward_text)
        self._bottom_lbl.setStyleSheet(
            f"color: {reward_color}; font-size: 10pt; font-weight: bold; background: transparent;"
        )

        # Extra text (e.g., resource list)
        if extra:
            if not hasattr(self, '_extra_lbl'):
                self._extra_lbl = QLabel(self)
                self._extra_lbl.setStyleSheet(
                    f"color: {P.fg_dim}; font-family: Consolas; font-size: 8pt; background: transparent;"
                )
                self._extra_lbl.setWordWrap(True)
                self._layout.addWidget(self._extra_lbl)
            self._extra_lbl.setText(extra)
            self._extra_lbl.setVisible(True)
        elif hasattr(self, '_extra_lbl'):
            self._extra_lbl.setVisible(False)

    def clear_content(self):
        """Reset card to blank state for recycling."""
        self._title_lbl.setText("")
        self._desc_lbl.setText("")
        self._bottom_lbl.setText("")
        self._bottom_lbl.setStyleSheet(
            f"color: {P.fg_dim}; font-size: 9pt; background: transparent;"
        )
        self.clear_tags()
        if hasattr(self, '_extra_lbl'):
            self._extra_lbl.setText("")
            self._extra_lbl.setVisible(False)
        self._data = None
        self._index = 0

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self._on_click and self._data is not None:
                try:
                    self._on_click(self._data, self._index)
                except Exception:
                    log.exception("[Card] click handler failed for index=%s", self._index)
            else:
                log.warning("[Card] click ignored: on_click=%s, data=%s, index=%s",
                            bool(self._on_click), self._data is not None, self._index)
        super().mousePressEvent(event)


class FabCard(MissionCard):
    """Card variant for the Fabricator tab with different set_data signature."""

    def set_data(self, name: str, bp_type: str, type_color: str,
                 type_fg: str, bp_sub: str, res_text: str, time_text: str):
        """Populate the card with fabricator blueprint data."""
        self.clear_tags()
        self._title_lbl.setText(name)
        self.add_tag(bp_type, bg_color=type_color, fg_color=type_fg)
        if bp_sub:
            self.add_tag(bp_sub, bg_color=P.bg_card, fg_color=P.fg_dim)
        self._desc_lbl.setText(res_text)
        self._bottom_lbl.setText(time_text)
        self._bottom_lbl.setStyleSheet(
            f"color: {P.fg_dim}; font-size: 9pt; background: transparent;"
        )

    def clear_content(self):
        """Reset for recycling."""
        self._title_lbl.setText("")
        self._desc_lbl.setText("")
        self._bottom_lbl.setText("")
        self._bottom_lbl.setStyleSheet(
            f"color: {P.fg_dim}; font-size: 9pt; background: transparent;"
        )
        self.clear_tags()
        self._data = None
        self._index = 0


# ── Virtual Scroll Grid ─────────────────────────────────────────────────────

class VirtualScrollGrid(QWidget):
    """True virtualized scrollable grid of cards.

    Only creates widgets for visible rows plus a small buffer.
    Cards are recycled as the user scrolls — off-screen cards are
    detached and reused for newly-visible rows. This keeps the widget
    count at ~30-50 regardless of total item count.
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        card_width: int = 320,
        row_height: int = 130,
        fill_fn: Optional[Callable] = None,
        on_click_fn: Optional[Callable] = None,
        card_class: type = MissionCard,
    ):
        super().__init__(parent)
        self._card_width = card_width
        self._row_height = row_height
        self._fill_fn = fill_fn
        self._click_fn = on_click_fn
        self._card_class = card_class
        self._items: list = []
        self._num_cols = 1
        self._buffer_rows = 3  # extra rows above/below viewport

        # Visible card pool: row_index -> list of card widgets
        self._visible_cards: dict[int, list[QWidget]] = {}
        # Recycling pool
        self._pool: list[QWidget] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(f"""
            QScrollArea {{ border: none; background: transparent; }}
            QScrollArea > QWidget > QWidget {{ background: transparent; }}
            QScrollBar:vertical {{
                background: {P.bg_primary}; width: 8px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {P.border}; border-radius: 4px; min-height: 20px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
        """)
        layout.addWidget(self._scroll)

        # The container is a tall empty widget sized to the full data height.
        # Cards are placed absolutely inside it at their row positions.
        self._container = QWidget()
        self._container.setStyleSheet(f"background-color: {P.bg_primary};")
        self._scroll.setWidget(self._container)

        self._scroll.verticalScrollBar().valueChanged.connect(self._on_scroll)

        # Debounce timer for scroll
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.setInterval(16)  # ~60fps
        self._scroll_timer.timeout.connect(self._update_visible)

    @property
    def _total_rows(self) -> int:
        if not self._items:
            return 0
        return (len(self._items) + self._num_cols - 1) // self._num_cols

    def set_data(self, items: list):
        """Replace all data and rebuild."""
        self._items = items or []
        self._recycle_all()
        self._recompute_layout()

    def _recompute_layout(self):
        """Recalculate columns, resize container, populate visible cards."""
        avail_w = self._scroll.viewport().width() or 800
        self._num_cols = max(1, avail_w // self._card_width)
        total_h = self._total_rows * self._row_height + 8
        self._container.setFixedHeight(max(total_h, 1))
        self._update_visible()

    def _on_scroll(self):
        self._scroll_timer.start()

    def _update_visible(self):
        """Create/recycle cards so only visible rows have widgets."""
        if not self._items:
            self._recycle_all()
            return

        vp = self._scroll.viewport()
        scroll_y = self._scroll.verticalScrollBar().value()
        vp_height = vp.height()

        # Which rows are visible?
        first_row = max(0, scroll_y // self._row_height - self._buffer_rows)
        last_row = min(
            self._total_rows - 1,
            (scroll_y + vp_height) // self._row_height + self._buffer_rows,
        )

        needed_rows = set(range(first_row, last_row + 1))
        current_rows = set(self._visible_cards.keys())

        # Recycle rows no longer visible
        for r in current_rows - needed_rows:
            for card in self._visible_cards.pop(r):
                card.setVisible(False)
                self._pool.append(card)

        # Create/reuse cards for newly visible rows
        card_w = (self._scroll.viewport().width() - 8 - 6 * (self._num_cols - 1)) // self._num_cols
        card_w = max(card_w, 200)

        for r in needed_rows - current_rows:
            row_cards = []
            for c in range(self._num_cols):
                idx = r * self._num_cols + c
                if idx >= len(self._items):
                    break

                card = self._get_card()
                card.setParent(self._container)

                # Position absolutely
                x = 4 + c * (card_w + 6)
                y = r * self._row_height
                card.setGeometry(x, y, card_w, self._row_height - 6)

                # Fill content — set click data FIRST so clicks always work
                data_item = self._items[idx]
                card.set_click_data(data_item, idx, self._click_fn)
                if self._fill_fn:
                    try:
                        self._fill_fn(card, data_item, idx)
                    except Exception:
                        log.exception("[Grid] fill_fn failed for index=%d", idx)
                card.setVisible(True)
                row_cards.append(card)

            self._visible_cards[r] = row_cards

    def _get_card(self) -> QWidget:
        """Get a card from the pool or create a new one."""
        if self._pool:
            card = self._pool.pop()
            card.clear_content()
            return card
        return self._card_class(on_click=self._click_fn)

    def _recycle_all(self):
        """Return all visible cards to the pool."""
        for cards in self._visible_cards.values():
            for card in cards:
                card.setVisible(False)
                self._pool.append(card)
        self._visible_cards.clear()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._items:
            new_cols = max(1, self._scroll.viewport().width() // self._card_width)
            if new_cols != self._num_cols:
                self._recycle_all()
                self._recompute_layout()
            else:
                # Reposition existing cards for new width
                self._recycle_all()
                self._update_visible()

"""Step 3 — label captures. The most-used screen, so it is keyboard-first.

    Enter      confirm the value in the box (the suggestion, or what you typed)
    typing     replaces the suggestion (it arrives pre-selected)
    R          reject the crop (unreadable, cut off, not a value)
    arrows     previous / next crop (PageUp/PageDown jump 10)
    Esc        throw away what you typed and restore the suggestion

Saving happens on a worker thread and the view advances immediately, so
labelling never waits on the disk. If a save fails the crop is put back
the way it was and the error is shown.
"""

from __future__ import annotations

import html
import re
from typing import Optional

from PySide6.QtCore import QRegularExpression, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListView, QListWidget,
    QListWidgetItem, QSizePolicy, QVBoxLayout, QWidget,
)

from shared.qt.theme import P

from ..style import (
    ACCENT, COMBO_QSS, HEAD, KIND_LABELS, KINDS, MONO, SOURCE_INFO, STATUS_COLOURS,
    Card, Chip, button, heading,
)
from .base import StepPage, load_qimage, progress_bar, scaled_pixmap, status_style

PAGE_SIZE = 200
TODO = ("unlabeled", "proposed")
SHOW_OPTIONS = (
    ("To review", "review"),
    ("Has a suggestion", "proposed"),
    ("No suggestion", "unlabeled"),
    ("Confirmed", "confirmed"),
    ("Rejected", "rejected"),
    ("Everything", None),
)
THUMB = QSize(120, 54)


def _norm(v: Optional[str]) -> str:
    """Compare values the way a human would: ignore thousands separators/spaces."""
    return re.sub(r"[,\s]", "", v or "")


class LabelEntry(QLineEdit):
    """The value box. Owns the labelling keys so focus never has to move."""

    confirm_pressed = Signal()
    reject_pressed = Signal()
    move_requested = Signal(int)
    reset_pressed = Signal()

    def keyPressEvent(self, event) -> None:
        key, mods = event.key(), event.modifiers()
        plain = not (mods & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier))
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self.confirm_pressed.emit()
        elif key == Qt.Key_R and plain:
            self.reject_pressed.emit()
        elif key in (Qt.Key_Left, Qt.Key_Up):
            self.move_requested.emit(-1)
        elif key in (Qt.Key_Right, Qt.Key_Down):
            self.move_requested.emit(1)
        elif key == Qt.Key_PageUp:
            self.move_requested.emit(-10)
        elif key == Qt.Key_PageDown:
            self.move_requested.emit(10)
        elif key == Qt.Key_Escape:
            self.reset_pressed.emit()
        else:
            super().keyPressEvent(event)
            return
        event.accept()


class CropView(QLabel):
    """Shows the current crop as large as fits, in whole-pixel steps."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._img: Optional[QImage] = None
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(420, 190)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet(f"background: #05070a; border: 1px solid {P.border_card}; "
                           f"border-radius: 3px; color: {P.fg_dim}; font-family: {MONO};")

    def set_image(self, img: Optional[QImage], placeholder: str = "") -> None:
        self._img = img
        if img is None:
            self.setPixmap(QPixmap())
            self.setText(placeholder)
        else:
            self.setText("")
            self._rescale()

    def _rescale(self) -> None:
        if self._img is not None:
            self.setPixmap(scaled_pixmap(self._img, self.width() - 16, self.height() - 16))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._rescale()


def _thumb(img: Optional[QImage], status: str, current: bool = False) -> QPixmap:
    pm = QPixmap(THUMB)
    pm.fill(QColor("#05070a"))
    p = QPainter(pm)
    if img is not None:
        sp = scaled_pixmap(img, THUMB.width() - 4, THUMB.height() - 8, integer=False)
        p.drawPixmap((THUMB.width() - sp.width()) // 2, (THUMB.height() - 5 - sp.height()) // 2, sp)
    p.fillRect(0, THUMB.height() - 4, THUMB.width(), 4, QColor(STATUS_COLOURS.get(status, P.fg_dim)))
    p.end()
    return pm


class LabelPage(StepPage):
    title = "Label"

    def __init__(self, handle, parent: Optional[QWidget] = None):
        super().__init__(handle, parent)
        self._items: list[dict] = []
        self._idx = -1
        self._more_available = False
        self._syncing_grid = False

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 16, 22, 12)
        root.setSpacing(10)

        # ── header: title + filters ──
        top = QHBoxLayout()
        top.setSpacing(10)
        top.addWidget(heading("Label", self))
        top.addSpacing(12)
        top.addWidget(self._caption("Kind"))
        self.kind = QComboBox(self)
        self.kind.setStyleSheet(COMBO_QSS)
        self.kind.addItem("All kinds", None)
        for k in KINDS:
            self.kind.addItem(KIND_LABELS[k], k)
        self.kind.currentIndexChanged.connect(lambda _i: self.reload())
        top.addWidget(self.kind)
        top.addWidget(self._caption("Show"))
        self.show_combo = QComboBox(self)
        self.show_combo.setStyleSheet(COMBO_QSS)
        for text, val in SHOW_OPTIONS:
            self.show_combo.addItem(text, val)
        self.show_combo.currentIndexChanged.connect(lambda _i: self.reload())
        top.addWidget(self.show_combo)
        top.addStretch(1)
        root.addLayout(top)

        # ── stats bar ──
        stats = QHBoxLayout()
        stats.setSpacing(8)
        self.chips = {s: Chip(n, STATUS_COLOURS[s], self) for s, n in (
            ("unlabeled", "NO SUGGESTION"), ("proposed", "SUGGESTED"),
            ("confirmed", "CONFIRMED"), ("rejected", "REJECTED"))}
        for c in self.chips.values():
            stats.addWidget(c)
        stats.addSpacing(8)
        self.done_bar = progress_bar(self, visible=True)
        self.done_bar.setMinimumWidth(220)
        self.done_bar.setMaximumWidth(280)
        stats.addWidget(self.done_bar)
        stats.addStretch(1)
        root.addLayout(stats)

        # ── main: current crop (left) + grid (right) ──
        main = QHBoxLayout()
        main.setSpacing(12)
        root.addLayout(main, 1)

        left = Card(self, margins=(14, 10, 14, 12))
        main.addWidget(left, 3)
        self.pos_label = QLabel("", left)
        self.pos_label.setStyleSheet(status_style(P.fg_dim))
        left.body.addWidget(self.pos_label)
        self.view = CropView(left)
        left.body.addWidget(self.view, 1)

        entry_row = QHBoxLayout()
        entry_row.setSpacing(12)
        self.entry = LabelEntry(left)
        self.entry.setValidator(QRegularExpressionValidator(QRegularExpression(r"[0-9.,% ]*"), self.entry))
        self.entry.setMaxLength(16)
        self.entry.setFixedHeight(58)
        self.entry.setMinimumWidth(240)
        self.entry.setMaximumWidth(280)
        self.entry.setPlaceholderText("type value")
        self.entry.setStyleSheet(
            f"QLineEdit {{ background: #070a0f; color: {P.fg_bright}; border: 2px solid {ACCENT}; "
            f"border-radius: 4px; padding: 2px 12px; font-family: {MONO}; font-size: 26pt; "
            f"font-weight: bold; selection-background-color: rgba(51, 221, 136, 110); "
            f"selection-color: {P.fg_bright}; }}")
        self.entry.confirm_pressed.connect(self.confirm)
        self.entry.reject_pressed.connect(self.reject)
        self.entry.move_requested.connect(self.move)
        self.entry.reset_pressed.connect(self._reset_entry)
        entry_row.addWidget(self.entry)

        src_col = QVBoxLayout()
        src_col.setSpacing(4)
        self.badge = QLabel("", left)
        src_col.addWidget(self.badge, 0, Qt.AlignLeft)
        self.trust = QLabel("", left)
        self.trust.setWordWrap(True)
        self.trust.setStyleSheet(status_style(P.fg))
        src_col.addWidget(self.trust)
        self.engines = QLabel("", left)
        self.engines.setTextFormat(Qt.RichText)
        self.engines.setWordWrap(True)
        self.engines.setStyleSheet(status_style(P.fg_dim))
        src_col.addWidget(self.engines)
        entry_row.addLayout(src_col, 1)
        left.body.addLayout(entry_row)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        self.confirm_btn = button("✓  Confirm  [Enter]", left, "primary")
        self.confirm_btn.clicked.connect(self.confirm)
        self.reject_btn = button("✕  Reject  [R]", left, "danger")
        self.reject_btn.clicked.connect(self.reject)
        self.prev_btn = button("←  Prev", left)
        self.prev_btn.clicked.connect(lambda: self.move(-1))
        self.next_btn = button("Next  →", left)
        self.next_btn.clicked.connect(lambda: self.move(1))
        for b in (self.confirm_btn, self.reject_btn, self.prev_btn, self.next_btn):
            b.setFocusPolicy(Qt.NoFocus)
            btns.addWidget(b)
        btns.addStretch(1)
        left.body.addLayout(btns)
        # A plain label with a fixed height (not an auto-hiding status line):
        # this changes on every keypress and must not make the layout jump.
        self.message = QLabel("", left)
        self.message.setFixedHeight(20)
        self.message.setStyleSheet(status_style(P.fg_dim))
        left.body.addWidget(self.message)

        right = Card(self, margins=(10, 10, 10, 10))
        right.setFixedWidth(2 * (THUMB.width() + 10) + 44)
        main.addWidget(right)
        self.grid_caption = QLabel("", right)
        self.grid_caption.setStyleSheet(
            f"font-family: {HEAD}; font-size: 9pt; font-weight: bold; color: {ACCENT}; "
            f"background: transparent;")
        right.body.addWidget(self.grid_caption)
        self.grid = QListWidget(right)
        self.grid.setViewMode(QListView.IconMode)
        self.grid.setIconSize(THUMB)
        self.grid.setGridSize(QSize(THUMB.width() + 10, THUMB.height() + 26))
        self.grid.setMovement(QListView.Static)
        self.grid.setResizeMode(QListView.Adjust)
        self.grid.setUniformItemSizes(True)
        self.grid.setWordWrap(False)
        self.grid.setFocusPolicy(Qt.NoFocus)
        self.grid.setStyleSheet(
            f"QListWidget {{ background: rgba(5, 7, 10, 200); border: 1px solid {P.border}; "
            f"font-family: {MONO}; font-size: 9pt; color: {P.fg}; }}"
            f"QListWidget::item {{ border: 1px solid transparent; border-radius: 3px; padding: 2px; }}"
            f"QListWidget::item:selected {{ background: rgba(51, 221, 136, 45); "
            f"border: 1px solid {ACCENT}; color: {P.fg_bright}; }}"
            f"QListWidget::item:hover {{ border: 1px solid {P.border_card}; }}")
        self.grid.currentRowChanged.connect(self._grid_row_changed)
        right.body.addWidget(self.grid, 1)

        # ── key legend ──
        legend = QLabel(self)
        legend.setTextFormat(Qt.RichText)
        legend.setStyleSheet(status_style(P.fg_dim))
        legend.setText("  ·  ".join(
            f"{self._key(k)} {html.escape(v)}" for k, v in (
                ("Enter", "confirm"), ("0-9", "type to correct"), ("R", "reject"),
                ("← →", "move"), ("PgUp PgDn", "jump 10"), ("Esc", "undo typing"))))
        root.addWidget(legend)

        self._stats_timer = QTimer(self)
        self._stats_timer.setSingleShot(True)
        self._stats_timer.setInterval(250)
        self._stats_timer.timeout.connect(self._fetch_stats)
        self._show_item()

    # ── small builders ──
    def _caption(self, text: str) -> QLabel:
        lbl = QLabel(text.upper(), self)
        lbl.setStyleSheet(f"color: {P.fg_dim}; font-family: {HEAD}; font-size: 8pt; "
                          f"background: transparent;")
        return lbl

    @staticmethod
    def _key(k: str) -> str:
        return (f"<span style='color:{P.fg_bright}; background:{P.bg_input}; "
                f"border:1px solid {P.border_card};'>&nbsp;{html.escape(k)}&nbsp;</span>")

    # ── loading ──
    @property
    def show_mode(self):
        return self.show_combo.currentData()

    def refresh(self) -> None:
        if not self._items and not self.runner.is_running("Loading captures"):
            self.reload()
        else:
            self._stats_soon()
        self.entry.setFocus()

    def reload(self) -> None:
        kind, mode = self.kind.currentData(), self.show_mode
        self._say("Loading…")
        self.runner.start("Loading captures", self._fetch, kind, mode,
                          on_done=self._loaded, on_error=self._load_failed)
        self._stats_soon()

    def _fetch(self, kind, mode) -> dict:
        if mode == "review":
            # Suggested ones first: they are one keypress each.
            rows = self.api.list_captures(status="proposed", kind=kind, limit=PAGE_SIZE)
            more = len(rows) >= PAGE_SIZE
            rows2 = self.api.list_captures(status="unlabeled", kind=kind, limit=PAGE_SIZE)
            more = more or len(rows2) >= PAGE_SIZE
            rows = list(rows) + list(rows2)
        else:
            rows = list(self.api.list_captures(status=mode, kind=kind, limit=PAGE_SIZE))
            more = len(rows) >= PAGE_SIZE
        out = []
        for r in rows:
            item = dict(r)
            item["_img"] = load_qimage(item.get("image_path"))
            out.append(item)
        return {"rows": out, "more": more}

    def _loaded(self, data: dict) -> None:
        self._items = data["rows"]
        self._more_available = data["more"]
        self._syncing_grid = True
        self.grid.clear()
        for it in self._items:
            li = QListWidgetItem()
            self._decorate(li, it)
            self.grid.addItem(li)
        self._syncing_grid = False
        self._idx = 0 if self._items else -1
        self._say("")
        self._show_item()

    def _load_failed(self, msg: str) -> None:
        self._say(f"Could not load captures: {msg}", P.red)

    def _stats_soon(self) -> None:
        self._stats_timer.start()

    def _fetch_stats(self) -> None:
        kind = self.kind.currentData()
        self.runner.start("Counting labels", self.api.label_stats, kind, on_done=self._show_stats)

    def _show_stats(self, st: dict) -> None:
        for k, chip in self.chips.items():
            chip.set_value(f"{int(st.get(k, 0)):,}")
        total = sum(int(st.get(k, 0)) for k in self.chips)
        done = int(st.get("confirmed", 0)) + int(st.get("rejected", 0))
        self.done_bar.setValue(int(1000 * done / total) if total else 0)
        self.done_bar.setFormat(f"{done:,} of {total:,} reviewed")
        todo = int(st.get("unlabeled", 0)) + int(st.get("proposed", 0))
        self.set_summary(f"{todo:,} to review")

    # ── display ──
    def _current(self) -> Optional[dict]:
        return self._items[self._idx] if 0 <= self._idx < len(self._items) else None

    def _decorate(self, li: QListWidgetItem, it: dict) -> None:
        li.setIcon(_thumb(it.get("_img"), it.get("status", "")))
        if it.get("status") == "confirmed":
            text = it.get("label") or ""
        elif it.get("status") == "rejected":
            text = "rejected"
        else:
            text = it.get("proposed") or "?"
        li.setText(text)
        li.setForeground(QColor(STATUS_COLOURS.get(it.get("status"), P.fg)))
        li.setToolTip(f"{it.get('id')} · {KIND_LABELS.get(it.get('kind'), it.get('kind'))} · "
                      f"{it.get('status')}")
        li.setTextAlignment(Qt.AlignHCenter | Qt.AlignTop)

    def _refresh_thumb(self, idx: int) -> None:
        li = self.grid.item(idx)
        if li is not None and 0 <= idx < len(self._items):
            self._decorate(li, self._items[idx])

    def _show_item(self) -> None:
        it = self._current()
        n = len(self._items)
        todo_left = sum(1 for x in self._items if x.get("status") in TODO)
        self.grid_caption.setText(f"{n} LOADED  ·  {todo_left} STILL TO DO")
        has = it is not None
        for w in (self.confirm_btn, self.reject_btn, self.entry):
            w.setEnabled(has)
        self.prev_btn.setEnabled(has and self._idx > 0)
        self.next_btn.setEnabled(has and self._idx < n - 1)
        if not has:
            self.pos_label.setText("")
            empty = ("Nothing here yet. Turn on capture (step 2) or import old captures."
                     if self.show_mode in ("review", None) else "No captures match this filter.")
            self.view.set_image(None, empty)
            self.entry.clear()
            self._set_badge(None, None)
            self.trust.setText("")
            self.engines.setText("")
            return
        self.pos_label.setText(
            f"{self._idx + 1} of {n}   ·   {KIND_LABELS.get(it.get('kind'), it.get('kind'))}"
            f"   ·   {it.get('id')}")
        self.view.set_image(it.get("_img"), "image missing")
        status = it.get("status")
        if status == "confirmed":
            value = it.get("label") or ""
        else:
            value = it.get("proposed") or ""
        self.entry.setText(value)
        self.entry.selectAll()
        self._set_badge(it, status)
        self.engines.setText(self._engines_html(it))
        if self.grid.currentRow() != self._idx:
            self._syncing_grid = True
            self.grid.setCurrentRow(self._idx)
            self._syncing_grid = False
        self.grid.scrollToItem(self.grid.item(self._idx))
        if self.isVisible():
            self.entry.setFocus()

    def _set_badge(self, it: Optional[dict], status: Optional[str]) -> None:
        if it is None:
            self.badge.setText("")
            self.badge.setStyleSheet("background: transparent;")
            return
        if status in ("confirmed", "rejected"):
            colour = STATUS_COLOURS[status]
            text = "CONFIRMED" if status == "confirmed" else "REJECTED"
            trust = ("You confirmed this. Enter keeps it; type to change it."
                     if status == "confirmed" else
                     "You rejected this. Type a value and press Enter to label it after all.")
        else:
            src = it.get("proposal_source") if it.get("proposed") else None
            text, colour, trust = SOURCE_INFO.get(src, SOURCE_INFO[None])
            if src == "consensus":
                reads = [v for v in (it.get("engines") or {}).values() if v]
                agree = sum(1 for v in reads if _norm(v) == _norm(it.get("proposed")))
                if reads:
                    text = f"CONSENSUS  {agree}/{len(it.get('engines') or {})} ENGINES AGREE"
        self.badge.setText(text)
        self.badge.setStyleSheet(
            f"QLabel {{ color: {colour}; border: 1px solid {colour}; border-radius: 3px; "
            f"padding: 3px 10px; font-family: {HEAD}; font-size: 9pt; font-weight: bold; "
            f"background: rgba(20, 26, 38, 230); }}")
        self.trust.setText(trust)

    @staticmethod
    def _engines_html(it: dict) -> str:
        eng = it.get("engines") or {}
        if not eng:
            return "Engine readings: none recorded for this crop."
        ref = _norm(it.get("label") if it.get("status") == "confirmed" else it.get("proposed"))
        parts = []
        for name, val in eng.items():
            if not val:
                parts.append(f"{html.escape(name)}&nbsp;<span style='color:{P.fg_disabled}'>no&nbsp;read</span>")
                continue
            ok = ref and _norm(val) == ref
            colour = P.green if ok else (P.red if ref else P.fg)
            parts.append(f"{html.escape(name)}&nbsp;<b style='color:{colour}'>{html.escape(val)}</b>")
        return "Engines read: " + "  ·  ".join(parts)

    # ── navigation ──
    def move(self, delta: int) -> None:
        if not self._items:
            return
        self._idx = max(0, min(len(self._items) - 1, self._idx + delta))
        self._say("")
        self._show_item()

    def goto(self, idx: int) -> None:
        if 0 <= idx < len(self._items):
            self._idx = idx
            self._show_item()

    def _grid_row_changed(self, row: int) -> None:
        if not self._syncing_grid and row >= 0 and row != self._idx:
            self.goto(row)

    def _advance(self) -> None:
        n = len(self._items)
        if self.show_mode == "review":
            order = list(range(self._idx + 1, n)) + list(range(0, self._idx + 1))
            nxt = next((i for i in order if self._items[i].get("status") in TODO), None)
            if nxt is None:
                self._show_item()
                if self._more_available:
                    # Wait for in-flight saves, or the reload could fetch a crop
                    # whose confirm hasn't landed yet and show it as to-do again.
                    self._say("Page done, loading more…")
                    if self.runner.busy:
                        self._reload_when_idle = True
                    else:
                        self.reload()
                else:
                    self._say("All caught up. Every loaded crop has been reviewed.", P.green)
                return
            self._idx = nxt
        elif self._idx < n - 1:
            self._idx += 1
        self._show_item()

    def _reset_entry(self) -> None:
        self._say("")
        self._show_item()

    def _on_busy_changed(self, busy: bool) -> None:
        if not busy and self._reload_when_idle:
            self._reload_when_idle = False
            self.reload()

    def _say(self, text: str, colour: str = "") -> None:
        self.message.setText(text)
        self.message.setStyleSheet(status_style(colour or P.fg_dim))

    def _index_of(self, it: dict) -> int:
        return next((i for i, x in enumerate(self._items) if x is it), -1)

    # ── actions ──
    def confirm(self) -> None:
        it = self._current()
        if it is None:
            return
        value = self.entry.text().strip()
        if not _norm(value):
            self._say("Type the value you see first, or press R to reject the crop.", P.yellow)
            return
        self._apply(it, "confirmed", value)
        self.runner.start("Saving label", self.api.confirm, it["id"], value,
                          on_done=lambda _r: self._stats_soon(),
                          on_error=lambda m, it=it, prev=self._prev: self._undo(it, prev, m))
        corrected = it.get("proposed") and _norm(value) != _norm(it.get("proposed"))
        self._say(f"Confirmed {value}" + ("  (corrected)" if corrected else ""), P.green)
        self._advance()

    def reject(self) -> None:
        it = self._current()
        if it is None:
            return
        self._apply(it, "rejected", None)
        self.runner.start("Rejecting crop", self.api.reject, it["id"],
                          on_done=lambda _r: self._stats_soon(),
                          on_error=lambda m, it=it, prev=self._prev: self._undo(it, prev, m))
        self._say("Rejected", P.red)
        self._advance()

    def _apply(self, it: dict, status: str, label: Optional[str]) -> None:
        self._prev = (it.get("status"), it.get("label"))
        it["status"], it["label"] = status, label
        self._refresh_thumb(self._index_of(it))

    def _undo(self, it: dict, prev: tuple, msg: str) -> None:
        it["status"], it["label"] = prev
        if self._index_of(it) >= 0:
            self._refresh_thumb(self._index_of(it))
        self._say(f"Could not save {it.get('id')}: {msg}. It has been put back.", P.red)
        self._stats_soon()
        if self._current() is it:
            self._show_item()

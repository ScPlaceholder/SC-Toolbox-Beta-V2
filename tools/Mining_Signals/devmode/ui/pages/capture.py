"""Step 2 — turn capture on/off, see how much is stored, import old crops."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QPushButton, QWidget,
)

from shared.qt.theme import P

from ..style import (
    ACCENT, ACCENT_RGB, COMBO_QSS, HEAD, KIND_LABELS, KINDS, MONO, Card, button,
    fmt_bytes, page_frame, subtext,
)
from .base import StepPage, progress_bar, status_label, status_style


def _toggle_qss(on: bool) -> str:
    if on:
        return (f"QPushButton {{ background: rgba({ACCENT_RGB}, 55); color: {P.fg_bright}; "
                f"border: 2px solid {ACCENT}; border-radius: 6px; padding: 14px 24px; "
                f"font-family: {HEAD}; font-size: 16pt; font-weight: bold; letter-spacing: 2px; }}"
                f"QPushButton:hover {{ background: rgba({ACCENT_RGB}, 90); }}")
    return (f"QPushButton {{ background: rgba(28, 34, 51, 220); color: {P.fg}; "
            f"border: 2px solid rgba({ACCENT_RGB}, 70); border-radius: 6px; padding: 14px 24px; "
            f"font-family: {HEAD}; font-size: 16pt; font-weight: bold; letter-spacing: 2px; }}"
            f"QPushButton:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}")


class CapturePage(StepPage):
    title = "Capture"

    def __init__(self, handle, parent: Optional[QWidget] = None):
        super().__init__(handle, parent)
        self._on = False
        _, root = page_frame(self, "Capture",
                             "While capture is on, every crop the scanner reads is saved together "
                             "with what each OCR engine read from it, so you can label it in step 3.")

        card = Card(self, "Live capture")
        row = QHBoxLayout()
        row.setSpacing(16)
        self.toggle = QPushButton("CAPTURE OFF", card)
        self.toggle.setCheckable(True)
        self.toggle.setCursor(Qt.PointingHandCursor)
        self.toggle.setMinimumSize(260, 64)
        self.toggle.setStyleSheet(_toggle_qss(False))
        self.toggle.clicked.connect(self._on_toggle)
        row.addWidget(self.toggle)
        self.toggle_hint = subtext("", card)
        row.addWidget(self.toggle_hint, 1)
        card.body.addLayout(row)

        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(4)
        self.count_val = self._stat(card, grid, 0, "Crops saved")
        self.size_val = self._stat(card, grid, 1, "Disk used")
        self.cap_val = self._stat(card, grid, 2, "Limit")
        grid.setColumnStretch(3, 1)
        card.body.addLayout(grid)
        self.usage = progress_bar(card, visible=True)
        card.body.addWidget(self.usage)
        self.usage_note = subtext("", card)
        card.body.addWidget(self.usage_note)
        root.addWidget(card)

        imp = Card(self, "Import old captures")
        imp.body.addWidget(subtext(
            "Bring in crops saved by older versions (for example the live_samples folder). "
            "A value in an old file name is only used as a suggestion: the reader that wrote "
            "those names was wrong about 1 time in 5, so every imported crop still needs you "
            "to confirm it before it can train or score a model.", imp))
        irow = QHBoxLayout()
        irow.setSpacing(10)
        lbl = QLabel("Region kind", imp)
        lbl.setStyleSheet(f"color: {P.fg}; font-family: {MONO}; background: transparent;")
        irow.addWidget(lbl)
        self.kind = QComboBox(imp)
        self.kind.setStyleSheet(COMBO_QSS)
        for k in KINDS:
            self.kind.addItem(KIND_LABELS[k], k)
        self.kind.setCurrentIndex(KINDS.index("signal_rgb"))
        irow.addWidget(self.kind)
        self.import_btn = button("Import old captures…", imp, "primary")
        self.import_btn.clicked.connect(self._pick_folder)
        irow.addWidget(self.import_btn)
        irow.addStretch(1)
        imp.body.addLayout(irow)
        self.import_bar = progress_bar(imp)
        imp.body.addWidget(self.import_bar)
        self.import_status = status_label(imp)
        imp.body.addWidget(self.import_status)
        root.addWidget(imp)
        root.addStretch(1)

        # Keep the numbers moving while capture is on and the page is visible.
        self._poll = QTimer(self)
        self._poll.setInterval(3000)
        self._poll.timeout.connect(self._poll_tick)

    def _stat(self, parent, grid: QGridLayout, col: int, name: str) -> QLabel:
        cap = QLabel(name.upper(), parent)
        cap.setStyleSheet(f"color: {P.fg_dim}; font-family: {HEAD}; font-size: 8pt; "
                          f"background: transparent;")
        val = QLabel("—", parent)
        val.setStyleSheet(f"color: {P.fg_bright}; font-family: {MONO}; font-size: 16pt; "
                          f"font-weight: bold; background: transparent;")
        grid.addWidget(cap, 0, col)
        grid.addWidget(val, 1, col)
        return val

    # ── data ──
    def refresh(self) -> None:
        if not self.runner.is_running("Reading capture stats"):
            self.runner.start("Reading capture stats", self._fetch, on_done=self._show)

    def _fetch(self) -> dict:
        return {"on": bool(self.api.capture_enabled()), "stats": self.api.capture_stats()}

    def _show(self, data: dict) -> None:
        self._set_on(data["on"])
        st = data["stats"] or {}
        count, used, cap = st.get("count", 0), st.get("bytes", 0), st.get("cap_bytes", 0)
        self.count_val.setText(f"{count:,}")
        self.size_val.setText(fmt_bytes(used))
        self.cap_val.setText(fmt_bytes(cap) if cap else "none")
        frac = (used / cap) if cap else 0.0
        self.usage.setValue(int(min(1.0, frac) * 1000))
        self.usage.setFormat(f"{frac * 100:.1f}% of limit" if cap else "no limit set")
        if st.get("over_cap") or (cap and frac >= 0.95):
            self.usage_note.setText("Almost at the limit. Label or export what you have "
                                    "before capturing more.")
            self.usage_note.setStyleSheet(status_style(P.yellow))
        else:
            self.usage_note.setText("Each capture is a small crop of the scan region.")
            self.usage_note.setStyleSheet(status_style(P.fg_dim))
        self.set_summary(f"{'on' if self._on else 'off'} · {count:,} crops")

    def _set_on(self, on: bool) -> None:
        self._on = bool(on)
        self.toggle.setChecked(self._on)
        self.toggle.setText("●  CAPTURE ON" if self._on else "○  CAPTURE OFF")
        self.toggle.setStyleSheet(_toggle_qss(self._on))
        self.toggle_hint.setText(
            "Saving crops while you scan. Mine normally; come back to Label when you have a "
            "few dozen." if self._on else
            "Click to start saving crops while you mine. Turn it off again whenever you "
            "have enough to label.")
        if self._on and self.isVisible():
            self._poll.start()
        else:
            self._poll.stop()

    # ── actions ──
    def _on_toggle(self) -> None:
        want = self.toggle.isChecked()
        self._set_on(want)            # optimistic; corrected by the refresh below
        self.runner.start("Switching capture", self.api.set_capture_enabled, want,
                          on_done=lambda _r: self.refresh(),
                          on_error=lambda _m: self.refresh())

    def _poll_tick(self) -> None:
        if self.isVisible():
            self.refresh()
        else:
            self._poll.stop()

    def _pick_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Folder of old captures")
        if path:
            self.import_folder(path)

    def import_folder(self, path: str) -> None:
        kind = self.kind.currentData()
        self.run_long("Importing old captures", self.api.import_folder, path, kind,
                      bar=self.import_bar, status=self.import_status,
                      disable=(self.import_btn, self.kind),
                      on_done=lambda n: self._imported(n, kind))

    def _imported(self, n: int, kind: str) -> None:
        self.import_status.setText(
            f"Imported {n:,} crops as {KIND_LABELS.get(kind, kind)}. Their old values are "
            f"suggestions only; confirm them in step 3 (Label).")
        self.refresh()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._on:
            self._poll.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._poll.stop()

"""Step 7 — package the hard cases (misreads, no-reads, corrections) into
a small zip the user sends to J, so the next shipped model learns them."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QStandardPaths, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QTableWidget,
    QTableWidgetItem, QWidget,
)

from shared.qt.theme import P

from ..style import (
    ACCENT, KIND_LABELS, MONO, TABLE_QSS, Banner, Card, Chip, button, fmt_bytes, page_frame,
)
from .base import StepPage, progress_bar, status_label

REASONS = {
    "misread": ("MISREAD", P.red, "an engine read it wrong"),
    "no_read": ("NO READ", P.yellow, "no engine could read it"),
    "corrected": ("CORRECTED", P.orange, "you fixed the suggested value"),
}


def default_export_dir() -> str:
    for loc in (QStandardPaths.DesktopLocation, QStandardPaths.DownloadLocation,
                QStandardPaths.HomeLocation):
        p = QStandardPaths.writableLocation(loc)
        if p and os.path.isdir(p):
            return p
    return str(Path.home())


class ExportPage(StepPage):
    title = "Export"

    def __init__(self, handle, parent: Optional[QWidget] = None):
        super().__init__(handle, parent)
        self._rows: list[dict] = []
        _, root = page_frame(self, "Export for J",
                             "Your hardest crops (the ones an engine got wrong, couldn't read, or "
                             "you had to correct) are the most valuable training data there is. "
                             "This bundles them so they can go into the next model everyone gets.")

        chips = QHBoxLayout()
        chips.setSpacing(8)
        self.chips = {k: Chip(v[0], v[1], self) for k, v in REASONS.items()}
        for c in self.chips.values():
            chips.addWidget(c)
        self.total = QLabel("", self)
        self.total.setStyleSheet(f"color: {P.fg}; font-family: {MONO}; font-size: 9pt; "
                                 f"background: transparent;")
        chips.addSpacing(10)
        chips.addWidget(self.total)
        chips.addStretch(1)
        self.refresh_btn = button("Refresh", self)
        self.refresh_btn.clicked.connect(self.refresh)
        chips.addWidget(self.refresh_btn)
        self.zip_btn = button("Create zip…", self, "primary")
        self.zip_btn.clicked.connect(self._pick_dest)
        chips.addWidget(self.zip_btn)
        root.addLayout(chips)

        self.bar = progress_bar(self)
        root.addWidget(self.bar)
        self.result = Banner(self, "ok")
        self.result.setVisible(False)
        self.open_btn = self.result.add_action("Open folder", self._open_folder)
        root.addWidget(self.result)
        self.status = status_label(self)
        root.addWidget(self.status)

        card = Card(self, "What will be in the zip")
        self.table = QTableWidget(0, 4, card)
        self.table.setHorizontalHeaderLabels(("File", "Region kind", "Why it's included", "Size"))
        self.table.setStyleSheet(TABLE_QSS)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setDefaultSectionSize(24)
        card.body.addWidget(self.table, 1)
        self.footnote = status_label(card,
            "Plus labels.jsonl (your confirmed values) and settings.json (game resolution, HUD "
            "colour, toolbox version, which models are active, counts). Crops only: no full "
            "screenshots, nothing else from your PC.")
        card.body.addWidget(self.footnote)
        root.addWidget(card, 1)
        self._zip_path: Optional[str] = None

    def refresh(self) -> None:
        self.runner.start("Preparing export preview", self.api.export_preview,
                          on_done=self._got_preview)

    def _got_preview(self, rows: list) -> None:
        self._rows = [dict(r) for r in (rows or [])]
        self.table.setRowCount(len(self._rows))
        counts = {k: 0 for k in REASONS}
        size = 0
        crops = 0
        for i, r in enumerate(self._rows):
            reason = r.get("reason")
            counts[reason] = counts.get(reason, 0) + 1
            size += int(r.get("size") or 0)
            if reason in REASONS:
                crops += 1
            # Anything else (e.g. "metadata" for labels.jsonl / settings.json)
            # is shown as-is, dimmed, and not counted as a crop.
            name, colour, why = REASONS.get(reason, (str(reason), P.fg_dim, ""))
            cells = (str(r.get("file", "")), KIND_LABELS.get(r.get("kind"), str(r.get("kind"))),
                     f"{name} — {why}" if why else name, fmt_bytes(r.get("size")))
            for c, v in enumerate(cells):
                it = QTableWidgetItem(v)
                if c == 2:
                    it.setForeground(QColor(colour))
                if c == 3:
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(i, c, it)
        for k, chip in self.chips.items():
            chip.set_value(counts.get(k, 0))
        n = crops
        self.total.setText(f"{n:,} crops · {fmt_bytes(size)} before compression")
        self.zip_btn.setEnabled(n > 0)
        if n == 0:
            self.status.setText("Nothing to send yet. Confirm some captures in step 3; any you "
                                "correct, or that an engine misread, will show up here.")
        else:
            self.status.setText("")
        self.set_summary(f"{n:,} crops ready" if n else "nothing yet")

    def _pick_dest(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Save the zip in…", default_export_dir())
        if path:
            self.create_zip(path)

    def create_zip(self, dest_dir: str) -> None:
        self.result.setVisible(False)
        self.run_long("Creating zip", self.api.export_zip, dest_dir, bar=self.bar,
                      status=self.status, disable=(self.zip_btn, self.refresh_btn),
                      on_done=self._zipped)

    def _zipped(self, path: str) -> None:
        self._zip_path = str(path)
        try:
            size = fmt_bytes(os.path.getsize(path))
        except OSError:
            size = "?"
        self.bar.setVisible(False)
        self.status.setText("")
        self.result.set_text(
            f"<b style='color:{ACCENT}'>Zip created</b> ({size})<br>"
            f"<span style='font-family:{MONO}; color:{P.fg_bright}'>{path}</span><br><br>"
            f"<b>Send this file to J</b> so these crops can go into the next model everyone "
            f"gets.", "ok")
        self.result.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.result.setVisible(True)

    @property
    def zip_path(self) -> Optional[str]:
        return self._zip_path

    def _open_folder(self) -> None:
        if self._zip_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self._zip_path).parent)))

"""Refinery Yields sub-tab — shows per-mineral yield bonuses at each refinery.

Columns: MINERAL + one per unique refinery profile (primary station name
in the header, "+N others" subtitle).  Cells are green for bonuses, red
for penalties, dim for 0%.  Refineries that share a profile are grouped
under a single column.
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import Qt, QObject, Signal
from PySide6.QtGui import QColor, QBrush
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QStyledItemDelegate, QHeaderView, QSizePolicy,
)

from shared.qt.theme import P
from shared.qt.data_table import SCTable, ColumnDef, SCTableModel

from services.refinery_yields import (
    RefineryYieldData, RefineryEntry,
    load_refinery_yields, short_name, shared_profile_label,
)

log = logging.getLogger(__name__)

ACCENT = "#33dd88"
GREEN = "#33dd88"
RED = "#ff4444"


# ── Async loader ─────────────────────────────────────────────────


class _YieldLoader(QObject):
    loaded = Signal(object)   # RefineryYieldData
    failed = Signal(str)

    def start(self, force: bool = False) -> None:
        threading.Thread(target=self._run, args=(force,), daemon=True).start()

    def _run(self, force: bool) -> None:
        result = load_refinery_yields(force=force)
        if result.ok and result.data is not None:
            self.loaded.emit(result.data)
        else:
            self.failed.emit(result.error or "Unknown error")


# ── Per-cell color delegate ──────────────────────────────────────


class _YieldCellDelegate(QStyledItemDelegate):
    """Paints yield cells green / red / dim based on value sign."""

    def __init__(self, source_model: SCTableModel, col_offset: int,
                 col_keys: list[str], parent=None):
        super().__init__(parent)
        self._source_model = source_model
        self._col_offset = col_offset   # first yield column index
        self._col_keys = col_keys       # row-dict key per column index
        self._even_bg = QColor(P.bg_primary)
        self._odd_bg = QColor(P.bg_card)
        self._selection_bg = QColor(P.selection)

    def paint(self, painter, option, index):
        # Resolve source row through any proxy
        src_idx = index
        model = index.model()
        if hasattr(model, "mapToSource"):
            try:
                src_idx = model.mapToSource(index)
            except Exception:
                pass

        # Background
        painter.save()
        if option.state & option.state.__class__.State_Selected:
            painter.fillRect(option.rect, self._selection_bg)
        elif index.row() % 2 == 0:
            painter.fillRect(option.rect, self._even_bg)
        else:
            painter.fillRect(option.rect, self._odd_bg)

        text = index.data(Qt.DisplayRole)
        if text is None:
            text = ""
        else:
            text = str(text)

        align = index.data(Qt.TextAlignmentRole)
        if align is None:
            align = int(Qt.AlignRight | Qt.AlignVCenter)

        # Colour logic: mineral name column uses default fg;
        # yield columns colour based on the underlying numeric value.
        col = src_idx.column()
        if col < self._col_offset:
            painter.setPen(QColor(P.fg))
        else:
            raw = self._source_model.row_data(src_idx.row())
            if raw and col < len(self._col_keys):
                val = raw.get(self._col_keys[col], 0)
                if isinstance(val, (int, float)):
                    if val > 0:
                        painter.setPen(QColor(GREEN))
                    elif val < 0:
                        painter.setPen(QColor(RED))
                    else:
                        painter.setPen(QColor(P.fg_dim))
                else:
                    painter.setPen(QColor(P.fg_dim))
            else:
                painter.setPen(QColor(P.fg_dim))

        rect = option.rect.adjusted(8, 0, -8, 0)
        painter.drawText(rect, int(align), text)
        painter.restore()


# ── Tab widget ───────────────────────────────────────────────────


class RefineryYieldsTab(QWidget):
    """Sub-tab showing the mineral × refinery yield comparison table."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._data: RefineryYieldData | None = None
        self._table: SCTable | None = None
        self._loader = _YieldLoader()
        self._loader.loaded.connect(self._on_loaded)
        self._loader.failed.connect(self._on_failed)
        self._build_ui()
        self._loader.start(force=False)

    # ── build ──

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 8, 0, 0)
        root.setSpacing(6)

        # Header row
        header = QHBoxLayout()
        header.setContentsMargins(8, 0, 8, 0)
        header.setSpacing(8)

        self._status = QLabel("Loading refinery yield data...", self)
        self._status.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 8pt; "
            f"color: {P.fg_dim}; background: transparent;"
        )
        header.addWidget(self._status, 1)

        btn_refresh = QPushButton("Refresh", self)
        btn_refresh.setCursor(Qt.PointingHandCursor)
        btn_refresh.setStyleSheet(f"""
            QPushButton {{
                font-family: Consolas, monospace; font-size: 8pt;
                font-weight: bold; color: {ACCENT}; background: transparent;
                border: 1px solid {ACCENT}; border-radius: 3px; padding: 3px 10px;
            }}
            QPushButton:hover {{ background: rgba(51, 221, 136, 0.15); }}
        """)
        btn_refresh.clicked.connect(self._on_refresh)
        header.addWidget(btn_refresh)
        root.addLayout(header)

        # Placeholder for the table (built once data arrives)
        self._table_container = QVBoxLayout()
        self._table_container.setContentsMargins(0, 0, 0, 0)
        root.addLayout(self._table_container, 1)

        # Footer hint
        self._hint = QLabel(
            "Select minerals above to find the optimal refinery.",
            self,
        )
        self._hint.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 8pt; "
            f"color: {P.fg_dim}; background: transparent; padding: 4px 8px;"
        )
        root.addWidget(self._hint)

    # ── data ──

    def _on_loaded(self, data: RefineryYieldData) -> None:
        self._data = data
        self._build_table()
        n_ref = len(data.refineries)
        n_profiles = len(data.profiles)
        n_minerals = len(data.all_minerals)
        self._status.setText(
            f"v{data.version or '?'}  \u00b7  "
            f"{n_ref} refineries ({n_profiles} unique)  \u00b7  "
            f"{n_minerals} minerals"
        )

    def _on_failed(self, err: str) -> None:
        self._status.setText(f"Load failed: {err}")

    def _on_refresh(self) -> None:
        self._status.setText("Refreshing...")
        self._loader.start(force=True)

    # ── table construction ──

    def _build_table(self) -> None:
        if self._data is None:
            return

        # Clear previous table
        if self._table is not None:
            self._table_container.removeWidget(self._table)
            self._table.deleteLater()
            self._table = None

        data = self._data

        # Deduplicate by profile: pick one "primary" refinery per unique
        # profile and note how many others share it.
        seen_profiles: dict[str, RefineryEntry] = {}
        col_entries: list[RefineryEntry] = []
        for r in data.refineries:
            if r.profile_id not in seen_profiles:
                seen_profiles[r.profile_id] = r
                col_entries.append(r)

        # Build column definitions: MINERAL + one per unique profile.
        cols: list[ColumnDef] = [
            ColumnDef("MINERAL", "_mineral", width=200),
        ]

        def _yield_fmt(v):
            if v is None or v == 0:
                return "0%"
            return f"+{v}%" if v > 0 else f"{v}%"

        for entry in col_entries:
            # Find all refineries sharing this profile for the tooltip
            siblings = [
                r.name for r in data.refineries
                if r.profile_id == entry.profile_id
            ]
            label = shared_profile_label(entry, data.refineries)
            header_text = short_name(entry.name)
            if label:
                header_text = f"{header_text}\n{label}"
            # Build a tooltip listing all stations with this profile
            if len(siblings) > 1:
                tip = "Same yield profile:\n" + "\n".join(
                    f"  \u2022 {name}" for name in siblings
                )
            else:
                tip = entry.name
            key = f"_ref_{entry.profile_id}"
            cols.append(ColumnDef(
                header_text, key, width=72,
                alignment=Qt.AlignRight, fmt=_yield_fmt,
                tooltip=tip,
            ))

        self._table = SCTable(
            columns=cols, parent=self, sortable=True,
        )

        # Build the column-key list so the delegate can look up values
        # from the row dict without touching SCTableModel internals.
        col_keys = [c.key for c in cols]

        # Custom delegate for per-cell colouring.
        delegate = _YieldCellDelegate(
            self._table._source_model,
            col_offset=1,
            col_keys=col_keys,
            parent=self._table,
        )
        self._table.setItemDelegate(delegate)

        # Column sizing
        header = self._table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        for i in range(1, len(cols)):
            header.setSectionResizeMode(i, QHeaderView.ResizeToContents)

        # Populate rows: one per mineral.
        rows: list[dict] = []
        for mineral in data.all_minerals:
            row: dict = {"_mineral": mineral}
            for entry in col_entries:
                key = f"_ref_{entry.profile_id}"
                profile = data.profiles.get(entry.profile_id, {})
                row[key] = profile.get(mineral, 0)
            rows.append(row)

        self._table.set_data(rows)
        self._table_container.addWidget(self._table)

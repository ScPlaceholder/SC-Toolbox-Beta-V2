"""Regolith-style mining chart widget.

Shows a scrollable grid of mining locations (rows) versus resources
(columns), split into a blue "ship mining" group and a gold "FPS /
ground-vehicle" group. Colours follow the SC Toolbox MobiGlas palette.
"""

from __future__ import annotations

import logging
import math
import threading

from PySide6.QtCore import Qt, QObject, Signal, QRect, QPoint
from PySide6.QtGui import (
    QColor, QFont, QPainter, QPen, QBrush, QFontMetrics,
    QPainterPath, QTransform,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QSizePolicy, QTabBar,
)

from shared.qt.theme import P
from shared.qt.search_bar import SCSearchBar
from services.mining_chart_data import (
    MiningChartFetcher, MiningChartData, LocationRow,
)

# View modes for the inner tab
VIEW_SHIP = "ship"
VIEW_FPS = "fps"


def _fuzzy_match(needle: str, haystack: str) -> bool:
    """Case-insensitive subsequence fuzzy match.

    Returns True when all characters of ``needle`` appear in order
    (not necessarily contiguous) inside ``haystack``. An empty needle
    always matches. Non-alphanumeric characters in the needle are
    ignored so users can type ``mining base igb`` or ``igbfxw``.
    """
    if not needle:
        return True
    n = "".join(c for c in needle.lower() if c.isalnum())
    if not n:
        return True
    h = haystack.lower()
    i = 0
    for ch in h:
        if ch == n[i]:
            i += 1
            if i == len(n):
                return True
    return False

log = logging.getLogger(__name__)

# ── Base layout constants (scale = 1.0) ──
# These are the unscaled 1x values.  The grid multiplies them by its
# ``_scale`` factor to produce the actual metrics, so Ctrl+Wheel zoom
# and the "Reset Scale" button can resize the whole chart uniformly.
_BASE_NAME_COL_W = 220       # location name column
_BASE_CELL_W = 44
_BASE_CELL_H = 24
_BASE_SYSTEM_ROW_H = 28
_BASE_HEADER_BAND_H = 14     # "SHIP MINING" / "FPS / ROC MINING" band
_BASE_HEADER_PAD = 10        # padding below the rotated text
_BASE_FONT_NAME = 9          # name-column font pt
_BASE_FONT_CELL = 8          # resource-cell font pt
_BASE_FONT_SYS = 10          # system row font pt
_BASE_FONT_HEADER = 9        # rotated column-header font pt
# Scale bounds (matches the launcher's ui_scale range)
_SCALE_MIN = 0.5
_SCALE_MAX = 3.0
_SCALE_STEP = 0.1
# Column-header rotation angle (counter-clockwise from the baseline)
_HEADER_ANGLE_DEG = 60

# ── Colours ──
_SHIP_FG = "#6cc9ff"         # blue group header text
_SHIP_BG_DIM = "#0b1a2a"
_FPS_FG = "#ffcc66"           # gold group header text
_FPS_BG_DIM = "#1a1408"

# Resource category colours used for the name column
_SYSTEM_COLOR = "#ff9933"
_PLANET_COLOR = "#66ccff"
_MOON_COLOR = "#aaccff"
_LAGRANGE_COLOR = "#88aaff"
_OTHER_COLOR = P.fg_dim

_TYPE_GLYPH = {
    "system": "\u2699",       # gear
    "planet": "\u25cf",       # ●
    "moon":   "\u25d1",       # ◑
    "lagrange": "\u25c7",     # ◇
    "belt": "\u2219",         # ∙
    "cluster": "\u2217",      # ∗
    "cave": "\u25b3",         # △
    "special": "\u2605",      # ★
    "event":  "\u29bf",
}


def _cell_bg(pct: float, group: str) -> QColor:
    """Return the background colour for a percentage cell."""
    if pct <= 0:
        return QColor(0, 0, 0, 0)
    # 0 -> 45%+ → dim to bright
    t = min(1.0, pct / 45.0)
    if group == "ship":
        # Blue ramp
        r = int(12 + t * 20)
        g = int(28 + t * 70)
        b = int(60 + t * 140)
    else:
        # Gold ramp
        r = int(60 + t * 140)
        g = int(44 + t * 90)
        b = int(12 + t * 20)
    return QColor(r, g, b, 220)


def _cell_fg(pct: float) -> QColor:
    if pct <= 0:
        return QColor(P.fg_dim)
    if pct >= 30:
        return QColor(P.fg_bright)
    return QColor(P.fg)


# ─────────────────────────────────────────────────────────────────────────────
# Async loader — keeps the UI thread free while scmdb.net is fetched.
# ─────────────────────────────────────────────────────────────────────────────


class _ChartLoader(QObject):
    loaded = Signal(object)  # MiningChartData
    failed = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._fetcher = MiningChartFetcher()

    def start(self, force: bool = False) -> None:
        t = threading.Thread(target=self._run, args=(force,), daemon=True)
        t.start()

    def _run(self, force: bool) -> None:
        result = self._fetcher.load(force_refresh=force)
        if result.ok and result.data is not None:
            self.loaded.emit(result.data)
        else:
            self.failed.emit(result.error or "Unknown error")


# ─────────────────────────────────────────────────────────────────────────────
# The actual grid widget (custom-painted for speed and rotated headers).
# ─────────────────────────────────────────────────────────────────────────────


class MiningChartGrid(QWidget):
    """Custom-painted grid.  Supports rotated column headers and the
    ship/FPS column split without needing QTableView item delegates."""

    # Emitted whenever the focused column/row or sort direction changes.
    # The tab/bubble uses this to update its status line and header buttons.
    focused_column_changed = Signal(str)
    focus_state_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._data: MiningChartData | None = None
        self._view_mode: str = VIEW_SHIP
        self._resource_filter: str = ""
        self._location_filter: str = ""
        # Focus: either a column or a row, mutually exclusive.  Click
        # cycle is None → desc → asc → None on repeat clicks.
        self._focused_col: str | None = None
        self._focused_row: str | None = None
        self._sort_dir: str = "desc"   # "desc" = highest first, "asc" = lowest first
        # Chart-local zoom factor (1.0 = default).  Cells, fonts, and
        # the header area scale uniformly.  Users can change it via
        # Ctrl+Wheel or the "Reset Scale" button.
        self._scale: float = 1.0
        # Header height and column width are computed lazily in
        # ``_refilter`` from the longest visible column name so rotated
        # labels never clip or overflow.  The initial values have to be
        # generous enough that the very first paint (before any data
        # arrives) still has room for long rotated labels.
        self._header_h: int = 180
        self._dyn_cell_w: int = _BASE_CELL_W
        # Filtered views computed by ``_refilter``
        self._visible_rows: list[LocationRow] = []
        self._visible_cols: list[str] = []
        # Cached y-offsets for every row (populated by _recalculate_size)
        # so mousePress hit-testing can find the row under the cursor
        # without re-running the height calculation.
        self._row_y_offsets: list[tuple[int, int, LocationRow | None]] = []
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setMinimumSize(_BASE_NAME_COL_W + 200, self._header_h + 200)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self.setMouseTracking(True)

    # ── scale-aware metric helpers ──

    @property
    def _name_col_w(self) -> int:
        return int(round(_BASE_NAME_COL_W * self._scale))

    @property
    def _cell_w(self) -> int:
        # The scale multiplier is already baked into ``_dyn_cell_w``
        # (which is recomputed on every refilter), so we only need to
        # clamp to a sensible minimum here.
        return max(8, self._dyn_cell_w)

    @property
    def _cell_h(self) -> int:
        return max(8, int(round(_BASE_CELL_H * self._scale)))

    @property
    def _system_row_h(self) -> int:
        return max(10, int(round(_BASE_SYSTEM_ROW_H * self._scale)))

    @property
    def _header_band_h(self) -> int:
        return max(6, int(round(_BASE_HEADER_BAND_H * self._scale)))

    @property
    def _header_pad(self) -> int:
        return max(4, int(round(_BASE_HEADER_PAD * self._scale)))

    def _scaled_font(self, base_pt: int, bold: bool = False) -> QFont:
        pt = max(6, int(round(base_pt * self._scale)))
        weight = QFont.Bold if bold else QFont.Normal
        return QFont("Consolas", pt, weight)

    def set_scale(self, scale: float) -> None:
        new = max(_SCALE_MIN, min(_SCALE_MAX, round(scale, 2)))
        if new == self._scale:
            return
        self._scale = new
        self._refilter()

    def reset_scale(self) -> None:
        """Snap the chart zoom back to 1.0."""
        self.set_scale(1.0)

    def current_scale(self) -> float:
        return self._scale

    def _label_rotated_rect(self, text: str):
        """Return the tight bounding rect of ``text`` after rotation
        through ``-_HEADER_ANGLE_DEG``.  Used by header-height and
        cell-width computation to get pixel-accurate bounds."""
        font = self._scaled_font(_BASE_FONT_HEADER, bold=True)
        xform = QTransform()
        xform.rotate(-_HEADER_ANGLE_DEG)
        path = QPainterPath()
        path.addText(0.0, 0.0, font, text)
        return xform.map(path).boundingRect()

    def _compute_header_height(self) -> int:
        """Return the pixel height needed to fit the rotated column
        labels + group-title band without clipping.

        Uses :class:`QPainterPath` to measure the actual post-rotation
        bounding box of each visible label (including the sort-arrow
        prefix shown when a column is focused).  This is more reliable
        than multiplying ``QFontMetrics.horizontalAdvance`` by
        ``sin(angle)`` because it accounts for ascent/descent,
        sub-pixel text metrics, and font-fallback quirks.
        """
        labels = self._visible_cols or [
            "Hephaestanite", "Quantainium", "Lindinium",
        ]

        worst_top = 0.0
        for name in labels:
            # Measure both plain and "arrow-prefixed" versions so the
            # header reserves enough space whether or not the column
            # is currently focused.
            for text in (name, f"\u25bc {name}", f"\u25b2 {name}"):
                rot = self._label_rotated_rect(text)
                extent = -rot.top()
                if extent > worst_top:
                    worst_top = extent

        needed = int(math.ceil(worst_top))
        safety = max(16, int(round(20 * self._scale)))
        return (self._header_band_h
                + self._header_pad
                + needed
                + self._header_pad
                + safety)

    def _compute_cell_w(self) -> int:
        """Return the minimum cell width that lets the widest rotated
        column label fit inside a single column without overflowing
        into the neighbouring cell.

        The rotated labels are painted horizontally centred on their
        own column, so the required cell width is the width of the
        widest post-rotation bounding box plus a small side pad.
        """
        base = max(8, int(round(_BASE_CELL_W * self._scale)))
        labels = self._visible_cols or [
            "Hephaestanite", "Quantainium", "Lindinium",
        ]
        worst_w = 0.0
        for name in labels:
            for text in (name, f"\u25bc {name}", f"\u25b2 {name}"):
                rot = self._label_rotated_rect(text)
                if rot.width() > worst_w:
                    worst_w = rot.width()
        side_pad = max(6, int(round(8 * self._scale)))
        return max(base, int(math.ceil(worst_w)) + side_pad)

    # ── data ──

    def set_data(self, data: MiningChartData) -> None:
        self._data = data
        self._refilter()

    def set_view_mode(self, mode: str) -> None:
        if mode not in (VIEW_SHIP, VIEW_FPS):
            return
        if mode == self._view_mode:
            return
        self._view_mode = mode
        # Focused column belongs to the previous mode's column set and
        # focused row may no longer have any values in the new view, so
        # clear both when switching views.
        self._focused_col = None
        self._focused_row = None
        self._refilter()
        self.focused_column_changed.emit("")
        self.focus_state_changed.emit()

    def view_mode(self) -> str:
        return self._view_mode

    def set_resource_filter(self, text: str) -> None:
        self._resource_filter = (text or "").strip()
        self._refilter()

    def set_location_filter(self, text: str) -> None:
        self._location_filter = (text or "").strip()
        self._refilter()

    def focused_column(self) -> str | None:
        return self._focused_col

    def focused_row(self) -> str | None:
        return self._focused_row

    def sort_direction(self) -> str:
        return self._sort_dir

    def clear_focus(self) -> None:
        """Clear any focused row/column (keeps sort direction)."""
        if self._focused_col is None and self._focused_row is None:
            return
        self._focused_col = None
        self._focused_row = None
        self._refilter()
        self.focused_column_changed.emit("")
        self.focus_state_changed.emit()

    def toggle_sort_direction(self) -> None:
        """Flip between highest-first (desc) and lowest-first (asc)."""
        self._sort_dir = "asc" if self._sort_dir == "desc" else "desc"
        self._refilter()
        self.focus_state_changed.emit()

    def set_sort_direction(self, direction: str) -> None:
        if direction not in ("asc", "desc") or direction == self._sort_dir:
            return
        self._sort_dir = direction
        self._refilter()
        self.focus_state_changed.emit()

    def _cycle_focus(self, axis: str, key: str) -> None:
        """Advance the click cycle for a row/column click.

        Cycle: unfocused → focused desc → focused asc → unfocused.
        ``axis`` is ``"col"`` or ``"row"``.  Selecting one clears the
        other automatically.
        """
        if axis == "col":
            if self._focused_col == key:
                if self._sort_dir == "desc":
                    self._sort_dir = "asc"
                else:
                    self._focused_col = None
                    self._sort_dir = "desc"
            else:
                self._focused_col = key
                self._focused_row = None
                self._sort_dir = "desc"
        else:  # "row"
            if self._focused_row == key:
                if self._sort_dir == "desc":
                    self._sort_dir = "asc"
                else:
                    self._focused_row = None
                    self._sort_dir = "desc"
            else:
                self._focused_row = key
                self._focused_col = None
                self._sort_dir = "desc"
        self._refilter()
        self.focused_column_changed.emit(self._focused_col or "")
        self.focus_state_changed.emit()

    def visible_counts(self) -> tuple[int, int]:
        """Return ``(visible_location_rows, visible_resource_columns)``."""
        rows = sum(1 for r in self._visible_rows if r.depth > 0)
        return rows, len(self._visible_cols)

    # ── filtering ──

    def _row_resources(self, row: LocationRow) -> dict[str, float]:
        return row.ship_resources if self._view_mode == VIEW_SHIP else row.fps_resources

    def _refilter(self) -> None:
        """Recompute ``_visible_rows`` + ``_visible_cols`` and repaint."""
        if not self._data:
            self._visible_rows = []
            self._visible_cols = []
            self._recalculate_size()
            self.update()
            return

        # Columns: start from the view mode's native column list, then
        # narrow to ones that match the resource filter.
        all_cols = (self._data.ship_columns if self._view_mode == VIEW_SHIP
                    else self._data.fps_columns)
        res_needle = self._resource_filter
        if res_needle:
            visible_cols = [c for c in all_cols if _fuzzy_match(res_needle, c)]
        else:
            visible_cols = list(all_cols)

        # Drop stale column focus if the focused column fell out of the
        # visible column set (e.g. the resource filter now excludes it).
        if self._focused_col is not None and self._focused_col not in visible_cols:
            self._focused_col = None
            self.focused_column_changed.emit("")
            self.focus_state_changed.emit()

        focused_col = self._focused_col
        focused_row = self._focused_row
        reverse = self._sort_dir == "desc"
        visible_cols_set = set(visible_cols)

        # Rows: walk in source order, bucket rows under their system so
        # we can sort each bucket independently when a column is focused.
        loc_needle = self._location_filter
        # list of (system_header_row, [child_row, ...])
        buckets: list[tuple[LocationRow | None, list[LocationRow]]] = []
        current_header: LocationRow | None = None
        current_children: list[LocationRow] = []

        def _flush() -> None:
            nonlocal current_header, current_children
            if current_children:
                buckets.append((current_header, current_children))
            current_header = None
            current_children = []

        for row in self._data.rows:
            if row.depth == 0:
                _flush()
                current_header = row
                continue

            if loc_needle and not _fuzzy_match(loc_needle, row.name):
                continue

            resources = self._row_resources(row)
            # Focused column acts as an extra hard filter.
            if focused_col is not None:
                if resources.get(focused_col, 0) <= 0:
                    continue
            else:
                if visible_cols_set:
                    if not any(resources.get(c, 0) > 0 for c in visible_cols_set):
                        continue
                else:
                    continue
            current_children.append(row)
        _flush()

        # Drop stale row focus if the focused row fell out of the
        # visible row set (e.g. the location filter now excludes it).
        if focused_row is not None:
            still_visible = any(
                r.name == focused_row
                for _h, children in buckets
                for r in children
            )
            if not still_visible:
                self._focused_row = None
                focused_row = None
                self.focus_state_changed.emit()

        # Sort each bucket by the focused column, keeping source order
        # as the tiebreaker for stability.
        kept_rows: list[LocationRow] = []
        for header, children in buckets:
            if focused_col is not None:
                children = sorted(
                    children,
                    key=lambda r: self._row_resources(r).get(focused_col, 0.0),
                    reverse=reverse,
                )
            if header is not None:
                kept_rows.append(header)
            kept_rows.extend(children)

        # Column ordering: when a row is focused, sort the visible
        # columns by that row's resource percentages.  Columns with zero
        # are pushed to the end so the "interesting" ones cluster first.
        if focused_row is not None:
            target_row = next(
                (r for r in kept_rows
                 if r.depth > 0 and r.name == focused_row),
                None,
            )
            if target_row is not None:
                target_resources = self._row_resources(target_row)
                def _col_key(c: str) -> tuple[int, float]:
                    v = target_resources.get(c, 0.0)
                    # Zero values always go last regardless of direction
                    # so the interesting data clusters at the start.
                    return (0 if v > 0 else 1,
                            -v if reverse else v)
                visible_cols = sorted(visible_cols, key=_col_key)

        self._visible_rows = kept_rows
        self._visible_cols = visible_cols
        # Header height and cell width depend on the longest visible
        # column label AND the current font scale, so both must be
        # recomputed every time the visible column set changes.
        self._header_h = self._compute_header_height()
        self._dyn_cell_w = self._compute_cell_w()
        self._recalculate_size()
        self.update()

    def _recalculate_size(self) -> None:
        ncols = len(self._visible_cols)
        name_w = self._name_col_w
        cell_w = self._cell_w
        cell_h = self._cell_h
        sys_h = self._system_row_h
        head_h = self._header_h
        w = name_w + max(ncols, 1) * cell_w
        h = head_h
        # Build the y-offset lookup as we go so click hit-testing can
        # map a mouse Y coordinate to the row that was painted there.
        self._row_y_offsets = []
        y = head_h
        for r in self._visible_rows:
            row_h = sys_h if r.depth == 0 else cell_h
            self._row_y_offsets.append((y, y + row_h, r))
            y += row_h
            h += row_h
        if h == head_h:
            # Empty result — reserve space for the "no matches" message.
            h += 60
        self.setFixedSize(max(w, name_w + 200), h)

    # ── hit-testing / click interaction ──

    def _column_at(self, x: int) -> str | None:
        """Return the visible column name whose cell band contains ``x``."""
        if x < self._cols_x0():
            return None
        idx = (x - self._cols_x0()) // self._cell_w
        if 0 <= idx < len(self._visible_cols):
            return self._visible_cols[idx]
        return None

    def _row_at(self, y: int) -> LocationRow | None:
        """Return the row painted at ``y`` (or ``None`` for header / empty)."""
        if y < self._header_h:
            return None
        for top, bot, row in self._row_y_offsets:
            if top <= y < bot:
                return row
        return None

    def _in_sticky_header(self, y: int) -> bool:
        """Is the given widget-local y inside the sticky header band?"""
        top = self._sticky_header_top()
        return top <= y < top + self._header_h

    def mousePressEvent(self, ev) -> None:  # noqa: N802 — Qt API
        if ev.button() != Qt.LeftButton or not self._data:
            super().mousePressEvent(ev)
            return

        pos = ev.position().toPoint()

        # Sticky header band: treat any click here as a column-header
        # click, overriding whatever data row happens to live at this y
        # in widget-local coordinates.
        if self._in_sticky_header(pos.y()):
            if pos.x() < self._cols_x0():
                super().mousePressEvent(ev)
                return
            col = self._column_at(pos.x())
            if col is None:
                super().mousePressEvent(ev)
                return
            self._cycle_focus("col", col)
            ev.accept()
            return

        # Below the header: find the row under the cursor.
        row = self._row_at(pos.y())
        if row is None or row.depth == 0:
            super().mousePressEvent(ev)
            return

        # Clicks in the name column toggle row focus.  Clicks on a
        # resource cell toggle the column focus.
        if pos.x() < self._cols_x0():
            self._cycle_focus("row", row.name)
            ev.accept()
            return

        col = self._column_at(pos.x())
        if col is None:
            super().mousePressEvent(ev)
            return
        self._cycle_focus("col", col)
        ev.accept()

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802 — Qt API
        if not self._data:
            self.setCursor(Qt.ArrowCursor)
            super().mouseMoveEvent(ev)
            return
        pos = ev.position().toPoint()
        cursor = Qt.ArrowCursor
        if self._in_sticky_header(pos.y()):
            if pos.x() >= self._cols_x0() and self._column_at(pos.x()) is not None:
                cursor = Qt.PointingHandCursor
        else:
            row = self._row_at(pos.y())
            if row is not None and row.depth > 0:
                if pos.x() < self._cols_x0() or self._column_at(pos.x()) is not None:
                    cursor = Qt.PointingHandCursor
        self.setCursor(cursor)
        super().mouseMoveEvent(ev)

    def wheelEvent(self, ev) -> None:  # noqa: N802 — Qt API
        """Ctrl + wheel zooms the chart; plain wheel propagates."""
        if ev.modifiers() & Qt.ControlModifier:
            delta = ev.angleDelta().y()
            if delta == 0:
                ev.ignore()
                return
            step = _SCALE_STEP if delta > 0 else -_SCALE_STEP
            self.set_scale(self._scale + step)
            ev.accept()
            return
        super().wheelEvent(ev)

    # ── sticky-header scroll handling ──

    def _find_scroll_area(self) -> QScrollArea | None:
        """Walk up the parent chain to find the enclosing QScrollArea."""
        p = self.parent()
        while p is not None:
            if isinstance(p, QScrollArea):
                return p
            p = p.parent() if hasattr(p, "parent") else None
        return None

    def _sticky_header_top(self) -> int:
        """Return the y (in widget-local coords) where the header should
        be painted so it stays pinned to the top of the visible area.

        When the grid is placed inside a ``QScrollArea`` that scrolls it
        downward, ``self.y()`` becomes negative relative to the viewport.
        Negating it gives the current scroll offset, which is exactly
        the widget-local y at which the viewport's top edge lives.
        Returns 0 if the grid is not scrolled.
        """
        y = self.y()
        if y < 0:
            return -y
        return 0

    def showEvent(self, ev) -> None:  # noqa: N802
        """Connect to the parent scroll area so the sticky header
        repaints whenever the user scrolls vertically."""
        super().showEvent(ev)
        if getattr(self, "_scroll_connected", False):
            return
        sa = self._find_scroll_area()
        if sa is not None:
            sa.verticalScrollBar().valueChanged.connect(self.update)
            sa.horizontalScrollBar().valueChanged.connect(self.update)
            self._scroll_connected = True

    # ── painting ──

    def paintEvent(self, ev) -> None:  # noqa: N802 — Qt API
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        p.setRenderHint(QPainter.TextAntialiasing, True)

        bg = QColor(P.bg_primary)
        p.fillRect(self.rect(), bg)

        if not self._data:
            p.end()
            return

        # Paint body rows FIRST so the sticky header can overlay them
        # once the grid has been scrolled.
        if self._visible_cols and any(r.depth > 0 for r in self._visible_rows):
            self._paint_rows(p)
        else:
            self._paint_empty_state(p)

        # Sticky header: pinned to the top of the visible viewport.
        sticky_top = self._sticky_header_top()
        # Clear the area behind the header so any data rows we just
        # painted don't bleed through.
        p.fillRect(QRect(0, sticky_top, self.width(), self._header_h), bg)
        self._paint_header(p, y_offset=sticky_top)
        p.end()

    def _cols_x0(self) -> int:
        return self._name_col_w

    def _paint_header(self, p: QPainter, y_offset: int = 0) -> None:
        assert self._data is not None
        name_w = self._name_col_w
        head_h = self._header_h
        cell_w = self._cell_w
        band_h = self._header_band_h
        group = self._view_mode

        # ── Pass 1: paint all backgrounds + separators FIRST ──
        # This is critical: each rotated column label extends upward-and-
        # right into the neighbouring cells' rects.  If we filled each
        # column's background right before its own label, the next
        # column's fillRect would overwrite the previous label's top
        # characters.  So we draw every cell background in a first pass
        # and then overlay all the text in a second pass.

        # Name column background
        hdr_rect = QRect(0, y_offset, name_w, head_h)
        p.fillRect(hdr_rect, QColor(P.bg_header))

        # Resource column backgrounds (and cell separators)
        x = self._cols_x0()
        for name in self._visible_cols:
            self._paint_column_header_bg(p, name, x, group=group,
                                         y_offset=y_offset)
            x += cell_w

        # Bottom border line under the whole header
        p.setPen(QColor(P.border))
        p.drawLine(0, y_offset + head_h - 1,
                   self.width(), y_offset + head_h - 1)

        # Group title band across the column headers
        if self._visible_cols:
            w = len(self._visible_cols) * cell_w
            band = QRect(self._cols_x0(), y_offset + 2, w, band_h)
            if group == VIEW_SHIP:
                p.fillRect(band, QColor(_SHIP_BG_DIM))
                p.setPen(QColor(_SHIP_FG))
                title = "SHIP MINING"
            else:
                p.fillRect(band, QColor(_FPS_BG_DIM))
                p.setPen(QColor(_FPS_FG))
                title = "FPS / ROC MINING"
            p.setFont(self._scaled_font(_BASE_FONT_CELL, bold=True))
            p.drawText(band, Qt.AlignCenter, title)

        # ── Pass 2: paint all text + focus highlights on top ──

        # "LOCATION" text in the name column header
        p.setFont(self._scaled_font(_BASE_FONT_HEADER, bold=True))
        p.setPen(QColor(P.fg_dim))
        label_rect = hdr_rect.adjusted(10, 0, -10, 0)
        p.drawText(label_rect, Qt.AlignLeft | Qt.AlignBottom, "LOCATION")

        # Rotated column labels (and focus borders) — drawn AFTER all
        # backgrounds so they aren't truncated by neighbouring cells.
        x = self._cols_x0()
        for name in self._visible_cols:
            self._paint_column_header_text(p, name, x, group=group,
                                           y_offset=y_offset)
            x += cell_w

    def _paint_empty_state(self, p: QPainter) -> None:
        rect = QRect(0, self._header_h, self.width(), self.height() - self._header_h)
        p.setPen(QColor(P.fg_dim))
        p.setFont(self._scaled_font(_BASE_FONT_SYS))
        p.drawText(rect, Qt.AlignCenter, "No locations match your filters.")

    def _paint_column_header_bg(
        self, p: QPainter, name: str, x: int, group: str,
        y_offset: int = 0,
    ) -> None:
        """Pass 1: paint background fill and vertical separator for a
        single column header.  No text — that's drawn in pass 2 so
        neighbouring cells can't overwrite it."""
        head_h = self._header_h
        cell_w = self._cell_w
        band_h = self._header_band_h
        rect = QRect(x, y_offset, cell_w, head_h)
        is_focused = (name == self._focused_col)
        base_bg = QColor(_SHIP_BG_DIM if group == "ship" else _FPS_BG_DIM)
        if is_focused:
            focus_bg = QColor(P.accent)
            focus_bg.setAlpha(55)
            p.fillRect(rect, base_bg)
            p.fillRect(rect, focus_bg)
        else:
            p.fillRect(rect, base_bg)
        p.setPen(QColor(P.border))
        # Start the vertical separator just below the title band so it
        # doesn't cut through the "SHIP MINING" text.
        p.drawLine(rect.right(), y_offset + band_h + 2,
                   rect.right(), y_offset + head_h - 1)

    def _paint_column_header_text(
        self, p: QPainter, name: str, x: int, group: str,
        y_offset: int = 0,
    ) -> None:
        """Pass 2: paint the rotated label text (and focus border) for
        a single column header.  Called after every column's background
        has been filled so the rotated text can extend into neighbouring
        cell areas without being overwritten."""
        head_h = self._header_h
        cell_w = self._cell_w
        band_h = self._header_band_h
        pad = self._header_pad
        rect = QRect(x, y_offset, cell_w, head_h)
        is_focused = (name == self._focused_col)

        # Build the label text (sort arrow prefix only when focused).
        if is_focused:
            arrow = "\u25bc" if self._sort_dir == "desc" else "\u25b2"  # ▼ / ▲
            label_text = f"{arrow} {name}"
        else:
            label_text = name

        # Centre the label horizontally within its own cell.  A
        # -60°-rotated string of advance width ``W`` extends rightward
        # from its baseline origin by ``W·cos(60°) = W/2`` pixels; if
        # we placed the origin at the cell center the label would sit
        # in the right half of the cell and overflow long labels into
        # the next column.  Shifting the origin left by half that
        # horizontal span puts the label's visual midpoint on the
        # column center.
        rot_rect = self._label_rotated_rect(label_text)
        # Horizontal offset from the rotated bounding rect's left edge
        # to its centre.
        x_mid_offset = rot_rect.left() + rot_rect.width() / 2.0
        origin_x = x + cell_w / 2 - x_mid_offset
        origin_y = y_offset + head_h - pad

        p.save()
        p.translate(origin_x, origin_y)
        p.rotate(-_HEADER_ANGLE_DEG)
        if is_focused:
            text_color = QColor(P.fg_bright)
        else:
            text_color = QColor(_SHIP_FG if group == "ship" else _FPS_FG)
        p.setPen(text_color)
        p.setFont(self._scaled_font(_BASE_FONT_HEADER, bold=True))
        p.drawText(0, 0, label_text)
        p.restore()

        # Focused column: draw a thin accent border around the cell
        # (below the title band) so the highlight is visible at a glance.
        if is_focused:
            pen = QPen(QColor(P.accent))
            pen.setWidth(2)
            p.setPen(pen)
            p.drawLine(rect.left() + 1, y_offset + band_h + 2,
                       rect.left() + 1, y_offset + head_h - 2)
            p.drawLine(rect.right() - 1, y_offset + band_h + 2,
                       rect.right() - 1, y_offset + head_h - 2)
            p.drawLine(rect.left() + 1, y_offset + head_h - 2,
                       rect.right() - 1, y_offset + head_h - 2)

    def _paint_rows(self, p: QPainter) -> None:
        assert self._data is not None
        name_w = self._name_col_w
        cell_w = self._cell_w
        cell_h = self._cell_h
        sys_h = self._system_row_h
        y = self._header_h
        font_name = self._scaled_font(_BASE_FONT_NAME)
        font_name_bold = self._scaled_font(_BASE_FONT_NAME, bold=True)
        font_cell = self._scaled_font(_BASE_FONT_CELL, bold=True)
        font_sys = self._scaled_font(_BASE_FONT_SYS, bold=True)
        name_pad_l = max(10, int(round(14 * self._scale)))
        name_pad_r = max(4, int(round(4 * self._scale)))

        group = self._view_mode
        for r in self._visible_rows:
            if r.depth == 0:
                # System header row
                rect = QRect(0, y, self.width(), sys_h)
                p.fillRect(rect, QColor(P.bg_header))
                p.setPen(QColor(_SYSTEM_COLOR))
                p.setFont(font_sys)
                p.drawText(rect.adjusted(name_pad_l, 0, -8, 0),
                           Qt.AlignLeft | Qt.AlignVCenter,
                           f"{_TYPE_GLYPH.get('system', '')}  {r.name.upper()}")
                p.setPen(QColor(P.border))
                p.drawLine(0, y + sys_h - 1, self.width(), y + sys_h - 1)
                y += sys_h
                continue

            # Regular data row
            bg = QColor(P.bg_card) if (y // cell_h) % 2 == 0 else QColor(P.bg_primary)
            p.fillRect(QRect(0, y, self.width(), cell_h), bg)

            is_focus_row = (self._focused_row is not None
                            and r.name == self._focused_row)
            if is_focus_row:
                # Tint the whole row so it stands out regardless of zebra.
                tint = QColor(P.accent)
                tint.setAlpha(34)
                p.fillRect(QRect(0, y, self.width(), cell_h), tint)
                # Accent bar flush with the left edge of the name column.
                bar_w = max(2, int(round(3 * self._scale)))
                p.fillRect(QRect(0, y + 1, bar_w, cell_h - 2),
                           QColor(P.accent))

            # Location name
            name_color = _MOON_COLOR if r.loc_type == "moon" else (
                _PLANET_COLOR if r.loc_type == "planet" else (
                    _LAGRANGE_COLOR if r.loc_type == "lagrange" else _OTHER_COLOR
                )
            )
            if is_focus_row:
                name_color = P.fg_bright
            glyph = _TYPE_GLYPH.get(r.loc_type, "\u00b7")
            p.setPen(QColor(name_color))
            p.setFont(font_name_bold if (r.loc_type == "planet" or is_focus_row) else font_name)
            name_rect = QRect(name_pad_l, y, name_w - name_pad_l - name_pad_r, cell_h)
            if is_focus_row:
                arrow = "\u25bc" if self._sort_dir == "desc" else "\u25b2"  # ▼ / ▲
                label = f"{arrow} {glyph}  {r.name}"
            else:
                label = f"{glyph}  {r.name}"
            p.drawText(name_rect, Qt.AlignLeft | Qt.AlignVCenter, label)

            # Resource cells for the active view
            resources = self._row_resources(r)
            p.setFont(font_cell)
            x = self._cols_x0()
            for col in self._visible_cols:
                is_focus_col = (col == self._focused_col)
                self._paint_pct_cell(
                    p, x, y, resources.get(col, 0.0), group,
                    focused=is_focus_col,
                )
                x += cell_w

            # Thin row separator
            p.setPen(QColor(P.border))
            p.drawLine(0, y + cell_h - 1, self.width(), y + cell_h - 1)

            y += cell_h

    def _paint_pct_cell(
        self, p: QPainter, x: int, y: int, pct: float,
        group: str, focused: bool = False,
    ) -> None:
        rect = QRect(x, y, self._cell_w, self._cell_h)
        p.fillRect(rect, _cell_bg(pct, group))
        if focused:
            tint = QColor(P.accent)
            tint.setAlpha(38)
            p.fillRect(rect, tint)
        if pct > 0:
            p.setPen(_cell_fg(pct))
            label = f"{int(round(pct))}%"
            p.drawText(rect, Qt.AlignCenter, label)
        p.setPen(QColor(P.border))
        p.drawLine(rect.right(), rect.top(), rect.right(), rect.bottom())


# ─────────────────────────────────────────────────────────────────────────────
# Outer tab widget with header, refresh, pop-out button and status label.
# ─────────────────────────────────────────────────────────────────────────────


class MiningChartTab(QWidget):
    """A tab page that owns the ``MiningChartGrid`` inside a scroll area
    plus header controls (refresh + pop-out).  Shares a singleton bubble
    window with the caller so the same chart can be viewed while scanning.
    """

    def __init__(self, parent=None, popout_handler=None) -> None:
        super().__init__(parent)
        self._popout_handler = popout_handler
        self._data: MiningChartData | None = None
        self._loader = _ChartLoader(self)
        self._loader.loaded.connect(self._on_loaded)
        self._loader.failed.connect(self._on_failed)
        self._build_ui()
        self._loader.start(force=False)

    # ── build ──

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(6)

        # ── Header row: title + status + refresh + pop-out ──
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)

        self._title = QLabel("Live Mining Chart")
        self._title.setStyleSheet(
            f"font-family: Electrolize, Consolas, monospace; "
            f"font-size: 11pt; font-weight: bold; "
            f"color: {P.fg_bright}; background: transparent;"
        )
        header.addWidget(self._title)

        self._status = QLabel("Loading...")
        self._status.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 8pt; "
            f"color: {P.fg_dim}; background: transparent;"
        )
        header.addWidget(self._status, 1)

        btn_style = f"""
            QPushButton {{
                font-family: Consolas, monospace;
                font-size: 8pt; font-weight: bold;
                color: {P.accent}; background: transparent;
                border: 1px solid {P.accent}; border-radius: 3px;
                padding: 4px 10px;
            }}
            QPushButton:hover {{ background: rgba(68, 170, 255, 0.15); }}
            QPushButton:disabled {{
                color: {P.fg_dim}; border-color: {P.border};
            }}
        """

        self._btn_sort_dir = QPushButton("\u25bc Highest")  # ▼ Highest
        self._btn_sort_dir.setCursor(Qt.PointingHandCursor)
        self._btn_sort_dir.setToolTip(
            "Toggle sort direction for the focused column/row.\n"
            "Highest = descending, Lowest = ascending."
        )
        self._btn_sort_dir.setStyleSheet(btn_style)
        self._btn_sort_dir.clicked.connect(self._on_toggle_sort_dir)
        header.addWidget(self._btn_sort_dir)

        self._btn_clear_sort = QPushButton("Clear Sort")
        self._btn_clear_sort.setCursor(Qt.PointingHandCursor)
        self._btn_clear_sort.setToolTip("Clear the focused column or row")
        self._btn_clear_sort.setStyleSheet(btn_style)
        self._btn_clear_sort.clicked.connect(self._on_clear_sort)
        header.addWidget(self._btn_clear_sort)

        self._btn_reset_scale = QPushButton("Reset Scale")
        self._btn_reset_scale.setCursor(Qt.PointingHandCursor)
        self._btn_reset_scale.setToolTip(
            "Reset the chart zoom to 100%. "
            "Tip: hold Ctrl and scroll the wheel on the chart to zoom."
        )
        self._btn_reset_scale.setStyleSheet(btn_style)
        self._btn_reset_scale.clicked.connect(self._on_reset_scale)
        header.addWidget(self._btn_reset_scale)

        self._btn_fullscreen = QPushButton("\u26f6 Fullscreen")  # ⛶
        self._btn_fullscreen.setCursor(Qt.PointingHandCursor)
        self._btn_fullscreen.setToolTip(
            "Toggle the Mining Signals window between fullscreen and normal"
        )
        self._btn_fullscreen.setStyleSheet(btn_style)
        self._btn_fullscreen.clicked.connect(self._on_toggle_fullscreen)
        header.addWidget(self._btn_fullscreen)

        self._btn_popout = QPushButton("Pop-out Chart")
        self._btn_popout.setCursor(Qt.PointingHandCursor)
        self._btn_popout.setToolTip(
            "Open the mining chart in a floating window so you can keep "
            "it visible while scanning in-game."
        )
        self._btn_popout.setStyleSheet(btn_style)
        self._btn_popout.clicked.connect(self._on_popout)
        header.addWidget(self._btn_popout)

        self._btn_refresh = QPushButton("Refresh")
        self._btn_refresh.setCursor(Qt.PointingHandCursor)
        self._btn_refresh.setStyleSheet(btn_style)
        self._btn_refresh.clicked.connect(self._on_refresh)
        header.addWidget(self._btn_refresh)

        root.addLayout(header)

        # ── View-mode sub-tab bar ──
        self._view_tabs = QTabBar(self)
        self._view_tabs.setDrawBase(False)
        self._view_tabs.setExpanding(False)
        self._view_tabs.setStyleSheet(f"""
            QTabBar {{
                background: transparent;
                border: none;
            }}
            QTabBar::tab {{
                background: {P.bg_card};
                color: {P.fg_dim};
                border: 1px solid {P.border};
                border-bottom: none;
                padding: 5px 14px;
                font-family: Consolas, monospace;
                font-size: 9pt;
                font-weight: bold;
                margin-right: 2px;
            }}
            QTabBar::tab:selected {{
                background: {P.bg_primary};
                color: {P.accent};
                border-bottom: 2px solid {P.accent};
            }}
            QTabBar::tab:hover:!selected {{
                color: {P.fg};
            }}
        """)
        # Tab index 0 = Ship Mining, 1 = FPS / ROC.
        self._view_tabs.addTab("Ship Mining")
        self._view_tabs.addTab("FPS / ROC Mining")
        self._view_tabs.currentChanged.connect(self._on_view_tab_changed)
        root.addWidget(self._view_tabs)

        # ── Search row: resource + location fuzzy search ──
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(8)

        res_lbl = QLabel("Resource:", self)
        res_lbl.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 8pt; "
            f"font-weight: bold; color: {P.fg_dim}; background: transparent;"
        )
        search_row.addWidget(res_lbl)

        self._resource_search = SCSearchBar(
            placeholder="Filter resources (e.g. quant, titan)...",
            debounce_ms=150, parent=self,
        )
        self._resource_search.setFixedHeight(24)
        self._resource_search.search_changed.connect(self._on_resource_search)
        search_row.addWidget(self._resource_search, 1)

        loc_lbl = QLabel("Location:", self)
        loc_lbl.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 8pt; "
            f"font-weight: bold; color: {P.fg_dim}; background: transparent;"
        )
        search_row.addWidget(loc_lbl)

        self._location_search = SCSearchBar(
            placeholder="Filter locations (e.g. hurl1, igbfxw, aberdeen)...",
            debounce_ms=150, parent=self,
        )
        self._location_search.setFixedHeight(24)
        self._location_search.search_changed.connect(self._on_location_search)
        search_row.addWidget(self._location_search, 1)

        root.addLayout(search_row)

        # ── Grid inside a scroll area ──
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(False)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setStyleSheet(f"""
            QScrollArea {{
                background: {P.bg_primary};
                border: 1px solid {P.border};
            }}
        """)
        self._grid = MiningChartGrid(self._scroll)
        self._grid.focused_column_changed.connect(self._on_focused_col_changed)
        self._grid.focus_state_changed.connect(self._on_focus_state_changed)
        self._scroll.setWidget(self._grid)
        root.addWidget(self._scroll, 1)

        # Sync button state with the initial grid state.
        self._on_focus_state_changed()

    # ── data callbacks ──

    def _on_loaded(self, data: MiningChartData) -> None:
        self._data = data
        self._grid.set_data(data)
        self._update_status()

    def _on_failed(self, err: str) -> None:
        self._status.setText(f"Load failed: {err}")

    def _update_status(self) -> None:
        if not self._data:
            return
        rows, cols = self._grid.visible_counts()
        mode = ("Ship" if self._grid.view_mode() == VIEW_SHIP else "FPS/ROC")
        parts = [
            f"v{self._data.version or '?'}",
            mode,
            f"{rows} locations",
            f"{cols} resources",
        ]
        focused_col = self._grid.focused_column()
        focused_row = self._grid.focused_row()
        direction = ("\u25bc desc" if self._grid.sort_direction() == "desc"
                     else "\u25b2 asc")
        if focused_col:
            parts.append(f"sort: col {focused_col} {direction}")
        elif focused_row:
            parts.append(f"sort: row {focused_row} {direction}")
        self._status.setText("  \u00b7  ".join(parts))

    def _sync_sort_button(self) -> None:
        """Reflect the current sort direction in the toggle button label."""
        direction = self._grid.sort_direction()
        if direction == "desc":
            self._btn_sort_dir.setText("\u25bc Highest")
        else:
            self._btn_sort_dir.setText("\u25b2 Lowest")
        has_focus = (self._grid.focused_column() is not None
                     or self._grid.focused_row() is not None)
        self._btn_sort_dir.setEnabled(has_focus)
        self._btn_clear_sort.setEnabled(has_focus)

    def _on_focused_col_changed(self, _col: str) -> None:
        self._update_status()

    def _on_focus_state_changed(self) -> None:
        self._sync_sort_button()
        self._update_status()

    def _on_toggle_sort_dir(self) -> None:
        self._grid.toggle_sort_direction()

    def _on_clear_sort(self) -> None:
        self._grid.clear_focus()

    def _on_reset_scale(self) -> None:
        self._grid.reset_scale()

    def _on_toggle_fullscreen(self) -> None:
        """Call the parent SCWindow's toggle_fullscreen() if available.

        Falls back to Qt's showFullScreen/showNormal on the top-level
        window so the button still does *something* in isolation.
        """
        top = self.window()
        if top is None:
            return
        if hasattr(top, "toggle_fullscreen"):
            top.toggle_fullscreen()
            return
        if top.isFullScreen():
            top.showNormal()
        else:
            top.showFullScreen()

    # ── actions ──

    def _on_refresh(self) -> None:
        self._status.setText("Refreshing...")
        self._loader.start(force=True)

    def _on_popout(self) -> None:
        if self._popout_handler:
            self._popout_handler(self._data)

    def _on_view_tab_changed(self, index: int) -> None:
        mode = VIEW_SHIP if index == 0 else VIEW_FPS
        self._grid.set_view_mode(mode)
        self._update_status()

    def _on_resource_search(self, text: str) -> None:
        self._grid.set_resource_filter(text)
        self._update_status()

    def _on_location_search(self, text: str) -> None:
        self._grid.set_location_filter(text)
        self._update_status()

    def current_data(self) -> MiningChartData | None:
        return self._data

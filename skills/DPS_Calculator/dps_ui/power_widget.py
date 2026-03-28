"""PowerAllocatorWidget -- PySide6 widget wrapping PowerAllocatorEngine."""
from __future__ import annotations

from PySide6.QtCore import Qt, QRect, Signal
from PySide6.QtGui import QColor, QPainter, QFont, QMouseEvent
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QFrame,
    QSizePolicy,
)

from shared.i18n import s_ as _
from shared.qt.theme import P
from dps_ui.constants import (
    BG2, BG3, BG4, BORDER, FG, FG_DIM, ACCENT, GREEN, YELLOW, RED,
    ORANGE, CYAN, PURPLE, PHYS_COL, ENERGY_COL, THERM_COL, HEADER_BG,
)
from dps_ui.helpers import fmt_sig
from services.power_engine import PowerAllocatorEngine


class _PipCanvas(QWidget):
    """A single pip-bar drawn entirely with QPainter."""

    PIP_W       = 18
    PIP_H       = 7
    PIP_GAP     = 2
    GREEN_PIP   = GREEN
    ORANGE_PIP  = ORANGE
    DARK_PIP    = "#2a3040"
    GREY_PIP    = FG_DIM

    pip_clicked = Signal(int)       # emits new level
    right_clicked = Signal()

    def __init__(self, slot: dict, parent=None):
        super().__init__(parent)
        self._slot = slot
        max_seg = slot.get("max_segments", 1)
        h = max(max_seg * (self.PIP_H + self.PIP_GAP), 9)
        self.setFixedSize(self.PIP_W, h)
        self.setCursor(Qt.PointingHandCursor)

    def set_slot(self, slot: dict):
        self._slot = slot
        max_seg = slot.get("max_segments", 1)
        h = max(max_seg * (self.PIP_H + self.PIP_GAP), 9)
        self.setFixedSize(self.PIP_W, h)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        slot = self._slot
        max_seg  = slot.get("max_segments", 1)
        current  = slot.get("current_seg", 0)
        default  = slot.get("default_seg", 0)
        enabled  = slot.get("enabled", True)
        w        = self.PIP_W
        pip_h    = self.PIP_H
        gap      = self.PIP_GAP

        for i in range(max_seg):
            seg_idx = max_seg - 1 - i
            y = i * (pip_h + gap)

            if not enabled:
                fill = self.GREY_PIP
            elif seg_idx < current and seg_idx < default:
                fill = self.GREEN_PIP
            elif seg_idx < current and seg_idx >= default:
                fill = self.ORANGE_PIP
            else:
                fill = self.DARK_PIP

            painter.fillRect(QRect(1, y, w - 2, pip_h), QColor(fill))
        painter.end()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            max_seg = self._slot.get("max_segments", 1)
            pip_h = self.PIP_H + self.PIP_GAP
            clicked_row = int(event.position().y() / pip_h) if pip_h else 0
            new_level = max_seg - clicked_row
            new_level = max(0, min(new_level, max_seg))
            self.pip_clicked.emit(new_level)
        elif event.button() == Qt.RightButton:
            self.right_clicked.emit()


class _ConsumptionBar(QWidget):
    """Horizontal consumption bar drawn with QPainter."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(10)
        self.setMinimumWidth(120)
        self._pct = 0.0
        self._color = GREEN

    def set_values(self, pct: float, color: str):
        self._pct = pct
        self._color = color
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(BG4))
        bar_w = self.width()
        fill_w = min(bar_w, bar_w * self._pct / 100)
        if fill_w > 0:
            painter.fillRect(QRect(0, 0, int(fill_w), self.height()), QColor(self._color))
        painter.end()


class PowerAllocatorWidget(QWidget):
    """Interactive power allocation panel matching erkul.games' power widget.

    Shows stacked pip bars for each powered component category,
    consumption bar, signature readouts, and SCM/NAV mode toggle.

    Delegates all computation to PowerAllocatorEngine and keeps
    PySide6 visuals in sync via _sync_ui().
    """

    power_changed = Signal()

    def __init__(self, parent, item_lookup_fn, raw_lookup_fn=None,
                 on_change=None, **kwargs):
        super().__init__(parent)
        self._engine = PowerAllocatorEngine(item_lookup_fn, raw_lookup_fn)
        self._on_change = on_change
        self._pip_widgets: list[tuple[_PipCanvas, dict]] = []
        self._build_static_ui()

    # -- public API (delegate to engine, then sync UI) -------------------------

    def load_ship(self, ship_data):
        self._engine.load_ship(ship_data)
        self._rebuild_columns()
        self._sync_ui()

    def set_mode(self, mode):
        self._engine.set_mode(mode)
        self._update_mode_buttons()
        self._sync_ui()

    def set_level_by_type(self, category, slot_idx, level):
        self._engine.set_level_by_type(category, slot_idx, level)
        self._sync_ui()

    def toggle_by_type(self, category, slot_idx):
        self._engine.toggle_by_type(category, slot_idx)
        self._sync_ui()

    # -- property delegates ----------------------------------------------------

    @property
    def em_signature(self):
        return self._engine.em_signature

    @property
    def ir_signature(self):
        return self._engine.ir_signature

    @property
    def cs_signature(self):
        return self._engine.cs_signature

    @property
    def weapon_power_ratio(self):
        return self._engine.weapon_power_ratio

    @property
    def shield_power_ratio(self):
        return self._engine.shield_power_ratio

    @property
    def _slots(self):
        return self._engine.slots

    @property
    def _categories(self):
        return self._engine.categories

    @property
    def _mode(self):
        return self._engine.mode

    # -- sync UI from engine state ---------------------------------------------

    def _sync_ui(self):
        result = self._engine.recalculate()

        self._lbl_em.setText(fmt_sig(result["em_sig"]))
        self._lbl_ir.setText(fmt_sig(result["ir_sig"]))
        self._lbl_cs.setText(fmt_sig(result["cs_sig"]))
        self._lbl_output.setText(
            f"{result['pp_online']} / {int(result['total_capacity'])}"
        )
        self._lbl_draw.setText(
            f"{result['total_draw']:.0f} / {result['total_capacity']:.0f}"
        )
        consumption_pct = result["consumption_pct"]
        self._lbl_pct.setText(f"{consumption_pct:.0f}%")

        if consumption_pct > 100:
            bar_color = RED
        elif consumption_pct >= 80:
            bar_color = YELLOW
        else:
            bar_color = GREEN
        self._consumption_bar.set_values(consumption_pct, bar_color)

        for pip_w, slot in self._pip_widgets:
            pip_w.set_slot(slot)

        if self._on_change:
            try:
                self._on_change()
            except Exception:  # broad catch intentional: top-level UI handler
                pass
        self.power_changed.emit()

    # -- internal: UI construction ---------------------------------------------

    def _build_static_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(2)

        self.setStyleSheet(f"background-color: {BG2};")

        # Header row: signatures + consumption
        hdr = QWidget(self)
        hdr.setStyleSheet(f"background-color: {HEADER_BG};")
        hdr_layout = QHBoxLayout(hdr)
        hdr_layout.setContentsMargins(6, 4, 6, 2)
        hdr_layout.setSpacing(4)

        # Signatures
        sig_frame = QWidget(hdr)
        sig_layout = QHBoxLayout(sig_frame)
        sig_layout.setContentsMargins(0, 0, 0, 0)
        sig_layout.setSpacing(2)

        for icon, color, attr in [
            ("\u26a1", ENERGY_COL, "_lbl_em"),
            ("\U0001f525", THERM_COL, "_lbl_ir"),
            ("\u25ce", PHYS_COL, "_lbl_cs"),
        ]:
            ic_lbl = QLabel(icon, sig_frame)
            ic_lbl.setStyleSheet(f"color: {color}; font-size: 9pt; background: transparent;")
            sig_layout.addWidget(ic_lbl)
            val_lbl = QLabel("0", sig_frame)
            val_lbl.setStyleSheet(f"color: {FG}; font-family: Consolas; font-size: 9pt; background: transparent;")
            sig_layout.addWidget(val_lbl)
            setattr(self, attr, val_lbl)

        hdr_layout.addWidget(sig_frame)

        # Output label
        self._lbl_output = QLabel("0 pwr", hdr)
        self._lbl_output.setStyleSheet(
            f"color: {GREEN}; font-family: Consolas; font-size: 9pt; font-weight: bold; background: transparent;"
        )
        hdr_layout.addWidget(self._lbl_output)

        hdr_layout.addStretch(1)

        # Consumption area (right)
        self._lbl_draw = QLabel("0 / 0", hdr)
        self._lbl_draw.setStyleSheet(
            f"color: {FG_DIM}; font-family: Consolas; font-size: 8pt; background: transparent;"
        )
        hdr_layout.addWidget(self._lbl_draw)

        self._consumption_bar = _ConsumptionBar(hdr)
        hdr_layout.addWidget(self._consumption_bar)

        self._lbl_pct = QLabel("0%", hdr)
        self._lbl_pct.setStyleSheet(
            f"color: {FG}; font-family: Consolas; font-size: 9pt; font-weight: bold; background: transparent;"
        )
        hdr_layout.addWidget(self._lbl_pct)

        main_layout.addWidget(hdr)

        # Column grid frame (populated by _rebuild_columns)
        self._col_widget = QWidget(self)
        self._col_layout = QHBoxLayout(self._col_widget)
        self._col_layout.setContentsMargins(4, 2, 4, 2)
        self._col_layout.setSpacing(1)
        self._col_layout.setAlignment(Qt.AlignBottom | Qt.AlignLeft)
        main_layout.addWidget(self._col_widget, 1)

        # SCM / NAV toggle
        mode_frame = QWidget(self)
        mode_layout = QHBoxLayout(mode_frame)
        mode_layout.setContentsMargins(4, 2, 4, 4)
        mode_layout.setSpacing(2)

        self._btn_scm = QPushButton(_("SCM"), mode_frame)
        self._btn_scm.setFixedWidth(50)
        self._btn_scm.setCursor(Qt.PointingHandCursor)
        self._btn_scm.clicked.connect(lambda: self.set_mode("SCM"))
        mode_layout.addWidget(self._btn_scm)

        self._btn_nav = QPushButton(_("NAV"), mode_frame)
        self._btn_nav.setFixedWidth(50)
        self._btn_nav.setCursor(Qt.PointingHandCursor)
        self._btn_nav.clicked.connect(lambda: self.set_mode("NAV"))
        mode_layout.addWidget(self._btn_nav)

        mode_layout.addStretch(1)
        main_layout.addWidget(mode_frame)

        self._update_mode_buttons()

    def _update_mode_buttons(self):
        if self._mode == "SCM":
            self._btn_scm.setStyleSheet(
                f"background-color: {ACCENT}; color: {BG2}; font-weight: bold; font-size: 8pt; border: none;"
            )
            self._btn_nav.setStyleSheet(
                f"background-color: {BG4}; color: {FG_DIM}; font-weight: bold; font-size: 8pt; border: none;"
            )
        else:
            self._btn_scm.setStyleSheet(
                f"background-color: {BG4}; color: {FG_DIM}; font-weight: bold; font-size: 8pt; border: none;"
            )
            self._btn_nav.setStyleSheet(
                f"background-color: {ACCENT}; color: {BG2}; font-weight: bold; font-size: 8pt; border: none;"
            )

    def _rebuild_columns(self):
        """Destroy and recreate the column grid from current categories."""
        # Clear existing
        while self._col_layout.count():
            item = self._col_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._pip_widgets.clear()

        for cat_key, label, icon, color in self._engine.CATEGORY_ORDER:
            slots = self._categories.get(cat_key, [])
            if not slots:
                continue

            col = QWidget(self._col_widget)
            col_layout = QVBoxLayout(col)
            col_layout.setContentsMargins(0, 0, 0, 0)
            col_layout.setSpacing(0)
            col_layout.setAlignment(Qt.AlignBottom)

            # Category label at top
            cat_lbl = QLabel(label, col)
            cat_lbl.setStyleSheet(
                f"color: {color}; font-family: Consolas; font-size: 6pt; "
                f"font-weight: bold; background: transparent;"
            )
            cat_lbl.setAlignment(Qt.AlignCenter)
            col_layout.addWidget(cat_lbl)

            col_layout.addStretch(1)

            # Pip bars (stacked bottom-up by adding in reverse)
            for si in range(len(slots) - 1, -1, -1):
                slot = slots[si]
                pip_w = _PipCanvas(slot, col)
                pip_w.pip_clicked.connect(
                    lambda level, s=slot: self._on_pip_set(s, level)
                )
                pip_w.right_clicked.connect(
                    lambda s=slot: self._on_right_click(s)
                )
                col_layout.addWidget(pip_w, 0, Qt.AlignCenter)
                self._pip_widgets.append((pip_w, slot))

            # Icon at bottom
            icon_lbl = QLabel(icon, col)
            icon_lbl.setStyleSheet(
                f"color: {color}; font-size: 9pt; background: transparent;"
            )
            icon_lbl.setAlignment(Qt.AlignCenter)
            icon_lbl.setCursor(Qt.PointingHandCursor)
            icon_lbl.mousePressEvent = lambda e, ck=cat_key: self._toggle_category(ck)
            col_layout.addWidget(icon_lbl)

            self._col_layout.addWidget(col)

    # -- internal: interaction -------------------------------------------------

    def _on_pip_set(self, slot: dict, new_level: int):
        slot["current_seg"] = new_level
        self._engine.sync_seg_config_from_slots()
        self._sync_ui()

    def _on_right_click(self, slot: dict):
        slot["enabled"] = not slot["enabled"]
        if not slot["enabled"]:
            slot["current_seg"] = 0
        else:
            slot["current_seg"] = slot["default_seg"]
        self._engine.sync_seg_config_from_slots()
        self._sync_ui()

    def _toggle_category(self, cat_key: str):
        slots = self._categories.get(cat_key, [])
        if not slots:
            return
        any_on = any(s["enabled"] for s in slots)
        for s in slots:
            s["enabled"] = not any_on
            if s["enabled"]:
                s["current_seg"] = s["default_seg"]
            else:
                s["current_seg"] = 0
        self._engine.sync_seg_config_from_slots()
        self._sync_ui()

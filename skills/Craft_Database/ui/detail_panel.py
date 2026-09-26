"""Blueprint detail popup — ModalBase-style floating window with pin/close."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QPainter, QColor, QPen, QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from shared.qt.theme import P
from domain.models import Blueprint, IngredientSlot, QualityEffect, Mission, format_qty
from ui.constants import (
    TOOL_COLOR,
    STAT_POSITIVE,
    STAT_NEGATIVE,
    STAT_NEUTRAL,
    LAWFUL_COLOR,
    UNLAWFUL_COLOR,
    TAG_COLORS,
    POPUP_BRACKET_LEN,
    PYRO_LOCATION_COLOR,
    CLOSE_BTN_BG,
    CLOSE_BTN_COLOR,
    CLOSE_BTN_HOVER_BG,
    PIN_BTN_ACTIVE_BG,
    PIN_BTN_INACTIVE_BG,
)

MAX_OPEN_POPUPS = 5


def _slider_qss(groove_h: int = 4, handle_w: int = 12) -> str:
    """Return QSS for a horizontal quality slider."""
    return (
        f"QSlider::groove:horizontal {{ background: {P.bg_input}; height: {groove_h}px;"
        f"border-radius: {groove_h // 2}px; }}"
        f"QSlider::handle:horizontal {{ background: {P.fg}; width: {handle_w}px;"
        f"margin: -4px 0; border-radius: {handle_w // 2}px; }}"
    )


# ── Shared button helpers (match Mission Database modal pattern) ─────────


def _pin_btn_qss(pinned: bool, accent: str = "") -> str:
    c = accent or TOOL_COLOR
    if pinned:
        return f"""
            QPushButton#modalPin {{
                background-color: {PIN_BTN_ACTIVE_BG};
                color: {P.bg_primary};
                border: 1px solid {c};
                border-radius: 3px;
                font-family: Consolas; font-size: 8pt; font-weight: bold;
                padding: 3px 12px; min-height: 0px;
            }}
            QPushButton#modalPin:hover {{
                background-color: rgba(51, 221, 136, 50);
                color: {c};
                border-color: {c};
            }}
        """
    return f"""
        QPushButton#modalPin {{
            background-color: {PIN_BTN_INACTIVE_BG};
            color: {c};
            border: 1px solid rgba(51, 221, 136, 60);
            border-radius: 3px;
            font-family: Consolas; font-size: 8pt; font-weight: bold;
            padding: 3px 12px; min-height: 0px;
        }}
        QPushButton#modalPin:hover {{
            background-color: rgba(51, 221, 136, 60);
            color: {P.fg_bright};
            border-color: {c};
        }}
    """


class _ModalCloseBtn(QPushButton):
    def __init__(self, parent=None):
        super().__init__("x", parent)
        self.setObjectName("modalClose")
        self.setFixedSize(32, 28)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(f"""
            QPushButton#modalClose {{
                background: {CLOSE_BTN_BG};
                color: {CLOSE_BTN_COLOR};
                border: none;
                border-radius: 3px;
                font-family: Consolas;
                font-size: 13pt;
                font-weight: bold;
                padding: 0px;
                margin: 2px;
                min-height: 0px;
            }}
            QPushButton#modalClose:hover {{
                background-color: {CLOSE_BTN_HOVER_BG};
                color: #ffffff;
            }}
        """)


# ── Blueprint detail popup ───────────────────────────────────────────────


class BlueprintPopup(QDialog):
    """Floating detail popup for a blueprint with pin/close, drag, quality slider.

    Up to MAX_OPEN_POPUPS can be open at once.  Oldest unpinned popup is
    auto-evicted when the limit is hit.
    """

    _open_dialogs: list[BlueprintPopup] = []
    _pinned_dialogs: list[BlueprintPopup] = []

    def __init__(
        self,
        bp: Blueprint,
        parent: Optional[QWidget] = None,
        accent: str = "",
    ):
        super().__init__(parent)
        self._bp = bp
        self._accent = accent or TOOL_COLOR
        self._drag_pos: QPoint | None = None
        self._pinned = False
        self._quality = 500

        # Per-slot quality tracking: index → quality value
        self._slot_qualities: list[int] = []
        self._slot_sliders: list[QSlider] = []
        self._slot_spinboxes: list[QSpinBox] = []
        # Per-slot effect tags: list of (tag_label, QualityEffect, slot_index)
        self._effect_tags: list[tuple[QLabel, QualityEffect, int]] = []
        # Per-slot stat labels: list of (label, stat_name, list of (QualityEffect, slot_index))
        self._stat_labels: list[tuple[QLabel, str, list[tuple[QualityEffect, int]]]] = []

        self.setWindowTitle(bp.name)
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(380, 600)
        self.setMinimumSize(340, 350)

        # Enforce max popups
        self._evict_oldest()
        BlueprintPopup._open_dialogs.append(self)

        # Center on parent, clamped to screen bounds
        if parent:
            pg = parent.geometry()
            idx = len(BlueprintPopup._open_dialogs) - 1
            cx = pg.x() + (pg.width() - self.width()) // 2 + idx * 26
            cy = pg.y() + (pg.height() - self.height()) // 2 + idx * 30

            # Clamp to screen
            screen = QGuiApplication.primaryScreen()
            if screen:
                sr = screen.availableGeometry()
                cx = max(sr.x(), min(cx, sr.right() - self.width()))
                cy = max(sr.y(), min(cy, sr.bottom() - self.height()))

            self.move(max(0, cx), max(0, cy))

        self._build()
        self.show()

    # ── Build ────────────────────────────────────────────────────────────

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)

        frame = QWidget(self)
        frame.setStyleSheet("background-color: rgba(11, 14, 20, 230);")
        frame_lay = QVBoxLayout(frame)
        frame_lay.setContentsMargins(0, 0, 0, 0)
        frame_lay.setSpacing(0)

        # ── Title bar
        title_bar = QWidget(frame)
        title_bar.setFixedHeight(34)
        title_bar.setStyleSheet(f"background-color: {P.bg_header};")
        tb_lay = QHBoxLayout(title_bar)
        tb_lay.setContentsMargins(12, 0, 4, 0)
        tb_lay.setSpacing(8)

        title_lbl = QLabel(self._bp.name.upper(), title_bar)
        title_lbl.setStyleSheet(
            f"font-family: Electrolize, Consolas, monospace;"
            f"font-size: 11pt; font-weight: bold;"
            f"color: {self._accent}; letter-spacing: 2px; background: transparent;"
        )
        tb_lay.addWidget(title_lbl)
        tb_lay.addStretch(1)

        # Pin button
        self._pin_btn = QPushButton("Pin")
        self._pin_btn.setObjectName("modalPin")
        self._pin_btn.setCursor(Qt.PointingHandCursor)
        self._pin_btn.setStyleSheet(_pin_btn_qss(False, self._accent))
        self._pin_btn.clicked.connect(self._toggle_pin)
        tb_lay.addWidget(self._pin_btn)

        # Close button
        close_btn = _ModalCloseBtn(title_bar)
        close_btn.clicked.connect(self.close)
        tb_lay.addWidget(close_btn)

        frame_lay.addWidget(title_bar)

        # ── Scrollable body
        scroll = QScrollArea(frame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        self._layout = QVBoxLayout(inner)
        self._layout.setContentsMargins(16, 12, 16, 16)
        self._layout.setSpacing(6)
        scroll.setWidget(inner)

        frame_lay.addWidget(scroll, 1)
        outer.addWidget(frame)

        self._populate()

    def _populate(self):
        bp = self._bp
        lay = self._layout

        # ── Craft time + tiers header
        info_row = QHBoxLayout()
        info_row.setSpacing(8)

        time_lbl = QLabel(f"CRAFT TIME  {bp.craft_time_display}")
        time_lbl.setStyleSheet(
            f"color: {P.fg}; font-size: 8pt; background: {P.bg_input};"
            f"border: 1px solid {P.border}; border-radius: 3px; padding: 2px 6px;"
        )
        info_row.addWidget(time_lbl)

        tier_lbl = QLabel(f"TIERS  {bp.tiers}")
        tier_lbl.setStyleSheet(
            f"color: {P.fg_dim}; font-size: 8pt; background: {P.bg_input};"
            f"border: 1px solid {P.border}; border-radius: 10px; padding: 2px 8px;"
        )
        info_row.addWidget(tier_lbl)

        if bp.dismantle_seconds:
            dis_lbl = QLabel(f"DISMANTLE  {bp.dismantle_seconds}s")
            dis_lbl.setToolTip(f"Dismantling takes {bp.dismantle_seconds}s and returns "
                               f"{bp.dismantle_efficiency * 100:.0f}% of the materials")
            dis_lbl.setStyleSheet(
                f"color: {P.fg_dim}; font-size: 8pt; background: {P.bg_input};"
                f"border: 1px solid {P.border}; border-radius: 3px; padding: 2px 6px;"
            )
            info_row.addWidget(dis_lbl)
        info_row.addStretch()
        lay.addLayout(info_row)

        # ── Global quality slider
        self._add_section_header("GLOBAL QUALITY")
        q_row = QHBoxLayout()
        q_row.setSpacing(8)

        self._quality_slider = QSlider(Qt.Horizontal)
        self._quality_slider.setRange(0, 1000)
        self._quality_slider.setValue(self._quality)
        self._quality_slider.setStyleSheet(_slider_qss(groove_h=4, handle_w=12))
        self._quality_slider.valueChanged.connect(self._on_quality_changed)
        q_row.addWidget(self._quality_slider, 1)

        self._quality_spin = QSpinBox()
        self._quality_spin.setRange(0, 1000)
        self._quality_spin.setValue(self._quality)
        self._quality_spin.setFixedWidth(60)
        self._quality_spin.setStyleSheet(
            f"QSpinBox {{ color: {TOOL_COLOR}; background: {P.bg_input};"
            f"border: 1px solid {P.border}; border-radius: 3px; padding: 2px; }}"
        )
        self._quality_spin.valueChanged.connect(self._on_quality_spin_changed)
        q_row.addWidget(self._quality_spin)
        lay.addLayout(q_row)

        # ── Parts
        self._add_section_header("\u2699  PARTS")
        for slot in bp.ingredients:
            self._add_slot_card(slot)

        # ── Stats summary
        self._build_stats_table()

        # ── Missions (legacy data) or reward pools (datamine)
        if not bp.missions and (bp.sources or bp.default_owned):
            self._add_section_header("OBTAINED FROM")
            names = (["Known by default"] if bp.default_owned else []) + list(bp.sources)
            for name in names:
                row = QLabel(name)
                row.setWordWrap(True)
                row.setStyleSheet(
                    f"color: {P.fg}; background: {P.bg_card}; font-size: 8pt;"
                    f"border-left: 3px solid {TOOL_COLOR}; padding: 3px 8px;"
                )
                lay.addWidget(row)
            if bp.sources:
                note = QLabel("Reward pools as named in the game files. "
                              "The Mission Database lists the missions.")
                note.setWordWrap(True)
                note.setStyleSheet(f"color: {P.fg_dim}; font-size: 7pt;")
                lay.addWidget(note)
        elif not bp.missions:
            self._add_section_header("OBTAINED FROM")
            none_lbl = QLabel("Not given by any mission in this game build.")
            none_lbl.setStyleSheet(f"color: {P.fg_dim}; font-size: 8pt;")
            lay.addWidget(none_lbl)

        if bp.missions:
            self._add_section_header(f"DROPS ({len(bp.missions)} MISSIONS)")
            lawful = [m for m in bp.missions if m.lawful]
            unlawful = [m for m in bp.missions if not m.lawful]
            if lawful:
                self._add_lawfulness_header("LAWFUL", True)
                self._add_mission_group(lawful)
            if unlawful:
                self._add_lawfulness_header("UNLAWFUL", False)
                self._add_mission_group(unlawful)

        lay.addStretch()

    # ── Section helpers ──────────────────────────────────────────────────

    def _add_section_header(self, text: str):
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {P.fg_dim}; font-size: 8pt; font-weight: bold;"
            f"letter-spacing: 1px; margin-top: 8px;"
        )
        self._layout.addWidget(lbl)

    def _add_slot_card(self, slot: IngredientSlot):
        slot_idx = len(self._slot_qualities)
        self._slot_qualities.append(self._quality)

        card = QFrame()
        card.setStyleSheet(
            f"QFrame {{ background: {P.bg_card}; border: 1px solid {P.border_card};"
            f"border-radius: 4px; padding: 8px; }}"
        )
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(8, 6, 8, 6)
        card_lay.setSpacing(4)

        # Slot name
        slot_header = QHBoxLayout()
        color = TAG_COLORS.get(slot.name, TOOL_COLOR)
        dot = QLabel("\u25cf")
        dot.setStyleSheet(f"color: {color}; font-size: 8pt; border: none;")
        slot_header.addWidget(dot)
        slot_lbl = QLabel(f"{slot.slot}  ({slot.choose})" if slot.choose else slot.slot)
        slot_lbl.setStyleSheet(f"color: {P.fg}; font-size: 9pt; font-weight: bold; border: none;")
        slot_header.addWidget(slot_lbl, 1)
        card_lay.addLayout(slot_header)

        # Resource name + quantity
        res_lbl = QLabel(f"{slot.name}   {format_qty(slot.quantity_scu, slot.unit)}")
        res_lbl.setStyleSheet(f"color: {color}; font-size: 9pt; font-weight: bold; border: none;")
        card_lay.addWidget(res_lbl)

        # Quality slider (independently controllable)
        q_row = QHBoxLayout()
        q_row.setSpacing(4)
        q_label = QLabel("QUALITY")
        q_label.setStyleSheet(f"color: {P.fg_dim}; font-size: 7pt; border: none;")
        q_row.addWidget(q_label)

        slider = QSlider(Qt.Horizontal)
        slider.setRange(0, 1000)
        slider.setValue(self._quality)
        slider.setStyleSheet(_slider_qss(groove_h=3, handle_w=10))
        idx = slot_idx  # capture for lambda
        slider.valueChanged.connect(lambda val, i=idx: self._on_slot_quality_changed(i, val))
        self._slot_sliders.append(slider)
        q_row.addWidget(slider, 1)

        spin = QSpinBox()
        spin.setRange(0, 1000)
        spin.setValue(self._quality)
        spin.setFixedWidth(52)
        spin.setStyleSheet(
            f"QSpinBox {{ color: {TOOL_COLOR}; background: {P.bg_input};"
            f"border: 1px solid {P.border}; border-radius: 3px; padding: 1px; font-size: 8pt; }}"
        )
        spin.valueChanged.connect(lambda val, i=idx: self._on_slot_spin_changed(i, val))
        self._slot_spinboxes.append(spin)
        q_row.addWidget(spin)
        card_lay.addLayout(q_row)

        # Quality effect tags
        if slot.quality_effects:
            effects_row = QHBoxLayout()
            effects_row.setSpacing(4)
            for qe in slot.quality_effects:
                tag = QLabel()
                self._effect_tags.append((tag, qe, slot_idx))
                self._update_effect_tag(tag, qe, self._quality)
                effects_row.addWidget(tag)
            effects_row.addStretch()
            card_lay.addLayout(effects_row)

        self._layout.addWidget(card)

    def _build_stats_table(self):
        """Build stat summary using per-slot quality values."""
        # Collect all (effect, slot_index) pairs grouped by stat name
        stat_effects: dict[str, list[tuple[QualityEffect, int]]] = {}
        for slot_idx, slot in enumerate(self._bp.ingredients):
            for qe in slot.quality_effects:
                stat_effects.setdefault(qe.stat, []).append((qe, slot_idx))

        if not stat_effects:
            return

        self._add_section_header("STAT SUMMARY")

        # Header row
        hdr = QHBoxLayout()
        for text, w in [("STAT", 140), ("BASE", 50), ("CRAFTED", 60)]:
            lbl = QLabel(text)
            lbl.setFixedWidth(w)
            lbl.setStyleSheet(
                f"color: {P.fg_dim}; font-size: 7pt; font-weight: bold;"
                f"letter-spacing: 1px; border: none;"
            )
            hdr.addWidget(lbl)
        hdr.addStretch()
        self._layout.addLayout(hdr)

        # Data rows
        for stat_name, qe_list in stat_effects.items():
            row = QHBoxLayout()
            row.setSpacing(4)

            stat_lbl = QLabel(stat_name)
            stat_lbl.setFixedWidth(140)
            stat_lbl.setStyleSheet(f"color: {P.fg}; font-size: 8pt; border: none;")
            row.addWidget(stat_lbl)

            base_lbl = QLabel("\u2014")
            base_lbl.setFixedWidth(50)
            base_lbl.setStyleSheet(f"color: {P.fg_dim}; font-size: 8pt; border: none;")
            row.addWidget(base_lbl)

            crafted_lbl = QLabel()
            crafted_lbl.setFixedWidth(60)
            self._stat_labels.append((crafted_lbl, stat_name, qe_list))
            self._update_stat_label(crafted_lbl, qe_list)
            row.addWidget(crafted_lbl)

            row.addStretch()
            self._layout.addLayout(row)

    # ── Missions ─────────────────────────────────────────────────────────

    def _add_lawfulness_header(self, text: str, lawful: bool):
        color = LAWFUL_COLOR if lawful else UNLAWFUL_COLOR
        frame = QFrame()
        frame.setStyleSheet(
            f"QFrame {{ border-left: 3px solid {color};"
            f"background: {P.bg_card}; padding: 4px 8px; margin-top: 4px; }}"
        )
        fl = QHBoxLayout(frame)
        fl.setContentsMargins(8, 2, 8, 2)
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {color}; font-size: 9pt; font-weight: bold; border: none;")
        fl.addWidget(lbl)
        self._layout.addWidget(frame)

    def _add_mission_group(self, missions: list[Mission]):
        groups: dict[str, list[Mission]] = {}
        for m in missions:
            groups.setdefault(m.mission_type, []).append(m)

        for mtype, group in groups.items():
            type_row = QHBoxLayout()
            type_lbl = QLabel(mtype.upper())
            type_lbl.setStyleSheet(f"color: {P.red}; font-size: 8pt; font-weight: bold;")
            type_row.addWidget(type_lbl)
            count_lbl = QLabel(str(len(group)))
            count_lbl.setStyleSheet(
                f"color: {P.fg_dim}; background: {P.bg_input};"
                f"border: 1px solid {P.border}; border-radius: 8px;"
                f"padding: 0px 5px; font-size: 7pt;"
            )
            type_row.addWidget(count_lbl)
            type_row.addStretch()
            self._layout.addLayout(type_row)

            for m in group:
                self._add_mission_row(m)

    def _add_mission_row(self, m: Mission):
        card = QFrame()
        card.setStyleSheet(
            f"QFrame {{ background: {P.bg_card}; border-bottom: 1px solid {P.border};"
            f"padding: 4px 0; }}"
        )
        cl = QVBoxLayout(card)
        cl.setContentsMargins(8, 4, 8, 4)
        cl.setSpacing(2)

        name_lbl = QLabel(m.name)
        name_lbl.setWordWrap(True)
        name_lbl.setStyleSheet(f"color: {P.fg_bright}; font-size: 8pt; font-weight: bold; border: none;")
        cl.addWidget(name_lbl)

        info = QHBoxLayout()
        info.setSpacing(6)
        contractor_lbl = QLabel(m.contractor)
        contractor_lbl.setStyleSheet(f"color: {P.fg_dim}; font-size: 7pt; border: none;")
        info.addWidget(contractor_lbl)

        if m.locations:
            loc_color = PYRO_LOCATION_COLOR if "Pyro" in m.locations else TOOL_COLOR
            loc_lbl = QLabel(m.locations)
            loc_lbl.setStyleSheet(
                f"color: {P.fg}; background: {P.bg_input};"
                f"border: 1px solid {loc_color}; border-radius: 3px;"
                f"padding: 0px 4px; font-size: 7pt;"
            )
            info.addWidget(loc_lbl)

        drop_lbl = QLabel(m.drop_pct)
        drop_lbl.setStyleSheet(f"color: {P.fg}; font-size: 7pt; border: none;")
        info.addWidget(drop_lbl)
        info.addStretch()
        cl.addLayout(info)

        self._layout.addWidget(card)

    # ── Quality: in-place label updates (no rebuild) ─────────────────────

    def _update_effect_tag(self, tag: QLabel, qe: QualityEffect, quality: int):
        pct = qe.pct_at(quality)
        color = STAT_POSITIVE if pct > 0 else (STAT_NEGATIVE if pct < 0 else STAT_NEUTRAL)
        tag.setText(f"{qe.stat} {qe.label_at(quality)}")
        tag.setStyleSheet(
            f"color: {color}; background: {P.bg_input};"
            f"border: 1px solid {P.border}; border-radius: 3px;"
            f"padding: 1px 5px; font-size: 7pt;"
        )

    def _update_stat_label(self, lbl: QLabel, qe_list: list[tuple[QualityEffect, int]]):
        """Average the % modifier across all slots contributing to this stat;
        whole-number (additive) effects such as Power Pips are summed."""
        total = 0.0
        additive = bool(qe_list) and all(qe.additive for qe, _ in qe_list)
        for qe, slot_idx in qe_list:
            total += qe.pct_at(self._slot_qualities[slot_idx])
        pct = total if additive else (total / len(qe_list) if qe_list else 0.0)
        sign = "+" if pct >= 0 else ""
        color = STAT_POSITIVE if pct > 0 else (STAT_NEGATIVE if pct < 0 else STAT_NEUTRAL)
        lbl.setText(f"{sign}{pct:.0f}" if additive else f"{sign}{pct:.0f}%")
        lbl.setStyleSheet(f"color: {color}; font-size: 8pt; font-weight: bold; border: none;")

    def _on_slot_quality_changed(self, slot_idx: int, val: int):
        self._slot_qualities[slot_idx] = val
        # Sync spinbox
        self._slot_spinboxes[slot_idx].blockSignals(True)
        self._slot_spinboxes[slot_idx].setValue(val)
        self._slot_spinboxes[slot_idx].blockSignals(False)
        self._refresh_slot_effects(slot_idx)

    def _on_slot_spin_changed(self, slot_idx: int, val: int):
        self._slot_qualities[slot_idx] = val
        # Sync slider
        self._slot_sliders[slot_idx].blockSignals(True)
        self._slot_sliders[slot_idx].setValue(val)
        self._slot_sliders[slot_idx].blockSignals(False)
        self._refresh_slot_effects(slot_idx)

    def _refresh_slot_effects(self, slot_idx: int):
        """Update effect tags for one slot + all stat summary rows."""
        q = self._slot_qualities[slot_idx]
        for tag, qe, idx in self._effect_tags:
            if idx == slot_idx:
                self._update_effect_tag(tag, qe, q)
        for lbl, _stat_name, qe_list in self._stat_labels:
            if any(si == slot_idx for _, si in qe_list):
                self._update_stat_label(lbl, qe_list)

    def _on_quality_changed(self, val: int):
        """Global slider sets all slot sliders."""
        self._quality = val
        self._quality_spin.blockSignals(True)
        self._quality_spin.setValue(val)
        self._quality_spin.blockSignals(False)
        for i in range(len(self._slot_qualities)):
            self._slot_qualities[i] = val
            self._slot_sliders[i].blockSignals(True)
            self._slot_sliders[i].setValue(val)
            self._slot_sliders[i].blockSignals(False)
            self._slot_spinboxes[i].blockSignals(True)
            self._slot_spinboxes[i].setValue(val)
            self._slot_spinboxes[i].blockSignals(False)
        for tag, qe, idx in self._effect_tags:
            self._update_effect_tag(tag, qe, val)
        for lbl, _stat_name, qe_list in self._stat_labels:
            self._update_stat_label(lbl, qe_list)

    def _on_quality_spin_changed(self, val: int):
        self._quality = val
        self._quality_slider.blockSignals(True)
        self._quality_slider.setValue(val)
        self._quality_slider.blockSignals(False)
        self._on_quality_changed(val)

    # ── Pin / Unpin ──────────────────────────────────────────────────────

    def _toggle_pin(self):
        if self._pinned:
            self._pinned = False
            self._pin_btn.setText("Pin")
            self._pin_btn.setStyleSheet(_pin_btn_qss(False, self._accent))
            if self in BlueprintPopup._pinned_dialogs:
                BlueprintPopup._pinned_dialogs.remove(self)
        else:
            self._pinned = True
            self._pin_btn.setText("Unpin")
            self._pin_btn.setStyleSheet(_pin_btn_qss(True, self._accent))
            BlueprintPopup._pinned_dialogs.append(self)

    # ── Max popup enforcement ────────────────────────────────────────────

    @classmethod
    def _evict_oldest(cls):
        cls._open_dialogs = [d for d in cls._open_dialogs if d.isVisible()]
        cls._pinned_dialogs = [d for d in cls._pinned_dialogs if d.isVisible()]

        while len(cls._open_dialogs) >= MAX_OPEN_POPUPS:
            victim = None
            for d in cls._open_dialogs:
                if not d._pinned:
                    victim = d
                    break
            if victim is None:
                victim = cls._open_dialogs[0]
            victim.close()

    # ── Lifecycle ────────────────────────────────────────────────────────

    def closeEvent(self, event):
        if self in BlueprintPopup._open_dialogs:
            BlueprintPopup._open_dialogs.remove(self)
        if self in BlueprintPopup._pinned_dialogs:
            BlueprintPopup._pinned_dialogs.remove(self)
        super().closeEvent(event)

    # ── Paint border + corner brackets ───────────────────────────────────

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        w, h = self.width(), self.height()

        edge = QColor(self._accent)
        edge.setAlpha(100)
        painter.setPen(QPen(edge, 1))
        painter.drawRect(0, 0, w - 1, h - 1)

        bl = POPUP_BRACKET_LEN
        bracket = QColor(self._accent)
        bracket.setAlpha(200)
        painter.setPen(QPen(bracket, 2))
        painter.drawLine(0, 0, bl, 0)
        painter.drawLine(0, 0, 0, bl)
        painter.drawLine(w - 1, 0, w - 1 - bl, 0)
        painter.drawLine(w - 1, 0, w - 1, bl)
        painter.drawLine(0, h - 1, bl, h - 1)
        painter.drawLine(0, h - 1, 0, h - 1 - bl)
        painter.drawLine(w - 1, h - 1, w - 1 - bl, h - 1)
        painter.drawLine(w - 1, h - 1, w - 1, h - 1 - bl)
        painter.end()

    # ── Drag support ─────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.pos()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)

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
        # a ship with an installed power component that has no data
        # (scunpacked provider "power_gaps") gets "—" for EM / IR, not a
        # signature that silently leaves that component out
        self._sig_known = not (isinstance(ship_data, dict) and ship_data.get("power_gaps"))
        self._engine.load_ship(ship_data)
        self._rebuild_columns()
        self._sync_ui()

    def set_mode(self, mode):
        """Switch SCM/NAV and REBUILD the columns, because the engine replaced the slots.

        ⛔ 2026-09-26 (issue #7-1, "NAV mode will not let shields be turned off"): this
          method used to be `engine.set_mode` + `_update_mode_buttons` + `_sync_ui`, with
          no `_rebuild_columns`. But `PowerAllocatorEngine.set_mode` re-enters `load_ship`
          (power_engine.py:703), and `load_ship` → `_build_legacy_slots` CLEARS
          `_slots`/`_categories` (power_engine.py:604-605) and builds BRAND-NEW slot dicts.
          `_rebuild_columns` had captured the PREVIOUS mode's dicts — into `_pip_widgets`
          and into every per-pip lambda default (`s=slot`) — so after one SCM→NAV click the
          widget was painting and mutating objects the engine no longer owned:
            - `_sync_ui`'s `pip_w.set_slot(slot)` redrew the DEAD SCM dict, so the shield
              column kept showing 3 green pips while the live NAV slot was
              `enabled=False, current_seg=0`. Measured on the Gladius fixture: the SCM
              shield slot (enabled=True, cur=3) survives as an orphan; the NAV slot the
              engine actually computes from is a different object (enabled=False, cur=0).
            - `_on_pip_set` / `_on_right_click` mutated that orphan, and
              `sync_seg_config_from_slots()` iterates `engine._slots` — which no longer
              contains it — so "lowering the energy" moved pips on screen and changed
              NOTHING in the allocator, not the draw, not the percentage.
            - `_toggle_category` was the ONE path that still reached live state, because it
              re-reads `self._categories` from the engine. So clicking the shield icon in
              NAV flipped real shields ON (NAV leaves them off, so the first click is an
              ON) while the stale pips never moved — which is exactly "clicking the shield
              icon does not turn them off".
        ★ THE ENGINE WAS NOT CHANGED and NAV's fill order was not touched. NAV already
          leaves `shield` unpowered on purpose (`_power_config["shield"]["power"] = is_scm`,
          and NAV's phase-2 fill order omits `shield`), which is erkul-exact; giving NAV a
          shield allocation would move every ship's default AT LOAD. The defect was that
          the widget drew the wrong OBJECT, so the repair belongs here.
        ★★ Rebuilt unconditionally rather than "only when the slot list changed": every
          `load_ship` mints new dicts, so an identity diff always says "changed" and a
          contents diff that guesses wrong silently reintroduces a stale bar. `load_ship`
          already rebuilds on every call; a no-op mode click just rebuilds identical
          columns, which is cheap and cannot be stale.
        """
        self._engine.set_mode(mode)
        self._update_mode_buttons()
        self._rebuild_columns()
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
    def ammo_load_mult(self):
        return self._engine.ammo_load_mult

    @property
    def regen_per_sec_mult(self):
        return self._engine.regen_per_sec_mult

    @property
    def power_ratio_mult(self):
        return self._engine.power_ratio_mult

    @property
    def shield_regen_powered(self):
        return self._engine.shield_regen_powered

    @property
    def shield_res_powered(self):
        return self._engine.shield_res_powered

    @property
    def shield_powered_count(self):
        return self._engine.shield_powered_count

    @property
    def over_capacity_text(self) -> str:
        """The OVER CAPACITY marker's text: "" when the allocation fits.

        Exposed because `QLabel.isVisible()` is False for any widget whose window was
        never shown, so visibility cannot be asserted in a headless test. The text IS
        the state — `_set_over_capacity` clears it whenever the marker does not apply.
        """
        return self._lbl_over.text()

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

        known = getattr(self, "_sig_known", True)
        self._lbl_em.setText(fmt_sig(result["em_sig"]) if known else "—")
        self._lbl_ir.setText(fmt_sig(result["ir_sig"]) if known else "—")
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

        self._set_over_capacity(result["total_draw"], result["total_capacity"],
                                consumption_pct)

        for pip_w, slot in self._pip_widgets:
            pip_w.set_slot(slot)

        if self._on_change:
            try:
                self._on_change()
            except Exception:  # broad catch intentional: top-level UI handler
                pass
        self.power_changed.emit()

    def _set_over_capacity(self, total_draw, total_capacity, consumption_pct):
        """Say OVER CAPACITY in words when the allocation exceeds the power plant.

        ⛔ 2026-09-26 (issue #7-3, "power overdraw is allowed silently"): showing the
          overdraw is CORRECT and is not being changed — this tool reports what you
          allocated, it does not clamp to 100% the way erkul does, so 118% is a true
          reading. What was wrong is that 118% was announced only by a red bar and a bare
          percentage, and NEITHER of those reads as "you have overcommitted the plant":
          the bar is red at 101% and red at 300% and `_ConsumptionBar.paintEvent` clamps
          its fill at `min(bar_w, ...)`, so past 100% the bar stops moving entirely, which
          looks like a full bar, i.e. like a rendering quirk. The number needed a word next
          to it. Nothing is clamped, nothing is refused, and the percentage is untouched.
        ★ The marker carries the EXCESS in power segments, not a restatement of the
          percentage that is already on screen one label to the left. `+3 pwr` is the
          actionable figure: it is how much you must free up.
        ⚠ AND THE CAPACITY-0 CASE IS NOT A "0%". `recalculate()` computes
          `consumption_pct = (draw / capacity * 100) if capacity > 0 else 0`
          (power_engine.py:923), so a ship with no resolvable power plant reads 0% no
          matter how many pips are lit — and pips CAN be lit there, because `_on_pip_set`
          writes `current_seg` with no capacity check. That 0 is an ABSENCE of a
          denominator, not a measurement of a healthy draw, so it must not be allowed to
          render as the quiet state that a real 0% does. It gets its own text and the
          excess is deliberately left unstated: with no known capacity there is no
          "over by N" to state.
        """
        if total_capacity and total_capacity > 0:
            over = total_draw - total_capacity
            text = f"{_('OVER CAPACITY')}  +{over:.0f}" if over > 0 else ""
        elif total_draw > 0:
            text = f"{_('OVER CAPACITY')}  {_('no power plant data')}"
        else:
            text = ""

        self._lbl_over.setText(text)
        self._lbl_over.setVisible(bool(text))
        pct_color = RED if text else FG
        self._lbl_pct.setStyleSheet(
            f"color: {pct_color}; font-family: Consolas; font-size: 9pt; "
            f"font-weight: bold; background: transparent;"
        )

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

        # OVER CAPACITY marker — empty and hidden at or below 100%; see _set_over_capacity.
        self._lbl_over = QLabel("", hdr)
        self._lbl_over.setStyleSheet(
            f"color: {BG2}; background-color: {RED}; font-family: Consolas; "
            f"font-size: 8pt; font-weight: bold; padding: 0px 4px;"
        )
        self._lbl_over.setVisible(False)
        hdr_layout.addWidget(self._lbl_over)

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
        """Destroy and recreate the column grid from current categories.

        ⛔ 2026-09-26 (issue #7-2, "the cooler control is doubled"): this loop used to
          build ONE column per CATEGORY and drop every slot of that category into it as a
          separate `_PipCanvas`. A Gladius has two Bracers, so `_build_legacy_slots` gives
          `categories["cooler"]` TWO slots (power_engine.py:645-668 — coolers are the only
          category built one-slot-per-component) and the widget stacked two independent
          3-segment bars in one column under ONE shared snowflake. That is precisely the
          report: "one icon combining all the energy levels" and "behaving as if they were
          two separate bars, with one power bar above the other" — they ARE two bars, and
          the pips really do belong to different coolers. Worse, the single icon ran
          `_toggle_category("cooler")`, which switched BOTH coolers at once, so there was
          no way to unpower one. erkul draws one icon per cooler.
        ★ THE DATA WAS NEVER WRONG. Two slots for two coolers is correct, and the engine
          keeps per-cooler `_power_config["coolers"][idx]` entries and per-cooler pip lists.
          Nothing in the allocator changed; this is a layout defect and the fix is layout.
        ★★ THE SPLIT IS ON SLOT COUNT, NOT ON THE NAME "cooler". `len(slots) > 1` →
          one column, one icon, one toggle PER SLOT. Rejected `if cat_key == "cooler"`:
          the shape is "a category whose slots are separate physical components", and the
          next multi-slot category to appear would silently inherit the same defect.
        ⚠ AND IT CHANGES NOTHING ELSE TODAY, which is why the generic rule is safe here:
          `_build_legacy_slots` emits exactly ONE slot for each of weapon/engine/shield/
          radar/lifeSupport/qdrive (it aggregates all components of a type into one slot,
          power_engine.py:610-640), so `cooler` is the only category that currently takes
          the `> 1` branch. Single-slot categories keep the old column and the old
          `_toggle_category` call verbatim — no layout and no behaviour moves for any
          category nobody complained about.
        """
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

            if len(slots) > 1:
                # One column per component, each with its own icon and its own toggle.
                for slot in slots:
                    self._add_column(label, icon, color, [slot], slot=slot)
            else:
                self._add_column(label, icon, color, slots, cat_key=cat_key)

    def _add_column(self, label, icon, color, slots, cat_key=None, slot=None):
        """Build one pip column: a label, the pip bars for `slots`, and a clickable icon.

        Exactly one of `cat_key` / `slot` is given, and it decides what the icon toggles:
        `cat_key` → `_toggle_category` (every slot of the category, the pre-existing
        behaviour for single-slot categories), `slot` → `_toggle_slot` (that component
        alone, for the per-component columns of a multi-slot category).
        """
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
            s_ = slots[si]
            pip_w = _PipCanvas(s_, col)
            pip_w.pip_clicked.connect(
                lambda level, s=s_: self._on_pip_set(s, level)
            )
            pip_w.right_clicked.connect(
                lambda s=s_: self._on_right_click(s)
            )
            col_layout.addWidget(pip_w, 0, Qt.AlignCenter)
            self._pip_widgets.append((pip_w, s_))

        # Icon at bottom
        icon_lbl = QLabel(icon, col)
        icon_lbl.setStyleSheet(
            f"color: {color}; font-size: 9pt; background: transparent;"
        )
        icon_lbl.setAlignment(Qt.AlignCenter)
        icon_lbl.setCursor(Qt.PointingHandCursor)
        if slot is not None:
            # Two identical snowflakes side by side are indistinguishable without this;
            # the slot name is the installed component ("Bracer"), not a generic label.
            icon_lbl.setToolTip(str(slot.get("name") or label))
            icon_lbl.mousePressEvent = lambda e, s=slot: self._toggle_slot(s)
        else:
            icon_lbl.mousePressEvent = lambda e, ck=cat_key: self._toggle_category(ck)
        col_layout.addWidget(icon_lbl)

        self._col_layout.addWidget(col)

    # -- internal: interaction -------------------------------------------------

    def _on_pip_set(self, slot: dict, new_level: int):
        slot["current_seg"] = new_level
        self._engine.sync_seg_config_from_slots()
        self._sync_ui()

    def _toggle_slot(self, slot: dict):
        """Power one slot on/off, restoring its `default_seg` on the way back on.

        Added with the issue #7-2 column split: the per-component icon of a multi-slot
        category must move ONE component, where `_toggle_category` moves all of them.
        It is the same operation right-click has always performed on a single bar, so
        `_on_right_click` now delegates here rather than carrying a second copy.
        """
        slot["enabled"] = not slot["enabled"]
        if not slot["enabled"]:
            slot["current_seg"] = 0
        else:
            slot["current_seg"] = slot["default_seg"]
        self._engine.sync_seg_config_from_slots()
        self._sync_ui()

    def _on_right_click(self, slot: dict):
        self._toggle_slot(slot)

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

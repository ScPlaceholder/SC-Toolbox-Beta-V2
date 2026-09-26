"""
Crate tabs for the Cargo Loader: one panel per placed personal crate.

A panel lives as a tab next to the hold view, or popped out in its own small
window (closing that window docks it back). It holds no state of its own:
the crate's contents live in CargoApp._crates and every change goes through
the app (crate_add / crate_set_qty), which applies the strict volume rule in
cargo_engine.crate_items.

Labels are single short lines, never word-wrapped (wrapped labels in this
window get squeezed to nothing).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QTreeWidget, QTreeWidgetItem, QHeaderView, QSpinBox,
)

from shared.i18n import s_ as _
from shared.qt.theme import P

from cargo_engine import crate_items
from cargo_engine.crate_items import fmt_u

BG, BG2, BG3 = P.bg_primary, P.bg_secondary, P.bg_card
BORDER, FG, FG_DIM, ACCENT = P.border, P.fg, P.fg_dim, P.accent
GREEN, YELLOW, RED = P.green, P.yellow, P.red

_LBL = "font-family: Consolas; font-size: 8pt; background: transparent;"
_BTN = f"""
    QPushButton {{
        background-color: {BG3}; color: {FG_DIM};
        font-family: Consolas; font-size: 8pt;
        border: 1px solid {BORDER}; padding: 3px 8px;
    }}
    QPushButton:hover {{ background-color: {BORDER}; color: {FG}; }}
    QPushButton:disabled {{ color: {BORDER}; }}
"""
_TREE = (f"QTreeWidget {{ background-color: {BG}; color: {FG}; font-family: Consolas;"
         f" font-size: 8pt; border: 1px solid {BORDER}; }}"
         f"QTreeWidget::item:selected {{ background-color: {ACCENT}; color: {BG}; }}"
         f"QHeaderView::section {{ background-color: {BG3}; color: {FG_DIM};"
         f" font-family: Consolas; font-size: 8pt; border: none; padding: 2px 4px; }}")

ROLE_ROW = Qt.UserRole          # search result: the index row (list)
ROLE_KEY = Qt.UserRole + 1      # contents: the item key


def _label(text: str, color: str, parent) -> QLabel:
    lbl = QLabel(text, parent)
    lbl.setWordWrap(False)
    lbl.setStyleSheet(f"color: {color}; {_LBL}")
    return lbl


def fill_color(pct: float) -> str:
    return GREEN if pct < 0.85 else (YELLOW if pct < 1.0 else RED)


class CratePanel(QWidget):
    """Search + add on the left, contents + fill on the right."""

    def __init__(self, app, no: int, bar_cls, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self.no = no
        self.setStyleSheet(f"background-color: {BG2};")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)

        # ── header: title, fill, pop out ─────────────────────────────────
        head = QHBoxLayout()
        head.setSpacing(8)
        self.title_lbl = _label("", ACCENT, self)
        self.title_lbl.setStyleSheet(f"color: {ACCENT}; {_LBL} font-size: 10pt; font-weight: bold;")
        head.addWidget(self.title_lbl)
        head.addStretch(1)
        self.fill_lbl = _label("", GREEN, self)
        head.addWidget(self.fill_lbl)
        self.pop_btn = QPushButton("", self)
        self.pop_btn.setCursor(Qt.PointingHandCursor)
        self.pop_btn.setStyleSheet(_BTN)
        self.pop_btn.clicked.connect(lambda: self.app.crate_toggle_pop(self.no))
        head.addWidget(self.pop_btn)
        outer.addLayout(head)

        self.bar = bar_cls(self)
        self.bar.setFixedHeight(10)
        outer.addWidget(self.bar)

        cols = QHBoxLayout()
        cols.setSpacing(8)
        outer.addLayout(cols, 1)

        # ── left: find and add ───────────────────────────────────────────
        left = QVBoxLayout()
        left.setSpacing(4)
        left.addWidget(_label(_("ADD ITEMS"), FG_DIM, self))
        self.search = QLineEdit(self)
        self.search.setPlaceholderText(_("Search any item…"))
        self.search.setClearButtonEnabled(True)
        self.search.setStyleSheet(
            f"QLineEdit {{ background-color: {BG3}; color: {FG}; font-family: Consolas;"
            f" font-size: 8pt; border: 1px solid {BORDER}; padding: 3px 4px; }}")
        self.search.textChanged.connect(self.refresh_results)
        self.search.returnPressed.connect(self.add_selected)
        left.addWidget(self.search)

        self.results = QTreeWidget(self)
        self.results.setColumnCount(3)
        self.results.setHeaderLabels([_("Item"), _("Type"), _("Volume")])
        self.results.setRootIsDecorated(False)
        self.results.setUniformRowHeights(True)
        hdr = self.results.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.results.setStyleSheet(_TREE)
        self.results.currentItemChanged.connect(lambda cur, _prev: self._show_uex(cur, ROLE_ROW))
        self.results.itemDoubleClicked.connect(lambda _n, _c: self.add_selected())
        left.addWidget(self.results, 1)

        add_row = QHBoxLayout()
        add_row.setSpacing(4)
        add_row.addWidget(_label(_("Qty"), FG_DIM, self))
        self.qty = QSpinBox(self)
        self.qty.setRange(1, 9999)
        self.qty.setFixedWidth(64)
        self.qty.setStyleSheet(
            f"QSpinBox {{ background-color: {BG3}; color: {FG}; font-family: Consolas;"
            f" font-size: 8pt; border: 1px solid {BORDER}; }}")
        add_row.addWidget(self.qty)
        self.add_btn = QPushButton(_("Add to crate"), self)
        self.add_btn.setCursor(Qt.PointingHandCursor)
        self.add_btn.setStyleSheet(_BTN)
        self.add_btn.clicked.connect(self.add_selected)
        add_row.addWidget(self.add_btn)
        add_row.addStretch(1)
        left.addLayout(add_row)

        data_row = QHBoxLayout()
        data_row.setSpacing(4)
        self.data_lbl = _label("", YELLOW, self)
        data_row.addWidget(self.data_lbl, 1)
        self.dl_btn = QPushButton(_("Download"), self)
        self.dl_btn.setToolTip(_("Download the game item lists (pinned build, about 110 MB)"))
        self.dl_btn.setStyleSheet(_BTN)
        self.dl_btn.clicked.connect(self.app.crate_fetch_data)
        self.dl_btn.hide()
        data_row.addWidget(self.dl_btn)
        left.addLayout(data_row)
        cols.addLayout(left, 1)

        # ── right: what is in the crate ──────────────────────────────────
        right = QVBoxLayout()
        right.setSpacing(4)
        right.addWidget(_label(_("IN THIS CRATE"), FG_DIM, self))
        self.contents = QTreeWidget(self)
        self.contents.setColumnCount(4)
        self.contents.setHeaderLabels([_("Qty"), _("Item"), _("Each"), _("Total")])
        self.contents.setRootIsDecorated(False)
        self.contents.setUniformRowHeights(True)
        chdr = self.contents.header()
        chdr.setStretchLastSection(False)
        chdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        chdr.setSectionResizeMode(1, QHeaderView.Stretch)
        chdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        chdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.contents.setStyleSheet(_TREE)
        self.contents.currentItemChanged.connect(lambda cur, _prev: self._show_uex(cur, ROLE_KEY))
        right.addWidget(self.contents, 1)

        ctl = QHBoxLayout()
        ctl.setSpacing(4)
        self.minus_btn = QPushButton("−", self)
        self.plus_btn = QPushButton("+", self)
        self.remove_btn = QPushButton(_("Remove"), self)
        for b, fn in ((self.minus_btn, lambda: self._bump(-1)),
                      (self.plus_btn, lambda: self._bump(+1)),
                      (self.remove_btn, self._remove)):
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(_BTN)
            b.clicked.connect(fn)
            ctl.addWidget(b)
        ctl.addStretch(1)
        right.addLayout(ctl)

        uex_row = QHBoxLayout()
        uex_row.setSpacing(4)
        self.uex_lbl = _label("", FG_DIM, self)
        uex_row.addWidget(self.uex_lbl, 1)
        self.uex_btn = QPushButton(_("Open on UEX"), self)
        self.uex_btn.setCursor(Qt.PointingHandCursor)
        self.uex_btn.setStyleSheet(_BTN)
        self.uex_btn.setEnabled(False)
        self.uex_btn.clicked.connect(self._open_uex)
        uex_row.addWidget(self.uex_btn)
        right.addLayout(uex_row)
        cols.addLayout(right, 1)

        # one line for what just happened (a refusal is red)
        self.msg_lbl = _label("", FG_DIM, self)
        outer.addWidget(self.msg_lbl)

        self._uex_url: str | None = None
        self.refresh()

    # ── state -> widgets ────────────────────────────────────────────────────

    def refresh(self) -> None:
        st = self.app.crate_state(self.no)
        if st is None:
            return
        cap = st["capacity_u"]
        used = crate_items.used_u(st["contents"])
        pct = used / cap if cap else 0.0
        self.title_lbl.setText(_("Crate {n}  ·  {size}").format(n=self.no, size=st["short"]))
        self.fill_lbl.setText(f"{fmt_u(used)} / {fmt_u(cap)}  ({pct * 100:.0f}%)")
        col = fill_color(pct)
        self.fill_lbl.setStyleSheet(f"color: {col}; {_LBL} font-weight: bold;")
        self.bar.set_values(pct, col)
        self.pop_btn.setText(_("Dock") if self.app.crate_is_popped(self.no) else _("Pop out"))

        keep = self._current_key()
        self.contents.clear()
        for c in st["contents"]:
            n = QTreeWidgetItem([str(c["qty"]), c["name"], fmt_u(c["vol_u"]),
                                 fmt_u(c["vol_u"] * c["qty"])])
            n.setData(0, ROLE_KEY, c["key"])
            n.setToolTip(1, f"{c['name']}\n{c.get('kind') or ''}")
            n.setForeground(2, QBrush(QColor(FG_DIM)))
            self.contents.addTopLevelItem(n)
            if c["key"] == keep:
                self.contents.setCurrentItem(n)
        self.refresh_data_note()

    def refresh_data_note(self) -> None:
        text, show_dl = self.app.crate_data_note()
        self.data_lbl.setText(text)
        self.dl_btn.setVisible(show_dl)
        self.refresh_results()

    def refresh_results(self, _text=None) -> None:
        rows = self.app.crate_index_rows()
        cur = self.results.currentItem()
        keep = (cur.data(0, ROLE_ROW) or [None] * 4)[3] if cur is not None else None
        self.results.clear()
        q = self.search.text()
        if not rows or not q.strip():
            return
        st = self.app.crate_state(self.no)
        left = st["capacity_u"] - crate_items.used_u(st["contents"]) if st else 0
        for r in crate_items.search(rows, q):
            n = QTreeWidgetItem([r[0], r[2], fmt_u(r[1])])
            n.setData(0, ROLE_ROW, list(r))
            if r[1] > left:                       # would not fit: say so up front
                for col in range(3):
                    n.setForeground(col, QBrush(QColor(RED)))
                n.setToolTip(0, _("Too big for what is left in this crate"))
            self.results.addTopLevelItem(n)
            if keep is not None and r[3] == keep:
                self.results.setCurrentItem(n)       # the pick survives a refresh
        if self.results.topLevelItemCount() and self.results.currentItem() is None:
            self.results.setCurrentItem(self.results.topLevelItem(0))

    def _current_key(self):
        cur = self.contents.currentItem()
        return cur.data(0, ROLE_KEY) if cur is not None else None

    def say(self, text: str, bad: bool = False) -> None:
        self.msg_lbl.setText(text)
        self.msg_lbl.setStyleSheet(f"color: {RED if bad else GREEN}; {_LBL}")

    # ── actions ─────────────────────────────────────────────────────────────

    def add_selected(self) -> None:
        cur = self.results.currentItem()
        if cur is None:
            self.say(_("Search for an item, then pick it."), bad=True)
            return
        entry = crate_items.entry_from_row(cur.data(0, ROLE_ROW))
        qty = self.qty.value()
        ok, why = self.app.crate_add(self.no, entry, qty)
        if ok:
            self.say(_("Added {q} × {name}").format(q=qty, name=entry["name"]))
        else:
            self.say(_("Won't fit: {why}").format(why=why), bad=True)

    def _bump(self, d: int) -> None:
        key = self._current_key()
        if key is None:
            return
        st = self.app.crate_state(self.no)
        row = next((c for c in st["contents"] if c["key"] == key), None)
        if row is None:
            return
        ok, why = self.app.crate_set_qty(self.no, key, row["qty"] + d)
        if not ok:
            self.say(_("Won't fit: {why}").format(why=why), bad=True)
        else:
            self.say("")

    def _remove(self) -> None:
        key = self._current_key()
        if key is not None:
            self.app.crate_set_qty(self.no, key, 0)
            self.say("")

    # ── UEX (read-only) ─────────────────────────────────────────────────────

    def _show_uex(self, node, role) -> None:
        self._uex_url = None
        if node is None:
            self.uex_btn.setEnabled(False)
            self.uex_lbl.setText("")
            return
        if role == ROLE_ROW:
            entry = crate_items.entry_from_row(node.data(0, ROLE_ROW))
        else:
            st = self.app.crate_state(self.no)
            key = node.data(0, ROLE_KEY)
            entry = next((c for c in st["contents"] if c["key"] == key), None) or {}
        rec, state = self.app.crate_uex(entry)
        if rec is None:
            self.uex_lbl.setText(state)
            self.uex_btn.setEnabled(False)
            return
        where = " / ".join(x for x in (rec.get("section"), rec.get("category")) if x)
        self.uex_lbl.setText("UEX: " + where + (f"  ·  {rec['company']}" if rec.get("company") else ""))
        self._uex_url = crate_items.uex_url(rec)
        self.uex_btn.setEnabled(bool(self._uex_url))

    def _open_uex(self) -> None:
        if self._uex_url:
            self.app.crate_open_url(self._uex_url)


class CrateWindow(QWidget):
    """A popped-out crate: its own small window; closing it docks it back."""

    def __init__(self, app, panel: CratePanel) -> None:
        super().__init__(app, Qt.Window | Qt.WindowStaysOnTopHint)
        self.app = app
        self.panel = panel
        self.setWindowTitle(_("Crate {n}").format(n=panel.no))
        self.setStyleSheet(f"background-color: {BG2};")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(panel)
        panel.show()
        self.resize(640, 380)
        self._docking = False

    def take_panel(self) -> CratePanel:
        """Hand the panel back (for docking) without deleting it."""
        self._docking = True
        self.layout().removeWidget(self.panel)
        self.panel.setParent(None)
        return self.panel

    def closeEvent(self, event) -> None:
        if not self._docking:
            self.app.crate_dock(self.panel.no)
        super().closeEvent(event)

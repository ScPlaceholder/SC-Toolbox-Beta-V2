"""My Career tab — the trader's lifetime record: net profit, success rate, a
cumulative profit/loss chart over time, a calendar of daily P/L, saved favorite
routes (filterable by ship, with remove), and a per-ship leaderboard. Includes a
UEC-cycle hard reset (with undo). All data lives in career.Career (persisted).
"""
from __future__ import annotations

import time
from datetime import date
from typing import List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QPushButton, QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from shared.qt.theme import P
from shared.qt.fuzzy_combo import SCFuzzyCombo
from shared.ships import QUICK_SHIPS
from starmap.charts import LineChart

from career_calendar import CareerCalendar
from career_heatmap import CareerHeatmap


def _m(v) -> str:
    try:
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return "—"


def _ts(d: date) -> float:
    return time.mktime(d.timetuple())


class _NumItem(QTableWidgetItem):
    def __init__(self, value, text) -> None:
        super().__init__(text)
        self._v = value
        self.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def __lt__(self, other) -> bool:
        try:
            return self._v < other._v
        except Exception:
            return super().__lt__(other)


class CareerView(QWidget):
    def __init__(self, th, parent=None) -> None:
        super().__init__(parent)
        self._th = th
        self._career = th._career
        self._fav_ship = ""
        self._fav_rows: List[dict] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        head = QLabel("  MY CAREER   ·   your lifetime trading record")
        head.setStyleSheet(f"background:{P.bg_header}; color:{P.tool_trade}; font-family:Consolas; "
                           f"font-size:10pt; font-weight:bold; padding:8px 6px;")
        root.addWidget(head)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"QScrollArea{{border:none; background:{P.bg_primary};}}")
        root.addWidget(scroll, 1)
        body = QWidget()
        scroll.setWidget(body)
        b = QVBoxLayout(body)
        b.setContentsMargins(14, 12, 14, 16)
        b.setSpacing(10)

        self._summary = self._rich(13)
        b.addWidget(self._card(self._summary))
        self._best = self._rich(9)
        b.addWidget(self._card(self._best))

        b.addWidget(self._section("PROFIT / LOSS OVER TIME", P.tool_trade))
        self._chart = LineChart("Cumulative Net Profit", "aUEC")
        self._chart.setMinimumHeight(210)
        b.addWidget(self._chart)

        b.addWidget(self._section("COMMODITY RUN HEATMAP", P.purple))
        sub = QLabel("every finished (✓) and failed (✕) commodity run · "
                     "green = net gain, red = net loss · click a tile to open")
        sub.setStyleSheet(f"color:{P.fg_dim}; font-size:8pt;")
        b.addWidget(sub)
        self._cmd_heatmap = CareerHeatmap()
        self._cmd_heatmap.commodity_clicked.connect(self._open_commodity)
        b.addWidget(self._cmd_heatmap)

        b.addWidget(self._section("CAREER CALENDAR", P.energy_cyan))
        cal_row = QHBoxLayout(); cal_row.setSpacing(10)
        self._cal = CareerCalendar()
        self._cal.day_selected.connect(self._show_day)
        self._cal.setMinimumHeight(300)
        cal_row.addWidget(self._cal, 3)
        self._day = self._rich(8)
        self._day.setText("Click a day to see its trades.")
        self._day.setAlignment(Qt.AlignTop)
        self._day.setStyleSheet(f"font-family:Consolas; font-size:8pt; color:{P.fg}; "
                                f"background:{P.bg_card}; border:1px solid {P.border}; padding:8px;")
        self._day.setFixedWidth(280)
        cal_row.addWidget(self._day)
        cw = QWidget(); cw.setLayout(cal_row); b.addWidget(cw)

        rr = QHBoxLayout()
        self._btn_reset = QPushButton("⟲ UEC Reset")
        self._btn_reset.setCursor(Qt.PointingHandCursor)
        self._btn_reset.setStyleSheet(self._danger_ss())
        self._btn_reset.clicked.connect(self._reset)
        self._btn_undo = QPushButton("↶ Undo Reset")
        self._btn_undo.setCursor(Qt.PointingHandCursor)
        self._btn_undo.setStyleSheet(self._btn_ss())
        self._btn_undo.clicked.connect(self._undo)
        rr.addWidget(self._btn_reset); rr.addWidget(self._btn_undo); rr.addStretch(1)
        rw = QWidget(); rw.setLayout(rr); b.addWidget(rw)

        b.addWidget(self._section("FAVORITE ROUTES", P.green))
        fr = QHBoxLayout()
        lbl = QLabel("My ships:"); lbl.setStyleSheet(f"color:{P.fg_dim}; font-size:9pt;")
        fr.addWidget(lbl)
        self._ship_filter = SCFuzzyCombo(placeholder="filter favorites by ship…",
                                         items=[d for _n, d in QUICK_SHIPS])
        self._ship_filter.item_selected.connect(self._on_ship_filter)
        fr.addWidget(self._ship_filter, 1)
        self._btn_clear_ship = QPushButton("All")
        self._btn_clear_ship.setCursor(Qt.PointingHandCursor)
        self._btn_clear_ship.setStyleSheet(self._btn_ss())
        self._btn_clear_ship.clicked.connect(lambda: self._on_ship_filter(""))
        fr.addWidget(self._btn_clear_ship)
        self._btn_remove = QPushButton("✕ Remove Favorite")
        self._btn_remove.setCursor(Qt.PointingHandCursor)
        self._btn_remove.setStyleSheet(self._btn_ss())
        self._btn_remove.clicked.connect(self._remove_fav)
        fr.addWidget(self._btn_remove)
        fw = QWidget(); fw.setLayout(fr); b.addWidget(fw)
        self._fav_table = self._table(["Commodity", "Buy @", "Sell @", "Ship", "Est. Profit"])
        self._fav_table.cellDoubleClicked.connect(self._open_fav)
        b.addWidget(self._fav_table)

        b.addWidget(self._section("MY LEADERBOARD   ·   per ship", P.purple))
        lr = QHBoxLayout()
        sl = QLabel("Sort by:"); sl.setStyleSheet(f"color:{P.fg_dim}; font-size:9pt;")
        lr.addWidget(sl)
        self._sort = QComboBox()
        self._sort.addItems(["Net Profit", "Success Rate", "ROI %", "Avg / Route", "Routes"])
        self._sort.setStyleSheet(self._combo_ss())
        self._sort.currentTextChanged.connect(lambda *_: self._fill_leaderboard())
        lr.addWidget(self._sort); lr.addStretch(1)
        lw = QWidget(); lw.setLayout(lr); b.addWidget(lw)
        self._lb = self._table(["Ship", "Routes", "Success %", "Net Profit", "ROI %", "Avg / Route"])
        b.addWidget(self._lb)
        b.addStretch(1)

    # ── refresh ────────────────────────────────────────────────────────────────
    def refresh(self) -> None:
        c = self._career
        net, loss = c.net_profit(), c.total_loss()
        done, failed, rate = c.counts()
        col = P.green if net >= 0 else P.red
        self._summary.setText(
            f"<span style='color:{col}; font-size:18pt; font-weight:bold;'>{_m(net)} aUEC</span> "
            f"<span style='color:{P.fg_dim};'>net</span> &nbsp;·&nbsp; "
            f"<b>{done}</b> completed &nbsp;·&nbsp; <b style='color:{P.green};'>{rate:.0f}%</b> success "
            f"&nbsp;·&nbsp; <span style='color:{P.red};'>−{_m(loss)}</span> lost across {failed} fails")
        bship = max(c.ship_stats(), key=lambda s: s["net"], default=None)
        bsys, bcom = c.best_by("sell_system"), c.best_by("commodity")
        parts = []
        if bship and bship["completed"]:
            parts.append(f"Top ship: <b style='color:{P.fg_bright}'>{bship['ship']}</b> "
                         f"({_m(bship['net'])} aUEC · {bship['success']:.0f}% success)")
        if bsys:
            parts.append(f"Top system: <b style='color:{P.fg_bright}'>{bsys[0]}</b> ({_m(bsys[1])})")
        if bcom:
            parts.append(f"Top commodity: <b style='color:{P.fg_bright}'>{bcom[0]}</b> ({_m(bcom[1])})")
        self._best.setText("&nbsp; · &nbsp;".join(parts)
                           or "No trades logged yet — open a route and press Complete Route to start your career.")
        cum = c.cumulative()
        self._chart.set_series([{"points": [(_ts(d), v) for d, v in cum],
                                 "color": P.tool_trade, "width": 2.4}])
        self._cmd_heatmap.set_data(c.commodity_stats())
        self._cal.set_data(c.per_day())
        self._fill_favorites()
        self._fill_leaderboard()
        self._btn_undo.setEnabled(c.has_undo())

    # ── favorites ──────────────────────────────────────────────────────────────
    def _on_ship_filter(self, ship: str) -> None:
        self._fav_ship = (ship or "").strip()
        self._fill_favorites()

    def _fill_favorites(self) -> None:
        favs = self._career.favorites()
        if self._fav_ship:
            fl = self._fav_ship.lower()
            favs = [f for f in favs if fl in (f.get("ship", "") or "").lower()]
        self._fav_rows = favs
        t = self._fav_table
        t.setRowCount(len(favs))
        for i, f in enumerate(favs):
            t.setItem(i, 0, QTableWidgetItem(f.get("commodity", "?")))
            t.setItem(i, 1, QTableWidgetItem(f"{f.get('buy_location','')} · {f.get('buy_system','')}"))
            t.setItem(i, 2, QTableWidgetItem(f"{f.get('sell_location','')} · {f.get('sell_system','')}"))
            t.setItem(i, 3, QTableWidgetItem(f.get("ship", "—")))
            t.setItem(i, 4, _NumItem(f.get("profit", 0), _m(f.get("profit", 0))))

    def _open_fav(self, row: int, _col: int = 0) -> None:
        if 0 <= row < len(self._fav_rows) and hasattr(self._th, "_open_route_data"):
            self._th._open_route_data(self._fav_rows[row])

    def _open_commodity(self, name: str) -> None:
        th = self._th
        if name and hasattr(th, "_starmap_panel") and hasattr(th._starmap_panel, "_open_commodity"):
            th._starmap_panel._open_commodity(name)

    def _remove_fav(self) -> None:
        row = self._fav_table.currentRow()
        if row < 0 or row >= len(self._fav_rows):
            QMessageBox.information(self, "Remove Favorite", "Select a favorite row first.")
            return
        fav = self._fav_rows[row]
        if QMessageBox.question(
                self, "Remove Favorite",
                f"Remove this favorite route?\n\n{fav.get('commodity','?')}: "
                f"{fav.get('buy_location','')} → {fav.get('sell_location','')}",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        full = self._career.favorites()
        for idx, f in enumerate(full):
            if f is fav or f == fav:
                self._career.remove_favorite(idx)
                break
        self.refresh()

    # ── leaderboard ────────────────────────────────────────────────────────────
    def _fill_leaderboard(self) -> None:
        rows = self._career.ship_stats()
        key = {"Net Profit": "net", "Success Rate": "success", "ROI %": "roi",
               "Avg / Route": "avg", "Routes": "routes"}.get(self._sort.currentText(), "net")
        rows.sort(key=lambda r: -r[key])
        t = self._lb
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            t.setItem(i, 0, QTableWidgetItem(r["ship"]))
            t.setItem(i, 1, _NumItem(r["routes"], f"{r['completed']}/{r['routes']}"))
            t.setItem(i, 2, _NumItem(r["success"], f"{r['success']:.0f}%"))
            t.setItem(i, 3, _NumItem(r["net"], _m(r["net"])))
            t.setItem(i, 4, _NumItem(r["roi"], f"{r['roi']:.0f}%"))
            t.setItem(i, 5, _NumItem(r["avg"], _m(r["avg"])))

    # ── calendar day detail ────────────────────────────────────────────────────
    def _show_day(self, d: date) -> None:
        comp, fail = self._career.trades_on(d)
        net = sum(float(e.get("profit", 0) or 0) for e in comp) - \
            sum(float(e.get("loss", 0) or 0) for e in fail)
        col = P.green if net >= 0 else P.red
        lines = [f"<b style='color:{P.fg_bright}'>{d:%A %d %b %Y}</b>",
                 f"Net: <b style='color:{col}'>{_m(net)} aUEC</b> &nbsp; "
                 f"({len(comp)} done, {len(fail)} failed)<br>"]
        for e in comp:
            lines.append(f"<span style='color:{P.green}'>+{_m(e.get('profit',0))}</span> "
                         f"{e.get('commodity','?')} · {e.get('ship','')}")
        for e in fail:
            lines.append(f"<span style='color:{P.red}'>−{_m(e.get('loss',0))}</span> "
                         f"{e.get('commodity','?')} ({e.get('kind','full')})")
        if not comp and not fail:
            lines.append(f"<span style='color:{P.fg_dim}'>No trades this day.</span>")
        self._day.setText("<br>".join(lines))

    # ── reset / undo ───────────────────────────────────────────────────────────
    def _reset(self) -> None:
        done, failed, _ = self._career.counts()
        if QMessageBox.warning(
                self, "UEC Reset — start a new cycle",
                "This wipes your entire trade ledger to start a fresh UEC cycle from zero:\n\n"
                f"• {done} completed routes and {failed} failures will be cleared\n"
                "• Net profit, the P/L chart, the calendar, and the leaderboard reset to 0\n"
                "• Your saved FAVORITE routes are KEPT\n\n"
                "You can Undo this once, right after. Continue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self._career.reset()
        self.refresh()

    def _undo(self) -> None:
        if self._career.undo_reset():
            self.refresh()
        else:
            QMessageBox.information(self, "Undo Reset", "Nothing to undo.")

    # ── small UI helpers ───────────────────────────────────────────────────────
    @staticmethod
    def _rich(pt: float) -> QLabel:
        lbl = QLabel()
        lbl.setTextFormat(Qt.RichText)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"font-family:Consolas; font-size:{pt}pt; color:{P.fg};")
        return lbl

    @staticmethod
    def _card(inner: QLabel) -> QWidget:
        w = QWidget()
        w.setStyleSheet(f"background:{P.bg_card}; border:1px solid {P.border}; border-radius:6px;")
        lay = QVBoxLayout(w); lay.setContentsMargins(14, 10, 14, 10)
        lay.addWidget(inner)
        return w

    @staticmethod
    def _section(title: str, accent: str) -> QLabel:
        lbl = QLabel(title)
        lbl.setStyleSheet(f"color:{accent}; font-family:Consolas; font-size:11pt; "
                          f"font-weight:bold; padding-top:4px;")
        return lbl

    def _table(self, cols: List[str]) -> QTableWidget:
        t = QTableWidget(0, len(cols))
        t.setHorizontalHeaderLabels(cols)
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setSelectionMode(QAbstractItemView.SingleSelection)
        t.setCursor(Qt.PointingHandCursor)
        t.setStyleSheet(
            f"QTableWidget{{background:{P.bg_primary}; color:{P.fg}; border:1px solid {P.border}; "
            f"gridline-color:{P.border}; font-size:9pt;}} "
            f"QTableWidget::item:selected{{background:{P.bg_card}; color:{P.fg_bright};}} "
            f"QHeaderView::section{{background:{P.bg_header}; color:{P.fg_dim}; border:none; "
            f"padding:4px; font-weight:bold;}}")
        hdr = t.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in range(1, len(cols)):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        t.setMinimumHeight(150)
        return t

    @staticmethod
    def _btn_ss() -> str:
        return (f"QPushButton{{background:{P.bg_card}; color:{P.fg}; border:1px solid {P.border}; "
                f"border-radius:5px; padding:6px 14px; font-family:Consolas; font-size:9pt;}} "
                f"QPushButton:hover{{color:{P.fg_bright}; border-color:{P.energy_cyan};}} "
                f"QPushButton:disabled{{color:{P.fg_disabled};}}")

    @staticmethod
    def _danger_ss() -> str:
        return (f"QPushButton{{background:{P.bg_card}; color:{P.red}; border:1px solid {P.red}; "
                f"border-radius:5px; padding:6px 14px; font-family:Consolas; font-size:9pt; font-weight:bold;}} "
                f"QPushButton:hover{{background:{P.red}; color:#1a0000;}}")

    @staticmethod
    def _combo_ss() -> str:
        return (f"QComboBox{{background:{P.bg_input}; color:{P.fg}; border:1px solid {P.border}; "
                f"border-radius:4px; padding:4px 8px; font-size:9pt;}}")

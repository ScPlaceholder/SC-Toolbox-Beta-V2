"""Dev History — main window.

Search Star Citizen's development history (dev-video transcripts, RSI comm-links and
CIG Devtracker posts), streamed from GitHub by the shared ``sc_dev_history`` engine;
browse every Monthly Report on its own tab, and the CIG Dev Tracker on another.
What the archive does not have yet (an article's text, the newest dev posts) is fetched
live from robertsspaceindustries.com on demand, one request per click.

Threading rule: anything that can touch the network runs in a ``Task`` (QThread):
the index load (a few MB on first run, a conditional re-check after that) and each
text fetch (excerpts, a full article, a report).  Searching and listing the Monthly
Reports are in-memory lookups over the loaded index and run on the UI thread only
once the index is in memory.
"""
from __future__ import annotations

import datetime
import html
import logging
import os
import re
import threading
from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QHBoxLayout, QHeaderView,
    QLabel, QProgressBar, QPushButton, QSplitter, QStackedWidget, QTableView, QTableWidget,
    QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from shared.qt.base_window import SCWindow
from shared.qt.search_bar import SCSearchBar
from shared.qt.theme import P
from shared.qt.title_bar import SCTitleBar

from core import settings as st
from core.engine import ATTRIBUTION, INDEX_SIZE_HINT, DevHistoryService, kind_label
from ui.lazy_model import Column, LazyTableModel
from ui.workers import MainThreadGC, Task

log = logging.getLogger(__name__)

ACCENT = "#88aaff"          # matches skill.json "color"
VIDEO_COLOR = P.energy_cyan
COMMLINK_COLOR = P.yellow
DEVPOST_COLOR = P.green
SHIP_COLOR = P.orange
KIND_COLOR = {"v": VIDEO_COLOR, "c": COMMLINK_COLOR, "d": DEVPOST_COLOR, "s": SHIP_COLOR, "b": SHIP_COLOR}
SERIES_COLOR = {"Star Citizen": P.energy_cyan, "Squadron 42": P.orange, "Studio": P.purple}
TAB_SEARCH, TAB_REPORTS, TAB_DEVTRACKER = 0, 1, 2
_TAB_KEYS = {TAB_SEARCH: "search", TAB_REPORTS: "reports", TAB_DEVTRACKER: "devtracker"}
_MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August",
                "September", "October", "November", "December"]

_ROLE_DOC = Qt.UserRole


def _btn(text: str, accent: str = ACCENT) -> QPushButton:
    """Same outlined button as PlayTime's ``_btn``."""
    b = QPushButton(text)
    b.setCursor(Qt.PointingHandCursor)
    c = QColor(accent)
    b.setStyleSheet(
        f"QPushButton {{ font-family: Consolas; font-size: 8pt; font-weight: bold;"
        f" color: {accent}; background: transparent; border: 1px solid {accent};"
        f" border-radius: 3px; padding: 4px 12px; }}"
        f"QPushButton:hover {{ background: rgba({c.red()},{c.green()},{c.blue()},0.15); }}"
        f"QPushButton:disabled {{ color: {P.fg_disabled}; border-color: {P.fg_disabled}; }}")
    return b


def _kind_label(doc: dict) -> str:
    return kind_label(doc).upper()


def _tab_btn(text: str) -> QPushButton:
    """A flat, checkable tab button for the top bar."""
    b = QPushButton(text)
    b.setCheckable(True)
    b.setCursor(Qt.PointingHandCursor)
    b.setStyleSheet(
        f"QPushButton {{ font-family: Consolas; font-size: 8pt; font-weight: bold; letter-spacing: 1px;"
        f" color: {P.fg_dim}; background: transparent; border: none;"
        f" border-bottom: 2px solid transparent; padding: 5px 10px; }}"
        f"QPushButton:hover {{ color: {P.fg}; }}"
        f"QPushButton:checked {{ color: {ACCENT}; border-bottom: 2px solid {ACCENT}; }}")
    return b


def _lazy_view(parent, model: LazyTableModel, widths: tuple) -> QTableView:
    """A table view over a LazyTableModel: rows stream in as you scroll."""
    v = QTableView(parent)
    v.setModel(model)
    v.setSelectionBehavior(QAbstractItemView.SelectRows)
    v.setSelectionMode(QAbstractItemView.SingleSelection)
    v.setEditTriggers(QAbstractItemView.NoEditTriggers)
    v.setAlternatingRowColors(True)
    v.setWordWrap(False)
    v.verticalHeader().setVisible(False)
    v.verticalHeader().setDefaultSectionSize(26)
    v.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)     # no per-row measuring
    hh = v.horizontalHeader()
    hh.setStretchLastSection(True)
    # Fixed starting widths (the user can drag them). ResizeToContents would measure every loaded row on every
    # batch, which is what made long lists slow.
    hh.setSectionResizeMode(QHeaderView.Interactive)
    for c, px in enumerate(widths):
        v.setColumnWidth(c, px)
    return v


PHRASE_NEAR = 60      # words: a phrase said within this stretch counts as "said together" (◆ in the list)


def _search_title(d: dict) -> str:
    t = d.get("t") or "(untitled)"
    t = f"{d['a']}: {t}" if d.get("k") == "d" and d.get("a") else t
    return f"◆ {t}" if d.get("near") and d["near"] <= PHRASE_NEAR else t


def _month_label(month: str) -> str:
    """'2026-08' -> 'Aug 2026'."""
    try:
        y, m = month.split("-")[:2]
        return f"{_MONTH_NAMES[int(m) - 1][:3]} {y}"
    except (ValueError, IndexError):
        return month or "—"


def render_text(text: str, terms: Optional[list[str]] = None) -> str:
    """Corpus text -> reader HTML. '## ' lines are headings, '- ' lines are bullets, the rest paragraphs."""
    terms = terms or []
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("## "):
            out.append(f'<h3 style="font-family: Electrolize, Consolas; color:{ACCENT};'
                       f' margin: 14px 0 4px 0;">{highlight(line[3:], terms)}</h3>')
        elif line.startswith("- "):
            out.append(f'<p style="margin: 0 0 4px 14px; color:{P.fg};">&bull; {highlight(line[2:], terms)}</p>')
        else:
            out.append(f'<p style="margin: 0 0 8px 0; color:{P.fg};">{highlight(line, terms)}</p>')
    return "".join(out)


def _label(text: str) -> str:
    return (f'<p style="color:{P.fg_dim}; font-size:7pt; letter-spacing:1px; margin: 4px 0 6px 0;">'
            f'<b>{text}</b></p>')


def _note(text: str) -> str:
    return f'<p style="color:{P.fg_dim}; font-style: italic;">{text}</p>'


_SOURCE_NOTE = {
    "rsi": "Not in the archive yet: fetched from robertsspaceindustries.com just now.",
    "cached": "Not in the archive yet: fetched from robertsspaceindustries.com earlier and kept on this PC.",
}


def article_html(art: dict, terms: Optional[list[str]] = None, full: bool = True) -> str:
    """SUMMARY (a digest of what the article says) and, when *full*, the whole text below it."""
    parts = []
    if art.get("summary"):
        parts.append(_label("SUMMARY") + render_text(art["summary"], terms))
    if art.get("source") in _SOURCE_NOTE:
        parts.append(_note(_SOURCE_NOTE[art["source"]]))
    if full and art.get("text"):
        parts.append(f'<hr style="border: none; border-top: 1px solid {P.border_card};">' + _label("FULL TEXT")
                     + render_text(art["text"], terms))
    return "".join(parts)


def highlight(text: str, terms: list[str]) -> str:
    """HTML-escape *text* and wrap whole-word query terms in an accent span."""
    safe = html.escape(text, quote=False)
    words = sorted({t for t in terms if t}, key=len, reverse=True)
    if not words:
        return safe
    pat = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(re.escape(w) for w in words) + r")(?![A-Za-z0-9])",
                     re.IGNORECASE)
    return pat.sub(lambda m: f'<span style="color:{ACCENT}; font-weight:bold;">{m.group(1)}</span>', safe)


# J, 2026-09-26: "add a disclaimer pop up that it might get removed at a future event pending CIG's decision".
# Shown on the first SHOW of the window (never while it is preloaded hidden), once per DISCLAIMER_VERSION:
# bump the version when the wording changes and every user sees it again.
DISCLAIMER_VERSION = 1
DISCLAIMER_TITLE = "Dev History - please read"
DISCLAIMER_TEXT = (
    "<p><b>Dev History is an unofficial fan archive.</b> It searches transcripts of Cloud Imperium Games' "
    "development videos, RSI comm-links and CIG posts from the Spectrum Devtracker. Star Citizen and all of "
    "this content are &copy; Cloud Imperium "
    "Games. This project is not affiliated with or endorsed by Cloud Imperium Games.</p>"
    "<p><b>This tool may be removed in a future update</b>, pending CIG's decision on fan archives of their "
    "content. If CIG asks, it goes, no questions asked.</p>"
    "<p>Transcripts are machine-made and contain mistakes, especially in names and ship designations. Every "
    "result links back to the original video or comm-link: when it matters, check the source.</p>"
)


class DevHistoryWindow(SCWindow):
    """The Dev History search window."""

    def __init__(self, geometry, hotkey_text: str = "", cmd_file: Optional[str] = None) -> None:
        super().__init__(
            title="Dev History",
            width=geometry.w, height=geometry.h,
            min_w=720, min_h=460,
            opacity=geometry.opacity, accent=ACCENT,
        )
        self.restore_geometry_from_args(
            geometry.x, geometry.y, geometry.w, geometry.h, geometry.opacity)
        self._standalone = not cmd_file or cmd_file == os.devnull

        # Cyclic GC on the GUI thread only — see MainThreadGC for the crash it prevents.
        self._gc = MainThreadGC(self)

        self._settings = st.load_settings()
        self._disclaimer_pending = int(self._settings.get("disclaimer_ack", 0) or 0) < DISCLAIMER_VERSION
        self._ref = self._settings["ref"]
        self._service = DevHistoryService(ref=self._ref, live=self._settings["live_rsi"])

        self._tasks: set[Task] = set()
        self._index_task: Optional[Task] = None
        self._index_ready = False
        self._n_docs = 0
        self._pending_query = ""
        self._query = ""
        self._current_doc: Optional[dict] = None
        self._excerpt_seq = 0
        self._showing_full = False
        self._search_status = ""
        self._phrase_seq = 0
        self._reports: list[dict] = []
        self._report_doc: Optional[dict] = None
        self._report_seq = 0
        self._dev_posts: list[dict] = []
        self._dev_post: Optional[dict] = None
        self._dev_seq = 0
        self._dev_page = 0                  # last live Devtracker page fetched this session
        self._dev_day = ""                  # the day header that page ended on
        self._dev_task: Optional[Task] = None
        self._dev_refreshed = False
        self._bulk_task: Optional[Task] = None
        self._bulk_stop = threading.Event()
        self._bulk_kind = ""

        # ── Title bar ──
        self._title_bar = SCTitleBar(
            window=self, title="DEV HISTORY", icon_text="\U0001f4dc", accent_color=ACCENT,
            hotkey_text=hotkey_text, show_minimize=True,
            extra_buttons=[("Tutorial", self._show_tutorial)],
        )
        self._title_bar.minimize_clicked.connect(self.showMinimized)
        self._title_bar.close_clicked.connect(self._on_close)
        self.content_layout.addWidget(self._title_bar)

        self._build_controls()
        self._build_banner()
        self._stack = QStackedWidget(self)
        self._build_body()
        self._build_reports()
        self._build_devtracker()
        self.content_layout.addWidget(self._stack, 1)
        self._build_footer()
        start = {v: k for k, v in _TAB_KEYS.items()}.get(self._settings.get("tab"), TAB_SEARCH)
        self._switch_tab(start, save=False)

        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._shutdown_tasks)

        QTimer.singleShot(120, self._start_index_load)

    # ══ Layout ═══════════════════════════════════════════════════════════════

    def _build_controls(self) -> None:
        bar = QWidget(self)
        bar.setStyleSheet(f"background: {P.bg_header};")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 6, 12, 6)
        lay.setSpacing(8)

        self._tab_search = _tab_btn("SEARCH")
        self._tab_reports = _tab_btn("MONTHLY REPORTS")
        self._tab_dev = _tab_btn("DEV TRACKER")
        self._tabs = QButtonGroup(bar)
        self._tabs.setExclusive(True)
        self._tabs.addButton(self._tab_search, TAB_SEARCH)
        self._tabs.addButton(self._tab_reports, TAB_REPORTS)
        self._tabs.addButton(self._tab_dev, TAB_DEVTRACKER)
        self._tabs.idClicked.connect(self._switch_tab)
        lay.addWidget(self._tab_search)
        lay.addWidget(self._tab_reports)
        lay.addWidget(self._tab_dev)

        self._search = SCSearchBar(
            placeholder="Search dev history… e.g. quantum drive, laser head, server meshing",
            debounce_ms=350, parent=bar)
        self._search.setMinimumWidth(180)
        self._search.search_changed.connect(self._on_search)
        self._search.returnPressed.connect(lambda: self._on_search(self._search.text().strip()))
        lay.addWidget(self._search, 1)

        self._phrase_box = QCheckBox("Phrase", bar)
        self._phrase_box.setToolTip(
            "Search by phrase: \"Kraken land on planets\" finds where the Kraken LANDING on a PLANET is\n"
            "talked about, in any wording (land, landed, landing; planet, planetside, surface, moon...).\n"
            "Every word must appear; results where they are said close together come first (◆).")
        self._phrase_box.setCursor(Qt.PointingHandCursor)
        self._phrase_box.setStyleSheet(
            f"QCheckBox {{ font-family: Consolas; font-size: 8pt; font-weight: bold; color: {P.fg_dim};"
            f" background: transparent; spacing: 5px; }}"
            f"QCheckBox:checked {{ color: {ACCENT}; }}")
        self._phrase_box.setChecked(bool(self._settings.get("phrase")))
        self._phrase_box.toggled.connect(self._on_phrase_toggled)
        lay.addWidget(self._phrase_box)

        # Monthly Reports filters (shown instead of the search box on that tab).
        self._report_filters = QWidget(bar)
        fl = QHBoxLayout(self._report_filters)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(6)
        self._series_box = QComboBox(self._report_filters)
        self._series_box.addItems(["All series", "Star Citizen", "Squadron 42", "Studio"])
        self._series_box.currentIndexChanged.connect(lambda _i: self._fill_reports())
        self._year_box = QComboBox(self._report_filters)
        self._year_box.addItem("All years")
        self._year_box.currentIndexChanged.connect(lambda _i: self._fill_reports())
        self._report_find = SCSearchBar(placeholder="Filter reports by title…", debounce_ms=200,
                                        parent=self._report_filters)
        self._report_find.setMinimumWidth(140)
        self._report_find.search_changed.connect(lambda _t: self._fill_reports())
        fl.addWidget(self._series_box)
        fl.addWidget(self._year_box)
        fl.addWidget(self._report_find, 1)
        lay.addWidget(self._report_filters, 1)

        # Dev Tracker filters.
        self._dev_filters = QWidget(bar)
        dl = QHBoxLayout(self._dev_filters)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(6)
        self._dev_author_box = QComboBox(self._dev_filters)
        self._dev_author_box.addItem("All devs")
        self._dev_author_box.currentIndexChanged.connect(lambda _i: self._fill_devposts())
        self._dev_forum_box = QComboBox(self._dev_filters)
        self._dev_forum_box.addItem("All forums")
        self._dev_forum_box.currentIndexChanged.connect(lambda _i: self._fill_devposts())
        self._dev_find = SCSearchBar(placeholder="Filter by thread or text…", debounce_ms=200,
                                     parent=self._dev_filters)
        self._dev_find.setMinimumWidth(140)
        self._dev_find.search_changed.connect(lambda _t: self._fill_devposts())
        self._dev_refresh_btn = _btn("Refresh")
        self._dev_refresh_btn.setToolTip("Fetch the newest posts from the RSI Devtracker")
        self._dev_refresh_btn.clicked.connect(lambda: self._fetch_devtracker(older=False))
        self._dev_older_btn = _btn("Load older")
        self._dev_older_btn.setToolTip("Fetch the next page of older posts from the RSI Devtracker")
        self._dev_older_btn.clicked.connect(lambda: self._fetch_devtracker(older=True))
        self._dev_author_box.setMinimumWidth(110)
        self._dev_forum_box.setMinimumWidth(110)
        dl.addWidget(self._dev_author_box)
        dl.addWidget(self._dev_forum_box)
        dl.addWidget(self._dev_find, 1)
        lay.addWidget(self._dev_filters, 1)

        self._status = QLabel("", bar)
        self._status.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; background: transparent;")
        self._status.setMaximumWidth(190)       # long progress text must not squeeze the search box
        lay.addWidget(self._status)
        self.content_layout.addWidget(bar)

    def _build_banner(self) -> None:
        """Download / offline / error strip.  Hidden when everything is fine."""
        self._banner = QWidget(self)
        lay = QHBoxLayout(self._banner)
        lay.setContentsMargins(12, 6, 12, 6)
        lay.setSpacing(10)

        self._banner_text = QLabel("", self._banner)
        self._banner_text.setWordWrap(True)
        lay.addWidget(self._banner_text, 1)

        self._progress = QProgressBar(self._banner)
        self._progress.setFixedWidth(180)
        self._progress.setFixedHeight(12)
        self._progress.setTextVisible(False)
        lay.addWidget(self._progress)

        self._retry_btn = _btn("Retry")
        self._retry_btn.clicked.connect(self._start_index_load)
        lay.addWidget(self._retry_btn)

        self._banner.hide()
        self.content_layout.addWidget(self._banner)

    def _set_banner(self, text: str, tone: str, busy: bool = False, retry: bool = False) -> None:
        color = {"info": ACCENT, "warn": P.yellow, "error": P.red}.get(tone, ACCENT)
        self._banner.setStyleSheet(
            f"background: {P.bg_card}; border-bottom: 1px solid {color};")
        self._banner_text.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; color: {color}; border: none;"
            f" background: transparent;")
        self._banner_text.setText(text)
        self._progress.setVisible(busy)
        if busy:
            self._progress.setRange(0, 0)       # indeterminate until bytes arrive
        self._retry_btn.setVisible(retry)
        self._banner.show()

    def _build_body(self) -> None:
        split = QSplitter(Qt.Horizontal, self)
        split.setChildrenCollapsible(False)

        # ── Results list ──
        left = QWidget(split)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(10, 8, 4, 8)
        ll.setSpacing(4)
        self._model = LazyTableModel([
            Column("Date", lambda d: d.get("d") or "—"),
            Column("Type", _kind_label, color=lambda d: KIND_COLOR.get(d.get("k", ""), COMMLINK_COLOR),
                   bold=True, align_center=True),
            Column("Title", _search_title),
        ], batch=self._settings["max_results"], parent=self)
        self._table = _lazy_view(left, self._model, (100, 96))
        self._table.selectionModel().currentRowChanged.connect(lambda cur, _prev: self._on_selection_changed())
        self._table.doubleClicked.connect(lambda _i: self._open_source())
        ll.addWidget(self._table, 1)

        # ── Detail pane ──
        right = QWidget(split)
        rl = QVBoxLayout(right)
        rl.setContentsMargins(4, 8, 10, 8)
        rl.setSpacing(6)

        self._d_title = QLabel("", right)
        self._d_title.setWordWrap(True)
        self._d_title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._d_title.setStyleSheet(
            f"font-family: Electrolize, Consolas; font-size: 12pt; font-weight: bold;"
            f" color: {P.fg_bright}; background: transparent;")
        rl.addWidget(self._d_title)

        self._d_meta = QLabel("", right)
        self._d_meta.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; background: transparent;")
        rl.addWidget(self._d_meta)

        self._d_excerpts = QTextBrowser(right)
        self._d_excerpts.setOpenLinks(False)
        self._d_excerpts.setStyleSheet(
            f"QTextBrowser {{ background: {P.bg_card}; color: {P.fg};"
            f" border: 1px solid {P.border_card}; padding: 6px;"
            f" font-family: Consolas; font-size: 9pt; }}")
        rl.addWidget(self._d_excerpts, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self._full_btn = _btn("Full text")
        self._full_btn.setVisible(False)
        self._full_btn.clicked.connect(self._toggle_full_text)
        row.addWidget(self._full_btn)
        self._open_btn = _btn("Open source ↗")
        self._open_btn.setEnabled(False)
        self._open_btn.clicked.connect(self._open_source)
        row.addWidget(self._open_btn)
        rl.addLayout(row)

        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        self._stack.addWidget(split)
        self._show_placeholder()

    def _build_reports(self) -> None:
        """Monthly Reports tab: every report, newest first, and a reader for the whole text."""
        split = QSplitter(Qt.Horizontal, self)
        split.setChildrenCollapsible(False)

        left = QWidget(split)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(10, 8, 4, 8)
        ll.setSpacing(4)
        self._rtable = QTableWidget(left)
        self._rtable.setColumnCount(3)
        self._rtable.setHorizontalHeaderLabels(["Month", "Series", "Title"])
        self._rtable.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._rtable.setSelectionMode(QAbstractItemView.SingleSelection)
        self._rtable.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._rtable.setAlternatingRowColors(True)
        self._rtable.setWordWrap(False)
        self._rtable.verticalHeader().setVisible(False)
        hh = self._rtable.horizontalHeader()
        hh.setStretchLastSection(True)
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._rtable.itemSelectionChanged.connect(self._on_report_selected)
        self._rtable.itemDoubleClicked.connect(lambda _i: self._open_report_source())
        ll.addWidget(self._rtable, 1)
        rrow = QHBoxLayout()
        self._reports_all_btn = _btn("Download all")
        self._reports_all_btn.setToolTip(
            "Fetch every Monthly Report the archive does not have yet from robertsspaceindustries.com,\n"
            "so they read instantly and turn up in Search. Kept on this PC; a few minutes, once.")
        self._reports_all_btn.clicked.connect(lambda: self._toggle_bulk("reports"))
        rrow.addWidget(self._reports_all_btn)
        rrow.addStretch(1)
        ll.addLayout(rrow)

        right = QWidget(split)
        rl = QVBoxLayout(right)
        rl.setContentsMargins(4, 8, 10, 8)
        rl.setSpacing(6)
        self._r_title = QLabel("", right)
        self._r_title.setWordWrap(True)
        self._r_title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._r_title.setStyleSheet(
            f"font-family: Electrolize, Consolas; font-size: 12pt; font-weight: bold;"
            f" color: {P.fg_bright}; background: transparent;")
        rl.addWidget(self._r_title)
        self._r_meta = QLabel("", right)
        self._r_meta.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; background: transparent;")
        rl.addWidget(self._r_meta)
        self._r_text = QTextBrowser(right)
        self._r_text.setOpenLinks(False)
        self._r_text.setStyleSheet(
            f"QTextBrowser {{ background: {P.bg_card}; color: {P.fg};"
            f" border: 1px solid {P.border_card}; padding: 8px;"
            f" font-family: Consolas; font-size: 9pt; }}")
        rl.addWidget(self._r_text, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        self._r_open_btn = _btn("Open on RSI ↗")
        self._r_open_btn.setEnabled(False)
        self._r_open_btn.clicked.connect(self._open_report_source)
        row.addWidget(self._r_open_btn)
        rl.addLayout(row)

        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        self._stack.addWidget(split)
        self._r_text.setHtml(f'<p style="color:{P.fg_dim};">Loading the list of Monthly Reports…</p>')

    def _build_devtracker(self) -> None:
        """Dev Tracker tab: CIG posts on Spectrum, newest first; the archive's plus the live Devtracker."""
        split = QSplitter(Qt.Horizontal, self)
        split.setChildrenCollapsible(False)

        left = QWidget(split)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(10, 8, 4, 8)
        ll.setSpacing(4)
        self._dmodel = LazyTableModel([
            Column("Date", lambda p: p.get("date") or "—"),
            Column("Dev", lambda p: p.get("author") or "?", color=lambda p: DEVPOST_COLOR, bold=True),
            Column("Forum", lambda p: p.get("category") or ""),
            Column("Thread", lambda p: p.get("thread") or "(Spectrum post)",
                   tooltip=lambda p: f"{p.get('thread', '')}\n\n{p.get('teaser', '')}"),
        ], batch=200, parent=self)
        self._dtable = _lazy_view(left, self._dmodel, (100, 150, 130))
        self._dtable.selectionModel().currentRowChanged.connect(lambda cur, _prev: self._on_devpost_selected())
        self._dtable.doubleClicked.connect(lambda _i: self._open_devpost_source())
        ll.addWidget(self._dtable, 1)
        brow = QHBoxLayout()
        brow.addWidget(self._dev_refresh_btn)
        brow.addWidget(self._dev_older_btn)
        self._dev_history_btn = _btn("Load full history")
        self._dev_history_btn.setToolTip(
            "Fetch the whole Devtracker, back to Spectrum's launch in February 2017 (about 900 pages).\n"
            "Takes several minutes; kept on this PC and searchable in Search. Stops and resumes safely.")
        self._dev_history_btn.clicked.connect(lambda: self._toggle_bulk("history"))
        brow.addWidget(self._dev_history_btn)
        brow.addStretch(1)
        ll.addLayout(brow)

        right = QWidget(split)
        rl = QVBoxLayout(right)
        rl.setContentsMargins(4, 8, 10, 8)
        rl.setSpacing(6)
        self._dv_title = QLabel("", right)
        self._dv_title.setWordWrap(True)
        self._dv_title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._dv_title.setStyleSheet(
            f"font-family: Electrolize, Consolas; font-size: 12pt; font-weight: bold;"
            f" color: {P.fg_bright}; background: transparent;")
        rl.addWidget(self._dv_title)
        self._dv_meta = QLabel("", right)
        self._dv_meta.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; background: transparent;")
        rl.addWidget(self._dv_meta)
        self._dv_text = QTextBrowser(right)
        self._dv_text.setOpenLinks(False)
        self._dv_text.setStyleSheet(
            f"QTextBrowser {{ background: {P.bg_card}; color: {P.fg};"
            f" border: 1px solid {P.border_card}; padding: 8px;"
            f" font-family: Consolas; font-size: 9pt; }}")
        rl.addWidget(self._dv_text, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        self._dv_open_btn = _btn("Open on Spectrum ↗")
        self._dv_open_btn.setEnabled(False)
        self._dv_open_btn.clicked.connect(self._open_devpost_source)
        row.addWidget(self._dv_open_btn)
        rl.addLayout(row)

        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        self._stack.addWidget(split)
        self._dv_text.setHtml(f'<p style="color:{P.fg_dim};">Loading the Dev Tracker…</p>')

    def _build_footer(self) -> None:
        self._attribution = QLabel(ATTRIBUTION, self)
        self._attribution.setWordWrap(True)
        self._attribution.setStyleSheet(
            f"font-family: Consolas; font-size: 7pt; color: {P.fg_dim};"
            f" background: {P.bg_header}; padding: 5px 12px;"
            f" border-top: 1px solid {P.border};")
        self.content_layout.addWidget(self._attribution)

    # ══ Background task plumbing ═════════════════════════════════════════════

    def _spawn(self, fn: Callable[[Task], object], on_ok, on_fail,
               on_progress=None) -> Task:
        task = Task(fn, ref=self._ref)
        task.succeeded.connect(on_ok)
        task.failed.connect(on_fail)
        if on_progress is not None:
            task.progress.connect(on_progress)
        task.finished.connect(lambda t=task: self._tasks.discard(t))
        self._tasks.add(task)
        task.start()
        return task

    def _shutdown_tasks(self) -> None:
        """Network calls cannot be interrupted; give them a moment, then stop them
        so no QThread is destroyed while running (a fatal Qt error)."""
        for t in list(self._tasks):
            if t.isRunning() and not t.wait(1500):
                t.terminate()
                t.wait(500)

    # ══ Index ════════════════════════════════════════════════════════════════

    def _start_index_load(self) -> None:
        if self._index_task is not None and self._index_task.isRunning():
            return
        state = self._service.index_state()
        if state == "missing":
            self._set_banner(
                f"Downloading the search index ({INDEX_SIZE_HINT}) from GitHub, branch "
                f"'{self._ref}'. First run only; after this, search works offline.",
                "info", busy=True)
            self._status.setText("downloading index…")
        elif state == "stale":
            self._set_banner("Checking GitHub for an updated search index…", "info", busy=True)
            self._status.setText("updating index…")
        else:
            self._banner.hide()
            self._status.setText("loading index…")

        svc = self._service

        def job(task: Task) -> dict:
            svc.progress_cb = lambda got, total: task.progress.emit(got, total)
            try:
                return svc.load_index()
            finally:
                svc.progress_cb = None

        self._index_task = self._spawn(job, self._on_index_loaded, self._on_index_failed,
                                       self._on_index_progress)

    def _on_index_progress(self, got: int, total: int) -> None:
        if total > 0:
            self._progress.setRange(0, total)
            self._progress.setValue(min(got, total))
            self._status.setText(f"downloading index… {got / 1e6:.1f} / {total / 1e6:.1f} MB")
        else:
            self._status.setText(f"downloading index… {got / 1e6:.1f} MB")

    def _on_index_loaded(self, meta: dict) -> None:
        self._index_ready = True
        self._n_docs = int(meta.get("n_docs", 0))
        self._banner.hide()
        self._status.setText(f"{self._n_docs:,} records · branch {self._ref}")
        self._load_reports()
        self._load_devposts()
        if self._stack.currentIndex() == TAB_DEVTRACKER:
            self._fetch_devtracker(older=False)
        q = self._pending_query or self._search.text().strip()
        self._pending_query = ""
        if q:
            self._on_search(q)

    def _on_index_failed(self, message: str) -> None:
        self._index_ready = False
        offline = message.startswith(("Could not reach", "Timed out"))
        prefix = "OFFLINE" if offline else "CAN'T LOAD THE INDEX"
        self._set_banner(
            f"{prefix} — {message}  The search index ({INDEX_SIZE_HINT}) has to be downloaded "
            f"once; after that, search works without a connection.",
            "error", busy=False, retry=True)
        self._status.setText("no index")

    # ══ Search ═══════════════════════════════════════════════════════════════

    def _on_search(self, text: str, keep_limit: bool = False) -> None:
        """Every match, ranked. The list streams them in as you scroll (see LazyTableModel).
        ``keep_limit``: re-run the same query and keep the selected result (after new text was merged)."""
        text = (text or "").strip()
        keep_id = (self._current_doc or {}).get("id") if keep_limit and text == self._query else None
        self._query = text
        if not text:
            self._fill_table([])
            self._show_placeholder()
            if self._index_ready:
                self._set_search_status(f"{self._n_docs:,} records · branch {self._ref}")
            return
        if not self._index_ready:
            self._pending_query = text
            if self._index_task is None or not self._index_task.isRunning():
                self._start_index_load()
            return
        phrase = self._phrase_box.isChecked()
        try:
            results = self._service.phrase_search(text) if phrase else self._service.search(text, None)
            total = len(results)
        except Exception as exc:   # index is in memory; this should not fail, but never crash
            log.exception("dev_history: search failed")
            self._set_search_status(f"search failed: {exc}")
            return
        self._fill_table(results)
        if results:
            self._set_search_status(f"{total:,} match{'es' if total != 1 else ''}"
                                    + (" · phrase" if phrase else ""))
            row = self._model.find(lambda d: d.get("id") == keep_id) if keep_id else -1
            self._select(self._table, self._model, max(row, 0), notify=row < 0)
            if phrase:
                self._start_phrase_rank(text, results)
        else:
            self._set_search_status("no matches")
            if phrase:
                words = [w for w, _t in self._service.phrase_concepts(text)]
                self._show_placeholder(
                    f"Nothing in the dev history mentions all of: {', '.join(words) or text}. "
                    f"Try fewer words, or untick “Phrase”.")
            else:
                self._show_placeholder(f"Nothing in the dev history matches “{text}”.")

    def _on_phrase_toggled(self, on: bool) -> None:
        s = st.load_settings()
        s["phrase"] = bool(on)
        st.save_settings(s)
        self._search.setPlaceholderText(
            "Search by phrase… e.g. Kraken land on planets" if on else
            "Search dev history… e.g. quantum drive, laser head, server meshing")
        if self._query:
            self._on_search(self._query)

    def _start_phrase_rank(self, query: str, results: list) -> None:
        """Check the top results on their text and bring the ones that say the phrase together to the top."""
        self._phrase_seq += 1
        seq = self._phrase_seq
        svc = self._service
        self._spawn(lambda _t: svc.phrase_rank(results, query, 40),
                    lambda ranked, s=seq, q=query: self._on_phrase_ranked(s, q, ranked),
                    lambda _msg: None)

    def _on_phrase_ranked(self, seq: int, query: str, ranked: list) -> None:
        if seq != self._phrase_seq or query != self._query or not self._phrase_box.isChecked():
            return                      # the user searched for something else meanwhile
        keep_id = (self._current_doc or {}).get("id")
        at_top = self._table.currentIndex().row() <= 0
        self._fill_table(ranked)
        n_near = sum(1 for d in ranked if d.get("near") and d["near"] <= PHRASE_NEAR)
        self._set_search_status(f"{len(ranked):,} matches · phrase" + (f" · {n_near} ◆" if n_near else ""))
        if at_top:
            self._select(self._table, self._model, 0)        # show the best phrase match
        else:
            row = self._model.find(lambda d: d.get("id") == keep_id)
            self._select(self._table, self._model, max(row, 0), notify=False)

    def _terms(self, query: str) -> list:
        """Words to highlight: the phrase's word forms and synonyms in phrase mode, else the query words."""
        if self._phrase_box.isChecked():
            return self._service.phrase_terms(query)
        return self._service.query_terms(query)

    def _fill_table(self, results: list[dict]) -> None:
        self._table.selectionModel().blockSignals(True)
        self._model.set_rows(results)
        self._table.selectionModel().blockSignals(False)
        self._table.scrollToTop()

    @staticmethod
    def _select(view: QTableView, model: LazyTableModel, row: int, notify: bool = True) -> None:
        """Select *row* (loading rows up to it if it is not on screen yet) and scroll to it.
        ``notify=False`` keeps the detail pane as it is (the same item stays selected)."""
        if row < 0 or row >= model.total():
            return
        model.ensure_shown(row)
        sm = view.selectionModel()
        unchanged = sm.currentIndex().row() == row
        if not notify:
            sm.blockSignals(True)
        view.selectRow(row)
        view.scrollTo(model.index(row, 0))
        if not notify:
            sm.blockSignals(False)
        elif unchanged:
            # selectRow() does not re-signal an unchanged current row: tell the owner ourselves.
            sm.currentRowChanged.emit(model.index(row, 0), model.index(row, 0))

    # ══ Detail / excerpts ════════════════════════════════════════════════════

    def _show_placeholder(self, text: str = "") -> None:
        self._current_doc = None
        self._d_title.setText("")
        self._d_meta.setText("")
        self._open_btn.setEnabled(False)
        self._full_btn.setVisible(False)
        msg = text or ("Type to search CIG's dev-video transcripts, RSI comm-links (every Monthly Report "
                       "included) and developer posts from the Spectrum Devtracker. Select a result to see "
                       "where it matched.")
        self._d_excerpts.setHtml(f'<p style="color:{P.fg_dim};">{html.escape(msg)}</p>')

    def _on_selection_changed(self) -> None:
        doc = self._model.row(self._table.currentIndex().row())
        if isinstance(doc, dict):
            self._show_doc(doc)

    def _prefetch_next(self) -> None:
        """Quietly fetch the texts of the next few results, so stepping through the list is instant."""
        row = self._table.currentIndex().row()
        nxt = [self._model.row(i) for i in range(row + 1, row + 4)]
        nxt = [d for d in nxt if d and d.get("p")]
        busy = getattr(self, "_prefetch_task", None)
        if nxt and not (busy is not None and busy.isRunning()):
            svc = self._service
            self._prefetch_task = self._spawn(lambda _t: svc.prefetch(nxt), lambda _n: None, lambda _msg: None)

    def _show_doc(self, doc: dict) -> None:
        QTimer.singleShot(300, self._prefetch_next)
        self._current_doc = doc
        self._d_title.setText(doc.get("t") or "(untitled)")
        n_terms = len(set(self._service.query_terms(self._query))) or 1
        who = ""
        if doc.get("k") == "d":
            who = "  ·  ".join(x for x in (doc.get("a"), doc.get("c")) if x) + "  ·  "
        if "near" in doc:           # phrase mode
            near = doc.get("near")
            how = (f"said together (within {near} words)" if near and near <= PHRASE_NEAR else
                   f"all words, {near} words apart at the closest" if near else
                   "all words appear" if near is None else "all words, not in one passage")
            self._d_meta.setText(f"{doc.get('d') or 'undated'}  ·  {_kind_label(doc)}  ·  {who}{how}")
        else:
            self._d_meta.setText(
                f"{doc.get('d') or 'undated'}  ·  {_kind_label(doc)}  ·  {who}"
                f"matched {doc.get('matched', 0)}/{n_terms} term(s)  ·  score {doc.get('score', 0)}")
        self._open_btn.setEnabled(bool(doc.get("url")))
        self._showing_full = False
        self._full_btn.setText("Full text")
        self._full_btn.setVisible(doc.get("k") in ("c", "d", "s", "b"))

        self._excerpt_seq += 1
        seq = self._excerpt_seq
        query = self._query
        n = self._settings["excerpts"]
        svc = self._service

        if doc.get("k") == "c" and not doc.get("p"):
            # Not archived yet: fetch the article from RSI and show a digest of what it says.
            self._d_excerpts.setHtml(_label("RSI TEASER") + render_text(doc.get("s", "")) +
                                     _note("Fetching the article to summarize it…"))
            terms = self._terms(query)
            self._spawn(
                lambda _t: svc.article(doc),
                lambda art, s=seq: self._on_article(s, art, terms, full=False),
                lambda msg, s=seq, d=doc: self._on_article_failed(s, d, msg),
            )
            return

        waiting = ("Fetching this text from GitHub…" if doc.get("p") else "Loading summary…")
        self._d_excerpts.setHtml(f'<p style="color:{P.fg_dim};">{waiting}</p>')

        self._spawn(
            lambda _t: ((svc.phrase_excerpts(doc, query, n) if "near" in doc else svc.excerpts(doc, query, n)),
                        svc.article(doc)["summary"] if doc.get("k") == "c" and not doc.get("sd") else ""),
            lambda res, s=seq, d=doc, q=query: self._on_excerpts(s, d, q, res[0], res[1]),
            lambda msg, s=seq: self._on_excerpts_failed(s, msg),
        )

    def _on_excerpts(self, seq: int, doc: dict, query: str, lines: list, digest: str = "") -> None:
        if seq != self._excerpt_seq:
            return                      # user already moved to another result
        terms = self._terms(query)
        summary = digest or (doc.get("s", "") if doc.get("k") in ("c", "s", "b") else "")
        if not lines and summary:
            self._d_excerpts.setHtml(_label("SUMMARY") + render_text(summary, terms) +
                                     _note("No line of the text contains the search words — the match came "
                                           "from the title."))
            return
        if not lines:
            if doc.get("p"):
                what = ("No line of the text contains the search words — the match came from the "
                        "title or metadata.")
            else:
                what = f"No summary for this {kind_label(doc)}."
            self._d_excerpts.setHtml(f'<p style="color:{P.fg_dim};">{html.escape(what)}</p>')
            return
        if not doc.get("p"):
            self._d_excerpts.setHtml(_label("SUMMARY") + render_text("\n".join(map(str, lines)), terms))
            return
        parts = []
        if summary:
            parts.append(_label("SUMMARY") + render_text(summary, terms))
        parts.append(_label("WHERE IT'S SAID" if "near" in doc else "MATCHING EXCERPTS"))
        for line in lines:
            parts.append(
                f'<p style="margin: 0 0 10px 0; color:{P.fg};">'
                f'<span style="color:{P.fg_dim};">“</span>{highlight(str(line), terms)}'
                f'<span style="color:{P.fg_dim};">”</span></p>')
        self._d_excerpts.setHtml("".join(parts))

    def _on_excerpts_failed(self, seq: int, message: str) -> None:
        if seq != self._excerpt_seq:
            return
        self._d_excerpts.setHtml(
            f'<p style="color:{P.red};">Could not load excerpts.</p>'
            f'<p style="color:{P.fg_dim};">{html.escape(message)}</p>'
            f'<p style="color:{P.fg_dim};">The source link still works — use '
            f'<b>Open source</b>.</p>')

    def _on_article(self, seq: int, art: dict, terms: list, full: bool) -> None:
        if seq != self._excerpt_seq:
            return
        body = article_html(art, terms, full=full)
        if not full and art.get("text"):
            body += _note("Press <b>Full text</b> to read the whole article.")
        self._d_excerpts.setHtml(body or _note("RSI returned no text for this one. Use <b>Open source</b>."))

    def _on_article_failed(self, seq: int, doc: dict, message: str) -> None:
        if seq != self._excerpt_seq:
            return
        self._d_excerpts.setHtml(
            _label("RSI TEASER") + render_text(doc.get("s", "")) +
            f'<p style="color:{P.red};">Could not fetch the article to summarize it.</p>'
            f'<p style="color:{P.fg_dim};">{html.escape(message)}</p>')

    def _open_source(self) -> None:
        doc = self._current_doc
        if doc and doc.get("url"):
            QDesktopServices.openUrl(QUrl(doc["url"]))

    def _toggle_full_text(self) -> None:
        """Comm-links and dev posts: swap the excerpts for the whole article, and back."""
        doc = self._current_doc
        if not doc:
            return
        if self._showing_full:
            self._show_doc(doc)
            return
        self._showing_full = True
        self._full_btn.setText("Summary" if doc.get("k") == "c" and not doc.get("p") else "Excerpts")
        self._d_excerpts.setHtml(f'<p style="color:{P.fg_dim};">Fetching the full text…</p>')
        self._excerpt_seq += 1
        seq = self._excerpt_seq
        terms = self._terms(self._query)
        svc = self._service
        if doc.get("k") == "c":
            self._spawn(
                lambda _t: svc.article(doc),
                lambda art, s=seq: self._on_article(s, art, terms, full=True),
                lambda msg, s=seq, d=doc: self._on_article_failed(s, d, msg),
            )
            return
        if doc.get("k") == "d":
            post = {"id": doc.get("id", ""), "p": doc.get("p", ""), "teaser": doc.get("s", ""),
                    "slug": doc.get("slug", ""), "reply_id": doc.get("reply_id", ""), "thread": doc.get("t", "")}
            self._spawn(
                lambda _t: svc.devpost_text(post)["text"],
                lambda text, s=seq: self._on_full_text(s, text, terms),
                lambda msg, s=seq: self._on_excerpts_failed(s, msg),
            )
            return
        self._spawn(
            lambda _t: svc.full_text(doc),
            lambda text, s=seq: self._on_full_text(s, text, terms),
            lambda msg, s=seq: self._on_excerpts_failed(s, msg),
        )

    def _on_full_text(self, seq: int, text: str, terms: list) -> None:
        if seq != self._excerpt_seq:
            return
        self._d_excerpts.setHtml(render_text(text, terms) or
                                 f'<p style="color:{P.fg_dim};">No full text for this one yet.</p>')

    # ══ Tabs ═════════════════════════════════════════════════════════════════

    def _switch_tab(self, tab: int, save: bool = True) -> None:
        self._tabs.button(tab).setChecked(True)
        self._stack.setCurrentIndex(tab)
        on_search = tab == TAB_SEARCH
        self._search.setVisible(on_search)
        self._report_filters.setVisible(tab == TAB_REPORTS)
        self._dev_filters.setVisible(tab == TAB_DEVTRACKER)
        if on_search:
            self._search.setFocus()
        if tab == TAB_DEVTRACKER and self._index_ready and not self._dev_refreshed:
            self._fetch_devtracker(older=False)
        self._refresh_status()
        if save:
            s = st.load_settings()
            s["tab"] = _TAB_KEYS[tab]
            st.save_settings(s)

    def _refresh_status(self) -> None:
        if not self._index_ready:
            return                      # the index load owns the status line until it finishes
        if self._stack.currentIndex() == TAB_REPORTS:
            n_full = sum(1 for r in self._reports if r.get("have"))
            shown, total = len(self._filtered_reports()), len(self._reports)
            self._status.setText(f"{shown} of {total} reports" if shown != total else f"{total} reports")
            self._status.setToolTip(f"{n_full} of {total} Monthly Reports are on hand (archive or downloaded) "
                                    f"and searchable; the rest are fetched from RSI when you open them, or all at "
                                    f"once with Download all.")
        elif self._stack.currentIndex() == TAB_DEVTRACKER:
            shown, total = len(self._filtered_devposts()), len(self._dev_posts)
            busy = " · fetching…" if self._dev_task is not None and self._dev_task.isRunning() else ""
            self._status.setText((f"{shown} of {total} posts" if shown != total else f"{total} posts") + busy)
            self._status.setToolTip("Dev posts from the archive plus the ones fetched live from the RSI Devtracker.")
        else:
            self._status.setText(self._search_status or f"{self._n_docs:,} records · branch {self._ref}")

    def _set_search_status(self, text: str) -> None:
        self._search_status = text
        if self._stack.currentIndex() == TAB_SEARCH:
            self._status.setText(text)

    # ══ Monthly Reports ══════════════════════════════════════════════════════

    def _load_reports(self) -> None:
        """In-memory over the loaded index: no network, safe on the UI thread."""
        try:
            self._reports = self._service.monthly_reports()
        except Exception:                # the index is loaded, so this should not fail; never crash on it
            log.exception("dev_history: listing monthly reports failed")
            self._reports = []
        years = sorted({r["month"][:4] for r in self._reports if r.get("month")}, reverse=True)
        self._year_box.blockSignals(True)
        current = self._year_box.currentText()
        self._year_box.clear()
        self._year_box.addItem("All years")
        self._year_box.addItems(years)
        i = self._year_box.findText(current)
        self._year_box.setCurrentIndex(max(0, i))
        self._year_box.blockSignals(False)
        self._fill_reports()

    def _filtered_reports(self) -> list[dict]:
        series = self._series_box.currentText()
        year = self._year_box.currentText()
        words = [w for w in self._report_find.text().lower().split() if w]
        out = []
        for r in self._reports:
            if series != "All series" and r.get("series") != series:
                continue
            if year != "All years" and not r.get("month", "").startswith(year):
                continue
            if words and not all(w in (r.get("t") or "").lower() for w in words):
                continue
            out.append(r)
        return out

    def _fill_reports(self) -> None:
        rows = self._filtered_reports()
        keep = (self._report_doc or {}).get("id")
        self._rtable.blockSignals(True)
        self._rtable.clearContents()
        self._rtable.setRowCount(len(rows))
        bold = QFont("Consolas")
        bold.setBold(True)
        select = -1
        for row, r in enumerate(rows):
            month = QTableWidgetItem(_month_label(r.get("month", "")))
            month.setData(_ROLE_DOC, r)
            series = QTableWidgetItem(r.get("series", ""))
            series.setFont(bold)
            series.setForeground(QColor(SERIES_COLOR.get(r.get("series", ""), ACCENT)))
            title = QTableWidgetItem(r.get("t") or "(untitled)")
            title.setToolTip(r.get("t") or "")
            if not r.get("have"):
                title.setForeground(QColor(P.fg_dim))
                title.setToolTip((r.get("t") or "") + "\n(not downloaded yet: fetched from RSI when opened)")
            self._rtable.setItem(row, 0, month)
            self._rtable.setItem(row, 1, series)
            self._rtable.setItem(row, 2, title)
            if r.get("id") == keep:
                select = row
        self._rtable.blockSignals(False)
        self._refresh_status()
        if not rows:
            self._report_doc = None
            self._r_title.setText("")
            self._r_meta.setText("")
            self._r_open_btn.setEnabled(False)
            msg = ("No Monthly Reports match these filters." if self._reports
                   else "No Monthly Reports in the index yet.")
            self._r_text.setHtml(f'<p style="color:{P.fg_dim};">{msg}</p>')
            return
        self._rtable.selectRow(select if select >= 0 else 0)
        if select < 0:
            self._on_report_selected()

    def _on_report_selected(self) -> None:
        sel = self._rtable.selectionModel().selectedRows()
        if not sel:
            return
        item = self._rtable.item(sel[0].row(), 0)
        r = item.data(_ROLE_DOC) if item else None
        if not isinstance(r, dict):
            return
        self._report_doc = r
        self._r_title.setText(r.get("t") or "(untitled)")
        self._r_meta.setText(f"{r.get('series', '')}  ·  covers {_month_label(r.get('month', ''))}  ·  "
                             f"published {r.get('d') or 'undated'}")
        self._r_open_btn.setEnabled(bool(r.get("url")))
        self._report_seq += 1
        seq = self._report_seq
        where = "GitHub" if r.get("p") else "robertsspaceindustries.com"
        self._r_text.setHtml(f'<p style="color:{P.fg_dim};">Fetching this report from {where}…</p>')
        svc = self._service
        self._spawn(
            lambda _t: svc.article(r),
            lambda art, s=seq, d=r: self._on_report_text(s, d, art),
            lambda msg, s=seq, d=r: self._on_report_failed(s, d, msg),
        )

    def _on_report_text(self, seq: int, r: dict, art: dict) -> None:
        if seq != self._report_seq:
            return                      # the user already picked another report
        body = article_html(art)
        if not art.get("text"):
            body = (_label("RSI TEASER") + render_text(r.get("s", "")) +
                    _note("RSI returned no text for this report. Use <b>Open on RSI</b>."))
        self._r_text.setHtml(body)
        self._r_text.verticalScrollBar().setValue(0)

    def _on_report_failed(self, seq: int, r: dict, message: str) -> None:
        if seq != self._report_seq:
            return
        self._r_text.setHtml(_label("RSI TEASER") + render_text(r.get("s", "")) +
                             f'<p style="color:{P.red};">Could not load the report.</p>'
                             f'<p style="color:{P.fg_dim};">{html.escape(message)}</p>')

    def _open_report_source(self) -> None:
        r = self._report_doc
        if r and r.get("url"):
            QDesktopServices.openUrl(QUrl(r["url"]))

    # ══ Bulk downloads (all Monthly Reports / whole Devtracker) ═══════════════

    def _toggle_bulk(self, kind: str) -> None:
        """Start a bulk download, or stop the one running."""
        if self._bulk_task is not None and self._bulk_task.isRunning():
            self._bulk_stop.set()
            btn = self._reports_all_btn if self._bulk_kind == "reports" else self._dev_history_btn
            btn.setText("Stopping…")
            btn.setEnabled(False)
            return
        if not self._index_ready or not self._settings["live_rsi"]:
            self._status.setText("live RSI fetching is off (\"live_rsi\" in settings.json)"
                                 if self._index_ready else "the index is still loading")
            return
        self._bulk_stop = threading.Event()
        self._bulk_kind = kind
        stop = self._bulk_stop.is_set
        svc = self._service
        if kind == "reports":
            fn = lambda t: svc.fetch_all_reports(lambda a, b: t.progress.emit(a, b), stop)  # noqa: E731
            self._reports_all_btn.setText("Stop")
        else:
            fn = lambda t: svc.fetch_tracker_history(lambda a, b: t.progress.emit(a, b), stop)  # noqa: E731
            self._dev_history_btn.setText("Stop")
            self._dev_refresh_btn.setEnabled(False)
            self._dev_older_btn.setEnabled(False)
        self._bulk_task = self._spawn(fn, self._on_bulk_done, self._on_bulk_failed, self._on_bulk_progress)

    def _on_bulk_progress(self, done: int, total: int) -> None:
        what = "Monthly Reports" if self._bulk_kind == "reports" else "Devtracker pages"
        self._status.setText(f"downloading {what}… {done:,} / {total:,}")
        self._status.setToolTip("Press Stop to pause; it resumes where it left off next time.")
        if self._bulk_kind == "history" and done % 10 == 0:
            self._load_devposts()       # let the list grow while it runs

    def _bulk_finished(self) -> None:
        self._reports_all_btn.setText("Download all")
        self._reports_all_btn.setEnabled(True)
        self._dev_history_btn.setText("Load full history")
        self._dev_history_btn.setEnabled(True)
        self._dev_refresh_btn.setEnabled(True)
        self._dev_older_btn.setEnabled(True)
        self._load_reports()
        self._load_devposts()
        if self._query:
            self._on_search(self._query, keep_limit=True)

    def _on_bulk_done(self, res: dict) -> None:
        kind = self._bulk_kind
        self._bulk_finished()
        if kind == "reports":
            msg = f"Monthly Reports: {res.get('fetched', 0)} downloaded"
            if res.get("failed"):
                msg += f", {res['failed']} failed"
            if res.get("remaining"):
                msg += f", {res['remaining']} left (press Download all to continue)"
        else:
            msg = (f"Devtracker: {res.get('new', 0):,} new posts from {res.get('pages', 0)} pages"
                   + (" · full history loaded" if res.get("done") else " · paused (press again to continue)"))
        self._status.setText(msg)
        self._status.setToolTip(msg)

    def _on_bulk_failed(self, message: str) -> None:
        self._bulk_finished()
        self._status.setText("download stopped: could not reach RSI")
        self._status.setToolTip(message + "\nWhat was fetched is kept; press the button again to resume.")

    # ══ Dev Tracker ══════════════════════════════════════════════════════════

    def _load_devposts(self) -> None:
        """Archive posts (in the loaded index) plus live ones kept on this PC. No network."""
        try:
            self._dev_posts = self._service.devtracker_posts()
        except Exception:                # index is loaded; a bad local cache must not break the window
            log.exception("dev_history: listing dev posts failed")
            self._dev_posts = []
        for box, key, first in ((self._dev_author_box, "author", "All devs"),
                                (self._dev_forum_box, "category", "All forums")):
            current = box.currentText()
            values = sorted({p.get(key) or "" for p in self._dev_posts} - {""}, key=str.lower)
            box.blockSignals(True)
            box.clear()
            box.addItem(first)
            box.addItems(values)
            box.setCurrentIndex(max(0, box.findText(current)))
            box.blockSignals(False)
        self._fill_devposts()

    def _filtered_devposts(self) -> list[dict]:
        author = self._dev_author_box.currentText()
        forum = self._dev_forum_box.currentText()
        words = [w for w in self._dev_find.text().lower().split() if w]
        out = []
        for p in self._dev_posts:
            if author != "All devs" and p.get("author") != author:
                continue
            if forum != "All forums" and p.get("category") != forum:
                continue
            hay = f"{p.get('thread', '')} {p.get('teaser', '')} {p.get('author', '')}".lower()
            if words and not all(w in hay for w in words):
                continue
            out.append(p)
        return out

    def _fill_devposts(self) -> None:
        rows = self._filtered_devposts()
        keep = (self._dev_post or {}).get("id")
        self._dtable.selectionModel().blockSignals(True)
        self._dmodel.set_rows(rows)
        self._dtable.selectionModel().blockSignals(False)
        select = self._dmodel.find(lambda p: p.get("id") == keep) if keep else -1
        self._refresh_status()
        if not rows:
            self._dev_post = None
            self._dv_title.setText("")
            self._dv_meta.setText("")
            self._dv_open_btn.setEnabled(False)
            if self._dev_posts:
                msg = "No dev posts match these filters."
            elif self._dev_task is not None and self._dev_task.isRunning():
                msg = "Fetching the newest posts from the RSI Devtracker…"
            else:
                msg = ("No dev posts yet. Press <b>Refresh</b> to fetch the newest ones from the RSI "
                       "Devtracker.")
            self._dv_text.setHtml(f'<p style="color:{P.fg_dim};">{msg}</p>')
            return
        self._select(self._dtable, self._dmodel, max(select, 0), notify=select < 0)

    def _fetch_devtracker(self, older: bool) -> None:
        """Newest page (Refresh) or the next older page (Load older), live from RSI, in a worker."""
        if self._dev_task is not None and self._dev_task.isRunning():
            return
        if not self._settings["live_rsi"]:
            self._dev_refreshed = True
            return
        if older and self._dev_page == 0:
            older = False               # nothing fetched yet this session: start from the top
        page = self._dev_page + 1 if older else 1
        day = self._dev_day if older else (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        svc = self._service
        self._dev_refresh_btn.setEnabled(False)
        self._dev_older_btn.setEnabled(False)
        self._dev_task = self._spawn(
            lambda _t: svc.devtracker_fetch(page, day),
            lambda res, pg=page: self._on_devtracker_fetched(pg, res),
            self._on_devtracker_failed,
        )
        if not self._dev_posts:
            self._dv_text.setHtml(f'<p style="color:{P.fg_dim};">Fetching the newest posts from the RSI '
                                  f'Devtracker…</p>')
        self._refresh_status()

    def _on_devtracker_fetched(self, page: int, res: dict) -> None:
        self._dev_refreshed = True
        self._dev_refresh_btn.setEnabled(True)
        self._dev_older_btn.setEnabled(bool(res.get("posts")))
        if res.get("posts") and page > self._dev_page:
            self._dev_page, self._dev_day = page, res.get("day") or self._dev_day
        self._load_devposts()

    def _on_devtracker_failed(self, message: str) -> None:
        self._dev_refreshed = True
        self._dev_refresh_btn.setEnabled(True)
        self._dev_older_btn.setEnabled(self._dev_page > 0)
        self._status.setText("Dev Tracker: could not reach RSI")
        self._status.setToolTip(message)
        if not self._dev_posts:
            self._dv_text.setHtml(f'<p style="color:{P.red};">Could not fetch the RSI Devtracker.</p>'
                                  f'<p style="color:{P.fg_dim};">{html.escape(message)}</p>')

    def _on_devpost_selected(self) -> None:
        p = self._dmodel.row(self._dtable.currentIndex().row())
        if not isinstance(p, dict):
            return
        self._dev_post = p
        self._dv_title.setText(p.get("thread") or "(Spectrum post)")
        self._dv_meta.setText("  ·  ".join(x for x in (p.get("author"), p.get("category"), p.get("date")) if x))
        self._dv_open_btn.setEnabled(bool(p.get("url")))
        self._dev_seq += 1
        seq = self._dev_seq
        self._dv_text.setHtml(render_text(p.get("teaser", "")) + _note("Fetching the full post…"))
        svc = self._service
        self._spawn(
            lambda _t: svc.devpost_text(p),
            lambda res, s=seq, d=p: self._on_devpost_text(s, d, res),
            lambda msg, s=seq, d=p: self._on_devpost_failed(s, d, msg),
        )

    def _on_devpost_text(self, seq: int, p: dict, res: dict) -> None:
        if seq != self._dev_seq:
            return
        body = render_text(res.get("text", ""))
        if res.get("private"):
            body += _note("This post is in a forum that needs an RSI login (e.g. Focus Testing), so only the "
                          "public Devtracker preview is shown. <b>Open on Spectrum</b> to read it signed in.")
        elif res.get("source") in _SOURCE_NOTE:
            body += _note(_SOURCE_NOTE[res["source"]].replace("Not in the archive yet: ", ""))
        self._dv_text.setHtml(body or _note("No text for this post."))
        self._dv_text.verticalScrollBar().setValue(0)

    def _on_devpost_failed(self, seq: int, p: dict, message: str) -> None:
        if seq != self._dev_seq:
            return
        self._dv_text.setHtml(render_text(p.get("teaser", "")) +
                              f'<p style="color:{P.red};">Could not fetch the full post.</p>'
                              f'<p style="color:{P.fg_dim};">{html.escape(message)}</p>')

    def _open_devpost_source(self) -> None:
        p = self._dev_post
        if p and p.get("url"):
            QDesktopServices.openUrl(QUrl(p["url"]))

    # ══ IPC / lifecycle (same contract as PlayTime) ══════════════════════════

    def handle_ipc_command(self, cmd: dict) -> None:
        t = cmd.get("type", "")
        if t == "show":
            self.showNormal()
            self.raise_()
            self.activateWindow()
            if self._stack.currentIndex() == TAB_SEARCH:
                self._search.setFocus()
        elif t == "hide":
            self.hide()
        elif t == "quit":
            self._quit()

    def _show_tutorial(self) -> None:
        """Show (or raise) the tutorial popup.

        Button-triggered only, never on first show -- the disclaimer below
        already owns that moment, and two things to read at once is one too
        many.
        """
        # Imported here so a tutorial import error can never stop the window
        # from opening.
        from ui.tutorial_popup import TutorialPopup
        TutorialPopup(self)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if getattr(self, "_disclaimer_pending", False):
            QTimer.singleShot(400, self._maybe_show_disclaimer)

    def _maybe_show_disclaimer(self) -> None:
        """Once per DISCLAIMER_VERSION, and only while the window is actually visible."""
        if not getattr(self, "_disclaimer_pending", False) or not self.isVisible():
            return
        self._disclaimer_pending = False
        from PySide6.QtWidgets import QMessageBox
        box = QMessageBox(self)
        box.setWindowTitle(DISCLAIMER_TITLE)
        box.setIcon(QMessageBox.Information)
        box.setTextFormat(Qt.RichText)
        box.setText(DISCLAIMER_TEXT)
        box.addButton("I understand", QMessageBox.AcceptRole)
        box.exec()
        s = st.load_settings()
        s["disclaimer_ack"] = DISCLAIMER_VERSION
        st.save_settings(s)

    def _on_close(self) -> None:
        if self._standalone:
            self._quit()
        else:
            self.user_close()

    def _quit(self) -> None:
        self._shutdown_tasks()
        app = QApplication.instance()
        if app:
            app.quit()

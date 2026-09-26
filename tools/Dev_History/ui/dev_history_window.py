"""Dev History — main window.

Search Star Citizen's development history (dev-video transcripts + RSI comm-links),
streamed from GitHub by the shared ``sc_dev_history`` engine.

Threading rule: anything that can touch the network runs in a ``Task`` (QThread):
the first index load (a ~3.5 MB download on first run) and each transcript fetch
for excerpts.  Searching itself is an in-memory lookup over the loaded index and
runs on the UI thread only once the index is in memory.
"""
from __future__ import annotations

import html
import logging
import os
import re
from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QHBoxLayout, QHeaderView, QLabel, QProgressBar,
    QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout,
    QWidget,
)

from shared.qt.base_window import SCWindow
from shared.qt.search_bar import SCSearchBar
from shared.qt.theme import P
from shared.qt.title_bar import SCTitleBar

from core import settings as st
from core.engine import ATTRIBUTION, INDEX_SIZE_HINT, DevHistoryService
from ui.workers import MainThreadGC, Task

log = logging.getLogger(__name__)

ACCENT = "#88aaff"          # matches skill.json "color"
VIDEO_COLOR = P.energy_cyan
COMMLINK_COLOR = P.yellow

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
    return "VIDEO" if doc.get("k") == "v" else "COMM-LINK"


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
    "development videos and RSI comm-links. Star Citizen and all of this content are &copy; Cloud Imperium "
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
        self._service = DevHistoryService(ref=self._ref)

        self._tasks: set[Task] = set()
        self._index_task: Optional[Task] = None
        self._index_ready = False
        self._n_docs = 0
        self._pending_query = ""
        self._query = ""
        self._current_doc: Optional[dict] = None
        self._excerpt_seq = 0

        # ── Title bar ──
        self._title_bar = SCTitleBar(
            window=self, title="DEV HISTORY", icon_text="\U0001f4dc", accent_color=ACCENT,
            hotkey_text=hotkey_text, show_minimize=True,
        )
        self._title_bar.minimize_clicked.connect(self.showMinimized)
        self._title_bar.close_clicked.connect(self._on_close)
        self.content_layout.addWidget(self._title_bar)

        self._build_controls()
        self._build_banner()
        self._build_body()
        self._build_footer()

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

        self._search = SCSearchBar(
            placeholder="Search dev history… e.g. quantum drive, laser head, server meshing",
            debounce_ms=350, parent=bar)
        self._search.search_changed.connect(self._on_search)
        self._search.returnPressed.connect(lambda: self._on_search(self._search.text().strip()))
        lay.addWidget(self._search, 1)

        self._status = QLabel("", bar)
        self._status.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; background: transparent;")
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
        self._table = QTableWidget(left)
        self._table.setColumnCount(3)
        self._table.setHorizontalHeaderLabels(["Date", "Type", "Title"])
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setWordWrap(False)
        self._table.verticalHeader().setVisible(False)
        hh = self._table.horizontalHeader()
        hh.setStretchLastSection(True)
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        self._table.itemDoubleClicked.connect(lambda _i: self._open_source())
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
        self._open_btn = _btn("Open source ↗")
        self._open_btn.setEnabled(False)
        self._open_btn.clicked.connect(self._open_source)
        row.addWidget(self._open_btn)
        rl.addLayout(row)

        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        self.content_layout.addWidget(split, 1)
        self._show_placeholder()

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

    def _on_search(self, text: str) -> None:
        text = (text or "").strip()
        self._query = text
        if not text:
            self._fill_table([])
            self._show_placeholder()
            if self._index_ready:
                self._status.setText(f"{self._n_docs:,} records · branch {self._ref}")
            return
        if not self._index_ready:
            self._pending_query = text
            if self._index_task is None or not self._index_task.isRunning():
                self._start_index_load()
            return
        try:
            results = self._service.search(text, self._settings["max_results"])
        except Exception as exc:   # index is in memory; this should not fail, but never crash
            log.exception("dev_history: search failed")
            self._status.setText(f"search failed: {exc}")
            return
        self._fill_table(results)
        if results:
            self._status.setText(f"{len(results)} result(s) · {self._n_docs:,} records")
            self._table.selectRow(0)
        else:
            self._status.setText(f"no matches · {self._n_docs:,} records")
            self._show_placeholder(f"Nothing in the dev history matches “{text}”.")

    def _fill_table(self, results: list[dict]) -> None:
        self._table.blockSignals(True)
        self._table.clearContents()
        self._table.setRowCount(len(results))
        bold = QFont("Consolas")
        bold.setBold(True)
        for row, doc in enumerate(results):
            date_item = QTableWidgetItem(doc.get("d") or "—")
            date_item.setData(_ROLE_DOC, doc)
            kind_item = QTableWidgetItem(_kind_label(doc))
            kind_item.setFont(bold)
            kind_item.setForeground(QColor(VIDEO_COLOR if doc.get("k") == "v" else COMMLINK_COLOR))
            kind_item.setTextAlignment(Qt.AlignCenter)
            title_item = QTableWidgetItem(doc.get("t") or "(untitled)")
            title_item.setToolTip(doc.get("t") or "")
            self._table.setItem(row, 0, date_item)
            self._table.setItem(row, 1, kind_item)
            self._table.setItem(row, 2, title_item)
        self._table.blockSignals(False)

    # ══ Detail / excerpts ════════════════════════════════════════════════════

    def _show_placeholder(self, text: str = "") -> None:
        self._current_doc = None
        self._d_title.setText("")
        self._d_meta.setText("")
        self._open_btn.setEnabled(False)
        msg = text or ("Type to search 1,300+ dev-video transcripts and 5,000+ RSI comm-links. "
                       "Select a result to see where it matched.")
        self._d_excerpts.setHtml(f'<p style="color:{P.fg_dim};">{html.escape(msg)}</p>')

    def _on_selection_changed(self) -> None:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return
        item = self._table.item(rows[0].row(), 0)
        doc = item.data(_ROLE_DOC) if item else None
        if not isinstance(doc, dict):
            return
        self._show_doc(doc)

    def _show_doc(self, doc: dict) -> None:
        self._current_doc = doc
        self._d_title.setText(doc.get("t") or "(untitled)")
        n_terms = len(set(self._service.query_terms(self._query))) or 1
        self._d_meta.setText(
            f"{doc.get('d') or 'undated'}  ·  {_kind_label(doc)}  ·  "
            f"matched {doc.get('matched', 0)}/{n_terms} term(s)  ·  score {doc.get('score', 0)}")
        self._open_btn.setEnabled(bool(doc.get("url")))

        is_video = doc.get("k") == "v"
        waiting = ("Fetching this transcript from GitHub…" if is_video
                   else "Loading summary…")
        self._d_excerpts.setHtml(f'<p style="color:{P.fg_dim};">{waiting}</p>')

        self._excerpt_seq += 1
        seq = self._excerpt_seq
        query = self._query
        n = self._settings["excerpts"]
        svc = self._service

        self._spawn(
            lambda _t: svc.excerpts(doc, query, n),
            lambda lines, s=seq, d=doc, q=query: self._on_excerpts(s, d, q, lines),
            lambda msg, s=seq: self._on_excerpts_failed(s, msg),
        )

    def _on_excerpts(self, seq: int, doc: dict, query: str, lines: list) -> None:
        if seq != self._excerpt_seq:
            return                      # user already moved to another result
        terms = self._service.query_terms(query)
        if not lines:
            what = ("No transcript line contains the search words — the match came from the "
                    "title or metadata.") if doc.get("k") == "v" else "No summary for this comm-link."
            self._d_excerpts.setHtml(f'<p style="color:{P.fg_dim};">{html.escape(what)}</p>')
            return
        heading = "MATCHING EXCERPTS" if doc.get("k") == "v" else "SUMMARY"
        parts = [f'<p style="color:{P.fg_dim}; font-size:7pt; letter-spacing:1px;">'
                 f'<b>{heading}</b></p>']
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

    def _open_source(self) -> None:
        doc = self._current_doc
        if doc and doc.get("url"):
            QDesktopServices.openUrl(QUrl(doc["url"]))

    # ══ IPC / lifecycle (same contract as PlayTime) ══════════════════════════

    def handle_ipc_command(self, cmd: dict) -> None:
        t = cmd.get("type", "")
        if t == "show":
            self.showNormal()
            self.raise_()
            self.activateWindow()
            self._search.setFocus()
        elif t == "hide":
            self.hide()
        elif t == "quit":
            self._quit()

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

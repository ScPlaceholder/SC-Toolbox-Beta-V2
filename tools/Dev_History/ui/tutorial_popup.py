"""Tutorial popup for Dev History.

Same chrome as the Craft Database / Star Map / Play Time popups: a frameless,
draggable, always-on-top tabbed dialog with corner brackets, opened from a
"Tutorial" button in the title bar.

Deliberately SHORT, and deliberately button-only. This tool already shows a
versioned disclaimer on first view (`DISCLAIMER_TEXT` / "I understand" in
dev_history_window.py), which covers provenance and the machine-made
transcripts. A second thing to read on first run would be one too many, so
this never auto-opens and does not repeat the disclaimer's argument -- it
covers the three things the disclaimer does not: the one-time download, that
double-clicking a row opens the source, and how the search actually matches.

Strings read out of the source:
  dev_history_window.py  the search placeholder, "Open source ↗", "Retry",
                         "Full text", the SEARCH / MONTHLY REPORTS / DEV
                         TRACKER tabs, "Refresh", "Load older",
                         the Date / Type / Title columns, the VIDEO,
                         COMM-LINK and DEV POST type labels, the
                         Month / Series / Title report columns, "Open on
                         RSI ↗", the status-line formats
  core/engine.py         INDEX_SIZE_HINT = "a few MB"; the index age
                         (sc_dev_history.INDEX_MAX_AGE_S, 12 hours)
  dev_history_app.py     hotkey_text = "Shift+H"
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QPainter, QColor, QPen
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)

from shared.qt.theme import P

TOOL_COLOR = "#88aaff"          # matches ACCENT in dev_history_window.py
POPUP_BRACKET_LEN = 14
CLOSE_BTN_BG = "rgba(255, 60, 60, 0.15)"
CLOSE_BTN_COLOR = "#cc6666"
CLOSE_BTN_HOVER_BG = "rgba(220, 50, 50, 0.85)"

_B = f"font-family: Consolas; color: {P.fg}; font-size: 9pt; line-height: 1.5;"
_DIM = f"color: {P.fg_dim};"
_ACC = f"color: {TOOL_COLOR};"
_YLW = f"color: {P.yellow};"

_C_START = TOOL_COLOR       # blue   — Searching
_C_READ = "#33dd88"         # green  — Reading a result
_C_TIPS = "#ffb347"         # amber  — Good to know

_FONT = "font-family: Electrolize, Consolas;"


def _h3(text: str, color: str) -> str:
    return f'<h3 style="{_FONT} color: {color};">{text}</h3>'


def _h4(text: str, color: str) -> str:
    return f'<h4 style="{_FONT} color: {color};">{text}</h4>'


def _html(body: str) -> str:
    return f'<div style="{_B}">{body}</div>'


_TAB_SEARCHING = _html(f"""
{_h3("Find out when CIG said it", _C_START)}
<p>Somebody insists a feature was promised, or cancelled, or reworked. This
searches the <b>transcripts of CIG's development videos</b>, the
<b>RSI comm-links</b> (every Monthly Report, in full) and <b>CIG developer
posts from the Spectrum Devtracker</b>, and shows you where it was actually
said &mdash; with a link to the original.</p>

{_h4("Just type", _C_START)}
<p>The box at the top is the whole interface. Type a subject the way you would
say it &mdash; <span style="{_ACC}">quantum drive</span>,
<span style="{_ACC}">laser head</span>,
<span style="{_ACC}">server meshing</span> &mdash; and results appear as you
go. Press <b>Enter</b> to search at once instead of waiting for the pause.</p>
<p>It matches on <b>words, not phrases</b>, and it ranks by how many of your
words a record contains. So more words narrow the results rather than breaking
them, and the status line tells you how many of your terms each hit
matched.</p>

{_h4("The results list", _C_START)}
{_h4("Search by phrase", _C_START)}
<p>Tick <b>Phrase</b> next to the search box and type what you are looking
for as a sentence: <span style="{_ACC}">Kraken land on planets</span>. It
finds records that mention <em>all</em> of it in any wording (land, landed,
landing; planet, planetside, surface, moon&hellip;), then checks the texts and
moves the ones that say it <b>together</b> to the top, marked
<b>&#9670;</b>. The panel shows the passage where it is said.</p>
<p><b>Every</b> match is listed, best first; the status line says how many.
Long lists fill in as you scroll, so even thousands of results open
instantly.</p>
<p>Three columns: <b>Date</b>, <b>Type</b> and <b>Title</b>. Type is
<b>VIDEO</b> (a dev video transcript), <b>COMM-LINK</b> (an RSI article) or
<b>DEV POST</b> (a CIG developer on Spectrum; the title starts with who
posted). Hover a title that is cut off to see it in full, and drag the
divider between the list and the panel to give either side more room.</p>

{_h4("Monthly Reports tab", _C_START)}
<p>The <b>MONTHLY REPORTS</b> tab at the top lists every Star Citizen,
Squadron 42 and Studio monthly report, newest first. Filter by series, year
or title, and click one: you get a <b>SUMMARY</b> (the report's opening plus
one line per team) and the <b>FULL TEXT</b> below it. <b>Download all</b>
fetches every report once (a few minutes) so they read instantly and are
searchable word for word in the Search tab.</p>

{_h4("Dev Tracker tab", _C_START)}
<p>The <b>DEV TRACKER</b> tab lists what CIG developers posted on Spectrum,
newest first. Filter by dev, by forum or by words in the thread. Click a post
to read it in full; <b>Refresh</b> pulls the newest posts, <b>Load older</b>
goes one page further back, and <b>Load full history</b> fetches everything
back to Spectrum's launch in February 2017 (about 16,000 posts, several
minutes; press it again to pause, it resumes where it stopped). Posts in
login-only forums (Focus Testing and the like) show only the public
preview.</p>
""")

_TAB_READING = _html(f"""
{_h3("Reading a result", _C_READ)}

{_h4("Click a row", _C_READ)}
<p>The panel on the right shows why it matched. For a <b>video</b> you get
<b>MATCHING EXCERPTS</b> &mdash; the actual transcript lines, with your words
highlighted. <b>Comm-links</b> show a <b>SUMMARY</b> of what the article
actually says (built from its text, not RSI's one-line teaser) and the
matching passages; press <b>Full text</b> to read the whole thing.</p>
<p>If the archive has not reached an article yet, the tool fetches that one
article from robertsspaceindustries.com when you click it and says so. That
needs a connection; set <span style="{_ACC}">"live_rsi": false</span> in
settings.json to turn it off.</p>
<p>If the excerpts are empty, the match came from the title or the metadata
rather than anything said out loud. The panel says so instead of leaving you
guessing.</p>

{_h4("Double-click a row to open the source", _C_READ)}
<p><span style="{_YLW}">This is the one worth knowing.</span>
<b>Double-clicking any result opens the original video or comm-link in your
browser</b> &mdash; the same as selecting it and pressing
<b>Open source &#8599;</b>. Nothing on screen advertises it.</p>

{_h4("Always check the source when it matters", _C_READ)}
<p>The transcripts are <b>machine-made</b>, so they get names and ship
designations wrong. Treat this tool as a way to find <em>where</em> something
was said, then read it at the source before you quote it. Every result links
back, which is the whole point of the design.</p>
""")

_TAB_TIPS = _html(f"""
{_h3("Good to know", _C_TIPS)}

{_h4("The first run downloads once, then works offline", _C_TIPS)}
<p>The search index is <b>a few MB</b>, fetched from GitHub the first time
you open the tool. A banner tells you it is happening. After that,
<b>searching needs no connection at all</b> &mdash; the index lives on your
disk. The archive picks up new comm-links and dev posts every day; the tool
checks for that at most twice a day, and when nothing changed the check
downloads nothing.</p>
<p>Transcripts, articles and reports are fetched <b>when you select a result</b>,
so that part does want a connection. If one cannot be fetched, the tool says
so and the <b>Open source &#8599;</b> link still works.</p>

{_h4("If it says OFFLINE", _C_TIPS)}
<p>You got no index on first run. Press <b>Retry</b> in the banner once you
have a connection. There is nothing to install and nothing to configure.</p>

{_h4("What the status line is telling you", _C_TIPS)}
<p>Bottom left, and it is the honest account of what the tool has:
<span style="{_DIM}">downloading index&hellip;</span>,
<span style="{_DIM}">12,345 records &middot; branch main</span>,
<span style="{_DIM}">7 result(s)</span>, or
<span style="{_DIM}">no index</span> if the download never landed.</p>

{_h4("Hotkey", _C_TIPS)}
<p><b>Shift + H</b> shows and hides this window by default, and the search box
already has the cursor when it opens, so you can hit the hotkey and start
typing. Reassign it in the SC Toolbox settings.</p>

{_h4("Unofficial, and on sufferance", _C_TIPS)}
<p>A fan archive. Star Citizen and everything quoted here belong to
<b>Cloud Imperium Games</b>, and this is not affiliated with or endorsed by
them. If CIG ever asks for it to go, it goes.</p>
""")

_TABS = [
    ("Searching", _TAB_SEARCHING),
    ("Reading a Result", _TAB_READING),
    ("Good to Know", _TAB_TIPS),
]


# ── Close button ─────────────────────────────────────────────────────────


class _CloseBtn(QPushButton):
    def __init__(self, parent=None):
        super().__init__("x", parent)
        self.setObjectName("tutClose")
        self.setFixedSize(32, 28)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(f"""
            QPushButton#tutClose {{
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
            QPushButton#tutClose:hover {{
                background-color: {CLOSE_BTN_HOVER_BG};
                color: #ffffff;
            }}
        """)


# ── Scrollable tab content ───────────────────────────────────────────────


def _make_tab(html: str, parent: QWidget) -> QScrollArea:
    scroll = QScrollArea(parent)
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    scroll.setStyleSheet(f"""
        QScrollArea {{ background: transparent; border: none; }}
        QScrollBar:vertical {{
            background: {P.scrollbar_bg}; width: 6px; border: none;
        }}
        QScrollBar::handle:vertical {{
            background: {P.scrollbar_handle}; min-height: 20px; border-radius: 3px;
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
    """)
    lbl = QLabel(html)
    lbl.setWordWrap(True)
    lbl.setTextFormat(Qt.RichText)
    lbl.setAlignment(Qt.AlignTop | Qt.AlignLeft)
    lbl.setStyleSheet(f"background: transparent; padding: 16px; color: {P.fg};")
    lbl.setOpenExternalLinks(True)
    scroll.setWidget(lbl)
    return scroll


# ── Tutorial popup ───────────────────────────────────────────────────────


class TutorialPopup(QDialog):
    """Tabbed tutorial popup for Dev History.

    Singleton: a second call just raises the existing window.
    """

    _instance: Optional["TutorialPopup"] = None

    def __new__(cls, parent: Optional[QWidget] = None):
        if cls._instance is not None and cls._instance.isVisible():
            cls._instance.raise_()
            cls._instance.activateWindow()
            return cls._instance
        instance = super().__new__(cls)
        cls._instance = instance
        return instance

    def __init__(self, parent: Optional[QWidget] = None):
        if getattr(self, "_initialised", False):
            return
        self._initialised = True

        super().__init__(parent)
        self._drag_pos: QPoint | None = None

        self.setWindowTitle("Dev History — Tutorial")
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(560, 470)
        self.setMinimumSize(460, 340)

        if parent:
            pg = parent.geometry()
            x = pg.x() + (pg.width() - 560) // 2
            y = pg.y() + (pg.height() - 470) // 2
            self.move(max(0, x), max(0, y))

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

        title_bar = QWidget(frame)
        title_bar.setFixedHeight(34)
        title_bar.setStyleSheet(f"background-color: {P.bg_header};")
        tb_lay = QHBoxLayout(title_bar)
        tb_lay.setContentsMargins(12, 0, 4, 0)
        tb_lay.setSpacing(8)

        title_lbl = QLabel("DEV HISTORY  —  TUTORIAL", title_bar)
        title_lbl.setStyleSheet(
            f"font-family: Electrolize, Consolas, monospace;"
            f"font-size: 11pt; font-weight: bold;"
            f"color: {TOOL_COLOR}; letter-spacing: 2px; background: transparent;"
        )
        tb_lay.addWidget(title_lbl)
        tb_lay.addStretch(1)

        close_btn = _CloseBtn(title_bar)
        close_btn.clicked.connect(self.close)
        tb_lay.addWidget(close_btn)

        frame_lay.addWidget(title_bar)

        tabs = QTabWidget()
        tabs.setStyleSheet(f"""
            QTabWidget::pane {{
                border: none;
                background: transparent;
            }}
            QTabBar::tab {{
                background: {P.bg_secondary};
                color: {P.fg_dim};
                border: none;
                padding: 6px 14px;
                font-family: Consolas;
                font-size: 9pt;
                font-weight: bold;
            }}
            QTabBar::tab:selected {{
                background: #141a2a;
                color: {TOOL_COLOR};
            }}
            QTabBar::tab:hover:!selected {{
                color: {P.fg};
            }}
        """)

        for tab_title, html in _TABS:
            tabs.addTab(_make_tab(html, tabs), tab_title)

        frame_lay.addWidget(tabs, 1)
        outer.addWidget(frame)

    # ── Paint: border + corner brackets ─────────────────────────────────

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        w, h = self.width(), self.height()

        edge = QColor(TOOL_COLOR)
        edge.setAlpha(100)
        painter.setPen(QPen(edge, 1))
        painter.drawRect(0, 0, w - 1, h - 1)

        bl = POPUP_BRACKET_LEN
        bracket = QColor(TOOL_COLOR)
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

    # ── Cleanup ──────────────────────────────────────────────────────────

    def closeEvent(self, event):
        TutorialPopup._instance = None
        super().closeEvent(event)

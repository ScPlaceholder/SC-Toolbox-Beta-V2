"""Tutorial popup for the Play Time calculator.

Same chrome as the Craft Database / Star Map popups: a frameless, draggable,
always-on-top tabbed dialog with corner brackets, opened from a "Tutorial"
button in the title bar.

Every control named below was read out of the source:
  playtime_window.py  Link Folder, "↻ Rescan", "Trim AFK over" + " h" suffix,
                      the Hours/Days/Calendar and Day/Week/Month/Year
                      toggles, the eight Overview card titles, the tab order
                      (Overview 0, Trends 1, Calendar 2, Fun Stats 3,
                      Career 4, Injuries 5, Sessions 6), the Sessions filter
                      placeholder and its six columns
  fun_stats_tab.py    "↻ Re-analyze" and the lazy first-open scan
  calendar_view.py    "◀" / "▶", the clickable period title, Month / Year /
                      Latest
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

TOOL_COLOR = "#44ccff"          # matches ACCENT in playtime_window.py
POPUP_BRACKET_LEN = 14
CLOSE_BTN_BG = "rgba(255, 60, 60, 0.15)"
CLOSE_BTN_COLOR = "#cc6666"
CLOSE_BTN_HOVER_BG = "rgba(220, 50, 50, 0.85)"

# ── Shared rich-text style fragments ─────────────────────────────────────

_B = f"font-family: Consolas; color: {P.fg}; font-size: 9pt; line-height: 1.5;"
_DIM = f"color: {P.fg_dim};"
_ACC = f"color: {TOOL_COLOR};"
_YLW = f"color: {P.yellow};"

_C_START = TOOL_COLOR       # blue   — Getting Started
_C_NUM = "#ffb347"          # amber  — The Numbers
_C_EXPLORE = "#33dd88"      # green  — Trends & Calendar
_C_DEEP = "#cc88ff"         # purple — Fun Stats, Career, Injuries
_C_TIPS = "#44dd88"         # green  — Tips

_FONT = "font-family: Electrolize, Consolas;"


def _h3(text: str, color: str) -> str:
    return f'<h3 style="{_FONT} color: {color};">{text}</h3>'


def _h4(text: str, color: str) -> str:
    return f'<h4 style="{_FONT} color: {color};">{text}</h4>'


def _html(body: str) -> str:
    return f'<div style="{_B}">{body}</div>'


def _hotkey() -> str:
    """This tool's hotkey as the launcher has it now (follows a rebind)."""
    try:
        from shared.hotkey_label import hotkey_label
        return hotkey_label("hotkey_playtime", "<ctrl>+4")
    except Exception:                       # noqa: BLE001 - a tutorial is not worth a failed open
        return "Ctrl+4"


_TAB_GETTING_STARTED = _html(f"""
{_h3("How long have you actually played?", _C_START)}
<p>Star Citizen does not tell you. This reads the game's own log files off
your disk and works it out &mdash; every session, when it started, when it
ended &mdash; then lets you divide what you have spent by the hours you have
had.</p>

{_h4("Step one: point it at the game", _C_START)}
<p>It usually finds your install by itself. If the top bar says
<span style="{_DIM}">(no folder linked)</span>, press <b>Link Folder</b> and
pick your <b>Star Citizen folder</b> &mdash; the install root, or a
<b>LIVE</b> / <b>PTU</b> folder. Then it scans, and the total appears.</p>
<p>Press <b>&#8635; Rescan</b> after you have played to pick up new sessions.
Nothing is uploaded and nothing is sent anywhere; it is your own log
files.</p>

{_h4("How far back does it go?", _C_START)}
<p>As far as your logs do. The game keeps the current <b>Game.log</b> plus a
folder of backups, so the history ends where your oldest backup does. If you
have ever wiped the folder, the time before that is gone &mdash; not a bug in
the tool, just missing evidence.</p>

{_h4("Trim AFK over N h", _C_START)}
<p>A session that ran overnight because you left the game open is not play
time. Tick <b>Trim AFK over</b> and set the hours, and any single session
longer than that is capped to it.</p>
<p><span style="{_YLW}">It changes every number in the window</span> &mdash;
the total, the cost per hour, the charts, the streaks. When the cost line
reads <span style="{_DIM}">(AFK-trimmed)</span>, that is this setting talking.
Untick it to see the raw figures.</p>
""")

_TAB_NUMBERS = _html(f"""
{_h3("The two big numbers", _C_NUM)}

{_h4("Total play time", _C_NUM)}
<p>Every session added up. The <b>FORMAT</b> toggle beside it changes how it
reads:</p>
<ul>
  <li><b>Hours</b> &mdash; a plain hour count.</li>
  <li><b>Days</b> &mdash; the same figure as whole days, which is usually the
      one that hurts.</li>
  <li><b>Calendar</b> &mdash; broken into years, months and days.</li>
</ul>

{_h4("Cost per hour", _C_NUM)}
<p>Type what you have spent on the game into the box marked
<span style="{_DIM}">e.g. 1200</span> &mdash; pledges, ships, subscriptions,
all of it &mdash; and pick your <b>currency</b>. It divides that by your hours
and shows the arithmetic underneath, e.g.
<span style="{_ACC}">$1,200 &divide; 430 h played</span>.</p>
<p>The figure is <b>saved</b>, so you type it once and it survives patches and
restarts. Until you enter something, the cost shows a dash and the line under
it reads <span style="{_DIM}">enter your lifetime spend</span>.</p>
<p style="{_DIM}">Some currencies show as their three-letter code rather than
a symbol. That is deliberate &mdash; a few symbols do not render reliably, and
a wrong glyph is worse than a clear code.</p>

{_h4("The eight cards, and what clicking one does", _C_NUM)}
<p><b>Overview</b> has eight cards: <b>Longest Session</b>,
<b>Most Played Day</b>, <b>Most Played Week</b>, <b>Most Played Month</b>,
<b>Longest Streak</b>, <b>Avg / Active Day</b>, <b>Busiest Hour</b> and
<b>Busiest Weekday</b>.</p>
<p><b>Four of them are buttons.</b> Nothing says so, so it is worth knowing:
<b>Longest Session</b> opens the <b>Sessions</b> log sorted longest-first;
<b>Most Played Day</b> opens <b>Calendar</b> on that day;
<b>Most Played Week</b> and <b>Most Played Month</b> open <b>Trends</b> at
that grouping. The other four have nowhere further to take you and stay on
<b>Overview</b>.</p>

{_h4("The two charts below", _C_NUM)}
<p><b>Time of Day</b> shows when you play. <b>Hover</b> a bar for the exact
total, or <b>click</b> it to have that hour's total written out under the
chart. <b>By Weekday &amp; Release Channel</b> splits your time across the
week and between LIVE and PTU.</p>
""")

_TAB_EXPLORE = _html(f"""
{_h3("Trends, Calendar and the session log", _C_EXPLORE)}

{_h4("Trends", _C_EXPLORE)}
<p>One bar chart of your play time over time, at whichever grouping you pick:
<b>Day</b>, <b>Week</b>, <b>Month</b> or <b>Year</b>. <b>Hover</b> a bar for
its total; <b>click</b> a bar to list that period's sessions in the box
below.</p>

{_h4("Calendar", _C_EXPLORE)}
<p>A heat grid of your play. Brighter means more hours; the darkest cells
are days with none.</p>
<ul>
  <li><b>&#9664;</b> and <b>&#9654;</b> step back and forward a period;
      <b>Latest</b> jumps to now.</li>
  <li><b>Month</b> / <b>Year</b> switch between one month and a whole year.
      Both are drawn a day to a cell.</li>
  <li>Three things in the grid are clickable and none of them look it:
      a <b>day</b> cell, a <b>weekday name</b> in the header (every Tuesday,
      say), and in year view a <b>month label</b>. The cursor changes to a
      hand when you are over one.</li>
  <li>The <b>title in the middle</b> is also a button &mdash; press it for a
      summary of the whole period on screen.</li>
  <li>Click a day and the detail panel gives its total and lists
      <b>Sessions started today</b>. A session that ran past midnight counts
      toward the next day's total too, but is listed only on the day it
      started.</li>
</ul>

{_h4("Sessions", _C_EXPLORE)}
<p>The raw list, one row per session: <b>Date</b>, <b>Start</b>, <b>End</b>,
<b>Duration</b>, <b>Channel</b> and <b>Build</b>. Click any column heading to
sort by it.</p>
<p>The <b>Filter</b> box matches date, channel and build at once, so
<span style="{_ACC}">2026-05</span> gives you that month and
<span style="{_ACC}">LIVE</span> drops your PTU sessions. The count underneath
tells you how many of the total you are looking at.</p>
""")

_TAB_DEEP = _html(f"""
{_h3("Fun Stats, Career and Injuries", _C_DEEP)}
<p>These three read the <b>contents</b> of the logs, not just the timestamps
&mdash; what you flew, what you shot, who paid you, what broke your legs.</p>

{_h4("They scan separately, and only when you ask", _C_DEEP)}
<p>That is a much heavier read than the play-time scan, so it does not happen
on startup. <b>It starts the first time you open one of these three tabs</b>,
and it shows you how far along it is. All three share the one scan, so
whichever you open first pays for the other two.</p>
<p><span style="{_YLW}">Two refresh buttons, two different jobs.</span>
<b>&#8635; Rescan</b> in the top bar redoes the fast play-time scan.
<b>&#8635; Re-analyze</b> inside these tabs redoes this deep one. Played since
you last looked? Press <b>&#8635; Rescan</b> while one of these three tabs is
showing and it redoes both. From any other tab it redoes only the fast one.</p>

{_h4("Fun Stats", _C_DEEP)}
<p>Your most-flown ship, favourite weapon, favourite manufacturer and
multitool attachment, with the full rankings under them.</p>

{_h4("Career", _C_DEEP)}
<p>Missions completed, your top contract type, your top employer, your
completion rate, and your mining, salvage and trading activity.</p>
<p><b>These are counts, not earnings.</b> Payouts live on CIG's servers and
never touch your log, so the tool cannot know what you were paid &mdash; and
says so rather than inventing a number.</p>

{_h4("Injuries", _C_DEEP)}
<p>Where you get hurt, on a body diagram, stacked by severity
(<b>Tier 1</b> is the worst), plus injuries per week and how many med bed
surgeries you have had. The diagram is drawn <b>facing you</b>, and the two
sides are labelled <b>YOUR RIGHT</b> and <b>YOUR LEFT</b>.</p>

{_h4("Why some tabs look empty", _C_DEEP)}
<p>The game only started writing these events to the log in later builds
&mdash; ship and loadout events from mid-2025, injury notifications from
late-2025. Sessions older than that are still counted in your
<b>play time</b>; they simply have no ships, guns or injuries recorded. An
empty panel here means <b>the log never said</b>, not that you never did it.
</p>
""")

_TAB_TIPS = _html(f"""
{_h3("Tips", _C_TIPS)}

{_h4("Two scans, and which button you want", _C_TIPS)}
<p><b>&#8635; Rescan</b> (top bar) = the fast timestamp pass behind the total,
the charts and the Calendar. <b>&#8635; Re-analyze</b> (inside Fun Stats /
Career / Injuries) = the deep content pass. Re-analyze never redoes the fast
pass. Rescan redoes the deep one only if one of those three tabs is the one
showing.</p>

{_h4("Everything is local", _C_TIPS)}
<p>No account, no login, no network. The only inputs are your log folder and
the spend figure you type, and both stay on this PC.</p>

{_h4("Hover before you click", _C_TIPS)}
<p>Nearly every bar and cell in the tool answers a hover with an exact figure,
and quite a few answer a click by drilling in. If a number looks wrong, hover
the thing it came from.</p>

{_h4("A cheaper hour", _C_TIPS)}
<p>Cost per hour falls every time you play and rises every time you pledge.
Leave your spend in the box and it keeps score for you.</p>
""")


def _tab_tips() -> str:
    """Tips, with the hotkey as the launcher has it when the tutorial opens."""
    return _TAB_TIPS.replace("</div>", _TIP_HOTKEY.format(hotkey=_hotkey()) + "</div>")


_TIP_HOTKEY = (
    _h4("Hotkey", _C_TIPS)
    + '<p>The launcher\'s hotkey for this tool is <span style="' + _ACC + '">{hotkey}</span>. '
      'It shows and hides the window.</p>'
)


def _tabs() -> list:
    return [
        ("Getting Started", _TAB_GETTING_STARTED),
        ("The Numbers", _TAB_NUMBERS),
        ("Explore", _TAB_EXPLORE),
        ("Deep Stats", _TAB_DEEP),
        ("Tips", _tab_tips()),
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
    """Tabbed tutorial popup for Play Time.

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

        self.setWindowTitle("Play Time — Tutorial")
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(620, 500)
        self.setMinimumSize(480, 360)

        if parent:
            pg = parent.geometry()
            x = pg.x() + (pg.width() - 620) // 2
            y = pg.y() + (pg.height() - 500) // 2
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

        title_lbl = QLabel("PLAY TIME  —  TUTORIAL", title_bar)
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
                background: #0e1c26;
                color: {TOOL_COLOR};
            }}
            QTabBar::tab:hover:!selected {{
                color: {P.fg};
            }}
        """)

        for tab_title, html in _tabs():
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

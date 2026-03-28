"""Title bar component for the mining loadout window — PySide6 version.

Uses SCTitleBar from shared/qt/ with mining tool accent colour.
"""
import shared.path_setup  # noqa: E402  # centralised path config

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel

from shared.qt.theme import P
from shared.qt.title_bar import SCTitleBar


def build_title_bar(
    window: QWidget,
    on_close,
    on_refresh,
) -> dict:
    """Build the mining loadout title bar.

    Returns dict with keys: 'title_bar', 'upd_label', 'src_label'.
    """
    title_bar = SCTitleBar(
        window=window,
        title="MINING LOADOUT",
        icon_text="\u26cf",
        accent_color=P.tool_mining,
        show_minimize=False,
    )
    title_bar.close_clicked.connect(on_close)

    # Add refresh button and status labels to the title bar layout
    layout = title_bar.layout()

    upd_label = QLabel("", title_bar)
    upd_label.setStyleSheet(f"""
        font-family: Consolas;
        font-size: 9pt;
        color: {P.fg_dim};
        background: transparent;
    """)

    src_label = QLabel("", title_bar)
    src_label.setStyleSheet(f"""
        font-family: Consolas;
        font-size: 8pt;
        color: {P.fg_dim};
        background: transparent;
    """)

    refresh_btn = QLabel(" \u27f3 ", title_bar)
    refresh_btn.setStyleSheet(f"""
        font-family: Consolas;
        font-size: 14pt;
        color: {P.tool_mining};
        background: transparent;
    """)
    refresh_btn.setCursor(Qt.PointingHandCursor)
    refresh_btn.mousePressEvent = lambda _: on_refresh()

    # Insert before the stretch (index varies, insert near end before window controls)
    # The layout is: [stripe, icon, title, stretch, minimize_btn, close_btn]
    # We want to insert before the stretch
    stretch_idx = -1
    for i in range(layout.count()):
        item = layout.itemAt(i)
        if item.spacerItem() is not None:
            stretch_idx = i
            break

    if stretch_idx >= 0:
        layout.insertWidget(stretch_idx + 1, src_label)
        layout.insertWidget(stretch_idx + 1, upd_label)
        layout.insertWidget(stretch_idx + 1, refresh_btn)

    return {
        "title_bar": title_bar,
        "upd_label": upd_label,
        "src_label": src_label,
    }

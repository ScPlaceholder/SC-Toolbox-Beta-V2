"""
Optional extension loader for Trade Hub.
Reads configuration from an encrypted data file if present.
"""
import base64
import hashlib
import json
import logging
import os
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

_SALT = b"trade_hub_ext_v1"
_ITERATIONS = 100_000
_DATA_FILE = os.path.join(os.path.dirname(__file__), "data", "ext.dat")

# ── Module state ───────────────────────────────────────────────────────────────
_cfg: Optional[Dict] = None
_active_mode: Dict = {"id": "standard", "params": {}}
_panel_ref = None


def _derive_key(raw: str) -> bytes:
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_SALT,
        iterations=_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(raw.encode("utf-8")))


def _check_phrase(raw_input: str) -> bool:
    """Verify the input matches the stored digest."""
    if not os.path.isfile(_DATA_FILE):
        return False
    try:
        with open(_DATA_FILE, "rb") as fp:
            stored_digest = fp.readline().strip().decode("ascii", errors="ignore")
    except OSError:
        return False
    return hashlib.sha256(raw_input.encode("utf-8")).hexdigest() == stored_digest


def try_load(raw_input: str) -> bool:
    """Attempt to unlock the extension with the given input string."""
    global _cfg

    if not _check_phrase(raw_input):
        return False

    if _cfg is not None:
        return True

    if not os.path.isfile(_DATA_FILE):
        return False

    try:
        with open(_DATA_FILE, "rb") as fp:
            content = fp.read()
    except OSError:
        return False

    parts = content.split(b"\n", 1)
    if len(parts) != 2:
        return False

    token = parts[1]

    try:
        from cryptography.fernet import Fernet
        fernet_key = _derive_key(raw_input)
        f = Fernet(fernet_key)
        plaintext = f.decrypt(token)
        _cfg = json.loads(plaintext)
        logger.info("[ExtLoader] Extension loaded")
        return True
    except Exception:
        return False


def is_active() -> bool:
    return _cfg is not None


def get_active_mode() -> Dict:
    return _active_mode


def set_active_mode(mode: Dict) -> None:
    global _active_mode
    _active_mode = mode


def show_panel(parent, on_mode_change: Callable) -> None:
    """Display the extension configuration panel (PySide6)."""
    global _panel_ref, _active_mode

    if _cfg is None:
        return

    # If panel already open, bring to front
    if _panel_ref is not None:
        try:
            _panel_ref.raise_()
            _panel_ref.activateWindow()
            return
        except RuntimeError:
            _panel_ref = None

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
        QRadioButton, QButtonGroup, QFrame,
    )

    colors = _cfg.get("colors", {})
    modes = _cfg.get("modes", [])

    bg      = colors.get("bg", "#0a0a0a")
    bg2     = colors.get("bg2", "#1a0000")
    panel_c = colors.get("panel", "#111111")
    accent  = colors.get("accent", "#cc0000")
    accent2 = colors.get("accent2", "#ff2020")
    fg      = colors.get("fg", "#cccccc")
    fg2     = colors.get("fg2", "#888888")
    fg3     = colors.get("fg3", "#ffffff")
    sel     = colors.get("sel", "#330000")
    border  = colors.get("border", "#440000")
    btn_bg  = colors.get("btn", "#1a0808")
    btn_act = colors.get("btn_active", "#2a0000")

    # ── Window ────────────────────────────────────────────────────────────────
    win = QWidget(parent, Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
    win.setFixedSize(460, 560)
    win.setStyleSheet(f"background: {bg};")

    _panel_ref = win

    # Drag support
    win._drag_pos = None

    def _mouse_press(e):
        if e.button() == Qt.LeftButton:
            win._drag_pos = e.globalPosition().toPoint() - win.frameGeometry().topLeft()

    def _mouse_move(e):
        if win._drag_pos is not None:
            win.move(e.globalPosition().toPoint() - win._drag_pos)

    def _mouse_release(e):
        win._drag_pos = None

    win.mousePressEvent = _mouse_press
    win.mouseMoveEvent = _mouse_move
    win.mouseReleaseEvent = _mouse_release

    layout = QVBoxLayout(win)
    layout.setContentsMargins(1, 1, 1, 1)
    layout.setSpacing(0)

    # Outer border
    outer = QFrame()
    outer.setStyleSheet(f"background: {border}; border: 1px solid {border};")
    outer_lay = QVBoxLayout(outer)
    outer_lay.setContentsMargins(1, 1, 1, 1)
    outer_lay.setSpacing(0)

    inner = QFrame()
    inner.setStyleSheet(f"QFrame {{ background: {bg}; }}")
    inner_lay = QVBoxLayout(inner)
    inner_lay.setContentsMargins(0, 0, 0, 0)
    inner_lay.setSpacing(0)

    # ── Title bar ─────────────────────────────────────────────────────────────
    title_bar = QFrame()
    title_bar.setFixedHeight(47)
    title_bar.setStyleSheet(f"QFrame {{ background: {bg2}; }}")
    tb_lay = QHBoxLayout(title_bar)
    tb_lay.setContentsMargins(14, 0, 12, 0)

    title_text = _cfg.get("title", "Extension")
    title_lbl = QLabel(f"\u25c6  {title_text}")
    title_lbl.setStyleSheet(
        f"color: {accent2}; font: bold 27px 'Consolas'; background: transparent;"
    )
    tb_lay.addWidget(title_lbl)
    tb_lay.addStretch()

    close_btn = QPushButton("\u2715")
    close_btn.setFixedSize(28, 28)
    close_btn.setStyleSheet(f"""
        QPushButton {{
            color: {accent}; background: transparent;
            font: bold 16px 'Consolas'; border: none;
        }}
        QPushButton:hover {{ color: {fg3}; }}
    """)
    close_btn.clicked.connect(lambda: _close())
    tb_lay.addWidget(close_btn)
    inner_lay.addWidget(title_bar)

    # Subtitle
    sub_text = _cfg.get("subtitle", "")
    if sub_text:
        sub_lbl = QLabel(sub_text)
        sub_lbl.setAlignment(Qt.AlignCenter)
        sub_lbl.setStyleSheet(
            f"color: {fg2}; font: 12px 'Consolas'; padding: 4px 0 0 0; background: transparent;"
        )
        inner_lay.addWidget(sub_lbl)

    # Accent line
    line1 = QFrame()
    line1.setFixedHeight(1)
    line1.setStyleSheet(f"background: {accent}; margin: 8px 20px 4px 20px;")
    inner_lay.addWidget(line1)

    # ── Status ────────────────────────────────────────────────────────────────
    status_w = QWidget()
    status_w.setStyleSheet("background: transparent;")
    status_row = QHBoxLayout(status_w)
    status_row.setContentsMargins(20, 8, 20, 4)
    dot = QLabel("\u25cf")
    dot.setStyleSheet("color: #00ff00; font: 13px 'Consolas'; background: transparent;")
    status_row.addWidget(dot)
    status_text = QLabel(" SYSTEMS ACTIVE")
    status_text.setStyleSheet(
        "color: #00ff00; font: bold 13px 'Consolas'; background: transparent;"
    )
    status_row.addWidget(status_text)
    status_row.addStretch()
    inner_lay.addWidget(status_w)

    # ── Mode label ────────────────────────────────────────────────────────────
    mode_hdr = QLabel("CALCULATION MODE")
    mode_hdr.setAlignment(Qt.AlignCenter)
    mode_hdr.setStyleSheet(
        f"color: {fg2}; font: bold 13px 'Consolas'; padding: 8px 0 4px 0; background: transparent;"
    )
    inner_lay.addWidget(mode_hdr)

    # ── Mode radio buttons ────────────────────────────────────────────────────
    btn_group = QButtonGroup(win)
    mode_widgets = []

    for i, mode in enumerate(modes):
        mid = mode.get("id", "")
        mname = mode.get("name", mid)
        mdesc = mode.get("desc", "")

        mf = QFrame()
        mf.setObjectName(f"modecard_{i}")
        mf.setStyleSheet(f"""
            QFrame#modecard_{i} {{
                background: {panel_c};
                border: 1px solid {border};
                margin: 3px 20px;
                padding: 10px;
            }}
            QFrame#modecard_{i} QRadioButton {{
                color: {fg3}; font: bold 14px 'Consolas'; background: transparent;
            }}
            QFrame#modecard_{i} QRadioButton::indicator {{
                width: 14px; height: 14px;
            }}
            QFrame#modecard_{i} QRadioButton::indicator:checked {{
                background: {accent2}; border: 2px solid {accent}; border-radius: 7px;
            }}
            QFrame#modecard_{i} QRadioButton::indicator:unchecked {{
                background: {bg}; border: 2px solid {border}; border-radius: 7px;
            }}
            QFrame#modecard_{i} QLabel {{
                color: {fg2}; font: 11px 'Consolas'; background: transparent;
                padding-left: 22px;
            }}
        """)
        mf_lay = QVBoxLayout(mf)
        mf_lay.setContentsMargins(8, 6, 8, 6)
        mf_lay.setSpacing(4)

        rb = QRadioButton(mname)
        if mid == _active_mode.get("id", "standard"):
            rb.setChecked(True)
        btn_group.addButton(rb, i)
        mf_lay.addWidget(rb)

        desc_lbl = QLabel(mdesc)
        desc_lbl.setWordWrap(True)
        mf_lay.addWidget(desc_lbl)

        inner_lay.addWidget(mf)
        mode_widgets.append((i, mf))

    def _highlight():
        checked = btn_group.checkedId()
        for idx, mf in mode_widgets:
            border_color = accent if idx == checked else border
            mf.setStyleSheet(f"""
                QFrame#modecard_{idx} {{
                    background: {panel_c};
                    border: 1px solid {border_color};
                    margin: 3px 20px; padding: 10px;
                }}
                QFrame#modecard_{idx} QRadioButton {{
                    color: {fg3}; font: bold 14px 'Consolas'; background: transparent;
                }}
                QFrame#modecard_{idx} QRadioButton::indicator {{
                    width: 14px; height: 14px;
                }}
                QFrame#modecard_{idx} QRadioButton::indicator:checked {{
                    background: {accent2}; border: 2px solid {accent}; border-radius: 7px;
                }}
                QFrame#modecard_{idx} QRadioButton::indicator:unchecked {{
                    background: {bg}; border: 2px solid {border}; border-radius: 7px;
                }}
                QFrame#modecard_{idx} QLabel {{
                    color: {fg2}; font: 11px 'Consolas'; background: transparent;
                    padding-left: 22px;
                }}
            """)

    def _on_mode_selected(idx):
        if 0 <= idx < len(modes):
            _active_mode.clear()
            _active_mode.update(modes[idx])
            set_active_mode(_active_mode)
            on_mode_change(_active_mode)
            cur_label.setText(f"Active: {_active_mode.get('name', 'STANDARD')}")
        _highlight()

    btn_group.idClicked.connect(_on_mode_selected)
    _highlight()

    def _close():
        global _panel_ref
        try:
            win.close()
            win.deleteLater()
        except Exception:
            pass
        _panel_ref = None

    # Accent line + active mode indicator at bottom
    line2 = QFrame()
    line2.setFixedHeight(1)
    line2.setStyleSheet(f"background: {accent}; margin: 12px 20px 0 20px;")
    inner_lay.addWidget(line2)

    inner_lay.addStretch()
    cur_label = QLabel(f"Active: {_active_mode.get('name', 'STANDARD')}")
    cur_label.setAlignment(Qt.AlignCenter)
    cur_label.setStyleSheet(
        f"color: {fg2}; font: 12px 'Consolas'; padding: 4px 0 6px 0; background: transparent;"
    )
    inner_lay.addWidget(cur_label)

    outer_lay.addWidget(inner)
    layout.addWidget(outer)

    # Centre on parent
    pg = parent.geometry()
    win.move(
        pg.x() + (pg.width() - 460) // 2,
        pg.y() + (pg.height() - 560) // 2,
    )

    win.show()
    win.raise_()
    win.activateWindow()

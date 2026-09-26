"""Child process that builds the Settings popup on a simulated screen and prints its geometry.

Run by test_settings_popup_fit.py, never imported.  It must be a separate process because
QT_SCALE_FACTOR is read once, when QApplication is constructed: the launcher relaunches itself
to change UI scale (skill_launcher.py), so a faithful test has to do the same.

Usage:  python _settings_popup_probe.py <ui_scale> [--escape]
Prints one JSON object on stdout: the screen's available rect, the popup's frame, the Apply
button's rect in global coordinates, and which UI scales the popup offers.

With --escape it also walks the undocumented recovery path a stuck user is told to use — drag the
title bar clear, pick 1x, close with the popup's own [x] instead of Apply — and reports what the
save callback received, so a change to closeEvent cannot quietly take that path away.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..")))


def main() -> int:
    ui_scale = float(sys.argv[1])

    from PySide6.QtWidgets import QApplication, QWidget
    from PySide6.QtGui import QGuiApplication

    app = QApplication([])
    screen = QGuiApplication.primaryScreen()
    avail = screen.availableGeometry()

    from shared.config_models import SkillConfig
    from ui.settings_panel import SettingsPopup

    skills = [
        SkillConfig(id=f"tool{i}", name=f"Tool {i}", icon="*", color="#ffffff",
                    folder=f"tool{i}", script="main.py", hotkey=f"<alt>+{i}")
        for i in range(1, 6)
    ]

    # Stand-in for the launcher window, centred on the simulated screen.
    parent = QWidget()
    parent.resize(420, 260)
    parent.move(avail.center().x() - 210, avail.center().y() - 130)

    saved: list[dict] = []
    popup = SettingsPopup(
        parent_window=parent,
        skills=skills,
        launcher_hotkey="<alt>+q",
        disabled_skills=[],
        keybinds_disabled=[],
        grid_rows=2,
        grid_cols=3,
        grid_layout={},
        ui_scale=ui_scale,
        on_apply=saved.append,
    )
    popup.show()
    app.processEvents()

    apply_btn = getattr(popup, "_apply_btn", None)
    if apply_btn is None:
        # Pre-fix source did not keep a handle on it; find it by text.
        from PySide6.QtWidgets import QPushButton, QAbstractButton
        cands = [b for b in popup.findChildren(QAbstractButton)
                 if b.text().strip().lower() == "apply"]
        apply_btn = cands[0] if cands else None
    if apply_btn is None:
        print(json.dumps({"error": "Apply button not found"}))
        return 3

    top_left = apply_btn.mapToGlobal(apply_btn.rect().topLeft())
    combo = popup._scale_combo
    offered = [combo.itemData(i) for i in range(combo.count())]
    enabled = []
    model = combo.model()
    for i in range(combo.count()):
        item = model.item(i) if hasattr(model, "item") else None
        if item is None or item.isEnabled():
            enabled.append(combo.itemData(i))

    # Snapshot the geometry BEFORE the escape walk below, which deliberately moves and closes the
    # popup: reading popup.x() afterwards would report where the recovery put it, not where the
    # popup opened, and the two questions would contaminate each other.
    out = {
        "ui_scale": ui_scale,
        "dpr": screen.devicePixelRatio(),
        "screen_avail": [avail.x(), avail.y(), avail.width(), avail.height()],
        "popup": [popup.x(), popup.y(), popup.width(), popup.height()],
        "apply": [top_left.x(), top_left.y(), apply_btn.width(), apply_btn.height()],
        "scales_offered": offered,
        "scales_selectable": enabled,
        "scale_current": combo.currentData(),
    }

    escape: dict = {}
    if "--escape" in sys.argv:
        from PySide6.QtWidgets import QPushButton
        popup.move(avail.left() + 20, avail.top() + 20)   # the "drag the title bar clear" step
        one_x = combo.findData(1.0)
        combo.setCurrentIndex(one_x)
        close_btn = popup.findChild(QPushButton, "settingsClose")
        escape["found_1x"] = one_x >= 0
        escape["found_close_btn"] = close_btn is not None
        escape["title_bar_on_screen"] = avail.contains(
            popup.mapToGlobal(popup.rect().topLeft()))
        if close_btn is not None:
            close_btn.click()
            app.processEvents()
        escape["saved_scale"] = saved[-1].get("ui_scale") if saved else None
        escape["saves"] = len(saved)

    out["escape"] = escape
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())

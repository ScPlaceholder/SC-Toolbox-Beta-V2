"""sprite_pal.py — Pico on the desktop: the Drake loops, driven by Game.log through the mood engine.

    py -3.13 sprite_pal.py                         # tail the live Game.log, show the mood's loop
    py -3.13 sprite_pal.py --log <path/Game.log>
    py -3.13 sprite_pal.py --demo                  # no game: cycle every mood, 8s each
    py -3.13 sprite_pal.py --mood happy            # pin one mood (art review)
    py -3.13 sprite_pal.py --loops <folder>        # another outfit's loops (e.g. Origin)

A frameless, transparent, always-on-top window. Drag it with the left button. Right-click Pico for
Customise / Quit. PICO_CONTRACT.md, J's words: right-click on Pico re-opens the customise box, and
the box opens on FIRST LAUNCH, EVERY app start ("so users can't forget how to customize their pico").
Not a one-time "don't show again" flag; that is his call to make later.
Hover over Pico to see what he is feeling and WHY (the MoodReading). J asked for no caption on screen;
the reason stays one hover away, because a face with no stated reason is where debugging starts.

Layer B for the sprite path (see pico/sprites.py). Logic stays in pico/; this file only draws.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from PySide6.QtCore import QPoint, QSize, Qt, QTimer
from PySide6.QtGui import QMovie
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QLabel, QMenu, QSlider, QVBoxLayout, QWidget)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pico import sprites  # noqa: E402

DEFAULT_LOGS = (
    Path("C:/Star Citizen/StarCitizen/LIVE/Game.log"),
    Path("C:/Program Files/Roberts Space Industries/StarCitizen/LIVE/Game.log"),
)
HEIGHT = 280          # on-screen px for Pico; the loops are rendered much larger
POLL_MS = 1000
DEMO_S = 8.0
# ⚠ MY PICK, OPEN FOR J. events.FeedState leaves staleness undecided on purpose ("observed once
# counts as live forever"), and its stale test uses when PICO read a line, so a fresh read of an old
# log is never stale. First live run, 2026-10-01: a Game.log last written 4.8 DAYS earlier showed
# "mood=happy feed=live" and a dancing penguin. That is the lying dashboard face.py forbids.
# This window therefore judges the GAME by the log FILE's mtime: untouched for this long means the
# game is not running, and Pico shows UNKNOWN with the reason, not the last mood it remembers.
# mtime, not the parsed timestamps, because those are only "good to hours" (events.log_age_s).
GAME_QUIET_S = 15 * 60


class LogTail:
    """New lines since last call. Reopens if the game rotates the log (file shrank)."""

    def __init__(self, path: Path):
        self.path, self.pos = path, 0

    def quiet_s(self) -> float:
        try:
            return time.time() - self.path.stat().st_mtime
        except OSError:
            return float("inf")

    def read(self) -> list[str]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.pos:
            self.pos = 0
        with open(self.path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(self.pos)
            data = f.read()
            self.pos = f.tell()
        return data.splitlines()


def outfits(root: Path = sprites.DEFAULT_DIR) -> dict[str, Path]:
    """Every outfit with loops on disk: Drake's folder, plus pico_anim_sequences_<brand> beside it."""
    found = {"Drake": root} if root.is_dir() else {}
    for d in sorted(root.parent.glob(root.name + "_*")):
        if d.is_dir() and (any(d.glob("*.webp")) or any(d.glob("*.gif"))):
            found[d.name[len(root.name) + 1:].replace("_", " ").title()] = d
    return found


class Customise(QDialog):
    def __init__(self, parent, current: Path, height: int):
        super().__init__(parent)
        self.setWindowTitle("Customise Pico")
        self.outfit = QComboBox()
        self.choices = outfits()
        for name, d in self.choices.items():
            self.outfit.addItem(name, str(d))
            if d == current:
                self.outfit.setCurrentIndex(self.outfit.count() - 1)
        self.size = QSlider(Qt.Horizontal)
        self.size.setRange(140, 560)
        self.size.setValue(height)
        form = QFormLayout(self)
        form.addRow("Outfit", self.outfit)
        form.addRow("Size", self.size)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)


class Pal(QWidget):
    def __init__(self, chooser: sprites.LoopChooser, source=None, tail=None, demo=False, pinned=None):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.chooser, self.source, self.tail = chooser, source, tail
        self.demo, self.pinned = demo, pinned
        self.demo_moods = list(chooser.pools)
        self.demo_i, self.demo_t = 0, time.time()
        self.drag = None
        self.movie = None
        self.height_px = HEIGHT
        self.pic = QLabel(self)
        self.pic.setAlignment(Qt.AlignCenter)
        self.why = QLabel(self)
        self.why.setStyleSheet("color: #cfd6e4; background: rgba(20,22,30,170); padding: 2px 6px;"
                               "border-radius: 6px; font: 9pt 'Segoe UI';")
        self.why.setAlignment(Qt.AlignCenter)
        # J 2026-10-01: "We don't need the caption showing." The reason still travels with the face,
        # as a hover tooltip, because a face with no stated reason is where debugging starts.
        self.why.hide()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.pic)
        lay.addWidget(self.why)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(POLL_MS)
        self.tick()

    # what Pico feels right now, and the sentence that justifies it
    def reading(self):
        if self.pinned:
            return self.pinned, "pinned: %s" % self.pinned
        if self.demo:
            if time.time() - self.demo_t > DEMO_S:
                self.demo_i, self.demo_t = (self.demo_i + 1) % len(self.demo_moods), time.time()
            m = self.demo_moods[self.demo_i]
            return (None if m == sprites.UNKNOWN else m), "demo: %s" % m
        if self.tail is None:
            return None, "UNKNOWN: no Game.log found"
        quiet = self.tail.quiet_s()
        if quiet > GAME_QUIET_S:
            return None, "UNKNOWN: game not running (Game.log untouched %.0f min)" % (quiet / 60)
        self.source.feed_lines(self.tail.read())
        r = self.source.reading()
        return r.mood, str(r)

    def tick(self):
        mood, why = self.reading()
        self.why.setText(why if len(why) < 90 else why[:87] + "...")
        self.pic.setToolTip(why)
        path = self.chooser.on_mood(mood)
        if path is not None:
            self.play(path)

    def play(self, path: Path):
        if self.movie is not None:
            self.movie.stop()
        m = QMovie(str(path))
        m.jumpToFrame(0)
        sz = m.currentImage().size()
        if sz.height() > 0:
            m.setScaledSize(QSize(int(sz.width() * self.height_px / sz.height()), self.height_px))
        m.frameChanged.connect(self.on_frame)
        self.last_frame = 0
        self.movie = m
        self.pic.setMovie(m)
        m.start()
        self.adjustSize()

    def on_frame(self, n: int):
        # QMovie loops forever on its own; a wrap back to frame 0 is the end of one pass.
        if n == 0 and self.last_frame > 0:
            self.play(self.chooser.on_loop_end())
            return
        self.last_frame = n

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        elif e.button() == Qt.RightButton:
            menu = QMenu(self)
            menu.addAction("Customise Pico...", self.customise)
            menu.addSeparator()
            menu.addAction("Quit Pico", QApplication.quit)
            menu.exec(e.globalPosition().toPoint())

    def customise(self):
        dlg = Customise(self, self.chooser.catalog.root, self.height_px)
        if dlg.exec() != QDialog.Accepted:
            return
        self.height_px = dlg.size.value()
        root = Path(dlg.outfit.currentData())
        try:
            self.chooser = sprites.LoopChooser(sprites.Catalog.scan(root))
        except sprites.SpriteError as ex:
            # An outfit still rendering may not cover every mood yet: say so, keep the old one.
            self.why.setText("outfit not ready: %s" % ex)
            return
        self.tick()

    def mouseMoveEvent(self, e):
        if self.drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self.drag)

    def mouseReleaseEvent(self, e):
        self.drag = None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Pico on the desktop")
    ap.add_argument("--log", type=Path)
    ap.add_argument("--loops", type=Path, default=sprites.DEFAULT_DIR)
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--mood")
    a = ap.parse_args(argv)
    chooser = sprites.LoopChooser(sprites.Catalog.scan(a.loops))
    if a.mood and a.mood not in chooser.pools:
        print("unknown mood %r; known: %s" % (a.mood, ", ".join(chooser.pools)))
        return 2
    source = tail = None
    if not (a.demo or a.mood):
        from pico import events
        source = events.MoodSource(events.load_suit())
        log = a.log or next((p for p in DEFAULT_LOGS if p.exists()), None)
        if log is None:
            print("no Game.log found; pass --log, or --demo to run without the game."
                  " Pico will show UNKNOWN until a log is readable.")
        else:
            tail = LogTail(log)
            print("tailing", log)
    app = QApplication(sys.argv)
    pal = Pal(chooser, source, tail, demo=a.demo, pinned=a.mood)
    scr = app.primaryScreen().availableGeometry()
    pal.move(QPoint(scr.right() - 320, scr.bottom() - HEIGHT - 60))
    pal.show()
    QTimer.singleShot(300, pal.customise)     # first launch, every app start (J, PICO_CONTRACT.md)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

"""sprite_pal.py — Pico on the desktop: the Drake loops, driven by Game.log through the mood engine.

    py -3.13 sprite_pal.py                         # tail the live Game.log, show the mood's loop
    py -3.13 sprite_pal.py --log <path/Game.log>
    py -3.13 sprite_pal.py --demo                  # no game: cycle every mood, 8s each
    py -3.13 sprite_pal.py --mood happy            # pin one mood (art review)
    py -3.13 sprite_pal.py --loops <folder>        # another outfit's loops (e.g. Origin)

From the toolbox launcher he is the "Pico Pals" tile, which runs pico_pals_app.py: the launcher passes
window geometry and a command file, which this CLI rejects, so that file adapts them and calls main().

Moods pick his looping idle; Game.log EVENTS (docking, quantum, injury, contract complete...) play a
one-shot gesture from sprites.EVENT_LOOPS, then he goes back to his mood. Drawing a weapon in game
(slot 1, slot 2, multitool) makes him hold the matching prop until you holster (sprites.HAND_LOOPS).

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
import json
import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import QPoint, QPointF, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QMovie, QPainter, QPixmap, QRadialGradient
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QLabel, QMenu, QSlider, QVBoxLayout, QWidget)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pico import aura, snap, sprites  # noqa: E402

DEFAULT_LOGS = (
    Path("C:/Star Citizen/StarCitizen/LIVE/Game.log"),
    Path("C:/Program Files/Roberts Space Industries/StarCitizen/LIVE/Game.log"),
)
HEIGHT = 280          # on-screen px for Pico; the loops are rendered much larger
MARGIN = 0.75         # transparent room around Pico, as a fraction of his frame width, so a snapped
                      # prop held out to the side (a sign) is not cut off at the window edge
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


# Where he sat, how big, which outfit - remembered between launches (J's polish list, 2026-10-01).
SETTINGS = Path(os.environ.get("APPDATA", str(Path.home()))) / "PicoPal" / "settings.json"


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}                       # first launch, or an unreadable file: defaults, never a crash


def save_settings(d: dict) -> None:
    try:
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
        os.replace(tmp, SETTINGS)
    except OSError:
        pass                            # a read-only profile must not take Pico down


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


def outfit_name(d: Path, root: Path = sprites.DEFAULT_DIR) -> str:
    """The outfit's display name from its loop folder: Drake for the base folder, else the brand
    suffix of pico_anim_sequences_<brand>. One function, so the Customise list and the aura rule
    can never disagree about what an outfit is called."""
    d = Path(d)
    if d == root:
        return "Drake"
    if d.name.startswith(root.name + "_"):
        return d.name[len(root.name) + 1:].replace("_", " ").title()
    return d.name


def outfits(root: Path = sprites.DEFAULT_DIR) -> dict[str, Path]:
    """Every outfit with loops on disk: Drake's folder, plus pico_anim_sequences_<brand> beside it."""
    found = {"Drake": root} if root.is_dir() else {}
    for d in sorted(root.parent.glob(root.name + "_*")):
        if d.is_dir() and (any(d.glob("*.webp")) or any(d.glob("*.gif"))):
            found[outfit_name(d, root)] = d
    return found


AURA_GRID = 24         # silhouette mask resolution: a GRID x GRID thumbnail of the loop's first frame
AURA_TICK_MS = 33      # ~30 fps; the aura runs on its own clock, so loop changes never stutter it


def silhouette_cells(img: QImage, grid: int = AURA_GRID) -> tuple[list, float]:
    """Centres of the opaque cells of a loop frame, normalised to 0..1, and the cell size.

    The frame is shrunk to a grid-by-grid thumbnail (Qt averages alpha while scaling) so this is a
    few hundred pixel reads per LOOP, not per frame."""
    if img.isNull() or img.width() <= 0 or img.height() <= 0:
        return [], 0.0
    small = img.scaled(grid, grid, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    cells = []
    for gy in range(grid):
        for gx in range(grid):
            if small.pixelColor(gx, gy).alpha() >= 140:
                cells.append(((gx + 0.5) / grid, (gy + 0.5) / grid))
    return cells, 1.0 / grid


def tint_to(img: QImage, rgb) -> QImage:
    """Recolour a glow sprite to rgb, keeping its alpha and brightness: dim parts take the colour, the
    brightest core stays near white so it still reads as a glint."""
    out = img.convertToFormat(QImage.Format_ARGB32)
    r0, g0, b0 = rgb

    def luma(c):
        return (0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()) / 255.0
    # normalise to the sprite's own peak, so a saturated source (the lavender star) is not dimmed by the swap
    peak = max((luma(out.pixelColor(x, y)) for y in range(out.height()) for x in range(out.width())
                if out.pixelColor(x, y).alpha() > 0), default=1.0) or 1.0
    for y in range(out.height()):
        for x in range(out.width()):
            c = out.pixelColor(x, y)
            if c.alpha() == 0:
                continue
            lum = min(1.0, luma(c) / peak)
            core = lum ** 4
            out.setPixelColor(x, y, QColor(int(min(255, r0 * lum + (255 - r0) * core)),
                                           int(min(255, g0 * lum + (255 - g0) * core)),
                                           int(min(255, b0 * lum + (255 - b0) * core)), c.alpha()))
    return out


def round_off(img: QImage) -> QImage:
    """Fade a region crop to zero alpha outside a centred circle.

    vfx_map's region boxes are hand-read to ~10px, and s60.star_d's box catches the edge of a
    neighbouring blue object in its bottom-left corner: drawn as-is, every sparkle trailed a light
    blue speck (seen in the first proof render). The star's rays reach the box edges along the axes,
    which stay inside the circle; only the corners, where the bleed is, are cut."""
    out = img.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    w, h = out.width(), out.height()
    g = QRadialGradient(QPointF(w / 2.0, h / 2.0), max(w, h) / 2.0)
    g.setColorAt(0.0, QColor(0, 0, 0, 255))
    g.setColorAt(0.75, QColor(0, 0, 0, 255))
    g.setColorAt(1.0, QColor(0, 0, 0, 0))
    p = QPainter(out)
    p.setCompositionMode(QPainter.CompositionMode_DestinationIn)
    p.fillRect(out.rect(), g)
    p.end()
    return out


class AuraLayer(QWidget):
    """Draws an outfit's aura (aura.AURAS) over Pico. Transparent to the mouse, so hover, drag and the
    right-click menu all reach the window underneath; draws no background, only the sparkles."""

    def __init__(self, parent: "Pal"):
        super().__init__(parent)
        self.pal = parent
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAutoFillBackground(False)
        self.field = None
        self.which = None                 # the Aura currently running, so a re-set is a no-op
        self.pix = None
        self.t0 = time.monotonic()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update)
        self.hide()

    def set_aura(self, a) -> None:
        """Turn the aura on (an aura.Aura) or off (None). Same aura again keeps the running field,
        so its twinkles carry straight through a loop change."""
        if a is self.which:
            return
        self.which, self.field, self.pix = a, None, None
        if a is not None:
            try:
                clip, rrec, srec, sample = aura.load_clip(a.clip)
                sheet = QImage(str(Path(__file__).resolve().parent / "assets" / "reference" / "sheets"
                                   / srec["file"]))
                if sheet.isNull():
                    raise aura.AuraError("sheet %s not readable" % srec["file"])
                # ⚠ The map says sheet 60 blends SCREEN (glow on a black field). Over a translucent
                # desktop window there is no backdrop to screen against, and s60.star_d already
                # carries a real graded alpha (corners at 0-2), so it is drawn source-over.
                crop = sheet.copy(QRect(rrec["x"], rrec["y"], rrec["w"], rrec["h"]))
                if a.tint:
                    crop = tint_to(crop, a.tint)
                self.pix = QPixmap.fromImage(round_off(crop))
                self.field = aura.SparkleField(a, clip, sample)
            except aura.AuraError as ex:
                print("aura off: %s" % ex)       # a broken sparkle must never take Pico down
                self.which = None
        if self.field is None:
            self.timer.stop()
            self.hide()
        else:
            self.timer.start(AURA_TICK_MS)
            self.show()
            self.raise_()
            self.pal.prop_lbl.raise_()           # a prop held in front stays on top of the sparkles

    def set_area(self, cells, cell) -> None:
        if self.field is not None:
            self.field.set_area(cells, cell)

    def paintEvent(self, _e):
        if self.field is None or self.pix is None:
            return
        rect = self.pal.movie_rect()
        if rect is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        pw, ph = self.pix.width(), self.pix.height()
        for (x, y, size, alpha, rot) in self.field.states(time.monotonic() - self.t0):
            side = size * rect.height()                  # size is a fraction of Pico's height
            k = side / float(max(pw, ph))
            p.save()
            p.setOpacity(alpha)
            p.translate(QPointF(rect.x() + x * rect.width(), rect.y() + y * rect.height()))
            p.rotate(rot)
            p.scale(k, k)
            p.drawPixmap(QPointF(-pw / 2.0, -ph / 2.0), self.pix)
            p.restore()
        p.end()


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
        prefs = load_settings()
        self.gags = QCheckBox("Gag props (puppet, action figure, Whale certificate)")
        self.gags.setChecked(bool(prefs.get("gags", True)))
        self.signs = QCheckBox("Signs on game events")
        self.signs.setChecked(bool(prefs.get("signs", True)))
        self.often = QComboBox()
        for label, mins in (("every 30 min", 30), ("once an hour", 60), ("every 2 hours", 120),
                            ("every 4 hours", 240)):
            self.often.addItem(label, mins)
        cur = prefs.get("gag_cooldown_min", 60)
        self.often.setCurrentIndex(max(0, self.often.findData(cur)))
        form = QFormLayout(self)
        form.addRow("Outfit", self.outfit)
        form.addRow("Size", self.size)
        form.addRow(self.gags)
        form.addRow("Gags at most", self.often)
        form.addRow(self.signs)
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
        self.hand = sprites.HandTracker()      # what is in the player's right hand (Game.log)
        self.hand_change = None
        self.pic = QLabel(self)
        self.pic.setAlignment(Qt.AlignCenter)
        self.why = QLabel(self)
        self.why.setStyleSheet("color: #cfd6e4; background: rgba(20,22,30,170); padding: 2px 6px;"
                               "border-radius: 6px; font: 9pt 'Segoe UI';")
        self.why.setAlignment(Qt.AlignCenter)
        # J 2026-10-01: "We don't need the caption showing." The reason still travels with the face,
        # as a hover tooltip, because a face with no stated reason is where debugging starts.
        self.why.hide()
        # SNAP STATES (J 19:42): a prop is drawn by this label over (or under) the loop, moved every
        # frame to the loop's grip point from <loop>.anchors.json. One loop serves every prop.
        self.prop_lbl = QLabel(self)
        self.prop_lbl.hide()
        self.snap_props = {}
        try:
            self.snap_props = snap.load_props()
        except snap.SnapError:
            pass
        self.anchors = None
        self.prop_rec = None
        self.prop_pix = None
        # OUTFIT AURA (J 2026-10-01: Origin sparkles on every frame). Its own child widget and clock,
        # sized to the whole window; which outfit gets one is aura.AURAS, not a branch here.
        self.aura_layer = AuraLayer(self)
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
            if self.chooser.held:                 # game gone: nothing is in anyone's hand
                self.hand_change = ("holster", None)
            return None, "UNKNOWN: game not running (Game.log untouched %.0f min)" % (quiet / 60)
        # One line at a time, so every event is seen (MoodSource keeps only the LAST event).
        # The newest gesture-bearing event in this batch wins; older ones in the same second
        # would only be interrupted a frame later anyway.
        self.event = None
        for line in self.tail.read():
            hc = self.hand.feed_line(line)
            if hc is not None:
                self.hand_change = hc            # the newest hand change in this batch wins
            seen = self.source.events_seen
            self.source.feed_line(line)
            if self.source.events_seen != seen:
                et = getattr(self.source.last_event, "event_type", None)
                if et in self.chooser.events:
                    self.event = et
        r = self.source.reading()
        return r.mood, str(r)

    def tick(self):
        self.event = None
        mood, why = self.reading()
        if self.event:
            why = "event: %s | %s" % (self.event, why)
        self.why.setText(why if len(why) < 90 else why[:87] + "...")
        self.pic.setToolTip(why)
        let_go = self.chooser.expire()             # a thrown grenade never logs a holster
        if let_go is not None:
            carry, self.chooser.carry_frame = self.chooser.carry_frame, False
            self.play(let_go, carry=carry)
        if self.hand_change is not None:
            held = self.chooser.on_hand(self.hand_change, item=self.hand.item)
            self.hand_change = None
            if held is not None:
                self.play(held)
        if self.event:
            gesture = self.chooser.on_event(self.event)   # None while that event is cooling down
            if gesture is not None:
                self.play(gesture)
        path = self.chooser.on_mood(mood)
        if path is not None:
            self.play(path)

    def play(self, path: Path, carry: bool = False):
        at = self.movie.currentFrameNumber() if (carry and self.movie is not None) else 0
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
        self.setup_aura(m)
        self.setup_prop(path)
        self.pic.setMovie(m)
        sz = m.scaledSize()
        mx = int(sz.width() * MARGIN)
        self.layout().setContentsMargins(mx, int(sz.height() * 0.15), mx, 0)
        m.start()
        if at and at < m.frameCount():
            m.jumpToFrame(at)              # same pose, new eyes: no restart
            self.last_frame = at
        self.adjustSize()

    def setup_aura(self, m: QMovie):
        """Aura on or off for the current outfit, and his silhouette for this loop. Runs on every
        play(), so an outfit switch from Customise turns it on or off on the very next loop."""
        self.aura_layer.set_aura(aura.aura_for(outfit_name(self.chooser.catalog.root)))
        if self.aura_layer.field is not None:
            self.aura_layer.set_area(*silhouette_cells(m.currentImage()))

    def movie_rect(self):
        """Where the loop is drawn inside the window (the movie is centred in its label), or None."""
        if self.movie is None:
            return None
        mw = self.movie.scaledSize().width()
        if mw <= 0:
            return None
        ox = self.pic.x() + (self.pic.width() - mw) // 2
        oy = self.pic.y() + (self.pic.height() - self.height_px) // 2
        return QRect(ox, oy, mw, self.height_px)

    def resizeEvent(self, e):
        self.aura_layer.setGeometry(self.rect())
        super().resizeEvent(e)

    def setup_prop(self, path: Path):
        """Pick up the snap prop for the loop just started, if the chooser named one."""
        pid = self.chooser.prop_for()
        rec = self.snap_props.get(pid) if pid else None
        self.anchors = snap.load_anchors(path) if rec else None
        if not rec or not self.anchors:
            self.prop_rec = self.prop_pix = None
            self.prop_lbl.hide()
            return
        self.prop_rec = rec
        self.prop_pix = QPixmap(str(snap.PROPS_DIR / rec["png"]))
        self.place_prop(0)

    def place_prop(self, n: int):
        if not self.prop_rec or not self.anchors or self.prop_pix is None or self.prop_pix.isNull():
            return
        frames = self.anchors["frames"]
        f = frames[min(n, len(frames) - 1)]
        r = snap.place(self.prop_rec, f, self.anchors["belly_w"], (self.prop_pix.width(), self.prop_pix.height()))
        if r is None:
            self.prop_lbl.hide()
            return
        k = self.height_px / float(self.anchors["size"][1])      # loop pixels -> on-screen pixels
        w, h = max(1, int(r[2] * k)), max(1, int(r[3] * k))
        self.prop_lbl.setPixmap(self.prop_pix.scaled(w, h))
        self.prop_lbl.resize(w, h)
        mw = self.movie.scaledSize().width()
        ox = self.pic.x() + (self.pic.width() - mw) // 2          # the movie is centred in its label
        oy = self.pic.y() + (self.pic.height() - self.height_px) // 2
        self.prop_lbl.move(int(ox + r[0] * k), int(oy + r[1] * k))
        if self.prop_rec.get("layer") == "behind":
            self.prop_lbl.lower()
        else:
            self.prop_lbl.raise_()
        self.prop_lbl.show()

    def on_frame(self, n: int):
        self.place_prop(n)
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
        self.remember(outfit=str(root), gags=dlg.gags.isChecked(), signs=dlg.signs.isChecked(),
                      gag_cooldown_min=dlg.often.currentData())
        try:
            self.chooser = sprites.LoopChooser(sprites.Catalog.scan(root))
            self.chooser.apply_prefs(load_settings())
        except sprites.SpriteError as ex:
            # An outfit still rendering may not cover every mood yet: say so, keep the old one.
            self.why.setText("outfit not ready: %s" % ex)
            return
        self.tick()

    def mouseMoveEvent(self, e):
        if self.drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self.drag)

    def mouseReleaseEvent(self, e):
        if self.drag is not None:
            self.remember()
        self.drag = None

    def remember(self, **extra):
        d = load_settings()
        d.update({"x": self.x(), "y": self.y(), "height": self.height_px}, **extra)
        save_settings(d)


def main(argv=None, on_ready=None) -> int:
    """Run Pico. on_ready(app, pal), if given, is called once the window exists and before the event
    loop starts: pico_pals_app.py (the launcher's tile) uses it to answer show / hide / quit."""
    ap = argparse.ArgumentParser(description="Pico on the desktop")
    ap.add_argument("--log", type=Path)
    ap.add_argument("--loops", type=Path, default=sprites.DEFAULT_DIR)
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--mood")
    a = ap.parse_args(argv)
    saved = load_settings()
    loops = a.loops
    if a.loops == sprites.DEFAULT_DIR and saved.get("outfit") and Path(saved["outfit"]).is_dir():
        loops = Path(saved["outfit"])      # the outfit the user picked last time
    try:
        chooser = sprites.LoopChooser(sprites.Catalog.scan(loops))
    except sprites.SpriteError:
        chooser = sprites.LoopChooser(sprites.Catalog.scan(sprites.DEFAULT_DIR))
    chooser.apply_prefs(saved)                     # gags / signs / how often, from the Customise dialog
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
    if saved.get("height"):
        pal.height_px = int(saved["height"])
        if pal.chooser.current:                     # re-play so the first loop is the remembered size
            pal.play(pal.chooser.catalog.loops[pal.chooser.current])
    scr = app.primaryScreen().availableGeometry()
    pos = QPoint(int(saved.get("x", scr.right() - 320)), int(saved.get("y", scr.bottom() - HEIGHT - 60)))
    if not any(s.availableGeometry().contains(pos) for s in app.screens()):
        pos = QPoint(scr.right() - 320, scr.bottom() - HEIGHT - 60)   # saved spot is off every screen now
    pal.move(pos)
    pal.show()
    QTimer.singleShot(300, pal.customise)     # first launch, every app start (J, PICO_CONTRACT.md)
    if on_ready is not None:
        on_ready(app, pal)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

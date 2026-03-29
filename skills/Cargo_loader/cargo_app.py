"""
Cargo Loader — Star Citizen cargo grid viewer and container optimizer (PySide6).
Launched as a subprocess by main.py via WingmanAI.
Data sourced from sc-cargo.space (JS bundle, auto-detected URL).

Architecture:
  - cargo_engine/   : Pure logic (placement, collision, packing, rendering math)
  - ShipDataLoader  : Business logic (data loading, cache)
  - CargoRenderer   : Isometric rendering (QPainter on QWidget)
  - CargoApp        : UI shell (PySide6 widgets, layout, event wiring)

Args: <x> <y> <w> <h> <opacity> <cmd_file>
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import threading
import time

import requests

from PySide6.QtCore import Qt, QTimer, Signal, Slot, QPoint, QPointF
from PySide6.QtGui import QColor, QCursor, QPainter, QPixmap, QPolygonF, QFont, QPen, QBrush
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QSizePolicy, QSpinBox, QTabWidget,
    QGraphicsView, QGraphicsScene, QGraphicsPolygonItem, QGraphicsTextItem,
    QGraphicsItemGroup, QDialog,
    QApplication,
)

# Bootstrap project root and skill directory
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')))
from shared.app_bootstrap import bootstrap_skill  # noqa: E402
bootstrap_skill(__file__)
from shared.i18n import s_ as _
from shared.qt.theme import P, apply_theme
from shared.qt.base_window import SCWindow
from shared.qt.title_bar import SCTitleBar
from shared.qt.ipc_thread import IPCWatcher
from shared.qt.fuzzy_combo import SCFuzzyCombo
from shared.qt.animated_button import SCButton
from shared.data_utils import parse_cli_args
from shared.api_config import (
    UEX_BASE_URL, SC_CARGO_BASE_URL, SC_CARGO_HEADERS,
    SC_CARGO_HOMEPAGE_TIMEOUT, SC_CARGO_BUNDLE_TIMEOUT, SC_CARGO_UEX_TIMEOUT,
    CACHE_TTL_CARGO,
)

from cargo_engine.schema import CONTAINER_SIZES, CONTAINER_COLORS, CONTAINER_DIMS
from cargo_engine.placement import best_rotation, max_containers_in_slot
from cargo_engine.packing import place_containers_3d, build_slots
from cargo_engine.optimizer import greedy_optimize_3d, assign_slots_from_counts
from cargo_engine.rendering import (
    iso_project, auto_fit_cell, center_origin, compute_scene_extents,
    topological_sort_boxes, shade, label_color,
)
from cargo_engine.validation import validate_layout

from cargo_common import (
    CONTAINER_DIMS as LEGACY_CONTAINER_DIMS,
    CONTAINER_MAX_CH,
    load_reference_loadouts, find_reference_loadout,
)

log = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────
_DIR       = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(_DIR, ".cargo_cache.json")
CACHE_TTL  = CACHE_TTL_CARGO

HEADERS = SC_CARGO_HEADERS

REFERENCE_LOADOUTS: dict[str, dict[int, int]] = load_reference_loadouts(_DIR)

# ── Layout JSON loader ───────────────────────────────────────────────────────
LAYOUTS_DIR = os.path.join(_DIR, "layouts")


def _load_ship_layouts() -> dict[str, dict]:
    result: dict[str, dict] = {}
    if not os.path.isdir(LAYOUTS_DIR):
        return result
    import glob
    for path in glob.glob(os.path.join(LAYOUTS_DIR, "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            errors = validate_layout(data)
            if errors:
                log.warning("Layout %s has validation errors: %s", path, errors[:3])
            ship = data.get("ship", "")
            if ship and ship != "Custom":
                result[ship.lower()] = data
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            log.warning("Failed to load layout %s: %s", path, exc)
    return result


SHIP_LAYOUTS = _load_ship_layouts()


def _layout_to_slots(layout: dict) -> tuple[list[dict], tuple]:
    placements = layout.get("placements", [])
    if not placements:
        return [], (0, 0, 1, 1)
    slots = []
    for p in placements:
        dims = p["dims"]
        pw, ph, pl = dims["w"], dims["h"], dims["l"]
        px, py, pz = p["pos"]["x"], p["pos"]["y"], p["pos"]["z"]
        slots.append({
            "x": px, "y0": py, "z": pz,
            "w": pw, "h": ph, "l": pl,
            "capacity": p["scu"], "scu": p["scu"],
            "placed_size": p["scu"], "maxSize": p["scu"], "minSize": p["scu"],
        })
    x_min = min(s["x"] for s in slots)
    z_min = min(s["z"] for s in slots)
    x_max = max(s["x"] + s["w"] for s in slots)
    z_max = max(s["z"] + s["l"] for s in slots)
    return slots, (x_min, z_min, x_max, z_max)


def _find_reference_loadout(ship_name: str) -> dict[int, int] | None:
    return find_reference_loadout(ship_name, REFERENCE_LOADOUTS)


# ── Commodity colors ─────────────────────────────────────────────────────────
_COMMODITY_COLORS_PATH = os.path.join(_DIR, "commodity_colors.json")
_COMMODITY_COLORS: dict[str, str] = {}
_COMMODITY_LOCK = threading.Lock()

def _load_commodity_colors() -> dict[str, str]:
    global _COMMODITY_COLORS
    try:
        with open(_COMMODITY_COLORS_PATH, encoding="utf-8") as f:
            colors = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        colors = {}
    with _COMMODITY_LOCK:
        _COMMODITY_COLORS = colors
    return _COMMODITY_COLORS

_load_commodity_colors()


def commodity_color(name: str) -> str:
    """Return the color for a commodity name.

    Uses commodity_colors.json for known commodities.
    Generates a deterministic color from a hash for unknown ones.
    """
    with _COMMODITY_LOCK:
        cached = _COMMODITY_COLORS.get(name)
    if cached is not None:
        return cached
    # Generate from hash
    h = hashlib.md5(name.encode()).hexdigest()
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    # Ensure reasonable brightness
    brightness = 0.299 * r + 0.587 * g + 0.114 * b
    if brightness < 80:
        r = min(255, r + 80)
        g = min(255, g + 60)
        b = min(255, b + 60)
    return f"#{r:02x}{g:02x}{b:02x}"


# ── UEX commodity fetcher ────────────────────────────────────────────────────
_UEX_COMMODITIES: list[str] = []
_UEX_LOADED = threading.Event()


def _fetch_uex_commodities() -> None:
    """Fetch commodity list from UEX API in background."""
    global _UEX_COMMODITIES
    try:
        r = requests.get(
            f"{UEX_BASE_URL}/commodities",
            headers=HEADERS, timeout=SC_CARGO_UEX_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        items = data if isinstance(data, list) else data.get("data", [])
        names = []
        for item in items:
            name = item.get("name") or item.get("commodity_name") or ""
            if name:
                names.append(name)
        with _COMMODITY_LOCK:
            _UEX_COMMODITIES = sorted(set(names))
    except (requests.RequestException, ValueError, KeyError) as exc:
        log.warning("Failed to fetch UEX commodities: %s", exc)
        with _COMMODITY_LOCK:
            _UEX_COMMODITIES = sorted(_COMMODITY_COLORS.keys())
    finally:
        _UEX_LOADED.set()


def get_commodity_names() -> list[str]:
    """Return merged commodity list (UEX + commodity_colors.json keys)."""
    with _COMMODITY_LOCK:
        names = set(_UEX_COMMODITIES)
        names.update(_COMMODITY_COLORS.keys())
    return sorted(names)


# ── Palette (from shared theme) ───────────────────────────────────────────────
BG        = P.bg_primary
BG2       = P.bg_secondary
BG3       = P.bg_card
BORDER    = P.border
FG        = P.fg
FG_DIM    = P.fg_dim
ACCENT    = P.accent
GREEN     = P.green
YELLOW    = P.yellow
RED       = P.red
HEADER_BG = P.bg_header

CONT_COL    = CONTAINER_COLORS
GRID_LINE   = "#1e2740"
SLOT_FILL   = "#111827"
SLOT_OUTLINE = "#252f48"

_ROTATION_LABELS = ["0\u00b0", "90\u00b0", "180\u00b0", "270\u00b0"]


# ── Data loader ────────────────────────────────────────────────────────────────

class ShipDataLoader:
    """Loads ship data from sc-cargo.space with caching and fallback."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.ships: list = []
        self.by_name: dict = {}
        self.error: str = ""
        self.loaded = False

    def load_async(self, callback) -> None:
        t = threading.Thread(target=self._run, args=(callback,), daemon=True)
        t.start()

    def _run(self, callback) -> None:
        try:
            cached = self._load_cache()
            if cached:
                self._index(cached)
            else:
                ships = self._fetch_and_parse()
                self._save_cache(ships)
                self._index(ships)
        except (OSError, requests.RequestException, RuntimeError, ValueError, json.JSONDecodeError, KeyError, TypeError) as e:
            with self._lock:
                self.error = str(e)
            self._try_stale_cache()
        finally:
            if not self.loaded:
                with self._lock:
                    self.loaded = True
            if callback:
                callback()

    def _try_stale_cache(self) -> None:
        if not os.path.exists(CACHE_FILE):
            return
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                obj = json.load(f)
            if isinstance(obj, dict) and "ships" in obj:
                self._index(obj["ships"])
                log.info("Loaded stale cache as fallback (%d ships)", len(obj["ships"]))
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            pass

    def _fetch_and_parse(self) -> list:
        try:
            r = requests.get(f"{SC_CARGO_BASE_URL}/", headers=HEADERS, timeout=SC_CARGO_HOMEPAGE_TIMEOUT)
            m = re.search(r'src="(/assets/index-[^"]+\.js)"', r.text)
            if m:
                bundle_url = SC_CARGO_BASE_URL + m.group(1)
            else:
                raise RuntimeError("Could not find bundle URL on sc-cargo.space homepage")
        except RuntimeError:
            raise
        except requests.RequestException as exc:
            raise RuntimeError(f"Failed to fetch sc-cargo.space homepage: {exc}") from exc

        r = requests.get(bundle_url, headers=HEADERS, timeout=SC_CARGO_BUNDLE_TIMEOUT)
        r.raise_for_status()
        ships = self._parse_js(r.text)
        if not ships:
            raise ValueError("No ships parsed from bundle")
        return ships

    def _parse_js(self, text: str) -> list:
        str_vars: dict[str, str] = {}
        for m in re.finditer(r'([A-Za-z_$][A-Za-z0-9_$]*)="([^"]{1,100})"', text):
            str_vars[m.group(1)] = m.group(2)

        ship_refs = []
        for m in re.finditer(
            r'\{manufacturer:([A-Za-z_$][A-Za-z0-9_$]*)'
            r',name:([A-Za-z_$][A-Za-z0-9_$]*)'
            r',official:([A-Za-z_$][A-Za-z0-9_$]*)\}',
            text,
        ):
            ship_refs.append((m.group(1), m.group(2), m.group(3)))

        ships = []
        for mfr_v, name_v, off_v in ship_refs:
            raw = self._extract_obj(text, off_v)
            if not raw:
                continue
            try:
                j = re.sub(r'(?<!\w)!0(?!\w)', 'true', raw)
                j = re.sub(r'(?<!\w)!1(?!\w)', 'false', j)
                j = re.sub(r'([{,\[])([A-Za-z_$][A-Za-z0-9_$]*):', r'\1"\2":', j)
                obj = json.loads(j)
                ships.append({
                    "manufacturer": str_vars.get(mfr_v, mfr_v),
                    "name":         str_vars.get(name_v, name_v),
                    **obj,
                })
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                log.debug("Failed to parse ship object: %s", exc)
        return ships

    def _extract_obj(self, text: str, var_name: str) -> str | None:
        marker = var_name + "={capacity:"
        start = text.find(marker)
        if start == -1:
            return None
        obj_start = start + len(var_name) + 1
        depth = 0
        i = obj_start
        while i < len(text):
            if text[i] == "{": depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    return text[obj_start : i + 1]
            i += 1
        return None

    def _index(self, ships: list) -> None:
        local_by_name = {s["name"].lower(): s for s in ships}
        with self._lock:
            self.ships = ships
            self.by_name = local_by_name

    _EXCLUDE = {"idris-m", "idris-p", "idrisp"}

    def get_ship_names(self) -> list[str]:
        with self._lock:
            ships = self.ships
        names = set(
            s["name"] for s in ships
            if s["name"].lower() not in self._EXCLUDE
        )
        for layout_key, layout in SHIP_LAYOUTS.items():
            display = layout.get("ship") or layout.get("shipName") or layout_key.title()
            if display.lower() not in {n.lower() for n in names}:
                names.add(display)
        return sorted(names)

    def find(self, name: str) -> None:
        if not name:
            return None
        with self._lock:
            by_name = self.by_name
        key = name.strip().lower()
        if key in by_name:
            return by_name[key]
        for layout_key, layout in SHIP_LAYOUTS.items():
            display = layout.get("ship") or layout.get("shipName") or layout_key.title()
            if key == display.lower() or key == layout_key:
                cap = layout.get("totalCapacity", 0)
                return {
                    "name": display, "ref": f"layout_{layout_key}",
                    "scu": cap, "cargo": cap,
                    "maxSize": 32, "minSize": 1, "loadout": [],
                }
        for k, v in by_name.items():
            if key in k or k in key:
                return v
        tokens = set(key.split())
        best, best_score = None, 0
        for k, v in by_name.items():
            score = len(tokens & set(k.split()))
            if score > best_score:
                best, best_score = v, score
        if best_score >= 2:
            return best
        return None

    def _load_cache(self) -> list | None:
        if not os.path.exists(CACHE_FILE):
            return None
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                obj = json.load(f)
            if not isinstance(obj, dict) or "ships" not in obj:
                return None
            if time.time() - obj.get("ts", 0) < CACHE_TTL:
                return obj["ships"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            log.warning("Cache load failed: %s", exc)
        return None

    def _save_cache(self, ships: list) -> None:
        try:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "ships": ships}, f)
        except OSError as exc:
            log.warning("Cache save failed: %s", exc)


# ── Brush-aware QGraphicsView ────────────────────────────────────────────────

class _BrushView(QGraphicsView):
    """QGraphicsView that keeps a brush cursor alive through ScrollHandDrag.

    ScrollHandDrag resets the viewport cursor on every mouse event (press,
    release, move).  We intercept all three and re-apply the brush cursor
    after Qt's own handling so it never disappears mid-paint.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._brush_cursor = None

    def set_brush_cursor(self, cursor) -> None:
        self._brush_cursor = cursor
        if cursor is not None:
            self.viewport().setCursor(cursor)

    def clear_brush_cursor(self) -> None:
        self._brush_cursor = None
        self.viewport().unsetCursor()

    def _restore(self) -> None:
        if self._brush_cursor is not None:
            self.viewport().setCursor(self._brush_cursor)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        self._restore()

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self._restore()

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        self._restore()


# ── Clickable box group ──────────────────────────────────────────────────────

class _CargoBoxGroup(QGraphicsItemGroup):
    """A group of 3 face polygons + optional label for one cargo box.

    Supports click-to-assign commodity in planning mode.
    """

    def __init__(self, box_index: int, box_data: tuple, parent=None) -> None:
        super().__init__(parent)
        self.box_index = box_index
        self.box_data = box_data
        # Stable position key: (wx, wy, wz, size)
        self.pos_key: tuple = (box_data[0], box_data[1], box_data[2], box_data[6])
        self.commodity: str | None = None
        self._face_items: list[QGraphicsPolygonItem] = []
        self._label_item: QGraphicsTextItem | None = None
        self._click_callback = None
        self.setAcceptedMouseButtons(Qt.LeftButton)

    def set_click_callback(self, cb) -> None:
        self._click_callback = cb

    def add_face(self, item: QGraphicsPolygonItem) -> None:
        self._face_items.append(item)
        self.addToGroup(item)

    def set_label(self, item: QGraphicsTextItem) -> None:
        self._label_item = item
        self.addToGroup(item)

    def recolor(self, base_color: str) -> None:
        """Recolor the three faces using the given base color."""
        colors = [
            shade(base_color, 0.50),  # wallB (darker)
            shade(base_color, 0.72),  # wallA (lighter)
            base_color,               # top
        ]
        edge = shade(base_color, 0.32)
        for i, face in enumerate(self._face_items):
            if i < len(colors):
                face.setBrush(QBrush(QColor(colors[i])))
                face.setPen(QPen(QColor(edge), 1))
        if self._label_item:
            c_lft = colors[0] if colors else base_color
            self._label_item.setDefaultTextColor(QColor(label_color(c_lft)))

    def shape(self):
        """Return the full bounding rect as hit area so any click on the box fires."""
        from PySide6.QtGui import QPainterPath
        path = QPainterPath()
        path.addRect(self.childrenBoundingRect())
        return path

    def mousePressEvent(self, event) -> None:
        if self._click_callback:
            self._click_callback(self)
        else:
            super().mousePressEvent(event)


# ── Isometric Renderer (QGraphicsScene) ──────────────────────────────────────

class CargoRenderer:
    """Renders the isometric cargo grid on a QGraphicsScene.

    Uses topological sort (same algorithm as the JS editor) for correct
    draw order. Supports 4 camera rotations.
    """

    def __init__(self, scene: QGraphicsScene) -> None:
        self._scene = scene
        self._render_timer = QTimer()
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(50)
        self._pending_render_fn = None
        self._render_timer.timeout.connect(self._do_pending_render)
        self._rotation = 0
        self._box_groups: list[_CargoBoxGroup] = []
        # Key = (wx, wy, wz, size) position tuple — stable across re-renders
        self._assignments: dict[tuple, str] = {}
        self._box_click_callback = None
        # Grid dimensions (unrotated) stored after last render
        self._last_gw = 0.0
        self._last_gl = 0.0

    def set_rotation(self, rotation: int) -> None:
        self._rotation = rotation % 4

    def rotate_cw(self) -> None:
        self._rotation = (self._rotation + 1) % 4

    def rotate_ccw(self) -> None:
        self._rotation = (self._rotation - 1) % 4

    def set_box_click_callback(self, cb) -> None:
        self._box_click_callback = cb

    def schedule_render(self, render_fn) -> None:
        self._pending_render_fn = render_fn
        self._render_timer.start()

    def _do_pending_render(self) -> None:
        if self._pending_render_fn:
            self._pending_render_fn()

    def render(self, slots, bounds, slot_assignment, has_layout, current_ship,
               grid_info_callback, view_width=800, view_height=600) -> None:
        self._scene.clear()
        self._box_groups = []

        if not current_ship or not slots:
            t = self._scene.addText(
                "Select a ship to view its cargo grid",
                QFont("Consolas", 11),
            )
            t.setDefaultTextColor(QColor(FG_DIM))
            t.setPos(view_width / 4, view_height / 3)
            return

        x_min, z_min, x_max, z_max = bounds
        gw = x_max - x_min
        gl = z_max - z_min
        max_h = max((s.get("y0", 0) + s["h"] for s in slots), default=1)
        self._last_gw = gw
        self._last_gl = gl

        rotation = self._rotation

        cw_px = max(view_width, 400)
        ch_px = max(view_height, 300)

        cell = auto_fit_cell(gw, gl, max_h, cw_px, ch_px, rotation=rotation)
        ox, oy = center_origin(gw, gl, max_h, cell, cw_px, ch_px, rotation=rotation)

        def pt(wx, wy, wz) -> None:
            return iso_project(wx, wy, wz, cell, ox, oy,
                               rotation=rotation, total_gw=gw, total_gl=gl)

        # Draw ground footprints
        self._draw_ground(slots, bounds, has_layout, current_ship, pt, cell, gw, gl)

        # Collect 3D box placements
        all_boxes = self._collect_boxes(slots, bounds, slot_assignment, has_layout)
        all_boxes = topological_sort_boxes(all_boxes, rotation=rotation,
                                           total_gw=gw, total_gl=gl)

        # Draw each box with 3 faces
        for idx, (wx, wy, wz, dw, dh, dl, size) in enumerate(all_boxes):
            self._draw_box(wx, wy, wz, dw, dh, dl, size, pt, cell, idx)

        # Prune assignments for positions that no longer exist
        live_keys = {g.pos_key for g in self._box_groups}
        stale = [k for k in self._assignments if k not in live_keys]
        for k in stale:
            del self._assignments[k]

        # Info
        tot_scu = sum(s["capacity"] for s in slots)
        rot_label = _ROTATION_LABELS[rotation]
        grid_info_callback(
            f"footprint {gw}\u00d7{gl}  \u00b7  {len(slots)} slots"
            f"  \u00b7  max H:{max_h}  \u00b7  {tot_scu:,} SCU"
            f"  \u00b7  rot {rot_label}"
        )

        # Scene rect
        sl, sr, st_y, sb = compute_scene_extents(gw, gl, max_h, cell, rotation=rotation)
        PAD = 48
        sr_w = max(cw_px, int(sr - sl) + PAD * 2)
        sr_h = max(ch_px, int(sb - st_y) + PAD * 2)
        self._scene.setSceneRect(0, 0, sr_w, sr_h)

    def _draw_ground(self, slots, bounds, has_layout, current_ship, pt, cell, gw, gl) -> None:
        x_min, z_min = bounds[0], bounds[1]

        if has_layout and current_ship:
            layout_key = current_ship["name"].lower()
            layout = SHIP_LAYOUTS.get(layout_key, {})
            floor_w = layout.get("gridW", gw)
            floor_l = layout.get("gridZ", gl)
            corners = [pt(0, 0, 0), pt(floor_w, 0, 0),
                       pt(floor_w, 0, floor_l), pt(0, 0, floor_l)]
            self._add_polygon(corners, SLOT_FILL, SLOT_OUTLINE)
            if cell >= 6:
                for lx in range(floor_w + 1):
                    p1, p2 = pt(lx, 0, 0), pt(lx, 0, floor_l)
                    self._add_line(p1, p2, GRID_LINE)
                for lz in range(floor_l + 1):
                    p1, p2 = pt(0, 0, lz), pt(floor_w, 0, lz)
                    self._add_line(p1, p2, GRID_LINE)
        else:
            for slot in slots:
                x0 = slot["x"] - x_min
                yf = slot.get("y0", 0)
                z0 = slot["z"] - z_min
                w = slot["w"]
                l = slot["l"]
                corners = [pt(x0, yf, z0), pt(x0 + w, yf, z0),
                           pt(x0 + w, yf, z0 + l), pt(x0, yf, z0 + l)]
                self._add_polygon(corners, SLOT_FILL, SLOT_OUTLINE)
                if cell >= 9:
                    for lx in range(w + 1):
                        p1, p2 = pt(x0 + lx, yf, z0), pt(x0 + lx, yf, z0 + l)
                        self._add_line(p1, p2, GRID_LINE)
                    for lz in range(l + 1):
                        p1, p2 = pt(x0, yf, z0 + lz), pt(x0 + w, yf, z0 + lz)
                        self._add_line(p1, p2, GRID_LINE)

    def _collect_boxes(self, slots, bounds, slot_assignment, has_layout) -> None:
        x_min, z_min = bounds[0], bounds[1]
        all_boxes: list[tuple] = []

        if has_layout:
            for i, slot in enumerate(slots):
                asgn = slot_assignment[i] if i < len(slot_assignment) else {}
                if not asgn:
                    continue
                bx = slot["x"] - x_min
                by = slot.get("y0", 0)
                bz = slot["z"] - z_min
                original_sz = slot.get("placed_size", 0)
                is_original = (len(asgn) == 1
                               and original_sz in asgn
                               and asgn[original_sz] == 1)
                if is_original:
                    all_boxes.append((bx, by, bz,
                                      slot["w"], slot["h"], slot["l"],
                                      original_sz))
                else:
                    for (lx, ly, lz, dw, dh, dl, size) in place_containers_3d(slot, asgn):
                        all_boxes.append((bx + lx, by + ly, bz + lz,
                                          dw, dh, dl, size))
        else:
            for i, slot in enumerate(slots):
                asgn = slot_assignment[i] if i < len(slot_assignment) else {}
                if not asgn:
                    continue
                x0 = slot["x"] - x_min
                y0 = slot.get("y0", 0)
                z0 = slot["z"] - z_min
                for (lx, ly, lz, dw, dh, dl, size) in place_containers_3d(slot, asgn):
                    all_boxes.append((x0 + lx, y0 + ly, z0 + lz, dw, dh, dl, size))

        return all_boxes

    def _draw_box(self, wx, wy, wz, dw, dh, dl, size, pt, cell, box_index) -> None:
        # Determine base color: commodity assignment overrides container color
        pos_key = (wx, wy, wz, size)
        commodity = self._assignments.get(pos_key)
        if commodity:
            base = commodity_color(commodity)
        else:
            base = CONT_COL.get(size, "#888888")

        c_top   = base
        c_wallA = shade(base, 0.72)
        c_wallB = shade(base, 0.50)
        edge    = shade(base, 0.32)

        group = _CargoBoxGroup(box_index, (wx, wy, wz, dw, dh, dl, size))
        group.commodity = commodity
        group.set_click_callback(self._on_box_clicked)

        rotation = self._rotation

        # Top face (always visible regardless of rotation)
        pts_t = [pt(wx, wy + dh, wz), pt(wx + dw, wy + dh, wz),
                 pt(wx + dw, wy + dh, wz + dl), pt(wx, wy + dh, wz + dl)]

        # Wall faces depend on camera rotation (matches JS editor logic)
        # rotation 0 (NE): right (+x) wall and front (+z) wall visible
        # rotation 1 (SE): front (+z) wall and left (-x) wall visible
        # rotation 2 (SW): left (-x) wall and back (-z) wall visible
        # rotation 3 (NW): back (-z) wall and right (+x) wall visible
        wall_a_faces = [
            # rotation 0: right face (x = wx + dw)
            [pt(wx+dw, wy, wz), pt(wx+dw, wy, wz+dl),
             pt(wx+dw, wy+dh, wz+dl), pt(wx+dw, wy+dh, wz)],
            # rotation 1: front face (z = wz + dl)
            [pt(wx, wy, wz+dl), pt(wx+dw, wy, wz+dl),
             pt(wx+dw, wy+dh, wz+dl), pt(wx, wy+dh, wz+dl)],
            # rotation 2: left face (x = wx)
            [pt(wx, wy, wz+dl), pt(wx, wy, wz),
             pt(wx, wy+dh, wz), pt(wx, wy+dh, wz+dl)],
            # rotation 3: back face (z = wz)
            [pt(wx+dw, wy, wz), pt(wx, wy, wz),
             pt(wx, wy+dh, wz), pt(wx+dw, wy+dh, wz)],
        ]
        wall_b_faces = [
            # rotation 0: front face (z = wz + dl)
            [pt(wx, wy, wz+dl), pt(wx+dw, wy, wz+dl),
             pt(wx+dw, wy+dh, wz+dl), pt(wx, wy+dh, wz+dl)],
            # rotation 1: left face (x = wx)
            [pt(wx, wy, wz+dl), pt(wx, wy, wz),
             pt(wx, wy+dh, wz), pt(wx, wy+dh, wz+dl)],
            # rotation 2: back face (z = wz)
            [pt(wx+dw, wy, wz), pt(wx, wy, wz),
             pt(wx, wy+dh, wz), pt(wx+dw, wy+dh, wz)],
            # rotation 3: right face (x = wx + dw)
            [pt(wx+dw, wy, wz), pt(wx+dw, wy, wz+dl),
             pt(wx+dw, wy+dh, wz+dl), pt(wx+dw, wy+dh, wz)],
        ]

        pts_wallA = wall_a_faces[rotation]
        pts_wallB = wall_b_faces[rotation]

        # Draw order: wallB (darker), wallA (lighter), top (brightest)
        for pts, color in ((pts_wallB, c_wallB), (pts_wallA, c_wallA), (pts_t, c_top)):
            item = self._make_polygon_item(pts, color, edge)
            group.add_face(item)

        # Size label — only on default rotation (labels look messy on rotated faces)
        if rotation == 0:
            face_px_h = abs(pts_wallB[2][1] - pts_wallB[0][1])
            face_px_w = abs(pts_wallB[1][0] - pts_wallB[0][0])
            if face_px_h >= 14 and face_px_w >= 10:
                cx = sum(p[0] for p in pts_wallB) / 4
                cy = sum(p[1] for p in pts_wallB) / 4
                fs = max(6, min(int(face_px_h * 0.38), int(face_px_w * 0.28), 14))
                lbl_text = str(size)
                if commodity and face_px_w >= 30:
                    short = commodity[:4] if len(commodity) > 4 else commodity
                    lbl_text = short
                t = self._scene.addText(lbl_text, QFont("Consolas", fs, QFont.Bold))
                t.setDefaultTextColor(QColor(label_color(c_wallB)))
                t.setPos(cx - t.boundingRect().width() / 2,
                         cy - t.boundingRect().height() / 2)
                group.set_label(t)

        self._scene.addItem(group)
        self._box_groups.append(group)

    def _on_box_clicked(self, group: _CargoBoxGroup) -> None:
        if self._box_click_callback:
            self._box_click_callback(group)

    def _make_polygon_item(self, points, fill_color, outline_color) -> QGraphicsPolygonItem:
        poly = QPolygonF([QPointF(x, y) for x, y in points])
        item = QGraphicsPolygonItem(poly)
        item.setPen(QPen(QColor(outline_color), 1))
        item.setBrush(QBrush(QColor(fill_color)))
        return item

    def _add_polygon(self, points, fill_color, outline_color) -> QGraphicsPolygonItem:
        poly = QPolygonF([QPointF(x, y) for x, y in points])
        item = self._scene.addPolygon(
            poly,
            QPen(QColor(outline_color), 1),
            QColor(fill_color),
        )
        return item

    def _add_line(self, p1, p2, color) -> None:
        self._scene.addLine(p1[0], p1[1], p2[0], p2[1],
                            QPen(QColor(color), 1))


# ── Filter Dialog ────────────────────────────────────────────────────────────

class _CargoFilterDialog(QDialog):
    """Dialog to filter commodity visibility by checking/unchecking them."""

    filter_changed = Signal()

    def __init__(self, assignments: dict[tuple, str | None],
                 visibility: dict[str, bool], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("CARGO FILTER"))
        self.setFixedWidth(320)
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {BG};
                border: 1px solid {ACCENT};
            }}
        """)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)

        self._visibility = visibility
        self._checkboxes: dict[str, QPushButton] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        # Title
        title = QLabel(_("CARGO FILTER"), self)
        title.setStyleSheet(
            f"color: {ACCENT}; font-family: Electrolize, Consolas; font-size: 11pt; "
            f"font-weight: bold; background: transparent;"
        )
        layout.addWidget(title)
        layout.addSpacing(4)

        # Count commodities
        counts: dict[str, int] = {}
        for idx, commodity in assignments.items():
            name = commodity if commodity else "Unidentified"
            counts[name] = counts.get(name, 0) + 1

        # Also count unassigned boxes
        # (boxes not in assignments are "Unidentified")

        # Scrollable area for commodity rows
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(400)
        scroll.setStyleSheet(f"""
            QScrollArea {{ background: transparent; border: none; }}
            QScrollArea > QWidget > QWidget {{ background: transparent; }}
            QScrollBar:vertical {{
                background: {BG2}; width: 8px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {BORDER}; border-radius: 4px; min-height: 20px;
            }}
        """)
        scroll_widget = QWidget()
        scroll_lay = QVBoxLayout(scroll_widget)
        scroll_lay.setContentsMargins(0, 0, 0, 0)
        scroll_lay.setSpacing(2)

        for name in sorted(counts.keys()):
            row = QWidget(scroll_widget)
            row_lay = QHBoxLayout(row)
            row_lay.setContentsMargins(4, 2, 4, 2)
            row_lay.setSpacing(6)

            _checked = self._visibility.get(name, True)
            cb = QPushButton("\u2713" if _checked else "", row)
            cb.setCheckable(True)
            cb.setChecked(_checked)
            cb.setFixedSize(20, 20)
            cb.setStyleSheet(f"""
                QPushButton {{
                    background-color: {BG3};
                    border: 1px solid {BORDER};
                    color: white;
                    font-family: Consolas;
                    font-size: 11pt;
                    font-weight: bold;
                    padding: 0px;
                }}
                QPushButton:checked {{
                    background-color: {ACCENT};
                    border-color: {ACCENT};
                    color: white;
                }}
            """)
            cb.toggled.connect(lambda chk, n=name: self._on_toggle(n, chk))
            cb.toggled.connect(lambda chk, b=cb: b.setText("\u2713" if chk else ""))
            row_lay.addWidget(cb)
            self._checkboxes[name] = cb

            # Color swatch
            swatch = QWidget(row)
            swatch.setFixedSize(14, 14)
            color = commodity_color(name)
            swatch.setStyleSheet(
                f"background-color: {color}; border: 1px solid {BORDER};"
            )
            row_lay.addWidget(swatch)

            # Name + count
            lbl = QLabel(f"{name}  ({counts[name]})", row)
            lbl.setStyleSheet(
                f"color: {FG}; font-family: Consolas; font-size: 9pt; background: transparent;"
            )
            row_lay.addWidget(lbl, 1)

            scroll_lay.addWidget(row)

        scroll_lay.addStretch(1)
        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll, 1)

        # Buttons
        btn_row = QWidget(self)
        btn_lay = QHBoxLayout(btn_row)
        btn_lay.setContentsMargins(0, 6, 0, 0)
        btn_lay.setSpacing(8)

        btn_all = QPushButton(_("Check All"), btn_row)
        btn_all.setCursor(Qt.PointingHandCursor)
        btn_all.setStyleSheet(f"""
            QPushButton {{
                background-color: {BG3}; color: {FG};
                font-family: Consolas; font-size: 8pt;
                border: 1px solid {BORDER}; padding: 4px 8px;
            }}
            QPushButton:hover {{ background-color: {BORDER}; }}
        """)
        btn_all.clicked.connect(self._check_all)
        btn_lay.addWidget(btn_all)

        btn_none = QPushButton(_("Uncheck All"), btn_row)
        btn_none.setCursor(Qt.PointingHandCursor)
        btn_none.setStyleSheet(f"""
            QPushButton {{
                background-color: {BG3}; color: {FG};
                font-family: Consolas; font-size: 8pt;
                border: 1px solid {BORDER}; padding: 4px 8px;
            }}
            QPushButton:hover {{ background-color: {BORDER}; }}
        """)
        btn_none.clicked.connect(self._uncheck_all)
        btn_lay.addWidget(btn_none)

        btn_lay.addStretch(1)

        btn_close = QPushButton(_("Close"), btn_row)
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.setStyleSheet(f"""
            QPushButton {{
                background-color: {ACCENT}; color: {BG};
                font-family: Consolas; font-size: 8pt; font-weight: bold;
                border: none; padding: 4px 12px;
            }}
            QPushButton:hover {{ background-color: #6cf; }}
        """)
        btn_close.clicked.connect(self.accept)
        btn_lay.addWidget(btn_close)

        layout.addWidget(btn_row)

    def _on_toggle(self, name: str, checked: bool) -> None:
        self._visibility[name] = checked
        self.filter_changed.emit()

    def _check_all(self) -> None:
        for cb in self._checkboxes.values():
            cb.setChecked(True)
            cb.setText("\u2713")

    def _uncheck_all(self) -> None:
        for cb in self._checkboxes.values():
            cb.setChecked(False)
            cb.setText("")

    def get_visibility(self) -> dict[str, bool]:
        return dict(self._visibility)


# ── Tutorial Dialog ────────────────────────────────────────────────────────────

class _CargoTutorialDialog(QDialog):
    """Tabbed tutorial bubble for the Cargo Loader tool."""

    _TABS = [
        ("Overview",     "\U0001f6f8"),
        ("Ship & View",  "\U0001f4e1"),
        ("Cargo Config", "\U0001f4e6"),
        ("Planning",     "\U0001f58c"),
    ]

    _CONTENT = [
        # ── Overview ──────────────────────────────────────────────────────────
        """
<h3 style="color:#33ccdd;margin-top:0">Welcome to Cargo Loader</h3>
<p>Cargo Loader lets you plan and optimise how containers are loaded
onto your Star Citizen ship before you undock.</p>

<b style="color:#c8d4e8">Quick-start steps:</b>
<ol>
  <li>Select your ship from the dropdown in the header.</li>
  <li>The isometric view shows your ship's cargo grid.</li>
  <li>Use <b>Cargo Configuration</b> (right panel) to choose how many
      containers of each size you want to carry.</li>
  <li>Hit <b style="color:#44aaff">▶ Optimize</b> to auto-fill the grid,
      or enter counts manually.</li>
  <li>Switch to <b>Planning Mode</b> (below Cargo Config) to paint
      commodities onto individual boxes.</li>
</ol>

<p style="color:#5a6480;font-size:8pt">Data sourced from sc-cargo.space — click ↻ in the header to refresh.</p>
""",
        # ── Ship & View ───────────────────────────────────────────────────────
        """
<h3 style="color:#33ccdd;margin-top:0">Ship Selection &amp; Isometric View</h3>

<b style="color:#c8d4e8">Ship selector (header)</b>
<ul>
  <li>Type any part of the ship name — results filter as you type.</li>
  <li>Click <b>▼</b> to browse the full list without typing.</li>
  <li>Click <b style="color:#5a6480">↻</b> to fetch the latest ship &amp; capacity data.</li>
</ul>

<b style="color:#c8d4e8">Isometric View</b>
<ul>
  <li><b>Scroll wheel</b> — zoom in/out.</li>
  <li><b>Click &amp; drag</b> — pan the view.</li>
  <li><b>◁ ▷ buttons</b> (toolbar) — rotate the camera 90° to see all sides.</li>
  <li>Container colours match the size legend at the bottom of the view.</li>
  <li>When a commodity brush is active, <b>click any box</b> to paint it.</li>
</ul>

<b style="color:#c8d4e8">Assignments overlay</b>
<p>The translucent panel in the <b>top-left</b> of the iso view shows a live
summary of which commodities are painted and how many boxes each has,
coloured to match the commodity.</p>
""",
        # ── Cargo Config ──────────────────────────────────────────────────────
        """
<h3 style="color:#33ccdd;margin-top:0">Cargo Configuration</h3>

<b style="color:#c8d4e8">Capacity bar</b>
<p>Shows <i>used / total SCU</i>. Turns <b style="color:#ff5533">red</b>
if you exceed the ship's capacity.</p>

<b style="color:#c8d4e8">Container rows</b>
<ul>
  <li>Each row is a container size (1 SCU → 32 SCU).</li>
  <li>The coloured swatch matches the isometric view colour for that size.</li>
  <li>Spinbox shows the <b>count</b>; the right column shows the total SCU
      contributed by that size.</li>
  <li>The spinbox <b>maximum</b> is capped automatically to:
    <ul>
      <li>The physical slot limit for that size on the selected ship.</li>
      <li>The remaining total capacity after other sizes are accounted for.</li>
    </ul>
  </li>
</ul>

<b style="color:#c8d4e8">Buttons</b>
<ul>
  <li><b style="color:#44aaff">▶ Optimize</b> — greedy-fills the grid with
      the best container mix for maximum SCU usage.</li>
  <li><b style="color:#ff5533">✕ Clear</b> — zeroes all container counts.</li>
  <li><b style="color:#ffaa22">↺ Reset</b> — restores the last saved/optimised
      layout for ships with a known reference loadout.</li>
</ul>
""",
        # ── Planning Mode ─────────────────────────────────────────────────────
        """
<h3 style="color:#33ccdd;margin-top:0">Planning Mode</h3>

<b style="color:#c8d4e8">Commodity Brush</b>
<ul>
  <li>Select a commodity from the <b>COMMODITY BRUSH</b> dropdown (type to search).</li>
  <li>Once selected, the cursor in the iso view becomes a <b>paint brush</b>
      tinted in the commodity's colour.</li>
  <li>Click any container box in the iso view to assign that commodity to it.</li>
  <li>Click the same box again with a different brush to reassign it.</li>
  <li>Click with <b>no brush</b> active to clear a box's assignment.</li>
  <li>Hit <b>Clear Brush</b> to deactivate the brush without clearing assignments.</li>
</ul>

<b style="color:#c8d4e8">Assignments overlay</b>
<p>Top-left of the iso view — updates live as you paint boxes, showing
commodity totals colour-coded to their commodity colour.</p>

<b style="color:#c8d4e8">Filter</b>
<ul>
  <li>Opens a dialog listing every assigned commodity.</li>
  <li>Uncheck a commodity to <b>hide</b> those boxes in the iso view
      (useful for planning complex multi-commodity loads).</li>
  <li>Use <b>Check All / Uncheck All</b> to toggle visibility in bulk.</li>
</ul>
""",
    ]

    def __init__(self, anchor: QWidget, parent=None) -> None:
        super().__init__(parent, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._anchor = anchor  # widget to position near (refresh button)
        self._build_ui()

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Outer card
        card = QFrame(self)
        card.setObjectName("tutCard")
        card.setStyleSheet(f"""
            QFrame#tutCard {{
                background-color: {BG2};
                border: 1px solid {P.tool_cargo};
                border-radius: 4px;
            }}
        """)
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)

        # ── Header strip ──────────────────────────────────────────────────────
        hdr = QWidget(card)
        hdr.setFixedHeight(32)
        hdr.setStyleSheet(f"background-color: {BG3}; border-bottom: 1px solid {P.tool_cargo};")
        hdr_lay = QHBoxLayout(hdr)
        hdr_lay.setContentsMargins(10, 0, 6, 0)
        hdr_lay.setSpacing(6)

        icon_lbl = QLabel("\u2b21", hdr)
        icon_lbl.setStyleSheet(f"color: {P.tool_cargo}; font-size: 11pt; background: transparent;")
        hdr_lay.addWidget(icon_lbl)

        title_lbl = QLabel("CARGO LOADER  —  TUTORIAL", hdr)
        title_lbl.setStyleSheet(
            f"color: {P.tool_cargo}; font-family: Electrolize, Consolas; "
            f"font-size: 9pt; font-weight: bold; letter-spacing: 2px; background: transparent;"
        )
        hdr_lay.addWidget(title_lbl)
        hdr_lay.addStretch(1)

        btn_close = QPushButton("✕", hdr)
        btn_close.setFixedSize(26, 22)
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {FG_DIM};
                border: none; font-family: Consolas; font-size: 10pt;
            }}
            QPushButton:hover {{ color: {RED}; }}
        """)
        btn_close.clicked.connect(self.close)
        hdr_lay.addWidget(btn_close)

        card_lay.addWidget(hdr)

        # ── Tabs ──────────────────────────────────────────────────────────────
        tabs = QTabWidget(card)
        tabs.setStyleSheet(f"""
            QTabBar::tab {{
                background: {BG3}; color: {FG_DIM};
                border: none; border-bottom: 2px solid transparent;
                padding: 5px 14px;
                font-family: Consolas; font-size: 8pt; font-weight: bold;
            }}
            QTabBar::tab:hover {{ color: {FG}; background: {BORDER}; }}
            QTabBar::tab:selected {{
                color: {P.tool_cargo}; border-bottom-color: {P.tool_cargo};
                background: {BG2};
            }}
            QTabWidget::pane {{
                background: {BG2}; border: none;
            }}
        """)

        for (label, _icon), content in zip(self._TABS, self._CONTENT):
            page = QWidget()
            page_lay = QVBoxLayout(page)
            page_lay.setContentsMargins(0, 0, 0, 0)

            scroll = QScrollArea(page)
            scroll.setWidgetResizable(True)
            scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

            inner = QWidget()
            inner.setStyleSheet(f"background-color: {BG2};")
            inner_lay = QVBoxLayout(inner)
            inner_lay.setContentsMargins(16, 12, 16, 12)

            lbl = QLabel(content.strip(), inner)
            lbl.setWordWrap(True)
            lbl.setTextFormat(Qt.RichText)
            lbl.setStyleSheet(
                f"color: {FG}; font-family: Consolas; font-size: 8pt; "
                f"background: transparent; line-height: 150%;"
            )
            lbl.setOpenExternalLinks(False)
            inner_lay.addWidget(lbl)
            inner_lay.addStretch(1)

            scroll.setWidget(inner)
            page_lay.addWidget(scroll)
            tabs.addTab(page, f"{_icon}  {label}")

        card_lay.addWidget(tabs)
        lay.addWidget(card)

    def show_near(self) -> None:
        """Position the dialog just below the anchor widget and show it."""
        self.adjustSize()
        if self._anchor and self._anchor.isVisible():
            gp = self._anchor.mapToGlobal(QPoint(0, self._anchor.height() + 4))
            # Nudge left so it doesn't run off-screen
            screen = QApplication.primaryScreen().availableGeometry()
            x = min(gp.x(), screen.right() - self.width() - 8)
            self.move(x, gp.y())
        self.show()
        self.raise_()


# ── Main App ───────────────────────────────────────────────────────────────────

class CargoApp(SCWindow):
    """UI shell — PySide6 cargo loader window."""

    # Thread-safe signal for relaying callbacks from background threads
    _main_thread_call = Signal(object)

    def __init__(self, x, y, w, h, opacity, cmd_file) -> None:
        super().__init__(
            title="Cargo Loader",
            width=w, height=h, min_w=600, min_h=400,
            opacity=opacity, always_on_top=True,
        )
        self._main_thread_call.connect(self._run_on_main, Qt.QueuedConnection)

        self._cmd_file = cmd_file
        self._current_ship: dict | None = None
        self._slots: list[dict] = []
        self._bounds: tuple = (0, 0, 1, 1)
        self._slot_assignment: list[dict] = []
        self._counts: dict[int, int] = {s: 0 for s in CONTAINER_SIZES}
        self._has_layout: bool = False

        # Planning mode state
        self._selected_commodity: str | None = None
        self._commodity_visibility: dict[str, bool] = {}

        self._build_ui()
        self.restore_geometry_from_args(x, y, w, h, opacity)

        self._data = ShipDataLoader()
        self._data.load_async(lambda: self._main_thread_call.emit(self._on_data_loaded))
        self._status_lbl.setText("Loading ship data from sc-cargo.space\u2026")

        # Start UEX commodity fetch in background
        threading.Thread(target=_fetch_uex_commodities, daemon=True).start()
        # Populate commodity combo once UEX data arrives
        self._commodity_poll = QTimer(self)
        self._commodity_poll.setInterval(500)
        self._commodity_poll.timeout.connect(self._try_populate_commodities)
        self._commodity_poll.start()

        # IPC
        if cmd_file:
            self._ipc = IPCWatcher(cmd_file, poll_ms=200, parent=self)
            self._ipc.command_received.connect(self._dispatch)
            self._ipc.start()

        # Ensure clean exit: stop background workers before Qt tears down
        QApplication.instance().aboutToQuit.connect(self._on_about_to_quit)

    # ── UI ─────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = self.content_layout

        # Title bar
        title_bar = SCTitleBar(
            self, title="CARGO LOADER",
            icon_text="\u2b21", accent_color=P.tool_cargo,
            show_minimize=False,
            extra_buttons=[("Tutorial", self._show_tutorial)],
        )
        title_bar.close_clicked.connect(self.hide)
        layout.addWidget(title_bar)

        # Header
        hdr = QWidget(self)
        hdr.setFixedHeight(44)
        hdr.setStyleSheet(f"background-color: {HEADER_BG};")
        hdr_lay = QHBoxLayout(hdr)
        hdr_lay.setContentsMargins(12, 0, 12, 0)
        hdr_lay.setSpacing(8)

        self._ship_combo = SCFuzzyCombo(
            placeholder="Select a ship\u2026", max_visible=12, parent=hdr,
        )
        self._ship_combo.setFixedWidth(280)
        self._ship_combo.item_selected.connect(self._load_ship)
        hdr_lay.addWidget(self._ship_combo)

        self._btn_refresh = QPushButton("\u21bb", hdr)
        btn_refresh = self._btn_refresh
        btn_refresh.setFixedSize(32, 28)
        btn_refresh.setCursor(Qt.PointingHandCursor)
        btn_refresh.setStyleSheet(f"""
            QPushButton {{
                background-color: {BG3}; color: {FG_DIM};
                font-family: Consolas; font-size: 11pt; border: none;
            }}
            QPushButton:hover {{ background-color: {BORDER}; color: {FG}; }}
        """)
        btn_refresh.clicked.connect(self._refresh)
        hdr_lay.addWidget(btn_refresh)

        hdr_lay.addStretch(1)

        self._status_lbl = QLabel("\u2014", hdr)
        self._status_lbl.setStyleSheet(
            f"color: {FG_DIM}; font-family: Consolas; font-size: 9pt; background: transparent;"
        )
        hdr_lay.addWidget(self._status_lbl)

        layout.addWidget(hdr)

        # Body: left (grid) + right (config) — use a tab widget for grid + editor
        body = QWidget(self)
        body_lay = QHBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(0)

        # Left: tabs for Isometric View and Grid Editor
        self._view_tabs = QTabWidget(body)
        self._view_tabs.setStyleSheet(f"""
            QTabBar::tab {{
                background-color: {BG2}; color: {FG_DIM};
                border: none; border-bottom: 2px solid transparent;
                padding: 4px 12px;
                font-family: Consolas; font-size: 8pt; font-weight: bold;
            }}
            QTabBar::tab:hover {{ color: {FG}; background-color: {BG3}; }}
            QTabBar::tab:selected {{ color: {ACCENT}; border-bottom-color: {ACCENT}; background-color: {BG}; }}
            QTabWidget::pane {{ background-color: {BG}; border: none; }}
        """)

        # Tab 0: Isometric view
        iso_container = QWidget()
        iso_lay = QVBoxLayout(iso_container)
        iso_lay.setContentsMargins(0, 0, 0, 0)
        iso_lay.setSpacing(0)

        # Info toolbar
        tb = QWidget(iso_container)
        tb.setFixedHeight(26)
        tb.setStyleSheet(f"background-color: {BG3};")
        tb_lay = QHBoxLayout(tb)
        tb_lay.setContentsMargins(8, 0, 8, 0)
        tb_lay.setSpacing(4)
        self._iso_info_lbl = QLabel(
            "ISOMETRIC VIEW  \u00b7  camera: +X +Y +Z  \u00b7  top=bright  right=mid  left=dark",
            tb,
        )
        self._iso_info_lbl.setStyleSheet(
            f"color: {FG_DIM}; font-family: Consolas; font-size: 8pt; background: transparent;"
        )
        tb_lay.addWidget(self._iso_info_lbl)
        tb_lay.addStretch(1)

        # Rotation buttons
        btn_style_small = f"""
            QPushButton {{
                background-color: {BG2}; color: {ACCENT};
                font-family: Consolas; font-size: 11pt; font-weight: bold;
                border: 1px solid {BORDER}; padding: 0px 4px;
                min-width: 24px; max-width: 24px; min-height: 20px; max-height: 20px;
            }}
            QPushButton:hover {{ background-color: {BORDER}; color: {FG}; }}
        """
        btn_ccw = QPushButton("\u21ba", tb)
        btn_ccw.setToolTip("Rotate view counter-clockwise")
        btn_ccw.setCursor(Qt.PointingHandCursor)
        btn_ccw.setStyleSheet(btn_style_small)
        btn_ccw.clicked.connect(self._rotate_ccw)
        tb_lay.addWidget(btn_ccw)

        btn_cw = QPushButton("\u21bb", tb)
        btn_cw.setToolTip("Rotate view clockwise")
        btn_cw.setCursor(Qt.PointingHandCursor)
        btn_cw.setStyleSheet(btn_style_small)
        btn_cw.clicked.connect(self._rotate_cw)
        tb_lay.addWidget(btn_cw)

        tb_lay.addSpacing(8)

        self._grid_info_lbl = QLabel("", tb)
        self._grid_info_lbl.setStyleSheet(
            f"color: {ACCENT}; font-family: Consolas; font-size: 8pt; background: transparent;"
        )
        tb_lay.addWidget(self._grid_info_lbl)
        iso_lay.addWidget(tb)

        # Iso view body: graphics view + planning panel
        iso_body = QWidget(iso_container)
        iso_body_lay = QHBoxLayout(iso_body)
        iso_body_lay.setContentsMargins(0, 0, 0, 0)
        iso_body_lay.setSpacing(0)

        # Graphics view
        self._scene = QGraphicsScene(self)
        self._view = _BrushView(self._scene, iso_body)
        self._view.setStyleSheet(f"background-color: {BG}; border: none;")
        self._view.setRenderHint(QPainter.Antialiasing)
        self._view.setDragMode(QGraphicsView.ScrollHandDrag)
        iso_body_lay.addWidget(self._view, 1)

        iso_lay.addWidget(iso_body, 1)

        # Assignments overlay in the top-left corner of the iso view
        self._assignments_overlay = self._build_assignments_overlay()

        # Legend
        leg = QWidget(iso_container)
        leg.setFixedHeight(24)
        leg.setStyleSheet(f"background-color: {BG3};")
        leg_lay = QHBoxLayout(leg)
        leg_lay.setContentsMargins(6, 0, 6, 0)
        leg_lay.setSpacing(3)
        empty_lbl = QLabel("\u25a0 " + _("Empty") + "  ", leg)
        empty_lbl.setStyleSheet(f"color: {FG_DIM}; font-family: Consolas; font-size: 7pt; background: transparent;")
        leg_lay.addWidget(empty_lbl)
        for size in CONTAINER_SIZES:
            swatch = QWidget(leg)
            swatch.setFixedSize(10, 10)
            swatch.setStyleSheet(f"background-color: {CONT_COL[size]};")
            leg_lay.addWidget(swatch)
            sz_lbl = QLabel(str(size), leg)
            sz_lbl.setStyleSheet(f"color: {FG_DIM}; font-family: Consolas; font-size: 7pt; background: transparent;")
            leg_lay.addWidget(sz_lbl)
        scu_lbl = QLabel(_("SCU"), leg)
        scu_lbl.setStyleSheet(f"color: {FG_DIM}; font-family: Consolas; font-size: 7pt; background: transparent;")
        leg_lay.addWidget(scu_lbl)
        leg_lay.addStretch(1)
        iso_lay.addWidget(leg)

        self._view_tabs.addTab(iso_container, "Isometric View")
        # Only one tab — hide the tab bar entirely
        self._view_tabs.tabBar().setVisible(False)
        # Grid Editor is a standalone HTML tool; don't create QWebEngineView here
        # (spawning QtWebEngineProcess delays startup and breaks graceful shutdown).
        self._web_view = None

        body_lay.addWidget(self._view_tabs, 1)

        # Separator
        sep = QFrame(body)
        sep.setFrameShape(QFrame.VLine)
        sep.setFixedWidth(1)
        sep.setStyleSheet(f"color: {BORDER};")
        body_lay.addWidget(sep)

        # Right panel (config)
        right = QWidget(body)
        right.setFixedWidth(280)
        right.setStyleSheet(f"background-color: {BG2};")
        self._build_config_panel(right)
        body_lay.addWidget(right)

        layout.addWidget(body, 1)

        self._renderer = CargoRenderer(self._scene)
        self._renderer.set_box_click_callback(self._on_box_clicked)

    def _build_assignments_overlay(self) -> QWidget:
        """Build the assignments summary overlay pinned to the top-left of the iso view."""
        overlay = QWidget(self._view.viewport())
        overlay.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        overlay.setFixedWidth(182)
        # More opaque so text composites against a near-solid surface (avoids ClearType blur)
        overlay.setStyleSheet(
            f"background-color: rgba(9, 12, 18, 235); border: 1px solid {P.tool_cargo};"
        )

        lay = QVBoxLayout(overlay)
        lay.setContentsMargins(10, 7, 10, 8)
        lay.setSpacing(4)

        # Header: accent bar + label
        hdr_row = QWidget(overlay)
        hdr_row.setStyleSheet("background: transparent;")
        hdr_lay = QHBoxLayout(hdr_row)
        hdr_lay.setContentsMargins(0, 0, 0, 0)
        hdr_lay.setSpacing(6)

        bar = QWidget(hdr_row)
        bar.setFixedSize(3, 14)
        bar.setStyleSheet(f"background-color: {P.tool_cargo}; border: none;")
        hdr_lay.addWidget(bar)

        sum_lbl = QLabel("ASSIGNMENTS", hdr_row)
        sum_lbl.setStyleSheet(
            f"color: {P.tool_cargo}; font-family: Electrolize, Consolas; font-size: 9pt; "
            f"font-weight: bold; letter-spacing: 1px; background: transparent;"
        )
        hdr_lay.addWidget(sum_lbl)
        hdr_lay.addStretch(1)
        lay.addWidget(hdr_row)

        # Thin separator line under header
        sep = QFrame(overlay)
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"color: {P.tool_cargo}; background: transparent;")
        sep.setFixedHeight(1)
        lay.addWidget(sep)
        lay.addSpacing(1)

        self._assignment_summary_lbl = QLabel(overlay)
        self._assignment_summary_lbl.setStyleSheet(
            f"color: {FG_DIM}; font-family: Consolas; font-size: 8pt; "
            f"background: transparent;"
        )
        self._assignment_summary_lbl.setWordWrap(True)
        self._assignment_summary_lbl.setText(
            f"<span style='color:{FG_DIM}'>No assignments yet.<br>"
            f"Click boxes to assign commodities.</span>"
        )
        lay.addWidget(self._assignment_summary_lbl)

        overlay.adjustSize()
        overlay.move(8, 8)
        overlay.raise_()
        overlay.show()
        return overlay

    def _build_config_panel(self, parent) -> None:
        pad = QWidget(parent)
        pad_lay = QVBoxLayout(pad)
        pad_lay.setContentsMargins(12, 10, 12, 10)
        pad_lay.setSpacing(4)

        title = QLabel(_("CARGO CONFIGURATION"), pad)
        title.setStyleSheet(
            f"color: {ACCENT}; font-family: Consolas; font-size: 9pt; "
            f"font-weight: bold; background: transparent;"
        )
        pad_lay.addWidget(title)
        pad_lay.addSpacing(8)

        # Capacity bar area
        cap_outer = QWidget(pad)
        cap_outer.setStyleSheet(f"background-color: {BG3}; padding: 6px 8px;")
        cap_lay = QVBoxLayout(cap_outer)
        cap_lay.setContentsMargins(8, 6, 8, 6)
        cap_lay.setSpacing(4)

        cap_row = QWidget(cap_outer)
        cap_row_lay = QHBoxLayout(cap_row)
        cap_row_lay.setContentsMargins(0, 0, 0, 0)
        lbl_cap = QLabel(_("Capacity"), cap_row)
        lbl_cap.setStyleSheet(f"color: {FG_DIM}; font-family: Consolas; font-size: 9pt; background: transparent;")
        cap_row_lay.addWidget(lbl_cap)
        cap_row_lay.addStretch(1)
        self._cap_lbl = QLabel("0 / 0 SCU", cap_row)
        self._cap_lbl.setStyleSheet(
            f"color: {GREEN}; font-family: Consolas; font-size: 9pt; "
            f"font-weight: bold; background: transparent;"
        )
        cap_row_lay.addWidget(self._cap_lbl)
        cap_lay.addWidget(cap_row)

        # Bar (simple QWidget painted)
        self._bar_widget = _CapacityBar(cap_outer)
        self._bar_widget.setFixedHeight(10)
        cap_lay.addWidget(self._bar_widget)

        pad_lay.addWidget(cap_outer)
        pad_lay.addSpacing(4)

        # Separator
        sep = QFrame(pad)
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"color: {BORDER};")
        pad_lay.addWidget(sep)
        pad_lay.addSpacing(4)

        # Container rows
        self._spinboxes: dict[int, QSpinBox] = {}
        self._cont_labels: dict[int, QLabel] = {}
        for size in CONTAINER_SIZES:
            row = QWidget(pad)
            row_lay = QHBoxLayout(row)
            row_lay.setContentsMargins(0, 2, 0, 2)
            row_lay.setSpacing(4)

            swatch = QWidget(row)
            swatch.setFixedSize(10, 10)
            swatch.setStyleSheet(f"background-color: {CONT_COL[size]};")
            row_lay.addWidget(swatch)

            sz_lbl = QLabel(f"{size:>2} SCU", row)
            sz_lbl.setFixedWidth(50)
            sz_lbl.setStyleSheet(f"color: {FG}; font-family: Consolas; font-size: 9pt; background: transparent;")
            row_lay.addWidget(sz_lbl)

            sb = QSpinBox(row)
            sb.setRange(0, 9999)
            sb.setValue(0)
            sb.setFixedWidth(52)
            sb.setStyleSheet(f"""
                QSpinBox {{
                    background-color: {BG3}; color: {FG};
                    font-family: Consolas; font-size: 9pt;
                    border: 1px solid {BORDER};
                    border-right: none;
                    padding: 2px 4px;
                }}
                QSpinBox::up-button, QSpinBox::down-button {{
                    width: 0; height: 0; border: none;
                }}
            """)
            sb.valueChanged.connect(self._update_fill)
            row_lay.addWidget(sb)
            self._spinboxes[size] = sb

            # Stacked ▲ / ▼ arrow buttons
            _arrow_btn_style = """
                QPushButton {{
                    background-color: {bg}; color: {fg};
                    border: 1px solid {border};
                    font-family: Consolas; font-size: 6pt;
                    padding: 0px; margin: 0px;
                }}
                QPushButton:hover {{ background-color: {hover}; color: {accent}; }}
                QPushButton:pressed {{ background-color: {accent}; color: {dark}; }}
            """
            arrow_wrap = QWidget(row)
            arrow_wrap.setFixedWidth(18)
            arrow_v = QVBoxLayout(arrow_wrap)
            arrow_v.setContentsMargins(0, 0, 0, 0)
            arrow_v.setSpacing(0)

            btn_up = QPushButton("\u25b2", arrow_wrap)
            btn_up.setCursor(Qt.PointingHandCursor)
            btn_up.setFixedHeight(14)
            btn_up.setStyleSheet(_arrow_btn_style.format(
                bg=BG3, fg=FG_DIM, border=BORDER, hover=BORDER, accent=ACCENT, dark=BG
            ) + f"QPushButton {{ border-bottom: none; }}")
            btn_up.clicked.connect(lambda _, s=sb: s.setValue(s.value() + 1))
            arrow_v.addWidget(btn_up)

            btn_dn = QPushButton("\u25bc", arrow_wrap)
            btn_dn.setCursor(Qt.PointingHandCursor)
            btn_dn.setFixedHeight(14)
            btn_dn.setStyleSheet(_arrow_btn_style.format(
                bg=BG3, fg=FG_DIM, border=BORDER, hover=BORDER, accent=ACCENT, dark=BG
            ))
            btn_dn.clicked.connect(lambda _, s=sb: s.setValue(max(0, s.value() - 1)))
            arrow_v.addWidget(btn_dn)

            row_lay.addWidget(arrow_wrap)

            eq_lbl = QLabel("=   0", row)
            eq_lbl.setFixedWidth(55)
            eq_lbl.setAlignment(Qt.AlignRight)
            eq_lbl.setStyleSheet(
                f"color: {FG_DIM}; font-family: Consolas; font-size: 8pt; background: transparent;"
            )
            row_lay.addWidget(eq_lbl)
            self._cont_labels[size] = eq_lbl

            pad_lay.addWidget(row)

        pad_lay.addSpacing(8)
        sep2 = QFrame(pad)
        sep2.setFrameShape(QFrame.HLine)
        sep2.setStyleSheet(f"color: {BORDER};")
        pad_lay.addWidget(sep2)
        pad_lay.addSpacing(4)

        # Buttons
        btn_row = QWidget(pad)
        btn_lay = QHBoxLayout(btn_row)
        btn_lay.setContentsMargins(0, 0, 0, 0)
        btn_lay.setSpacing(8)

        btn_optimize = QPushButton("\u25b6  " + _("Optimize"), btn_row)
        btn_optimize.setCursor(Qt.PointingHandCursor)
        btn_optimize.setStyleSheet(f"""
            QPushButton {{
                background-color: {ACCENT}; color: {BG};
                font-family: Consolas; font-size: 9pt; font-weight: bold;
                border: none; padding: 6px 10px;
            }}
            QPushButton:hover {{ background-color: #6cf; }}
        """)
        btn_optimize.clicked.connect(self._optimize)
        btn_lay.addWidget(btn_optimize)

        btn_clear = QPushButton("\u2715 " + _("Clear"), btn_row)
        btn_clear.setCursor(Qt.PointingHandCursor)
        btn_clear.setStyleSheet(f"""
            QPushButton {{
                background-color: {BG3}; color: {RED};
                font-family: Consolas; font-size: 9pt;
                border: none; padding: 6px 8px;
            }}
            QPushButton:hover {{ background-color: {BORDER}; }}
        """)
        btn_clear.clicked.connect(self._clear_containers)
        btn_lay.addWidget(btn_clear)

        btn_lay.addStretch(1)

        btn_reset = QPushButton("\u21ba " + _("Reset"), btn_row)
        btn_reset.setCursor(Qt.PointingHandCursor)
        btn_reset.setStyleSheet(f"""
            QPushButton {{
                background-color: {BG3}; color: {YELLOW};
                font-family: Consolas; font-size: 9pt;
                border: none; padding: 6px 8px;
            }}
            QPushButton:hover {{ background-color: {BORDER}; }}
        """)
        btn_reset.clicked.connect(self._reset_containers)
        btn_lay.addWidget(btn_reset)

        pad_lay.addWidget(btn_row)

        pad_lay.addSpacing(8)
        sep3 = QFrame(pad)
        sep3.setFrameShape(QFrame.HLine)
        sep3.setStyleSheet(f"color: {BORDER};")
        pad_lay.addWidget(sep3)
        pad_lay.addSpacing(4)

        self._info_lbl = QLabel(_("Select a ship to begin."), pad)
        self._info_lbl.setStyleSheet(
            f"color: {FG_DIM}; font-family: Consolas; font-size: 8pt; background: transparent;"
        )
        self._info_lbl.setWordWrap(True)
        pad_lay.addWidget(self._info_lbl)

        pad_lay.addSpacing(8)

        # Planning mode section
        sep_plan = QFrame(pad)
        sep_plan.setFrameShape(QFrame.HLine)
        sep_plan.setStyleSheet(f"color: {BORDER};")
        pad_lay.addWidget(sep_plan)
        pad_lay.addSpacing(4)

        plan_title = QLabel(_("PLANNING MODE"), pad)
        plan_title.setStyleSheet(
            f"color: {ACCENT}; font-family: Electrolize, Consolas; font-size: 10pt; "
            f"font-weight: bold; background: transparent;"
        )
        pad_lay.addWidget(plan_title)
        pad_lay.addSpacing(4)

        brush_lbl = QLabel(_("COMMODITY BRUSH"), pad)
        brush_lbl.setStyleSheet(
            f"color: {FG_DIM}; font-family: Electrolize, Consolas; font-size: 8pt; "
            f"background: transparent;"
        )
        pad_lay.addWidget(brush_lbl)

        self._commodity_combo = SCFuzzyCombo(placeholder="Select commodity\u2026", parent=pad)
        self._commodity_combo.item_selected.connect(self._on_commodity_selected)
        pad_lay.addWidget(self._commodity_combo)
        pad_lay.addSpacing(4)

        sel_row = QWidget(pad)
        sel_row_lay = QHBoxLayout(sel_row)
        sel_row_lay.setContentsMargins(0, 0, 0, 0)
        sel_row_lay.setSpacing(6)

        self._brush_swatch = QWidget(sel_row)
        self._brush_swatch.setFixedSize(16, 16)
        self._brush_swatch.setStyleSheet(
            f"background-color: {BG3}; border: 1px solid {BORDER};"
        )
        sel_row_lay.addWidget(self._brush_swatch)

        self._brush_name_lbl = QLabel(_("No brush"), sel_row)
        self._brush_name_lbl.setStyleSheet(
            f"color: {FG_DIM}; font-family: Consolas; font-size: 8pt; background: transparent;"
        )
        sel_row_lay.addWidget(self._brush_name_lbl, 1)

        pad_lay.addWidget(sel_row)
        pad_lay.addSpacing(6)

        btn_clear_brush = QPushButton(_("Clear Brush"), pad)
        btn_clear_brush.setCursor(Qt.PointingHandCursor)
        btn_clear_brush.setStyleSheet(f"""
            QPushButton {{
                background-color: {BG3}; color: {FG_DIM};
                font-family: Consolas; font-size: 8pt;
                border: 1px solid {BORDER}; padding: 4px 8px;
            }}
            QPushButton:hover {{ background-color: {BORDER}; color: {FG}; }}
        """)
        btn_clear_brush.clicked.connect(self._clear_brush)
        pad_lay.addWidget(btn_clear_brush)

        pad_lay.addSpacing(4)

        btn_filter = QPushButton(_("Filter"), pad)
        btn_filter.setCursor(Qt.PointingHandCursor)
        btn_filter.setStyleSheet(f"""
            QPushButton {{
                background-color: {BG3}; color: {ACCENT};
                font-family: Consolas; font-size: 8pt; font-weight: bold;
                border: 1px solid {ACCENT}; padding: 4px 8px;
            }}
            QPushButton:hover {{ background-color: {ACCENT}; color: {BG}; }}
        """)
        btn_filter.clicked.connect(self._open_filter_dialog)
        pad_lay.addWidget(btn_filter)

        pad_lay.addStretch(1)

        parent_lay = QVBoxLayout(parent)
        parent_lay.setContentsMargins(0, 0, 0, 0)
        parent_lay.addWidget(pad)

    # ── Data ───────────────────────────────────────────────────────────────────

    @Slot(object)
    def _run_on_main(self, fn) -> None:
        """Execute *fn()* on the main thread.  Safe to emit from any thread."""
        try:
            fn()
        except Exception:
            log.exception("_run_on_main crashed")

    def _on_data_loaded(self) -> None:
        self._data.loaded = True
        if self._data.error:
            self._status_lbl.setText(f"Error: {self._data.error}")
            return
        names = self._data.get_ship_names()
        self._ship_combo.set_items(names)
        self._status_lbl.setText(f"Ready \u2014 {len(names)} ships  |  sc-cargo.space")

    def _try_populate_commodities(self) -> None:
        """Poll until UEX commodities are loaded, then populate the combo."""
        if _UEX_LOADED.is_set():
            self._commodity_poll.stop()
            names = get_commodity_names()
            self._commodity_combo.set_items(names)

    def _load_ship(self, name: str) -> None:
        if not name:
            return
        ship = self._data.find(name)
        if not ship:
            self._status_lbl.setText(f"Ship not found: '{name}'")
            return
        self._current_ship = ship
        self._ship_combo.set_text(ship["name"])

        layout_key = ship["name"].lower()
        layout = SHIP_LAYOUTS.get(layout_key)
        if layout:
            self._slots, self._bounds = _layout_to_slots(layout)
            grid_w = layout.get("gridW", self._bounds[2])
            grid_z = layout.get("gridZ", self._bounds[3])
            self._bounds = (0, 0, grid_w, grid_z)
            self._has_layout = True
        else:
            self._slots, self._bounds = build_slots(ship)
            self._has_layout = False

        self._slot_assignment = []
        # Clear planning mode assignments and brush for new ship
        self._renderer._assignments.clear()
        self._commodity_visibility.clear()
        self._selected_commodity = None
        self._view.clear_brush_cursor()

        self._update_spinbox_limits()

        if self._has_layout:
            self._reset_containers()
        else:
            self._optimize()

        cap = ship.get("capacity") or ship.get("cargo") or ship.get("scu") or 0
        if layout and not cap:
            cap = layout.get("totalCapacity", 0)
        ship["capacity"] = cap
        mfr = ship.get("manufacturer", ship.get("company_name", ""))
        n_grp = len(ship.get("groups", []))
        max_h = max((s.get("y0", 0) + s["h"] for s in self._slots), default=1)
        self._info_lbl.setText(
            f"{mfr}  {ship['name']}\n"
            f"{cap:,} SCU  \u00b7  {len(self._slots)} grid slot(s)\n"
            f"{n_grp} section(s)  \u00b7  max slot height {max_h} SCU"
        )
        self._status_lbl.setText(f"{ship['name']}  \u2014  {cap:,} SCU")
        self._update_assignment_summary()

    def _show_tutorial(self) -> None:
        """Show (or raise) the tutorial popup anchored to the refresh button."""
        if not hasattr(self, "_tutorial_dlg") or self._tutorial_dlg is None:
            self._tutorial_dlg = _CargoTutorialDialog(
                anchor=self._btn_refresh, parent=self
            )
            self._tutorial_dlg.setFixedSize(460, 420)
            self._tutorial_dlg.finished.connect(
                lambda: setattr(self, "_tutorial_dlg", None)
            )
        self._tutorial_dlg.show_near()

    def _refresh(self) -> None:
        self._status_lbl.setText("Refreshing data\u2026")
        if os.path.exists(CACHE_FILE):
            try:
                os.remove(CACHE_FILE)
            except OSError as exc:
                log.warning("Failed to remove cache file: %s", exc)
        self._data = ShipDataLoader()
        self._data.load_async(lambda: self._main_thread_call.emit(self._on_data_loaded))

    # ── Rotation ──────────────────────────────────────────────────────────────

    def _rotate_cw(self) -> None:
        self._renderer.rotate_cw()
        self._update_iso_info_label()
        self._render_grid()

    def _rotate_ccw(self) -> None:
        self._renderer.rotate_ccw()
        self._update_iso_info_label()
        self._render_grid()

    def _update_iso_info_label(self) -> None:
        rot = self._renderer._rotation
        cam_labels = [
            "+X +Y +Z",
            "+Z +Y -X",
            "-X +Y -Z",
            "-Z +Y +X",
        ]
        self._iso_info_lbl.setText(
            f"ISOMETRIC VIEW  \u00b7  camera: {cam_labels[rot]}"
            f"  \u00b7  top=bright  right=mid  left=dark"
        )

    # ── Planning Mode ─────────────────────────────────────────────────────────

    @staticmethod
    def _make_brush_cursor(hex_color: str) -> QCursor:
        """Return a custom paint-brush QCursor tinted with the commodity color."""
        sz = 28
        px = QPixmap(sz, sz)
        px.fill(Qt.transparent)
        p = QPainter(px)
        p.setRenderHint(QPainter.Antialiasing)

        # ── Handle (wooden, diagonal) ─────────────────────────
        p.setPen(QPen(QColor("#b07a3a"), 3, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(sz - 4, 3, 14, 14)

        # ── Ferrule (silver band at handle/bristle join) ──────
        p.setPen(QPen(QColor("#8899aa"), 2))
        p.setBrush(QBrush(QColor("#8899aa")))
        p.drawRect(11, 12, 5, 4)

        # ── Bristles (commodity color, flared at tip) ─────────
        tip = QColor(hex_color)
        p.setPen(QPen(tip.darker(140), 1))
        p.setBrush(QBrush(tip))
        # Triangle: top-center at ferrule, splaying to bottom-left tip
        pts = QPolygonF([
            QPointF(13, 16),
            QPointF(10, 16),
            QPointF(3,  sz - 3),
            QPointF(9,  sz - 3),
        ])
        p.drawPolygon(pts)

        p.end()
        # Hotspot at the bristle tip (bottom-left of bristles)
        return QCursor(px, 4, sz - 4)

    def _on_commodity_selected(self, name: str) -> None:
        if not name:
            return
        self._selected_commodity = name
        color = commodity_color(name)
        self._brush_swatch.setStyleSheet(
            f"background-color: {color}; border: 1px solid {BORDER};"
        )
        self._brush_name_lbl.setText(name)
        self._brush_name_lbl.setStyleSheet(
            f"color: {FG}; font-family: Consolas; font-size: 8pt; background: transparent;"
        )
        self._view.set_brush_cursor(self._make_brush_cursor(color))

    def _clear_brush(self) -> None:
        self._selected_commodity = None
        self._brush_swatch.setStyleSheet(
            f"background-color: {BG3}; border: 1px solid {BORDER};"
        )
        self._brush_name_lbl.setText(_("No brush"))
        self._brush_name_lbl.setStyleSheet(
            f"color: {FG_DIM}; font-family: Consolas; font-size: 8pt; background: transparent;"
        )
        self._view.clear_brush_cursor()

    def _on_box_clicked(self, group: _CargoBoxGroup) -> None:
        """Handle a box click in planning mode."""
        if self._selected_commodity is None:
            # If no brush, clicking clears the assignment
            if group.pos_key in self._renderer._assignments:
                del self._renderer._assignments[group.pos_key]
                group.commodity = None
                # Restore original color
                size = group.box_data[6]
                base = CONT_COL.get(size, "#888888")
                group.recolor(base)
                self._update_assignment_summary()
                self._apply_visibility_filter()
            return

        # Assign commodity to this box
        commodity = self._selected_commodity
        self._renderer._assignments[group.pos_key] = commodity
        group.commodity = commodity
        color = commodity_color(commodity)
        group.recolor(color)

        # Ensure visibility entry exists
        if commodity not in self._commodity_visibility:
            self._commodity_visibility[commodity] = True

        self._update_assignment_summary()
        self._apply_visibility_filter()

    def _update_assignment_summary(self) -> None:
        """Update the assignment summary label in the planning panel."""
        assignments = self._renderer._assignments
        if not assignments:
            self._assignment_summary_lbl.setText(
                f"<span style='color:{FG_DIM}'>No assignments yet.<br>"
                f"Click boxes to assign commodities.</span>"
            )
            self._assignments_overlay.adjustSize()
            return

        counts: dict[str, int] = {}
        for commodity in assignments.values():
            counts[commodity] = counts.get(commodity, 0) + 1

        total = sum(counts.values())
        lines = [f"<span style='color:{FG_DIM}'>{total} box(es) assigned:</span>"]
        for name in sorted(counts.keys()):
            color = commodity_color(name)
            lines.append(
                f"<span style='color:{color}'>&#8194;{name}: {counts[name]}</span>"
            )
        self._assignment_summary_lbl.setText("<br>".join(lines))
        self._assignments_overlay.adjustSize()

    def _open_filter_dialog(self) -> None:
        """Open the cargo filter dialog."""
        # Build full assignment map including unassigned as "Unidentified"
        all_assignments: dict[tuple, str | None] = {}
        for group in self._renderer._box_groups:
            commodity = self._renderer._assignments.get(group.pos_key)
            all_assignments[group.pos_key] = commodity if commodity else "Unidentified"

        if not all_assignments:
            return

        # Ensure all commodities have visibility entries
        for commodity in all_assignments.values():
            if commodity not in self._commodity_visibility:
                self._commodity_visibility[commodity] = True

        dlg = _CargoFilterDialog(
            all_assignments, self._commodity_visibility, parent=self
        )
        dlg.filter_changed.connect(lambda: self._apply_visibility_from_dialog(dlg))
        dlg.exec()
        # Update visibility after dialog closes
        self._commodity_visibility = dlg.get_visibility()
        self._apply_visibility_filter()

    def _apply_visibility_from_dialog(self, dlg: _CargoFilterDialog) -> None:
        """Live update visibility while the filter dialog is open."""
        self._commodity_visibility = dlg.get_visibility()
        self._apply_visibility_filter()

    def _apply_visibility_filter(self) -> None:
        """Set opacity on box groups based on commodity visibility."""
        for group in self._renderer._box_groups:
            commodity = self._renderer._assignments.get(group.pos_key)
            name = commodity if commodity else "Unidentified"
            visible = self._commodity_visibility.get(name, True)
            group.setOpacity(1.0 if visible else 0.15)

    # ── Rendering ──────────────────────────────────────────────────────────────

    def _render_grid(self) -> None:
        vw = max(self._view.viewport().width(), 400)
        vh = max(self._view.viewport().height(), 300)
        self._renderer.render(
            self._slots, self._bounds, self._slot_assignment,
            self._has_layout, self._current_ship,
            lambda text: self._grid_info_lbl.setText(text),
            view_width=vw, view_height=vh,
        )
        # Re-apply visibility filter after re-render
        self._apply_visibility_filter()

    # ── Container calc ─────────────────────────────────────────────────────────

    def _update_spinbox_limits(self) -> None:
        """Set each spinbox's maximum to the physical capacity for that container size."""
        self._phys_max: dict[int, int] = {}
        for size in CONTAINER_SIZES:
            if self._slots:
                phys = sum(
                    max_containers_in_slot(size, s["w"], s["h"], s["l"])
                    for s in self._slots
                )
            else:
                phys = 9999
            self._phys_max[size] = phys
            sb = self._spinboxes[size]
            sb.setMaximum(phys)
            sb.setToolTip(f"Max: {phys}")
            # Clamp current value if it already exceeds the new cap
            if sb.value() > phys:
                sb.setValue(phys)

    def _compute_slot_phys_max(self) -> dict[int, int]:
        """Physical max per size, excluding slots already occupied by other sizes.

        For each container size S we greedy-claim slots for every OTHER size T
        (up to count[T] slots whose placed_size == T), then count how many
        size-S containers fit in the remaining unclaimed slots.
        This prevents, e.g., 4 SCU boxes being counted as fitting in 24 SCU slots
        that are already full of 24 SCU containers.
        """
        result: dict[int, int] = {}
        for size in CONTAINER_SIZES:
            # Count slots that OTHER sizes need to claim
            others_claimed: dict[int, int] = {}
            for other in CONTAINER_SIZES:
                if other == size:
                    continue
                n = self._get_count(other)
                if n:
                    others_claimed[other] = others_claimed.get(other, 0) + n

            total = 0
            remaining_claims = dict(others_claimed)
            for slot in self._slots:
                ps = slot.get("placed_size", 0)
                if ps > 0 and remaining_claims.get(ps, 0) > 0:
                    # This slot is occupied by another size — skip it
                    remaining_claims[ps] -= 1
                else:
                    total += max_containers_in_slot(
                        size, slot["w"], slot["h"], slot["l"]
                    )
            result[size] = total
        return result

    def _get_count(self, size: int) -> int:
        return self._spinboxes[size].value()

    def _update_fill(self) -> None:
        cap = self._current_ship.get("capacity", 0) if self._current_ship else 0
        used = sum(self._get_count(s) * s for s in CONTAINER_SIZES)

        # Tighten each spinbox: min(slot-aware physical max, remaining SCU // size)
        if cap > 0 and hasattr(self, "_phys_max"):
            slot_phys = self._compute_slot_phys_max()
            for size in CONTAINER_SIZES:
                used_by_others = used - self._get_count(size) * size
                remaining = max(0, cap - used_by_others)
                dyn_max = min(slot_phys.get(size, 9999), remaining // size)
                sb = self._spinboxes[size]
                sb.blockSignals(True)
                sb.setMaximum(dyn_max)
                sb.setToolTip(f"Max: {dyn_max}")
                if sb.value() > dyn_max:
                    sb.setValue(dyn_max)
                sb.blockSignals(False)
            # Recompute used after any clamping
            used = sum(self._get_count(s) * s for s in CONTAINER_SIZES)

        pct = min(used / cap, 1.0) if cap > 0 else 0.0

        color = RED if used > cap else GREEN
        self._cap_lbl.setText(f"{used:,} / {cap:,} SCU")
        self._cap_lbl.setStyleSheet(
            f"color: {color}; font-family: Consolas; font-size: 9pt; "
            f"font-weight: bold; background: transparent;"
        )
        self._bar_widget.set_values(pct, color)

        for size in CONTAINER_SIZES:
            n = self._get_count(size)
            self._cont_labels[size].setText(f"= {n * size:>5,}")

        # Update counts dict
        for s in CONTAINER_SIZES:
            self._counts[s] = self._get_count(s)

        self._update_assignment()
        self._render_grid()

    def _update_assignment(self) -> None:
        if self._has_layout:
            counts = dict(self._counts)
            remaining = dict(counts)
            self._slot_assignment = [{} for _ in self._slots]

            for i, slot in enumerate(self._slots):
                sz = slot.get("placed_size", 0)
                if sz and sz > 0 and sz in remaining and remaining[sz] > 0:
                    self._slot_assignment[i] = {sz: 1}
                    remaining[sz] -= 1

            for i, slot in enumerate(self._slots):
                if self._slot_assignment[i]:
                    continue
                slot_vol = slot.get("placed_size", 0)
                if slot_vol <= 0:
                    continue
                fill = {}
                vol_left = slot_vol
                for sz in sorted(remaining.keys(), reverse=True):
                    if sz > vol_left or remaining[sz] <= 0:
                        continue
                    n = min(remaining[sz], vol_left // sz)
                    if n > 0:
                        fill[sz] = n
                        remaining[sz] -= n
                        vol_left -= n * sz
                    if vol_left <= 0:
                        break
                if fill:
                    self._slot_assignment[i] = fill
        else:
            self._slot_assignment = assign_slots_from_counts(self._slots, self._counts)

    def _optimize(self) -> None:
        if not self._current_ship or not self._slots:
            return
        ship_name = self._current_ship.get("name", "")
        ref = _find_reference_loadout(ship_name)
        result = ref if ref is not None else greedy_optimize_3d(self._slots)
        for s in CONTAINER_SIZES:
            self._spinboxes[s].blockSignals(True)
            self._spinboxes[s].setValue(0)
            self._spinboxes[s].blockSignals(False)
        for size, count in result.items():
            if size in self._spinboxes:
                self._spinboxes[size].blockSignals(True)
                self._spinboxes[size].setValue(count)
                self._spinboxes[size].blockSignals(False)
        self._update_fill()

    def _reset_containers(self) -> None:
        for s in CONTAINER_SIZES:
            self._spinboxes[s].blockSignals(True)
            self._spinboxes[s].setValue(0)
            self._spinboxes[s].blockSignals(False)
        if self._has_layout and self._current_ship:
            layout_key = self._current_ship["name"].lower()
            layout = SHIP_LAYOUTS.get(layout_key)
            if layout:
                containers = layout.get("containers", {})
                for size_str, count in containers.items():
                    sz = int(size_str)
                    if sz in self._spinboxes:
                        self._spinboxes[sz].blockSignals(True)
                        self._spinboxes[sz].setValue(int(count))
                        self._spinboxes[sz].blockSignals(False)
        self._slot_assignment = []
        self._update_fill()

    def _clear_containers(self) -> None:
        for s in CONTAINER_SIZES:
            self._spinboxes[s].blockSignals(True)
            self._spinboxes[s].setValue(0)
            self._spinboxes[s].blockSignals(False)
        self._slot_assignment = []
        self._update_fill()

    # ── Shutdown ──────────────────────────────────────────────────────────────

    def _on_about_to_quit(self) -> None:
        """Stop background workers before Qt tears down."""
        if hasattr(self, '_commodity_poll'):
            self._commodity_poll.stop()

    # ── Command dispatch ──────────────────────────────────────────────────────

    @Slot(dict)
    def _dispatch(self, cmd: dict) -> None:
        t = cmd.get("type", "")
        if t == "show":
            self.show()
            self.raise_()
        elif t == "hide":
            self.hide()
        elif t == "set_ship":
            name = cmd.get("ship", "") or cmd.get("name", "")
            if name and self._data.loaded:
                self._load_ship(name)
                self.show()
                self.raise_()
        elif t == "optimize":
            self._optimize()
        elif t == "reset":
            self._reset_containers()
        elif t == "set_container":
            try:
                size = int(cmd.get("size", 0))
                count = int(cmd.get("count", 0))
            except (ValueError, TypeError):
                return
            if size in self._spinboxes:
                self._spinboxes[size].setValue(count)
        elif t == "import_layout":
            containers = cmd.get("containers", {})
            for size, count in containers.items():
                try:
                    s = int(size)
                    if s in self._spinboxes:
                        self._spinboxes[s].setValue(int(count))
                except (ValueError, TypeError):
                    pass
        elif t == "refresh":
            self._refresh()
        elif t == "quit":
            QApplication.instance().quit()


class _CapacityBar(QWidget):
    """Simple capacity bar widget painted with QPainter."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pct = 0.0
        self._color = GREEN

    def set_values(self, pct: float, color: str) -> None:
        self._pct = pct
        self._color = color
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(BORDER))
        w = int(self.width() * min(self._pct, 1.0))
        if w > 0:
            from PySide6.QtCore import QRect
            painter.fillRect(QRect(0, 0, w, self.height()), QColor(self._color))
        painter.end()


def main() -> None:
    from shared.crash_logger import init_crash_logging
    log = init_crash_logging("cargo")
    try:
        a = parse_cli_args(sys.argv[1:], {"w": 1200, "h": 700})

        app = QApplication(sys.argv)
        apply_theme(app)

        window = CargoApp(a["x"], a["y"], a["w"], a["h"], a["opacity"], a["cmd_file"])
        window.show()
        sys.exit(app.exec())
    except Exception:
        log.critical("FATAL crash in cargo main()", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

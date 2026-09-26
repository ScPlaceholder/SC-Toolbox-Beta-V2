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

from PySide6.QtCore import Qt, QTimer, Signal, Slot, QPoint, QPointF, QUrl
from PySide6.QtGui import (QColor, QCursor, QFontMetrics, QPainter, QPainterPath, QPixmap, QPolygonF, QFont,
                           QPen, QBrush, QKeySequence, QShortcut, QDesktopServices)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QSizePolicy, QSpinBox, QTabWidget,
    QGraphicsView, QGraphicsScene, QGraphicsPolygonItem, QGraphicsTextItem,
    QGraphicsItemGroup, QDialog, QFileDialog,
    QApplication, QLineEdit, QTreeWidget, QTreeWidgetItem, QHeaderView, QSlider,
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
    topological_sort_boxes, shade, label_color, iso_unproject,
)
from cargo_engine.manual_place import PlacementContext, rotate_yaw, move_box, is_item, OK
from cargo_engine import item_catalog
from cargo_engine.item_catalog import (
    CATEGORIES as ITEM_CATEGORIES, CATEGORY_COLORS as ITEM_COLORS,
    COMPONENT_CATEGORIES,
)
from cargo_engine.validation import validate_layout
from cargo_engine import crate_items
from crate_ui import CratePanel, CrateWindow

# Personal crates are items of their own category (not an Items-tree category:
# they have their own row of buttons and a tab each).
ITEM_COLORS = {**ITEM_COLORS, "crate": crate_items.CRATE_COLOR}

# Hover highlight: outline colour and how far the fill moves towards white.
HOVER_EDGE = "#ffffff"
HOVER_LIGHTEN = 0.28

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

# Grid data source switch. "scunpacked" (default since 2026-09-25, J: "Let's trust the
# datamine files") uses datamine/cargo_grids_scunpacked.json, rebuilt from the game's own
# data by datamine/refresh_grids.py (no network needed at load time); it also carries over
# every ship the game files lack (concepts), so no ship disappears. "sc_cargo_space" uses
# the old sc-cargo.space scrape cached in .cargo_cache.json. Choose with:
#   {"grid_source": "sc_cargo_space"}   in cargo_loader_config.json
CONFIG_FILE      = os.path.join(_DIR, "cargo_loader_config.json")
SCUNPACKED_FILE  = os.path.join(_DIR, "datamine", "cargo_grids_scunpacked.json")


def load_loader_config() -> dict:
    try:
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def grid_source() -> str:
    src = load_loader_config().get("grid_source", "scunpacked")
    return src if src in ("sc_cargo_space", "scunpacked") else "scunpacked"

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


# Mission cargo (J, 2026-09-26): contract boxes are painted by mission, not by
# commodity, so up to ten contracts in one hold can be told apart. Listed first
# in the brush; colours picked to stay distinct from each other.
MISSION_CARGO: dict[str, str] = {
    f"Mission Cargo {i}": c for i, c in enumerate(
        ["#ff4d6d", "#ffb703", "#8ac926", "#00b4d8", "#9d4edd",
         "#ff7f11", "#f15bb5", "#06d6a0", "#e9c46a", "#4361ee"], 1)
}
# Ship fuel hauled in cargo (J, 2026-09-26), always in the brush after the missions.
FUEL_CARGO: dict[str, str] = {"Hydrogen Fuel": "#8fd3ff", "Quantum Fuel": "#c77dff"}
PINNED_BRUSH = {**MISSION_CARGO, **FUEL_CARGO}


def commodity_color(name: str) -> str:
    """Return the color for a commodity name.

    Mission cargo has fixed colours; commodity_colors.json covers known
    commodities; unknown ones get a deterministic colour from a hash.
    """
    if name in PINNED_BRUSH:
        return PINNED_BRUSH[name]
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
    return list(PINNED_BRUSH) + sorted(names - set(PINNED_BRUSH))


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

# Items tab (J, 2026-09-26). An item that breaks a container rule still
# places, tinted this amber, with the reason in the status line.
ITEM_WARN = "#ffb000"


def item_catalog_path() -> str | None:
    """ship-items.json from the scunpacked cache (shared/scunpacked.py)."""
    return item_catalog.default_path()


def crate_data_dir() -> str | None:
    """Where the crate item lists live: the scunpacked cache of the pinned
    build (fps-items.json, ship-items.json, items.json + the derived index)."""
    try:
        from shared import scunpacked
    except Exception:                                   # noqa: BLE001
        return None
    return scunpacked.cache_dir()


def uex_cache_path() -> str:
    """The Item Finder's UEX items cache, read-only (never fetched from here)."""
    return crate_items.default_uex_cache(_DIR)


def crate_no(key) -> int | None:
    """The number of a placed crate from its item key ("<class>#<n>")."""
    if not isinstance(key, str) or "#" not in key:
        return None
    try:
        return int(key.rsplit("#", 1)[1])
    except ValueError:
        return None


def _mix(a: str, b: str, t: float) -> str:
    """Blend hex colour a toward b by t (0..1)."""
    ca, cb = QColor(a), QColor(b)
    return QColor(round(ca.red() + (cb.red() - ca.red()) * t),
                  round(ca.green() + (cb.green() - ca.green()) * t),
                  round(ca.blue() + (cb.blue() - ca.blue()) * t)).name()


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
            scunpacked = self._load_scunpacked()
            if scunpacked is not None:
                self._index(scunpacked)
            else:
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

        # Match ship reference objects with manufacturer/name/official in any order.
        # Values may be variable references or inline strings.
        _VAL = r'(?:[A-Za-z_$][A-Za-z0-9_$]*|"[^"]{0,120}")'
        ship_refs: list[tuple[str, str, str]] = []
        seen_off: set[str] = set()
        for m in re.finditer(
            rf'\{{(?=[^{{}}]{{1,300}}\bmanufacturer:({_VAL}))'
            rf'(?=[^{{}}]{{1,300}}\bname:({_VAL}))'
            rf'(?=[^{{}}]{{1,300}}\bofficial:({_VAL}))'
            rf'[^{{}}]{{1,300}}\}}',
            text,
        ):
            obj_text = m.group(0)
            mfr_m  = re.search(rf'\bmanufacturer:({_VAL})', obj_text)
            name_m = re.search(rf'\bname:({_VAL})', obj_text)
            off_m  = re.search(rf'\bofficial:({_VAL})', obj_text)
            if not (mfr_m and name_m and off_m):
                continue
            mfr_v  = mfr_m.group(1)
            name_v = name_m.group(1)
            off_v  = off_m.group(1)
            # off_v must be a bare variable (the cargo data object reference)
            if off_v.startswith('"') or off_v in seen_off:
                continue
            seen_off.add(off_v)
            ship_refs.append((mfr_v, name_v, off_v))

        def _resolve(val: str) -> str:
            """Resolve a string variable reference or strip inline string quotes."""
            if val.startswith('"'):
                return val[1:-1]
            return str_vars.get(val, val)

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
                    "manufacturer": _resolve(mfr_v),
                    "name":         _resolve(name_v),
                    **obj,
                })
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                log.debug("Failed to parse ship object: %s", exc)
        return ships

    def _extract_obj(self, text: str, var_name: str) -> str | None:
        # Fast path: cargo object starts with capacity (most common)
        obj_start = -1
        for suffix in ("={capacity:", "={"):
            marker = var_name + suffix
            pos = text.find(marker)
            if pos != -1:
                obj_start = pos + len(var_name) + 1
                break
        if obj_start == -1:
            return None
        depth = 0
        i = obj_start
        while i < len(text):
            if text[i] == "{": depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    raw = text[obj_start : i + 1]
                    # For the loose match, only accept objects that contain capacity
                    if suffix != "={capacity:" and "capacity:" not in raw:
                        return None
                    return raw
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

    def _load_scunpacked(self) -> list | None:
        """Ships from datamine/cargo_grids_scunpacked.json when the
        grid_source config setting is "scunpacked"; None otherwise."""
        if grid_source() != "scunpacked":
            return None
        if not os.path.exists(SCUNPACKED_FILE):
            raise RuntimeError(
                "grid_source is 'scunpacked' but %s is missing. "
                "Rebuild it with: python datamine/refresh_grids.py" % SCUNPACKED_FILE
            )
        with open(SCUNPACKED_FILE, encoding="utf-8") as f:
            obj = json.load(f)
        if not isinstance(obj, dict) or not isinstance(obj.get("ships"), list):
            raise RuntimeError("%s is not a valid converted grid file" % SCUNPACKED_FILE)
        log.info("Loaded %d ships from scunpacked grid file", len(obj["ships"]))
        return obj["ships"]

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
        # Drag-and-drop hooks (set by CargoApp). Each returns True if it
        # consumed the event because a box drag is in progress.
        self.drag_key_handler = None
        self.drag_right_click_handler = None
        # Manual placement hooks (set by CargoApp): pointer hover with no
        # button held, a click on empty grid (not a pan), pointer leaving.
        self.hover_handler = None
        self.empty_click_handler = None
        self.leave_handler = None
        # Alt+left-click (set by CargoApp): hide the boxes resting on the box
        # under the pointer. Returns True if it consumed the click.
        self.alt_click_handler = None
        self._press_at = None
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

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

    def _scene_at(self, event):
        return self.mapToScene(event.position().toPoint())

    def _box_under(self, event) -> bool:
        for it in self.items(event.position().toPoint()):
            g = it if isinstance(it, _CargoBoxGroup) else it.group()
            if isinstance(g, _CargoBoxGroup):
                return True
        return False

    def mousePressEvent(self, event):
        if (event.button() == Qt.RightButton and self.drag_right_click_handler
                and self.drag_right_click_handler(self._scene_at(event))):
            event.accept()
            return
        if (event.button() == Qt.LeftButton
                and event.modifiers() & Qt.AltModifier
                and self.alt_click_handler is not None
                and self.alt_click_handler(self._scene_at(event))):
            event.accept()
            return
        self._press_at = event.position() if event.button() == Qt.LeftButton else None
        super().mousePressEvent(event)
        self._restore()

    def mouseReleaseEvent(self, event):
        press, self._press_at = self._press_at, None
        super().mouseReleaseEvent(event)
        self._restore()
        if (event.button() == Qt.LeftButton and press is not None
                and self.empty_click_handler is not None
                and (event.position() - press).manhattanLength()
                < QApplication.startDragDistance()
                and not self._box_under(event)):
            self.empty_click_handler(self._scene_at(event))

    def leaveEvent(self, event):
        super().leaveEvent(event)
        if self.leave_handler is not None:
            self.leave_handler()

    def keyPressEvent(self, event):
        if self.drag_key_handler and self.drag_key_handler(event):
            event.accept()
            return
        super().keyPressEvent(event)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        self._restore()
        if event.buttons() == Qt.NoButton and self.hover_handler is not None:
            self.hover_handler(self._scene_at(event))


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
        self._drag_owner = None   # object with box_press/box_move/box_release
        # Items: their own outline pen, and the warnings that tint them amber
        self.item_pen: QPen | None = None
        self.warnings: list[str] = []
        # Hover highlight (view only): lighter fill + bright outline.
        self.highlighted: bool = False
        self._shape_cache = None
        self.setAcceptedMouseButtons(Qt.LeftButton)

    def set_click_callback(self, cb) -> None:
        self._click_callback = cb

    def add_face(self, item: QGraphicsPolygonItem) -> None:
        self._face_items.append(item)
        self._shape_cache = None
        self.addToGroup(item)

    def set_highlight(self, on: bool, base_color: str) -> None:
        """Hover highlight on/off; *base_color* is the box's normal colour."""
        self.highlighted = bool(on)
        self.recolor(base_color)

    def set_label(self, item: QGraphicsTextItem) -> None:
        self._label_item = item
        self.addToGroup(item)

    def recolor(self, base_color: str) -> None:
        """Recolor the three faces using the given base color."""
        if self.warnings:
            base_color = _mix(base_color, ITEM_WARN, 0.55)
        if self.highlighted:
            base_color = _mix(base_color, "#ffffff", HOVER_LIGHTEN)
        colors = [
            shade(base_color, 0.50),  # wallB (darker)
            shade(base_color, 0.72),  # wallA (lighter)
            base_color,               # top
        ]
        edge = shade(base_color, 0.32)
        if self.highlighted:
            pen = QPen(QColor(HOVER_EDGE), 2)
            pen.setCosmetic(True)
        elif self.item_pen is not None:
            pen = QPen(self.item_pen)
        else:
            pen = QPen(QColor(edge), 1)
        for i, face in enumerate(self._face_items):
            if i < len(colors):
                face.setBrush(QBrush(QColor(colors[i])))
                face.setPen(pen)
        if self._label_item and self.item_pen is None:
            c_lft = colors[0] if colors else base_color
            self._label_item.setDefaultTextColor(QColor(label_color(c_lft)))

    def shape(self):
        """Hit area = the box's drawn silhouette (its three faces together).

        It used to be the bounding rectangle, so a box drawn in front took
        clicks (and would take the hover) on the empty corners of that
        rectangle, over the box actually visible behind it."""
        if self._shape_cache is None:
            path = QPainterPath()
            path.setFillRule(Qt.WindingFill)
            for face in self._face_items:
                path.addPolygon(self.mapFromItem(face, face.polygon()))
                path.closeSubpath()
            if self._face_items:
                path = path.simplified()
            else:
                path.addRect(self.childrenBoundingRect())
            self._shape_cache = path
        return self._shape_cache

    def set_drag_owner(self, owner) -> None:
        self._drag_owner = owner

    def mousePressEvent(self, event) -> None:
        # Accept the press so this item grabs the mouse (move/release follow).
        # The click action now fires on RELEASE, and only if the mouse did not
        # travel far enough to become a drag.
        if self._drag_owner is not None:
            self._drag_owner.box_press(self, event.scenePos(), event.screenPos())
            event.accept()
        elif self._click_callback:
            self._click_callback(self)
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_owner is not None:
            self._drag_owner.box_move(self, event.scenePos(), event.screenPos())
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._drag_owner is not None:
            was_drag = self._drag_owner.box_release(self, event.scenePos())
            if not was_drag and self._click_callback:
                self._click_callback(self)
            event.accept()
        else:
            super().mouseReleaseEvent(event)


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
        self._drag_owner = None
        # Grid dimensions (unrotated) stored after last render
        self._last_gw = 0.0
        self._last_gl = 0.0
        # Drag-and-drop support: projection of the last render, the boxes it
        # drew (world coords), and an optional hand-arranged box list that
        # replaces the auto-packer's output until the counts change.
        self._proj: tuple | None = None          # (cell, ox, oy, rotation, gw, gl)
        self._pt = None
        self._last_boxes: list[tuple] = []
        self._manual_boxes: list[tuple] | None = None
        self._ghost = None
        # Items tab: placed items (x, y, z, w, h, l, key) — a str key where a
        # container has its SCU — kept apart from the containers so counts,
        # Optimize and Auto mode never touch them. _item_defs maps key ->
        # catalogue entry; _item_flags maps an item tuple -> its warnings.
        self._items: list[tuple] = []
        self._last_items: list[tuple] = []
        self._item_defs: dict[str, dict] = {}
        self._item_flags: dict[tuple, list[str]] = {}
        # View-only state (never saved, never counted, no placement rule sees
        # it): the hovered box, boxes peeled off a stack, and a height cap.
        self._hover: _CargoBoxGroup | None = None
        self._hover_key: tuple | None = None
        self._peeled: set[tuple] = set()
        self._layer_cap: int | None = None       # show boxes whose base y < cap
        self._levels: int = 1                    # height of the view, in cells

    def item_base_color(self, key: str) -> str:
        cat = (self._item_defs.get(key) or {}).get("category")
        return shade(ITEM_COLORS.get(cat, "#9e9e9e"), 0.62)

    def base_color_for(self, group) -> str:
        """Unpainted colour of a drawn box (container size or item category)."""
        size = group.box_data[6]
        if isinstance(size, str):
            return self.item_base_color(size)
        return CONT_COL.get(size, "#888888")

    def set_rotation(self, rotation: int) -> None:
        self._rotation = rotation % 4

    # ── Hover highlight + hidden boxes (view only) ───────────────────────────

    def color_for(self, group) -> str:
        """The colour a drawn box shows right now (paint wins over base)."""
        c = self._assignments.get(group.pos_key)
        return commodity_color(c) if c else self.base_color_for(group)

    def set_hover(self, group) -> bool:
        """Highlight *group* (None = nothing). Restyles only the old and the
        new box. Returns True if the hovered box changed."""
        old = self._hover
        if group is old:
            return False
        self._hover = None
        if old is not None:
            try:
                old.set_highlight(False, self.color_for(old))
            except RuntimeError:          # its scene item is already gone
                pass
        if group is not None:
            group.set_highlight(True, self.color_for(group))
            self._hover = group
        self._hover_key = tuple(group.box_data) if group is not None else None
        return True

    def is_hidden(self, box) -> bool:
        box = tuple(box)
        return box in self._peeled or (
            self._layer_cap is not None and box[1] >= self._layer_cap)

    def apply_hidden(self) -> int:
        """Show/hide the drawn boxes; returns how many are hidden."""
        n = 0
        for g in self._box_groups:
            hide = self.is_hidden(g.box_data)
            g.setVisible(not hide)
            n += hide
        if self._hover is not None and not self._hover.isVisible():
            self.set_hover(None)
        return n

    def hidden_count(self) -> int:
        return sum(1 for g in self._box_groups if not g.isVisible())

    def boxes_above(self, box) -> list[tuple]:
        """Every box (container, item, crate) whose footprint overlaps *box*
        and whose base is higher."""
        x, y, z, w, _h, l, _k = box
        box = tuple(box)
        return [tuple(o) for o in list(self._last_boxes) + list(self._last_items)
                if tuple(o) != box and o[1] > y
                and o[0] < x + w and x < o[0] + o[3]
                and o[2] < z + l and z < o[2] + o[5]]

    def peel(self, box) -> list[tuple]:
        """Hide the boxes resting above *box*; returns the ones newly hidden."""
        above = [o for o in self.boxes_above(box) if not self.is_hidden(o)]
        self._peeled.update(above)
        self.apply_hidden()
        return above

    def set_layer_cap(self, cap: int | None) -> int:
        self._layer_cap = None if cap is None or cap >= self._levels else int(cap)
        return self.apply_hidden()

    def show_all(self) -> None:
        self._peeled.clear()
        self._layer_cap = None
        self.apply_hidden()

    def reveal_under(self, box) -> bool:
        """A box was just put down at *box*: show the hidden boxes it rests
        on (footprint overlap, lower base), and drop the height cap if the new
        box would sit above it. Returns True if anything was shown again."""
        x, y, z, w, _h, l, _k = box
        under = {p for p in self._peeled
                 if p[1] < y and p[0] < x + w and x < p[0] + p[3]
                 and p[2] < z + l and z < p[2] + p[5]}
        self._peeled -= under
        capped = self._layer_cap is not None and y >= self._layer_cap
        if capped:
            self._layer_cap = None
        return bool(under) or capped

    def rotate_cw(self) -> None:
        self._rotation = (self._rotation + 1) % 4

    def rotate_ccw(self) -> None:
        self._rotation = (self._rotation - 1) % 4

    def set_box_click_callback(self, cb) -> None:
        self._box_click_callback = cb

    def set_drag_owner(self, owner) -> None:
        self._drag_owner = owner

    def unproject(self, scene_x: float, scene_y: float, wy: float = 0.0):
        """Scene point -> continuous world (x, z) on the plane at height wy."""
        if not self._proj:
            return None
        cell, ox, oy, rotation, gw, gl = self._proj
        return iso_unproject(scene_x, scene_y, wy, cell, ox, oy,
                             rotation=rotation, total_gw=gw, total_gl=gl)

    def show_ghost(self, wx, wy, wz, dw, dh, dl, valid: bool, warn: bool = False) -> None:
        """Draw (or move) the translucent landing preview. Green = valid,
        amber = an item that places with a warning, red = refused."""
        self.clear_ghost()
        if self._pt is None:
            return
        if valid and warn:
            fill, edge = QColor(ITEM_WARN), QColor("#ffe0a0")
        else:
            fill = QColor("#4caf50" if valid else "#f44336")
            edge = QColor("#b9f6ca" if valid else "#ffcdd2")
        fill.setAlpha(120)
        grp = QGraphicsItemGroup()
        # footprint on the floor it lands on, then the three visible faces
        for pts in ([self._pt(wx, wy, wz), self._pt(wx + dw, wy, wz),
                     self._pt(wx + dw, wy, wz + dl), self._pt(wx, wy, wz + dl)],
                    *self._face_points(wx, wy, wz, dw, dh, dl, self._pt)):
            item = QGraphicsPolygonItem(QPolygonF([QPointF(x, y) for x, y in pts]))
            item.setBrush(QBrush(fill))
            item.setPen(QPen(edge, 2))
            grp.addToGroup(item)
        grp.setZValue(10_000)
        grp.setAcceptedMouseButtons(Qt.NoButton)
        grp.ghost_valid = valid
        grp.ghost_warn = bool(valid and warn)
        self._scene.addItem(grp)
        self._ghost = grp

    def clear_ghost(self) -> None:
        if self._ghost is not None:
            try:
                self._scene.removeItem(self._ghost)
            except RuntimeError:
                pass
            self._ghost = None

    def schedule_render(self, render_fn) -> None:
        self._pending_render_fn = render_fn
        self._render_timer.start()

    def _do_pending_render(self) -> None:
        if self._pending_render_fn:
            self._pending_render_fn()

    def render(self, slots, bounds, slot_assignment, has_layout, current_ship,
               grid_info_callback, view_width=800, view_height=600) -> None:
        self._scene.clear()
        self._hover = None                 # its item was just deleted
        self._ghost = None
        self._box_groups = []
        self._last_boxes = []
        self._last_items = []
        self._proj = None
        self._pt = None

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

        self._proj = (cell, ox, oy, rotation, gw, gl)
        self._pt = pt

        # Draw ground footprints
        self._draw_ground(slots, bounds, has_layout, current_ship, pt, cell, gw, gl)

        # Collect 3D box placements (a hand-arranged list wins over the packer)
        if self._manual_boxes is not None:
            all_boxes = list(self._manual_boxes)
        else:
            all_boxes = self._collect_boxes(slots, bounds, slot_assignment, has_layout)
        # Items share the painter's sort with the containers, but stay out of
        # _last_boxes (the container list every count and drag reads).
        all_boxes = topological_sort_boxes(list(all_boxes) + list(self._items),
                                           rotation=rotation,
                                           total_gw=gw, total_gl=gl)
        self._last_boxes = [tuple(b) for b in all_boxes if not is_item(b)]
        self._last_items = [tuple(b) for b in all_boxes if is_item(b)]

        # Draw each box with 3 faces
        for idx, (wx, wy, wz, dw, dh, dl, size) in enumerate(all_boxes):
            if isinstance(size, str):
                self._draw_item(wx, wy, wz, dw, dh, dl, size, pt, cell, idx)
            else:
                self._draw_box(wx, wy, wz, dw, dh, dl, size, pt, cell, idx)

        # Hidden boxes: forget ones that no longer exist, hide the rest, and
        # keep the hover on the same box if it is still there and visible.
        top = max((b[1] + b[4] for b in all_boxes), default=0)
        self._levels = max(1, int(-(-max(max_h, top) // 1)))
        if self._layer_cap is not None and self._layer_cap >= self._levels:
            self._layer_cap = None
        self._peeled &= set(self._last_boxes) | set(self._last_items)
        self.apply_hidden()
        key, self._hover_key = self._hover_key, None
        if key is not None:
            g = next((g for g in self._box_groups
                      if tuple(g.box_data) == key and g.isVisible()), None)
            if g is not None:
                self.set_hover(g)

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
        if self._drag_owner is not None:
            group.set_drag_owner(self._drag_owner)

        pts_wallB, pts_wallA, pts_t = self._face_points(wx, wy, wz, dw, dh, dl, pt)
        rotation = self._rotation

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

    def _draw_item(self, wx, wy, wz, dw, dh, dl, key, pt, cell, box_index) -> None:
        """An item: darker category fill, a dashed outline in the category
        colour, and a short name label on its top, so it never reads as a
        numbered SCU container. Amber tint + solid amber outline = warning."""
        box = (wx, wy, wz, dw, dh, dl, key)
        d = self._item_defs.get(key) or {}
        cat_col = ITEM_COLORS.get(d.get("category"), "#9e9e9e")
        pos_key = (wx, wy, wz, key)
        commodity = self._assignments.get(pos_key)
        warnings = list(self._item_flags.get(box, []))

        group = _CargoBoxGroup(box_index, box)
        group.commodity = commodity
        group.warnings = warnings
        if warnings:
            pen = QPen(QColor(ITEM_WARN), 2)
        else:
            pen = QPen(QColor(cat_col), 2)
            if d.get("crate_no") is None:        # crates: solid, they are boxes
                pen.setStyle(Qt.DashLine)
        pen.setCosmetic(True)
        group.item_pen = pen
        group.set_click_callback(self._on_box_clicked)
        if self._drag_owner is not None:
            group.set_drag_owner(self._drag_owner)

        pts_wallB, pts_wallA, pts_t = self._face_points(wx, wy, wz, dw, dh, dl, pt)
        for pts in (pts_wallB, pts_wallA, pts_t):
            group.add_face(self._make_polygon_item(pts, "#000000", "#000000"))
        group.recolor(commodity_color(commodity) if commodity
                      else self.item_base_color(key))

        # Short name on the top face (items are often one cell: the top is the
        # face that is always there).
        tx = [p[0] for p in pts_t]
        ty = [p[1] for p in pts_t]
        top_w = max(tx) - min(tx)
        if d.get("crate_no") is not None and top_w >= 8:
            # Personal crate: its number, as big as the lid allows.
            text = str(d["crate_no"])
            fs = max(8, min(int(top_w * 0.42 / max(len(text), 1) * 1.6), 30))
            t = self._scene.addText(text, QFont("Consolas", fs, QFont.Bold))
            t.setDefaultTextColor(QColor(ITEM_WARN if warnings else "#ffffff"))
            t.setPos(sum(tx) / 4 - t.boundingRect().width() / 2,
                     sum(ty) / 4 - t.boundingRect().height() / 2)
            group.set_label(t)
        elif top_w >= 14:
            # A longer short name, fitted to the lid: the largest font from 10
            # down to 6 pt that fits, and only then a visible "..." cut. The old
            # hard 7-character cut gave "Colossu" and "Cryo-St" (J, 2026-09-26).
            from cargo_engine.item_catalog import abbrev as _abbrev
            full = _abbrev(d.get("name") or "", n=18) if d.get("name") else ""
            text = full or d.get("label") or key[:6]
            room = top_w * 0.6          # the lid is a diamond: narrower than its span where the text sits
            fs = 10
            while fs > 6 and QFontMetrics(QFont("Consolas", fs, QFont.Bold)).horizontalAdvance(text) > room:
                fs -= 1
            fm = QFontMetrics(QFont("Consolas", fs, QFont.Bold))
            if fm.horizontalAdvance(text) > room:
                text = fm.elidedText(text, Qt.ElideRight, int(room))
            t = self._scene.addText(text, QFont("Consolas", fs, QFont.Bold))
            t.setToolTip(d.get("name") or text)
            t.setDefaultTextColor(QColor(ITEM_WARN if warnings else "#ffffff"))
            t.setPos(sum(tx) / 4 - t.boundingRect().width() / 2,
                     sum(ty) / 4 - t.boundingRect().height() / 2)
            group.set_label(t)

        self._scene.addItem(group)
        self._box_groups.append(group)

    def _face_points(self, wx, wy, wz, dw, dh, dl, pt):
        """Screen polygons (wallB, wallA, top) for a box at the current camera."""
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

        return wall_b_faces[rotation], wall_a_faces[rotation], pts_t

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

    # Plain labels, no emoji: an emoji falls back to a font whose advance is
    # ~4x the Consolas glyph, and the tab bar then wants 1104px for six tabs
    # in a 620px dialog -- half of them behind scroll arrows. Measured
    # offscreen 2026-09-26; the four-tab version had the same fault at 460px.
    _TABS = [
        "Overview",
        "Ship & View",
        "Containers",
        "Items & Crates",
        "Commodities",
        "See Inside",
    ]

    _CONTENT = [
        # ── Overview ──────────────────────────────────────────────────────────
        """
<h3 style="color:#33ccdd;margin-top:0">Welcome to Cargo Loader</h3>
<p>Work out what actually fits in your hold before you undock: containers,
loose items such as ore pods and ship components, and numbered personal
crates you fill item by item.</p>

<b style="color:#c8d4e8">Quick-start steps:</b>
<ol>
  <li>Pick your ship in the header dropdown.</li>
  <li>The isometric view draws that ship's real cargo grids.</li>
  <li>You start in <b>✋ Manual</b> on an empty hold: click a size in
      <b>CARGO CONFIGURATION</b> (right panel), then click the grid to
      drop a box there.</li>
  <li>Prefer it done for you? Hit <b style="color:#44aaff">▶ Optimize</b>,
      or switch to <b>⚙ Auto</b> and type counts.</li>
  <li><b>PLANNING MODE</b> (lower right) has two tabs:
      <b>Commodities</b> paints what is in each box, <b>Items</b> places
      items and personal crates.</li>
  <li><b>⬇ Save Plan</b> keeps the whole arrangement; <b>⬆ Load Plan</b>
      brings it back.</li>
</ol>

<p style="color:#5a6480;font-size:8pt">Cargo grids are read from the game
files shipped with the toolbox, so the ship list works offline. ↻ in the
header re-reads them.</p>
""",
        # ── Ship & View ───────────────────────────────────────────────────────
        """
<h3 style="color:#33ccdd;margin-top:0">Picking a ship, and moving the camera</h3>

<b style="color:#c8d4e8">Header</b>
<ul>
  <li><b>Ship dropdown</b> — type any part of the name and the list filters
      as you type; click <b>▼</b> to browse it all.</li>
  <li><b style="color:#5a6480">↻</b> — re-read the ship and capacity data.</li>
  <li><b>⬇ Save Plan</b> / <b>⬆ Load Plan</b> — store and restore the whole
      arrangement: every container, every item, every crate and its contents,
      and what you painted.</li>
  <li>The line on the right is the <b>status line</b>. It names the ship and
      its SCU, and afterwards reports whatever you just did.</li>
</ul>

<b style="color:#c8d4e8">Getting around the view</b>
<ul>
  <li><b>Scroll wheel</b> — zoom in and out.</li>
  <li><b>Click &amp; drag</b> empty space — pan.</li>
  <li><b>↺ ↻</b> (top toolbar) — swing the camera 90° so you can see the
      sides that were facing away. The toolbar text tells you which way it
      is pointing, and which faces are lit.</li>
  <li><b>Hover a box</b> — it lights up and the status line names it, plus
      whatever commodity is painted on it.</li>
</ul>

<b style="color:#c8d4e8">Moving a box that is already placed</b>
<ul>
  <li><b>Drag it.</b> It snaps to the 1-SCU grid and flush against
      neighbouring boxes and walls.</li>
  <li>The ghost tells you the verdict:
      <b style="color:#4caf50">green</b> it lands there,
      <b style="color:#e0a54d">amber</b> an item lands but breaks a rule
      (see the Items tab),
      <b style="color:#f44336">red</b> refused — the box returns home.</li>
  <li><b>R</b> or <b>right-click</b> rotates it mid-drag, <b>Esc</b> cancels,
      <b>Ctrl+Z</b> undoes the last move.</li>
</ul>

<b style="color:#c8d4e8">Legend and overlay</b>
<p>The strip along the bottom maps each colour to a container size. The
translucent panel <b>top-left</b> lists what you have painted and how many
boxes each has.</p>
""",
        # ── Containers ────────────────────────────────────────────────────────
        """
<h3 style="color:#33ccdd;margin-top:0">Loading containers</h3>

<b style="color:#c8d4e8">Two ways to do it</b>
<ul>
  <li><b>✋ Manual</b> (where you start) — you place every box. Click a size
      button, then click the grid; a ghost shows where it will land. Click
      the top of a box to stack on it.</li>
  <li><b>⚙ Auto</b> — you type how many of each size and the packer arranges
      them. <b>Typing a count switches you to Auto by itself</b>, so if you
      were mid-way through placing by hand, that is why.</li>
</ul>
<p style="color:#5a6480;font-size:8pt">The yellow lines under the two buttons
are a reminder of whichever mode you are in.</p>

<b style="color:#c8d4e8">While placing (Manual)</b>
<ul>
  <li><b>R</b> — rotate the box you are about to drop.</li>
  <li><b>Right-click</b> a box — remove it. If something is resting on it you
      are told to take the top box off first.</li>
  <li><b>Esc</b>, or clicking the size button again — stop placing.</li>
  <li><b>Ctrl+Z</b> — undo.</li>
  <li>A container that breaks a rule is <b>refused</b>, and the status line
      says why: outside the cargo grids, overlaps another container, sticks
      out of the top, not supported underneath, or a grid that only takes
      certain sizes.</li>
</ul>

<b style="color:#c8d4e8">Capacity</b>
<ul>
  <li><b>Capacity</b> shows <i>used / total SCU</i> and goes
      <b style="color:#ff5533">red</b> past the ship's limit.</li>
  <li>The small line under the bar tallies <b>items</b> and <b>crates</b>
      separately, with a ⚠ count if any are flagged.
      <b>Items never count toward SCU</b> — they are not cargo.</li>
</ul>

<b style="color:#c8d4e8">The size rows</b>
<ul>
  <li>One row per container size, 1 SCU to 32 SCU, the swatch matching its
      colour in the view.</li>
  <li>The <b>size button itself is the place tool</b> in Manual mode.</li>
  <li>The number box is the count for Auto; <b>▲ ▼</b> nudge it. The right
      column is the SCU that size contributes.</li>
  <li>The count is capped to what the ship can physically hold in that size,
      and to the capacity left after the other sizes.</li>
</ul>

<b style="color:#c8d4e8">Buttons</b>
<ul>
  <li><b style="color:#44aaff">▶ Optimize</b> — fill the hold with the best
      mix for maximum SCU. You can keep editing afterwards.</li>
  <li><b style="color:#ff5533">✕ Clear</b> — zero every container count.</li>
  <li><b style="color:#ffaa22">↺ Reset</b> — go back to the known reference
      loadout, for ships that have one.</li>
</ul>
""",
        # ── Items & Crates ────────────────────────────────────────────────────
        """
<h3 style="color:#33ccdd;margin-top:0">Loose items, and personal crates</h3>
<p>Not everything you haul is a cargo container. The <b>Items</b> tab in
<b>PLANNING MODE</b> places the awkward things — and the crates you fill
yourself.</p>

<b style="color:#c8d4e8">Placing an item</b>
<ul>
  <li>The tree groups them: <b>Ore Pods</b>, <b>Missiles</b>, <b>Bombs</b>,
      <b>Ship Weapons</b>, and <b>Components</b> (coolers, power plants,
      shields, quantum drives). <b>Search items…</b> narrows it and opens the
      groups for you.</li>
  <li><b>Click a row, then click the grid.</b> It behaves exactly like a
      container size: ghost preview, <b>R</b> to rotate, <b>right-click</b> to
      remove, <b>Esc</b> to stop, <b>Ctrl+Z</b> to undo, and you can drag it
      afterwards. It snaps flush against containers and other items.</li>
  <li>The right-hand column is its footprint in cells, e.g. <b>S2 2×2×3</b>.
      A trailing <b>~</b> means the size is approximate — worked out from the
      item's volume because the game files give no box for it. Hover the row
      to see which.</li>
</ul>

<b style="color:#c8d4e8">Items warn, they do not refuse</b>
<p>An item that lands outside a grid, overlaps something, sticks out of the
top or floats is still placed, with an <b style="color:#e0a54d">amber</b>
ghost and a <b>⚠</b> in the status line. Real holds take shapes a grid model
does not, so the call is yours. Containers are still strict.</p>
<p style="color:#5a6480;font-size:8pt">Items are counted on their own line
under the capacity bar and never added to your SCU.
<b>✕ Clear items</b> removes them all (Ctrl+Z undoes it).
If the item list is missing, a <b>Download</b> button appears — the same
pinned game build the DPS tool uses.</p>

<h3 style="color:#e0b84d">Personal crates</h3>
<p>Stor*All boxes for a loot run: place them in the hold, then fill each one
item by item.</p>
<ul>
  <li><b>PERSONAL CRATES</b> sits at the top of the Items tab:
      <b>1/8 &middot; 1 &middot; 2 &middot; 4 &middot; 8 SCU</b>. Click one,
      then click the grid, same as any item.</li>
  <li>Each crate you place is <b>numbered</b> and gets <b>its own tab</b>
      beside <b>Hold</b> at the top of the view. Numbers never get reused, so
      removing crate 2 does not renumber crate 3.</li>
  <li><b>Pop out</b> puts that crate in its own small window so you can work
      on it next to the hold; closing that window docks it back.</li>
</ul>

<b style="color:#c8d4e8">Filling a crate</b>
<ul>
  <li><b>ADD ITEMS</b> — type in <b>Search any item…</b>: guns, armour,
      clothes, food, gadgets, components, minerals, anything carryable.
      Pick a row, set <b>Qty</b>, press <b>Add to crate</b>
      (double-click or Enter also works).</li>
  <li><b>IN THIS CRATE</b> lists what is inside with the volume of each and
      the running total. <b>−</b> and <b>+</b> change a quantity,
      <b>Remove</b> takes the row out.</li>
  <li>The fill bar goes amber near full and red at full.</li>
  <li><b>Open on UEX</b> opens the selected item's UEX page in your browser.</li>
</ul>

<b style="color:#c8d4e8">The volume rule is strict</b>
<p>A crate checks volume and refuses anything that does not fit —
<b style="color:#ff5533">Won't fit</b>, with the reason. Search results that
are already too big for the space left are shown in red before you try. You
cannot get a ship engine into a handheld box, and the tool will not pretend
otherwise.</p>
<p style="color:#5a6480;font-size:8pt">The item lists load the first time you
open a crate tab. If they are not downloaded yet a <b>Download</b> button
appears (pinned build, about 110 MB). Without the UEX cache you still get
every item and its volume, just no UEX links.</p>
""",
        # ── Commodities ───────────────────────────────────────────────────────
        """
<h3 style="color:#33ccdd;margin-top:0">Marking what is in each box</h3>

<b style="color:#c8d4e8">Commodity brush</b>
<ul>
  <li>Pick from the <b>COMMODITY BRUSH</b> dropdown on the
      <b>Commodities</b> tab (type to search).</li>
  <li><b>Mission Cargo 1</b> to <b>10</b> and <b>Hydrogen Fuel</b> /
      <b>Quantum Fuel</b> are always at the top of the list. Contract boxes
      are not a commodity, so give each contract its own number and you can
      tell ten of them apart in one hold. They are there before the
      commodity list has loaded, and stay if it never does.</li>
  <li>The cursor becomes a <b>paint brush</b> in that colour. Click any box to
      mark it; click it again with a different brush to change it.</li>
  <li>The swatch and name under the dropdown say which brush is live, or
      <b>No brush</b>.</li>
  <li><b>Click with no brush</b> to clear a box. <b>Clear Brush</b> puts the
      brush down without unmarking anything.</li>
</ul>

<b style="color:#c8d4e8">Assignments overlay</b>
<p>Top-left of the view, updating as you paint: every commodity you have used
and how many boxes carry it, in its own colour.</p>

<b style="color:#c8d4e8">Filter</b>
<ul>
  <li>Opens <b>CARGO FILTER</b>, listing every commodity you have painted with
      its box count.</li>
  <li>Untick one to <b>hide</b> those boxes — handy for seeing one contract's
      cargo on its own. <b>Check All</b> / <b>Uncheck All</b> do it in bulk,
      <b>Close</b> dismisses the dialog.</li>
</ul>
""",
        # ── See Inside ────────────────────────────────────────────────────────
        """
<h3 style="color:#33ccdd;margin-top:0">Seeing into a full hold</h3>
<p>Once the hold is packed, the boxes you care about are buried behind the
ones in front. The bar above the view exists to get them out of the way.
<b>Nothing here changes your plan</b> — it is the camera, not the cargo.
Nothing is moved, saved or counted.</p>

<b style="color:#c8d4e8">Alt+click — hide what is on top</b>
<ul>
  <li>Hover a box and the status line names it. If anything is resting on it,
      it also says <b>Alt+click hides the N above</b>.</li>
  <li><b>Alt+click</b> that box and they vanish, so you can see and reach it.
      Alt+click more boxes to keep digging.</li>
  <li>The status line confirms what went, or tells you nothing was on top.</li>
</ul>

<b style="color:#c8d4e8">Layer slider — take the hold down a level at a time</b>
<ul>
  <li>Drag the slider on the right of that bar to show only boxes up to a
      given height. The label reads <b>All layers</b>, or
      <b>Up to layer 2/4</b>.</li>
  <li>It only does something when the hold is stacked more than one high.</li>
</ul>

<b style="color:#c8d4e8">Getting them back</b>
<ul>
  <li>Whenever anything is hidden you get a count —
      <b>3 boxes hidden ·</b> — and a <b>Show all</b> button. Press it and
      everything comes back, slider included.</li>
  <li>Place a box where hidden ones were and they are revealed by themselves,
      so you never build on top of something you cannot see.</li>
  <li>If you try to remove a box with a hidden one on top, the status line
      says so and names <b>Show all</b>.</li>
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

        for label, content in zip(self._TABS, self._CONTENT):
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
            tabs.addTab(page, label)

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
        # Manual (default): blank grid, boxes placed by hand. Auto: counts + Optimize.
        self._mode: str = "manual"
        self._place_size: int | None = None
        self._place_item: str | None = None     # Items tab: catalogue key being placed
        self._item_catalog: list[dict] | None = None
        self._items_loading: bool = False
        # Personal crates: number -> {cls, key, name, short, capacity_u,
        # contents}. A removed crate keeps its record (Ctrl+Z brings it back
        # with its contents); only crates in the hold are saved or tabbed.
        self._crates: dict[int, dict] = {}
        self._crate_next: int = 1
        self._crate_panels: dict[int, CratePanel] = {}
        self._crate_windows: dict[int, CrateWindow] = {}
        self._crate_index: dict | None = None
        self._crate_index_state: str = "idle"   # idle loading ready missing downloading error
        self._uex: dict | None = None
        self._uex_state: str = "idle"            # idle loading ready none
        self._place_rot: bool = False
        self._syncing: bool = False
        self._pending_loadout: dict | None = None

        # Planning mode state
        self._selected_commodity: str | None = None
        self._commodity_visibility: dict[str, bool] = {}

        # Hover highlight: the status text it replaced (restored on leave)
        # and the text it put there.
        self._hover_status: str | None = None
        self._hover_shown: str = ""

        # Drag-and-drop state
        self._drag: dict | None = None
        self._move_undo: list[tuple] = []   # (manual_boxes_before, assignments_before)

        self._build_ui()
        for d in crate_items.crate_defs():
            self._renderer._item_defs[d["key"]] = d
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

        _plan_btn_style = f"""
            QPushButton {{
                background-color: {BG3}; color: {FG_DIM};
                font-family: Consolas; font-size: 8pt;
                border: none; padding: 3px 7px;
            }}
            QPushButton:hover {{ background-color: {BORDER}; color: {FG}; }}
            QPushButton:disabled {{ color: {FG_DIM}; opacity: 0.5; }}
        """
        btn_save_plan = QPushButton(_("\u2b07 Save Plan"), hdr)
        btn_save_plan.setCursor(Qt.PointingHandCursor)
        btn_save_plan.setStyleSheet(_plan_btn_style)
        btn_save_plan.clicked.connect(self._save_loadout)
        hdr_lay.addWidget(btn_save_plan)

        btn_load_plan = QPushButton(_("\u2b06 Load Plan"), hdr)
        btn_load_plan.setCursor(Qt.PointingHandCursor)
        btn_load_plan.setStyleSheet(_plan_btn_style)
        btn_load_plan.clicked.connect(self._load_loadout)
        hdr_lay.addWidget(btn_load_plan)

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

        # See-into-the-stack bar: Alt+click hint, height slider, and the
        # "N boxes hidden · Show all" indicator. View only; nothing here is
        # saved or counted.
        vb = QWidget(iso_container)
        vb.setFixedHeight(24)
        vb.setStyleSheet(f"background-color: {BG2};")
        vb_lay = QHBoxLayout(vb)
        vb_lay.setContentsMargins(8, 0, 8, 0)
        vb_lay.setSpacing(6)
        _vb_lbl = (f"color: {FG_DIM}; font-family: Consolas; font-size: 8pt;"
                   f" background: transparent;")
        peel_hint = QLabel(_("Alt+click a box: hide what is on top"), vb)
        peel_hint.setStyleSheet(_vb_lbl)
        vb_lay.addWidget(peel_hint)
        vb_lay.addStretch(1)
        self._layer_lbl = QLabel(_("All layers"), vb)
        self._layer_lbl.setStyleSheet(_vb_lbl)
        self._layer_lbl.setFixedWidth(118)
        self._layer_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        vb_lay.addWidget(self._layer_lbl)
        self._layer_slider = QSlider(Qt.Horizontal, vb)
        self._layer_slider.setFixedWidth(110)
        self._layer_slider.setRange(1, 1)
        self._layer_slider.setPageStep(1)
        self._layer_slider.setToolTip(_("Show boxes up to this height"))
        self._layer_slider.setStyleSheet(f"""
            QSlider::groove:horizontal {{ height: 4px; background: {BG3}; }}
            QSlider::sub-page:horizontal {{ background: {ACCENT}; }}
            QSlider::handle:horizontal {{
                background: {ACCENT}; width: 10px; margin: -5px 0;
            }}
        """)
        self._layer_slider.valueChanged.connect(self._on_layer_slider)
        vb_lay.addWidget(self._layer_slider)
        vb_lay.addSpacing(8)
        self._hidden_lbl = QLabel("", vb)
        self._hidden_lbl.setStyleSheet(
            f"color: {ACCENT}; font-family: Consolas; font-size: 8pt;"
            f" font-weight: bold; background: transparent;")
        self._hidden_lbl.setVisible(False)
        vb_lay.addWidget(self._hidden_lbl)
        self._show_all_btn = QPushButton(_("Show all"), vb)
        self._show_all_btn.setCursor(Qt.PointingHandCursor)
        self._show_all_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {BG3}; color: {FG};
                font-family: Consolas; font-size: 8pt;
                border: 1px solid {ACCENT}; padding: 1px 8px;
            }}
            QPushButton:hover {{ background-color: {BORDER}; }}
        """)
        self._show_all_btn.clicked.connect(self._show_all)
        self._show_all_btn.setVisible(False)
        vb_lay.addWidget(self._show_all_btn)
        iso_lay.addWidget(vb)

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

        self._view_tabs.addTab(iso_container, _("Hold"))
        # The tab bar shows once a personal crate is placed (one tab each).
        self._view_tabs.tabBar().setVisible(False)
        self._view_tabs.currentChanged.connect(self._on_view_tab)
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
        self._renderer.set_drag_owner(self)
        self._view.drag_key_handler = self._view_key
        self._view.drag_right_click_handler = self._view_right_click
        self._view.hover_handler = self._on_view_hover
        self._view.empty_click_handler = self._on_view_empty_click
        self._view.leave_handler = self._on_view_leave
        self._view.alt_click_handler = self._view_alt_click
        self._undo_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Undo), self)
        self._undo_shortcut.activated.connect(self._undo_move)
        self._refresh_mode_ui()

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

        # Items never count toward SCU; they get their own one-line tally.
        self._items_summary_lbl = QLabel("", cap_outer)
        self._items_summary_lbl.setWordWrap(False)
        self._items_summary_lbl.setStyleSheet(
            f"color: {FG_DIM}; font-family: Consolas; font-size: 8pt; background: transparent;"
        )
        cap_lay.addWidget(self._items_summary_lbl)

        pad_lay.addWidget(cap_outer)
        pad_lay.addSpacing(4)

        # Separator
        sep = QFrame(pad)
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"color: {BORDER};")
        pad_lay.addWidget(sep)
        pad_lay.addSpacing(4)

        # Mode: Manual (default) places boxes by hand on a blank grid; Auto
        # packs typed counts. J, 2026-09-26: "it should be obvious".
        mode_row = QWidget(pad)
        mode_lay = QHBoxLayout(mode_row)
        mode_lay.setContentsMargins(0, 0, 0, 0)
        mode_lay.setSpacing(0)
        self._mode_btns: dict[str, QPushButton] = {}
        for key, text, tip in (
                ("manual", "\u270b  " + _("Manual"),
                 _("Place boxes yourself: click a size below, then click the grid")),
                ("auto", "\u2699  " + _("Auto"),
                 _("Type container counts, then press Optimize to pack them"))):
            mb = QPushButton(text, mode_row)
            mb.setCheckable(True)
            mb.setCursor(Qt.PointingHandCursor)
            mb.setToolTip(tip)
            mb.setStyleSheet(f"""
                QPushButton {{
                    background-color: {BG3}; color: {FG_DIM};
                    font-family: Consolas; font-size: 9pt; font-weight: bold;
                    border: 1px solid {BORDER}; padding: 5px 8px;
                }}
                QPushButton:checked {{
                    background-color: {ACCENT}; color: {BG}; border-color: {ACCENT};
                }}
                QPushButton:hover:!checked {{ color: {FG}; }}
            """)
            mb.clicked.connect(lambda _c=False, k=key: self._set_mode(k))
            mode_lay.addWidget(mb, 1)
            self._mode_btns[key] = mb
        pad_lay.addWidget(mode_row)
        # Fixed short lines, no wrap: a wrapped label here lost its last line at 125% display scaling.
        self._mode_hint = QLabel("", pad)
        self._mode_hint.setWordWrap(False)
        self._mode_hint.setStyleSheet(
            f"color: {YELLOW}; font-family: Consolas; font-size: 8pt; background: transparent;"
        )
        pad_lay.addWidget(self._mode_hint)
        pad_lay.addSpacing(4)

        # Container rows
        self._spinboxes: dict[int, QSpinBox] = {}
        self._place_btns: dict[int, QPushButton] = {}
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

            # The size is also the Manual-mode place tool: click it, then the grid.
            sz_btn = QPushButton(f"{size:>2} SCU", row)
            sz_btn.setCheckable(True)
            sz_btn.setFixedWidth(54)
            sz_btn.setCursor(Qt.PointingHandCursor)
            sz_btn.setToolTip(
                _("Click, then click the grid to place a {n} SCU box").format(n=size))
            sz_btn.setStyleSheet(f"""
                QPushButton {{
                    color: {FG}; font-family: Consolas; font-size: 9pt;
                    background-color: {BG3}; border: 1px solid {BORDER};
                    padding: 1px 3px; text-align: left;
                }}
                QPushButton:hover {{ border-color: {ACCENT}; }}
                QPushButton:checked {{
                    background-color: {CONT_COL[size]}; color: {BG};
                    border-color: {FG}; font-weight: bold;
                }}
            """)
            sz_btn.clicked.connect(lambda on, s=size: self._on_place_btn(s, on))
            row_lay.addWidget(sz_btn)
            self._place_btns[size] = sz_btn

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
            sb.valueChanged.connect(self._on_count_edited)
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

        # Commodities | Items (J, 2026-09-26: "the player would swap between
        # the normal commodity tab and then the item tab").
        self._brush_tabs = QTabWidget(pad)
        self._brush_tabs.setStyleSheet(f"""
            QTabWidget::pane {{ border: 1px solid {BORDER}; background: {BG2}; }}
            QTabBar::tab {{
                background: {BG3}; color: {FG_DIM}; font-family: Consolas;
                font-size: 8pt; padding: 4px 12px; border: 1px solid {BORDER};
            }}
            QTabBar::tab:selected {{ background: {ACCENT}; color: {BG}; font-weight: bold; }}
        """)
        com_tab = QWidget()
        com_lay = QVBoxLayout(com_tab)
        com_lay.setContentsMargins(6, 6, 6, 6)
        com_lay.setSpacing(4)

        brush_lbl = QLabel(_("COMMODITY BRUSH"), com_tab)
        brush_lbl.setStyleSheet(
            f"color: {FG_DIM}; font-family: Electrolize, Consolas; font-size: 8pt; "
            f"background: transparent;"
        )
        com_lay.addWidget(brush_lbl)

        self._commodity_combo = SCFuzzyCombo(placeholder="Select commodity\u2026", parent=com_tab)
        self._commodity_combo.item_selected.connect(self._on_commodity_selected)
        # Mission cargo is there at once, before (or without) the UEX list.
        self._commodity_combo.set_items(list(PINNED_BRUSH))
        com_lay.addWidget(self._commodity_combo)
        com_lay.addSpacing(4)

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

        com_lay.addWidget(sel_row)
        com_lay.addSpacing(6)

        btn_clear_brush = QPushButton(_("Clear Brush"), com_tab)
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
        com_lay.addWidget(btn_clear_brush)

        com_lay.addSpacing(4)

        btn_filter = QPushButton(_("Filter"), com_tab)
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
        com_lay.addWidget(btn_filter)

        com_lay.addStretch(1)
        self._brush_tabs.addTab(com_tab, _("Commodities"))
        self._brush_tabs.addTab(self._build_items_tab(), _("Items"))
        self._brush_tabs.currentChanged.connect(self._on_brush_tab)
        pad_lay.addWidget(self._brush_tabs, 1)

        # The Items list needs real height; on a short window the panel
        # scrolls instead of crushing the list (or any row above it) to nothing.
        self._brush_tabs.setMinimumHeight(260)
        scroll = QScrollArea(parent)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"QScrollArea {{ background-color: {BG2}; border: none; }}"
                             f"QScrollBar:vertical {{ width: 8px; background: {BG2}; }}"
                             f"QScrollBar::handle:vertical {{ background: {BORDER}; }}")
        pad.setStyleSheet(f"background-color: {BG2};")
        # Width stays the panel's (as before the scroll area): never scroll sideways.
        pad.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        scroll.setWidget(pad)
        self._config_scroll = scroll
        parent_lay = QVBoxLayout(parent)
        parent_lay.setContentsMargins(0, 0, 0, 0)
        parent_lay.addWidget(scroll)

    # ── Items tab ──────────────────────────────────────────────────────────────
    #
    # J, 2026-09-26: "add a category tab which lists out those and basically
    # draws them in appropriately sized boxes. To snap and move around." An
    # item arms placement like a container size button (ghost, click, R,
    # right-click, Ctrl+Z, drag), snaps flush to containers AND items, and
    # every container rule it breaks is a warning, never a refusal: "if they
    # don't match their cargo bay that's user error not engine error".

    def _build_items_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        # Personal crates (J, 2026-09-26): placed like items, numbered 1..N,
        # each with its own tab to fill. Always available: their sizes are
        # pinned in crate_items.CRATES (checked against items.json).
        crate_lbl = QLabel(_("PERSONAL CRATES"), tab)
        crate_lbl.setStyleSheet(
            f"color: {crate_items.CRATE_COLOR}; font-family: Electrolize, Consolas;"
            f" font-size: 8pt; background: transparent;")
        lay.addWidget(crate_lbl)
        crate_row = QWidget(tab)
        crate_row_lay = QHBoxLayout(crate_row)
        crate_row_lay.setContentsMargins(0, 0, 0, 0)
        crate_row_lay.setSpacing(3)
        self._crate_btns: dict[str, QPushButton] = {}
        for d in crate_items.crate_defs():
            b = QPushButton(d["short"].replace(" SCU", ""), crate_row)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            w, h, l = d["dims"]
            b.setToolTip(f"{d['name']}\n{_('Holds')} {crate_items.fmt_u(d['capacity_u'])}"
                         f"  ·  {w}×{h}×{l} {_('cells')}")
            b.setStyleSheet(f"""
                QPushButton {{
                    background-color: {BG3}; color: {FG};
                    font-family: Consolas; font-size: 8pt;
                    border: 1px solid {crate_items.CRATE_COLOR}; padding: 3px 2px;
                }}
                QPushButton:hover {{ background-color: {BORDER}; }}
                QPushButton:checked {{ background-color: {crate_items.CRATE_COLOR}; color: {BG}; }}
            """)
            b.toggled.connect(lambda on, k=d["key"]: self._on_crate_btn(k, on))
            crate_row_lay.addWidget(b)
            self._crate_btns[d["key"]] = b
        scu_lbl = QLabel(_("SCU"), crate_row)
        scu_lbl.setStyleSheet(f"color: {FG_DIM}; font-family: Consolas; font-size: 8pt;"
                              f" background: transparent;")
        crate_row_lay.addWidget(scu_lbl)
        lay.addWidget(crate_row)
        lay.addSpacing(4)

        self._items_search = QLineEdit(tab)
        self._items_search.setPlaceholderText(_("Search items…"))
        self._items_search.setClearButtonEnabled(True)
        self._items_search.setStyleSheet(
            f"QLineEdit {{ background-color: {BG3}; color: {FG}; font-family: Consolas;"
            f" font-size: 8pt; border: 1px solid {BORDER}; padding: 3px 4px; }}"
        )
        self._items_search.textChanged.connect(self._populate_items_tree)
        lay.addWidget(self._items_search)

        self._items_tree = QTreeWidget(tab)
        self._items_tree.setColumnCount(2)
        self._items_tree.setHeaderHidden(True)
        self._items_tree.setIndentation(10)
        self._items_tree.setRootIsDecorated(True)
        self._items_tree.setUniformRowHeights(True)
        hdr = self._items_tree.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)            # name
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)   # S# WxHxL
        self._items_tree.setStyleSheet(
            f"QTreeWidget {{ background-color: {BG}; color: {FG}; font-family: Consolas;"
            f" font-size: 8pt; border: 1px solid {BORDER}; }}"
            f"QTreeWidget::item:selected {{ background-color: {ACCENT}; color: {BG}; }}"
        )
        self._items_tree.itemClicked.connect(self._on_item_row)
        self._items_tree.itemActivated.connect(self._on_item_row)
        lay.addWidget(self._items_tree, 1)

        # One short line, no wrap (wrapped labels in this panel lose lines).
        self._items_note = QLabel(_("Loading items…"), tab)
        self._items_note.setWordWrap(False)
        self._items_note.setStyleSheet(
            f"color: {YELLOW}; font-family: Consolas; font-size: 8pt; background: transparent;"
        )
        lay.addWidget(self._items_note)

        row = QWidget(tab)
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 0, 0, 0)
        row_lay.setSpacing(4)
        small = f"""
            QPushButton {{
                background-color: {BG3}; color: {FG_DIM};
                font-family: Consolas; font-size: 8pt;
                border: 1px solid {BORDER}; padding: 3px 6px;
            }}
            QPushButton:hover {{ background-color: {BORDER}; color: {FG}; }}
        """
        self._items_fetch_btn = QPushButton(_("Download"), row)
        self._items_fetch_btn.setToolTip(
            _("Download the game item data (the same pinned build the DPS tool uses)"))
        self._items_fetch_btn.setStyleSheet(small)
        self._items_fetch_btn.clicked.connect(self._fetch_item_data)
        self._items_fetch_btn.hide()
        row_lay.addWidget(self._items_fetch_btn)
        row_lay.addStretch(1)
        btn_clear_items = QPushButton("✕ " + _("Clear items"), row)
        btn_clear_items.setCursor(Qt.PointingHandCursor)
        btn_clear_items.setStyleSheet(small)
        btn_clear_items.clicked.connect(self._clear_items)
        row_lay.addWidget(btn_clear_items)
        lay.addWidget(row)
        return tab

    def _on_brush_tab(self, index: int) -> None:
        if index == 1:                       # Items
            self._ensure_item_catalog()
            # On a short window, bring the list into view (vertically only).
            bar = self._config_scroll.verticalScrollBar()
            bar.setValue(min(bar.maximum(), self._brush_tabs.y()))

    def _ensure_item_catalog(self) -> None:
        """Parse ship-items.json off the UI thread, once, on first need."""
        if self._item_catalog is not None or self._items_loading:
            return
        path = item_catalog_path()
        if not path or not os.path.isfile(path):
            self._items_note.setText(_("Item data not downloaded yet."))
            self._items_fetch_btn.show()
            return
        self._items_loading = True
        self._items_note.setText(_("Loading items…"))

        def work():
            try:
                cat = item_catalog.load_catalog(path) or []
            except Exception:                           # noqa: BLE001
                log.exception("item catalogue failed to load")
                cat = None
            self._main_thread_call.emit(lambda: self._on_items_loaded(cat))

        threading.Thread(target=work, daemon=True).start()

    def _on_items_loaded(self, catalog) -> None:
        self._items_loading = False
        if catalog is None:
            self._items_note.setText(_("Item data could not be read."))
            return
        self._set_item_catalog(catalog)

    def _fetch_item_data(self) -> None:
        """Download via shared/scunpacked.py (its pinned build, its cache)."""
        if self._items_loading:
            return
        self._items_loading = True
        self._items_fetch_btn.setEnabled(False)
        self._items_note.setText(_("Downloading item data…"))

        def work():
            ok = True
            try:
                from shared import scunpacked
                scunpacked.fetch_raw()
            except Exception:                           # noqa: BLE001
                log.exception("item data download failed")
                ok = False
            self._main_thread_call.emit(lambda: self._on_item_fetch_done(ok))

        threading.Thread(target=work, daemon=True).start()

    def _on_item_fetch_done(self, ok: bool) -> None:
        self._items_loading = False
        self._items_fetch_btn.setEnabled(True)
        if not ok:
            self._items_note.setText(_("Download failed. Try again later."))
            return
        self._items_fetch_btn.hide()
        self._ensure_item_catalog()

    def _set_item_catalog(self, catalog: list[dict]) -> None:
        self._item_catalog = list(catalog)
        for d in self._item_catalog:
            self._renderer._item_defs[d["key"]] = d
        self._items_fetch_btn.hide()
        self._populate_items_tree()
        self._render_grid()

    @staticmethod
    def _item_row_text(d: dict) -> str:
        w, h, l = d["dims"]
        size = f"S{d['size']} " if d.get("size") else ""
        return f"{size}{w}×{h}×{l}" + ("~" if d.get("approx") else "")

    def _populate_items_tree(self, _text=None) -> None:
        tree = self._items_tree
        tree.blockSignals(True)
        tree.clear()
        cat = self._item_catalog
        if not cat:
            tree.blockSignals(False)
            if cat is not None:
                self._items_note.setText(_("No items in the data."))
            return
        q = self._items_search.text().strip().lower()
        by_cat: dict[str, list[dict]] = {}
        for d in cat:
            if q and q not in d["name"].lower() and q not in d["key"].lower() \
                    and q not in item_catalog.CATEGORY_LABELS[d["category"]].lower():
                continue
            by_cat.setdefault(d["category"], []).append(d)

        def add_cat(parent, key, label):
            items = by_cat.get(key, [])
            node = QTreeWidgetItem([f"{label} ({len(items)})", ""])
            node.setForeground(0, QBrush(QColor(ITEM_COLORS[key])))
            node.setData(0, Qt.UserRole, None)
            node.setFlags(node.flags() & ~Qt.ItemIsSelectable)
            for d in items:
                leaf = QTreeWidgetItem([d["name"], self._item_row_text(d)])
                leaf.setData(0, Qt.UserRole, d["key"])
                leaf.setForeground(1, QBrush(QColor(FG_DIM)))
                src = {"grid": _("from its cargo grid"), "dims": _("from its dimensions"),
                       "volume": _("APPROXIMATE, from its volume")}[d["source"]]
                w, h, l = d["dims"]
                leaf.setToolTip(0, f"{d['name']}\n{w}×{h}×{l} cells (W×H×L), {src}")
                node.addChild(leaf)
            if isinstance(parent, QTreeWidget):
                parent.addTopLevelItem(node)
            else:
                parent.addChild(node)
            node.setExpanded(bool(q))
            return node

        comp = None
        for key, label in ITEM_CATEGORIES:
            if key in COMPONENT_CATEGORIES:
                if comp is None:
                    n = sum(len(by_cat.get(k, [])) for k in COMPONENT_CATEGORIES)
                    comp = QTreeWidgetItem([f"{_('Components')} ({n})", ""])
                    comp.setFlags(comp.flags() & ~Qt.ItemIsSelectable)
                    tree.addTopLevelItem(comp)
                    comp.setExpanded(bool(q))
                add_cat(comp, key, _(label))
            else:
                add_cat(tree, key, _(label))
        tree.blockSignals(False)
        shown = sum(len(v) for v in by_cat.values())
        self._items_note.setText(
            _("{n} items. Click one, then the grid.").format(n=shown) if shown
            else _("No items match."))

    def _on_item_row(self, node, _col=0) -> None:
        key = node.data(0, Qt.UserRole) if node is not None else None
        if not key:
            if node is not None:
                node.setExpanded(not node.isExpanded())
            return
        self._set_place_item(key)

    def _item_def(self, key: str) -> dict:
        return self._renderer._item_defs.get(key) or {"key": key, "name": key,
                                                       "category": None,
                                                       "dims": (1, 1, 1)}

    def _set_place_item(self, key: str | None) -> None:
        """Arm (or disarm) placing one catalogue item, like a size button."""
        if key is None:
            self._set_place_size(None)
            return
        if self._mode != "manual":
            self._set_mode("manual")
        self._set_place_size(None)          # unchecks the size buttons
        self._place_item = key
        cb = self._crate_btns.get(key)
        if cb is not None:
            cb.blockSignals(True)
            cb.setChecked(True)
            cb.blockSignals(False)
            self._ensure_crate_index()      # first use of the crates: load lazily
        cur = self._items_tree.currentItem()     # keep the armed row highlighted
        if cur is not None and cur.data(0, Qt.UserRole) == key:
            self._items_tree.blockSignals(True)
            cur.setSelected(True)
            self._items_tree.blockSignals(False)
        if self._selected_commodity is not None:
            self._clear_brush()
        self._view.set_brush_cursor(QCursor(Qt.CrossCursor))
        self._view.setFocus(Qt.OtherFocusReason)
        d = self._item_def(key)
        w, h, l = d["dims"]
        self._status_lbl.setText(
            _("Placing {name} ({w}×{h}×{l}): click the grid  ·  R rotate"
              "  ·  Esc done").format(name=d["name"], w=w, h=h, l=l))

    def _placing(self) -> bool:
        return self._place_size is not None or self._place_item is not None

    def _item_warnings(self) -> dict[tuple, list[str]]:
        """Warnings for every placed item against the current scene."""
        items = list(self._renderer._items)
        if not items or not self._slots:
            return {}
        containers = (list(self._renderer._manual_boxes)
                      if self._renderer._manual_boxes is not None
                      else list(self._renderer._last_boxes))
        ctx = PlacementContext(self._grids_world(), containers + items,
                               union=self._has_layout)
        out = {}
        for i, b in enumerate(items):
            out[b] = ctx.item_warnings(b[:3], b[3:6], skip=len(containers) + i)
        return out

    def _update_items_summary(self) -> None:
        crates = sum(1 for b in self._renderer._items if crate_no(b[6]) is not None)
        n = len(self._renderer._items) - crates
        flagged = sum(1 for b in self._renderer._items if self._renderer._item_flags.get(b))
        # Compact so it fits the fixed-width panel (it read "2 flagge"); full wording in the tooltip.
        text = _("Items {n}").format(n=n)
        full = _("Items: {n}").format(n=n)
        if crates:
            text += " · " + _("Crates {k}").format(k=crates)
            full += "  ·  " + _("Crates: {k}").format(k=crates)
        if flagged:
            text += " · ⚠ {k}".format(k=flagged)
            full += "  ·  " + _("{k} flagged").format(k=flagged)
        self._items_summary_lbl.setText(text)
        self._items_summary_lbl.setToolTip(full)
        self._items_summary_lbl.setStyleSheet(
            f"color: {ITEM_WARN if flagged else FG_DIM}; font-family: Consolas; "
            f"font-size: 8pt; background: transparent;")

    def _clear_items(self) -> None:
        if not self._renderer._items:
            return
        self._push_undo()
        for b in self._renderer._items:
            self._renderer._assignments.pop((b[0], b[1], b[2], b[6]), None)
        self._renderer._items = []
        self._render_grid()
        self._update_assignment_summary()
        self._status_lbl.setText(_("Items cleared  ·  Ctrl+Z to undo"))

    def _items_from_payload(self, raw) -> list[tuple]:
        """Parse a saved plan's "items" list (absent in older plans -> [])."""
        out = []
        if not isinstance(raw, list):
            return out
        for it in raw:
            try:
                key = str(it["key"])
                x, y, z = (int(v) for v in it["pos"])
                w, h, l = (int(v) for v in it["dims"])
            except (KeyError, TypeError, ValueError):
                continue
            if not key or min(w, h, l) < 1:
                continue
            if key not in self._renderer._item_defs:
                # Unknown here (no item data yet): keep what the plan says.
                self._renderer._item_defs[key] = {
                    "key": key, "name": str(it.get("name") or key),
                    "category": it.get("category"), "size": 0,
                    "dims": (w, h, l), "source": "plan", "approx": False,
                    "label": item_catalog.abbrev(str(it.get("name") or key)),
                }
            out.append((x, y, z, w, h, l, key))
        return out

    # ── Personal crates ────────────────────────────────────────────────────────
    #
    # J, 2026-09-26: "add personal boxes and then have them each have a number
    # on it and they will create a tab at the top of the page that you can
    # swap to or pop out ... fuzzy search and assign any item ingame that will
    # fit in the crate ... you can't shove a Kraken engine into a handheld
    # crate". In the hold a crate is an item (warnings, not walls) whose key is
    # "<class>#<n>"; what is INSIDE it obeys a strict volume rule
    # (cargo_engine.crate_items.check_fit).

    def _on_crate_btn(self, key: str, on: bool) -> None:
        if on:
            self._brush_tabs.setCurrentIndex(1)
            self._set_place_item(key)
        else:
            self._set_place_item(None)
            self._status_lbl.setText(_("Placing done"))

    def _new_crate(self, cls: str, no: int | None = None,
                   contents: list | None = None) -> str:
        """Register crate *no* (next free number if None) of class *cls*;
        returns its item key. Numbers only ever go up, so removing crate 2
        never renumbers crate 3."""
        base = self._item_def(cls)
        if no is None:
            no = self._crate_next
        self._crate_next = max(self._crate_next, no + 1)
        key = f"{cls}#{no}"
        short = base.get("short") or cls
        self._renderer._item_defs[key] = dict(
            base, key=key, crate_no=no, crate_cls=cls, label=str(no),
            name=_("Crate {n} ({size})").format(n=no, size=short))
        self._crates[no] = {"cls": cls, "key": key, "name": base.get("name", cls),
                            "short": short, "capacity_u": int(base.get("capacity_u") or 0),
                            "contents": list(contents or [])}
        return key

    def _live_crates(self) -> dict[int, tuple]:
        """Crates in the hold right now: number -> item tuple."""
        out = {}
        for b in self._renderer._items:
            no = crate_no(b[6])
            if no is not None and no in self._crates:
                out[no] = b
        return out

    def _crate_tab_text(self, no: int) -> str:
        return _("Crate {n} ({size})").format(n=no, size=self._crates[no]["short"])

    def _sync_crate_tabs(self) -> None:
        """One tab (or popped-out window) per crate in the hold, in number order."""
        if not hasattr(self, "_view_tabs"):
            return
        live = self._live_crates()
        for no in [n for n in self._crate_panels if n not in live]:
            panel = self._crate_panels.pop(no)
            win = self._crate_windows.pop(no, None)
            if win is not None:
                win.take_panel()
                win.close()
                win.deleteLater()
            else:
                i = self._view_tabs.indexOf(panel)
                if i >= 0:
                    self._view_tabs.removeTab(i)
            panel.deleteLater()
        for no in sorted(live):
            if no not in self._crate_panels:
                panel = CratePanel(self, no, _CapacityBar)
                self._crate_panels[no] = panel
                self._insert_crate_tab(no)
        for no, panel in self._crate_panels.items():
            i = self._view_tabs.indexOf(panel)
            if i >= 0:
                self._view_tabs.setTabText(i, self._crate_tab_text(no))
        self._view_tabs.tabBar().setVisible(bool(self._crate_panels))

    def _insert_crate_tab(self, no: int) -> None:
        panel = self._crate_panels[no]
        at = 1 + sum(1 for n, p in self._crate_panels.items()
                     if n < no and self._view_tabs.indexOf(p) >= 0)
        self._view_tabs.insertTab(at, panel, self._crate_tab_text(no))

    def _open_crate(self, no: int) -> None:
        """Show crate *no*: its tab, or raise its popped-out window."""
        if no not in self._crate_panels:
            self._sync_crate_tabs()
        panel = self._crate_panels.get(no)
        if panel is None:
            return
        win = self._crate_windows.get(no)
        if win is not None:
            win.show()
            win.raise_()
        else:
            self._view_tabs.setCurrentWidget(panel)
        self._on_crate_shown()

    def _on_view_tab(self, index: int) -> None:
        if index > 0:
            self._on_crate_shown()

    def _on_crate_shown(self) -> None:
        """A crate tab is in use: load the item lists and the UEX cache, lazily."""
        self._ensure_crate_index()
        self._ensure_uex()

    # -- the tab / window API used by crate_ui.CratePanel --------------------

    def crate_state(self, no: int) -> dict | None:
        return self._crates.get(no)

    def crate_is_popped(self, no: int) -> bool:
        return no in self._crate_windows

    def crate_toggle_pop(self, no: int) -> None:
        if no in self._crate_windows:
            self.crate_dock(no)
        else:
            self.crate_pop(no)

    def crate_pop(self, no: int) -> None:
        """Move crate *no* out of the tab bar into its own small window."""
        panel = self._crate_panels.get(no)
        if panel is None or no in self._crate_windows:
            return
        i = self._view_tabs.indexOf(panel)
        if i >= 0:
            self._view_tabs.removeTab(i)
        win = CrateWindow(self, panel)
        self._crate_windows[no] = win
        win.show()
        panel.refresh()
        self._on_crate_shown()

    def crate_dock(self, no: int, from_close: bool = False) -> None:
        """Put a popped-out crate back in the tab bar (closing its window)."""
        win = self._crate_windows.pop(no, None)
        if win is None:
            return
        panel = win.take_panel()
        if not from_close:
            win.close()
        win.deleteLater()
        if no in self._crate_panels:
            self._insert_crate_tab(no)
            self._view_tabs.setCurrentWidget(panel)
            panel.refresh()

    def _refresh_crate(self, no: int) -> None:
        panel = self._crate_panels.get(no)
        if panel is not None:
            panel.refresh()

    def crate_add(self, no: int, entry: dict, qty: int = 1) -> tuple[bool, str]:
        st = self._crates.get(no)
        if st is None:
            return False, _("no such crate")
        ok, why = crate_items.add_item(st["capacity_u"], st["contents"], entry, qty)
        if ok:
            self._refresh_crate(no)
            self._status_lbl.setText(_("Crate {n}: added {q} × {name}").format(
                n=no, q=qty, name=entry.get("name", "")))
        return ok, why

    def crate_set_qty(self, no: int, key: str, qty: int) -> tuple[bool, str]:
        st = self._crates.get(no)
        if st is None:
            return False, _("no such crate")
        ok, why = crate_items.set_qty(st["capacity_u"], st["contents"], key, qty)
        if ok:
            self._refresh_crate(no)
        return ok, why

    def crate_index_rows(self) -> list | None:
        idx = self._crate_index
        return idx.get("items") if idx else None

    def crate_data_note(self) -> tuple[str, bool]:
        """(one short line, show the Download button)."""
        st = self._crate_index_state
        if st == "ready":
            n = len(self.crate_index_rows() or [])
            if self._uex_state == "ready":
                return _("{n:,} items  ·  UEX linked").format(n=n), False
            if self._uex_state == "none":
                return _("{n:,} items  ·  no UEX cache, names only").format(n=n), False
            return _("{n:,} items").format(n=n), False
        return {
            "loading": (_("Loading item lists…"), False),
            "downloading": (_("Downloading item lists…"), False),
            "missing": (_("Item lists not downloaded yet."), True),
            "error": (_("Item lists could not be read."), True),
        }.get(st, (_("Item lists load on first use."), False))

    def crate_uex(self, entry: dict) -> tuple[dict | None, str]:
        """(UEX record or None, a line saying why not). Cache only, no network."""
        if self._uex_state != "ready":
            return None, (_("No UEX cache: name only") if self._uex_state == "none"
                          else _("UEX: loading…"))
        rec = crate_items.uex_match(self._uex, entry)
        return rec, ("" if rec else _("Not listed on UEX"))

    def crate_open_url(self, url: str) -> None:
        QDesktopServices.openUrl(QUrl(url))

    def _refresh_crate_notes(self) -> None:
        for panel in self._crate_panels.values():
            panel.refresh_data_note()

    # -- lazy loading (off the UI thread) -------------------------------------

    def _ensure_crate_index(self) -> None:
        """Read (or build once) the compact crate item index, off the UI thread."""
        if self._crate_index_state in ("loading", "ready", "downloading"):
            return
        d = crate_data_dir()
        if not d or not (os.path.isfile(crate_items.index_path(d))
                         or crate_items.have_sources(d)):
            self._crate_index_state = "missing"
            self._refresh_crate_notes()
            return
        self._crate_index_state = "loading"
        self._refresh_crate_notes()

        def work():
            try:
                idx = crate_items.load_index(d)
            except Exception:                           # noqa: BLE001
                log.exception("crate item index failed to load")
                idx = None
            self._main_thread_call.emit(lambda: self._on_crate_index(idx))

        threading.Thread(target=work, daemon=True).start()

    def _on_crate_index(self, idx) -> None:
        if idx is None:
            d = crate_data_dir()
            self._crate_index_state = ("missing" if d and not crate_items.have_sources(d)
                                       else "error")
            self._refresh_crate_notes()
            return
        self._set_crate_index(idx)

    def _set_crate_index(self, idx: dict) -> None:
        self._crate_index = idx
        self._crate_index_state = "ready"
        # The datamine's own crate figures win over the pinned copy.
        for d in crate_items.crate_defs(idx.get("crates") or None):
            base = self._renderer._item_defs.get(d["key"]) or {}
            base.update({k: d[k] for k in ("name", "dims", "capacity_u", "scu")})
            self._renderer._item_defs[d["key"]] = base
        self._refresh_crate_notes()

    def crate_fetch_data(self) -> None:
        """Download the pinned item lists (shared/scunpacked.py), then index."""
        if self._crate_index_state in ("loading", "downloading"):
            return
        self._crate_index_state = "downloading"
        self._refresh_crate_notes()
        d = crate_data_dir()

        def work():
            idx = None
            try:
                from shared import scunpacked
                scunpacked.fetch_raw(files_wanted=("ship-items.json",) + scunpacked.LOOT_FILES,
                                     dest=d)
                idx = crate_items.load_index(d)
            except Exception:                           # noqa: BLE001
                log.exception("crate item data download failed")
            self._main_thread_call.emit(lambda: self._on_crate_fetched(idx))

        threading.Thread(target=work, daemon=True).start()

    def _on_crate_fetched(self, idx) -> None:
        if idx is None:
            self._crate_index_state = "error"
            self._refresh_crate_notes()
            return
        self._set_crate_index(idx)

    def _ensure_uex(self) -> None:
        """The Item Finder's UEX cache, if there is one, read off the UI thread."""
        if self._uex_state != "idle":
            return
        self._uex_state = "loading"
        path = uex_cache_path()

        def work():
            try:
                uex = crate_items.load_uex(path)
            except Exception:                           # noqa: BLE001
                log.exception("UEX cache could not be read")
                uex = None
            self._main_thread_call.emit(lambda: self._set_uex(uex))

        threading.Thread(target=work, daemon=True).start()

    def _set_uex(self, uex) -> None:
        self._uex = uex
        self._uex_state = "ready" if uex else "none"
        self._refresh_crate_notes()

    # -- save / load -----------------------------------------------------------

    def _crates_payload(self) -> list[dict]:
        out = []
        live = self._live_crates()
        for no in sorted(live):
            b, st = live[no], self._crates[no]
            out.append({
                "no": no, "cls": st["cls"], "name": st["name"], "size": st["short"],
                "capacity_scu": st["capacity_u"] / crate_items.MICRO,
                "pos": [b[0], b[1], b[2]], "dims": [b[3], b[4], b[5]],
                "contents": [{"key": c["key"], "name": c["name"], "qty": int(c["qty"]),
                              "vol_u": int(c["vol_u"]), "kind": c.get("kind", ""),
                              "uuid": c.get("uuid", "")} for c in st["contents"]],
            })
        return out

    def _crates_from_payload(self, raw) -> list[tuple]:
        """Rebuild saved crates (absent in older plans -> []); their numbers
        are kept as saved, and the next new crate numbers after the highest."""
        out = []
        if not isinstance(raw, list):
            return out
        for c in raw:
            try:
                no = int(c["no"])
                cls = str(c["cls"])
                x, y, z = (int(v) for v in c["pos"])
                w, h, l = (int(v) for v in c["dims"])
            except (KeyError, TypeError, ValueError):
                continue
            if no < 1 or no in self._crates or min(w, h, l) < 1:
                continue
            if cls not in self._renderer._item_defs:
                # A crate class this build does not know: keep what the plan says.
                cap = int(round(float(c.get("capacity_scu") or 0) * crate_items.MICRO))
                self._renderer._item_defs[cls] = {
                    "key": cls, "name": str(c.get("name") or cls), "category": "crate",
                    "size": 0, "dims": (w, h, l), "source": "plan", "approx": False,
                    "label": str(c.get("size") or ""), "short": str(c.get("size") or "?"),
                    "capacity_u": cap}
            contents = []
            for it in c.get("contents") or []:
                try:
                    contents.append({"key": str(it["key"]), "name": str(it.get("name") or it["key"]),
                                     "qty": max(1, int(it["qty"])), "vol_u": int(it["vol_u"]),
                                     "kind": str(it.get("kind") or ""),
                                     "uuid": str(it.get("uuid") or "")})
                except (KeyError, TypeError, ValueError):
                    continue
            key = self._new_crate(cls, no, contents)
            out.append((x, y, z, w, h, l, key))
        return out


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
        self._drop_manual_layout()
        # Clear planning mode assignments and brush for new ship
        self._renderer._assignments.clear()
        self._renderer._items = []
        self._set_hover(None)
        self._renderer.show_all()
        self._crates = {}
        self._crate_next = 1
        self._commodity_visibility.clear()
        self._selected_commodity = None
        self._view.clear_brush_cursor()

        self._update_spinbox_limits()

        if self._mode == "manual":
            # J: "the default grid should be blank" - place boxes by hand.
            self._clear_containers()
        elif self._has_layout:
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

        if self._pending_loadout:
            self._apply_pending_loadout()

    def _show_tutorial(self) -> None:
        """Show (or raise) the tutorial popup anchored to the refresh button."""
        if not hasattr(self, "_tutorial_dlg") or self._tutorial_dlg is None:
            self._tutorial_dlg = _CargoTutorialDialog(
                anchor=self._btn_refresh, parent=self
            )
            # Six tabs need the width; the tab bar scrolls (and hides tabs)
            # if the dialog is narrower than its sizeHint.
            self._tutorial_dlg.setFixedSize(620, 500)
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
        self._set_hover(None)
        self._renderer.rotate_cw()
        self._update_iso_info_label()
        self._render_grid()

    def _rotate_ccw(self) -> None:
        self._set_hover(None)
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
        if self._placing():
            self._set_place_size(None)
        self._set_hover(None)
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
        """Handle a box click: stack onto it (place tool) or paint it (brush)."""
        if (self._mode == "manual" and self._placing()
                and self._selected_commodity is None):
            box = tuple(group.box_data)
            # Deferred: placing redraws, deleting the item whose handler runs.
            QTimer.singleShot(0, lambda: self._place_at(None, over_box=box))
            return
        if self._selected_commodity is None:
            no = crate_no(group.box_data[6])
            if no is not None and group.pos_key not in self._renderer._assignments:
                QTimer.singleShot(0, lambda: self._open_crate(no))
                return
            # If no brush, clicking clears the assignment
            if group.pos_key in self._renderer._assignments:
                del self._renderer._assignments[group.pos_key]
                group.commodity = None
                # Restore original color
                group.recolor(self._renderer.base_color_for(group))
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

    # ── Drag-and-drop box placement ──────────────────────────────────────────
    #
    # Left-drag a placed box to move it. A ghost shows where it will land
    # (green = legal, red = not). R or right-click rotates it 90 deg while
    # dragging; Esc cancels; an illegal drop puts it back. Ctrl+Z undoes a
    # move. The rules live in cargo_engine.manual_place (UI-free, tested).

    def _grids_world(self) -> list[dict]:
        """Cargo grids in the renderer's world coords (origin = bounds min)."""
        x_min, z_min = self._bounds[0], self._bounds[1]
        out = []
        for s in self._slots:
            g = dict(s)
            g["x"] = s["x"] - x_min
            g["z"] = s["z"] - z_min
            if self._has_layout:
                # Hand-made layouts: slots are placement volumes; their size
                # tags just echo the box drawn there, so they are not limits.
                g["maxSize"] = g["minSize"] = None
            out.append(g)
        return out

    def box_press(self, group, scene_pos, screen_pos) -> None:
        self._drag_cancelled = False
        self._drag = {"group": group, "press_scene": QPointF(scene_pos),
                      "press_screen": QPointF(screen_pos), "active": False}

    def box_move(self, group, scene_pos, screen_pos) -> None:
        d = self._drag
        if not d or d["group"] is not group:
            return
        if not d["active"]:
            delta = QPointF(screen_pos) - d["press_screen"]
            if delta.manhattanLength() < QApplication.startDragDistance():
                return
            if not self._drag_start(d):
                self._drag = None
                return
        d["last_scene"] = QPointF(scene_pos)
        self._drag_update()

    def box_release(self, group, scene_pos) -> bool:
        """Finish a drag. Returns True if this press was a drag (not a click)."""
        d = self._drag
        if d is None:
            # A drag that Esc already cancelled must not turn into a click.
            cancelled = getattr(self, "_drag_cancelled", False)
            self._drag_cancelled = False
            return cancelled
        self._drag = None
        if not d["active"]:
            return False
        d["last_scene"] = QPointF(scene_pos)
        self._drag_finish(d)
        return True

    def _drag_start(self, d: dict) -> bool:
        group = d["group"]
        box = tuple(group.box_data)
        boxes = list(self._renderer._last_boxes)
        items = list(self._renderer._items)
        item = is_item(box)
        try:
            index = items.index(box) if item else boxes.index(box)
        except ValueError:
            return False
        if item:
            others = boxes + items[:index] + items[index + 1:]
        else:
            others = boxes[:index] + boxes[index + 1:] + items
        x, y, z, w, h, l, size = box
        d.update({
            "active": True, "index": index, "boxes": boxes, "orig": box,
            "item": item, "items": items,
            "dims": (w, h, l), "size": size,
            "centre0": (x + w / 2.0, z + l / 2.0),
            "press_world": self._renderer.unproject(d["press_scene"].x(),
                                                    d["press_scene"].y(), y),
            "plane_y": y,
            "ctx": PlacementContext(self._grids_world(), others,
                                    union=self._has_layout),
            "result": None,
        })
        group.setOpacity(0.3)
        self._view.setFocus(Qt.MouseFocusReason)
        return True

    def _drag_update(self) -> None:
        d = self._drag
        if not d or not d.get("active"):
            return
        sp = d.get("last_scene", d["press_scene"])
        cur = self._renderer.unproject(sp.x(), sp.y(), d["plane_y"])
        if cur is None or d["press_world"] is None:
            return
        cx = d["centre0"][0] + cur[0] - d["press_world"][0]
        cz = d["centre0"][1] + cur[1] - d["press_world"][1]
        w, h, l = d["dims"]
        if d.get("item"):
            # Items always drop; a broken rule is a warning, shown amber.
            pos, warns = d["ctx"].snap_item((w, h, l, d["size"]),
                                            (cx - w / 2.0, cz - l / 2.0))
            reason = "; ".join(warns) if warns else OK
            d["result"] = (pos, True, reason)
            self._renderer.show_ghost(pos[0], pos[1], pos[2], w, h, l, True,
                                      warn=bool(warns))
            self._status_lbl.setText(
                (_("Drop here") if not warns
                 else "⚠ " + _("Drops anyway: ") + _(reason))
                + "  ·  R / right-click rotate  ·  Esc cancel")
            return
        pos, valid, reason = d["ctx"].snap((w, h, l, d["size"]),
                                           (cx - w / 2.0, cz - l / 2.0))
        d["result"] = (pos, valid, reason)
        self._renderer.show_ghost(pos[0], pos[1], pos[2], w, h, l, valid)
        self._status_lbl.setText(
            (_("Drop here") if valid else _("Can't drop: ") + _(reason))
            + "  ·  R / right-click rotate  ·  Esc cancel")

    def _drag_rotate(self) -> None:
        d = self._drag
        d["dims"] = rotate_yaw(d["dims"])
        self._drag_update()

    def _drag_cancel(self) -> None:
        d = self._drag
        self._drag = None
        self._drag_cancelled = True
        self._renderer.clear_ghost()
        try:
            d["group"].setOpacity(1.0)
            d["group"].ungrabMouse()
        except RuntimeError:
            pass
        self._apply_visibility_filter()
        self._status_lbl.setText(_("Move cancelled"))

    def _drag_key(self, event) -> bool:
        if not self._drag or not self._drag.get("active"):
            return False
        if event.key() == Qt.Key_Escape:
            self._drag_cancel()
            return True
        if event.key() == Qt.Key_R and not event.modifiers() & (
                Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            self._drag_rotate()
            return True
        return False

    def _drag_right_click(self) -> bool:
        if not self._drag or not self._drag.get("active"):
            return False
        self._drag_rotate()
        return True

    def _drag_finish(self, d: dict) -> None:
        self._drag_update_from(d)
        self._renderer.clear_ghost()
        pos, valid, reason = d.get("result") or (None, False, "no target")
        orig = d["orig"]
        new_box = (pos[0], pos[1], pos[2], *d["dims"], d["size"]) if pos else orig
        if not valid or new_box == orig:
            d["group"].setOpacity(1.0)
            self._apply_visibility_filter()
            if not valid:
                self._status_lbl.setText(_("Move cancelled: ") + _(reason))
            return
        # Deferred: the redraw deletes the box item whose release handler is
        # still on the stack.
        index, dims = d["index"], d["dims"]
        if d.get("item"):
            items = d["items"]
            QTimer.singleShot(0, lambda: self._apply_item_move(items, index, pos, dims, reason))
            return
        boxes = d["boxes"]
        QTimer.singleShot(0, lambda: self._apply_move(boxes, index, pos, dims))

    def _drag_update_from(self, d: dict) -> None:
        """Re-evaluate the drop at the release point (the last move may lag)."""
        self._drag, saved = d, self._drag
        try:
            self._drag_update()
        finally:
            self._drag = saved

    def _apply_move(self, boxes, index, pos, dims) -> None:
        """Commit a legal move: undo entry, commodity follows the box, redraw."""
        old = boxes[index]
        self._push_undo()
        new_boxes = move_box(boxes, index, pos, dims)
        old_key = (old[0], old[1], old[2], old[6])
        new_key = (pos[0], pos[1], pos[2], old[6])
        commodity = self._renderer._assignments.pop(old_key, None)
        if commodity:
            self._renderer._assignments[new_key] = commodity
        self._renderer._manual_boxes = new_boxes
        self._renderer.reveal_under((pos[0], pos[1], pos[2], *dims, old[6]))
        self._render_grid()
        self._update_assignment_summary()
        self._status_lbl.setText(
            _("Moved {n} SCU box  ·  Ctrl+Z to undo").format(n=old[6]))

    def _apply_item_move(self, items, index, pos, dims, reason=OK) -> None:
        """Commit an item move (always allowed): undo entry, paint follows."""
        old = items[index]
        self._push_undo()
        new_items = move_box(items, index, pos, dims)
        old_key = (old[0], old[1], old[2], old[6])
        commodity = self._renderer._assignments.pop(old_key, None)
        if commodity:
            self._renderer._assignments[(pos[0], pos[1], pos[2], old[6])] = commodity
        self._renderer._items = new_items
        self._renderer.reveal_under((pos[0], pos[1], pos[2], *dims, old[6]))
        self._render_grid()
        self._update_assignment_summary()
        name = self._item_def(old[6])["name"]
        self._status_lbl.setText(
            (_("Moved {name}").format(name=name) if reason == OK
             else "⚠ " + _("Moved {name}: ").format(name=name) + _(reason))
            + "  ·  Ctrl+Z to undo")

    def _undo_move(self) -> None:
        if self._drag and self._drag.get("active"):
            return
        if not self._move_undo:
            return
        entry = self._move_undo.pop()
        manual, assignments = entry[0], entry[1]
        self._renderer._manual_boxes = manual
        if len(entry) > 2:
            self._renderer._items = list(entry[2])
        self._renderer._assignments.clear()
        self._renderer._assignments.update(assignments)
        if self._mode == "manual" and manual is not None:
            self._sync_counts_from_boxes()
        self._render_grid()
        self._update_assignment_summary()
        self._status_lbl.setText(_("Undone"))

    # ── Manual / Auto mode ───────────────────────────────────────────────────
    #
    # J, 2026-09-26: "there should be a manual mode and it should be obvious
    # to the users ... otherwise they just see a ship with no way to arrange
    # things and the default grid should be blank". Manual (the default)
    # starts every ship empty: click a size, click the grid (a ghost previews
    # the landing), drag to move, right-click to remove, Ctrl+Z to undo.
    # Auto is the old behaviour: typed counts, packed by Optimize. Optimize,
    # Reset and a loaded plan in Manual fill the grid and hand the result back
    # as an arrangement you can keep editing. Typing a count switches to Auto.

    def _refresh_mode_ui(self) -> None:
        for k, b in self._mode_btns.items():
            b.blockSignals(True)
            b.setChecked(k == self._mode)
            b.blockSignals(False)
        if self._mode == "manual":
            self._mode_hint.setText("\n".join((
                _("Pick a size, then click the grid."),
                _("Drag moves, right-click removes."),
                _("R rotates. Optimize fills it."))))
        else:
            self._mode_hint.setText("\n".join((
                _("Type how many of each size,"),
                _("then press Optimize."))))

    def _set_mode(self, mode: str) -> None:
        if mode == self._mode:
            self._refresh_mode_ui()
            return
        self._mode = mode
        if mode == "manual":
            # What is on screen becomes the arrangement you edit.
            self._renderer._manual_boxes = list(self._renderer._last_boxes)
            self._sync_counts_from_boxes()
            self._render_grid()
            self._status_lbl.setText(_("Manual: click a size, then the grid"))
        else:
            self._set_place_size(None)
            self._drop_manual_layout()
            self._update_fill()
            self._status_lbl.setText(_("Auto: counts are packed for you"))
        self._refresh_mode_ui()

    def _on_count_edited(self, _v=None) -> None:
        """A typed count means "pack this for me": switch to Auto."""
        if self._syncing or self._mode != "manual":
            return
        self._mode = "auto"
        self._set_place_size(None)
        self._drop_manual_layout()
        self._refresh_mode_ui()
        self._status_lbl.setText(_("Typed a count \u2014 switched to Auto"))

    def _on_place_btn(self, size: int, on: bool) -> None:
        if not on:
            self._set_place_size(None)
            self._status_lbl.setText(_("Placing done"))
            return
        if self._mode != "manual":
            self._set_mode("manual")
        self._set_place_size(size)

    def _set_place_size(self, size: int | None) -> None:
        self._set_hover(None)
        self._place_size = size
        if self._place_item is not None:
            # A size button (or Esc / done) ends placing an item too.
            self._place_item = None
            self._items_tree.blockSignals(True)
            self._items_tree.clearSelection()
            self._items_tree.blockSignals(False)
            for b in self._crate_btns.values():
                b.blockSignals(True)
                b.setChecked(False)
                b.blockSignals(False)
        for s, b in self._place_btns.items():
            b.blockSignals(True)
            b.setChecked(s == size)
            b.blockSignals(False)
        self._renderer.clear_ghost()
        if size is None:
            if self._selected_commodity is None:
                self._view.clear_brush_cursor()
            return
        if self._selected_commodity is not None:
            self._clear_brush()
        self._view.set_brush_cursor(QCursor(Qt.CrossCursor))
        self._view.setFocus(Qt.OtherFocusReason)
        self._status_lbl.setText(
            _("Placing {n} SCU: click the grid  \u00b7  R rotate  \u00b7  Esc done").format(n=size))

    def _sync_counts_from_boxes(self) -> None:
        """Spinboxes and capacity show what is actually placed."""
        counts = {s: 0 for s in CONTAINER_SIZES}
        for b in self._renderer._manual_boxes or []:
            counts[b[6]] = counts.get(b[6], 0) + 1
        self._syncing = True
        try:
            for size in CONTAINER_SIZES:
                sb = self._spinboxes[size]
                sb.blockSignals(True)
                sb.setMaximum(max(sb.maximum(), counts[size]))
                sb.setValue(counts[size])
                sb.blockSignals(False)
        finally:
            self._syncing = False
        self._counts = {s: counts[s] for s in CONTAINER_SIZES}
        self._refresh_capacity()

    def _freeze_if_manual(self) -> None:
        """After a fill in Manual mode, keep the result as an editable arrangement."""
        if self._mode == "manual":
            self._renderer._manual_boxes = list(self._renderer._last_boxes)
            self._sync_counts_from_boxes()

    def _box_group_at(self, scene_pos):
        for it in self._scene.items(scene_pos):
            g = it if isinstance(it, _CargoBoxGroup) else it.group()
            if isinstance(g, _CargoBoxGroup):
                return g
        return None

    def _place_target(self, scene_pos, over_box=None):
        """Where the chosen size lands for this pointer: (pos, dims, valid, reason) or None."""
        if (not self._placing() or self._mode != "manual"
                or not self._current_ship or not self._slots):
            return None
        if self._renderer._manual_boxes is None:
            self._renderer._manual_boxes = list(self._renderer._last_boxes)
        item = self._place_item
        if item is not None:
            w, h, l = self._item_def(item)["dims"]
        else:
            w, h, l = CONTAINER_DIMS[self._place_size]
        if self._place_rot:
            w, h, l = rotate_yaw((w, h, l))
        grids = self._grids_world()
        if over_box is None and scene_pos is not None:
            g = self._box_group_at(scene_pos)
            over_box = tuple(g.box_data) if g is not None else None
        if over_box is not None:
            # Pointing at a box: stack on it (snap drops onto the top).
            bx, _by, bz, bw, _bh, bl, _sz = over_box
            cx, cz = bx + bw / 2.0, bz + bl / 2.0
        else:
            hit = None
            for y0 in sorted({int(g.get("y0") or 0) for g in grids}, reverse=True):
                p = self._renderer.unproject(scene_pos.x(), scene_pos.y(), y0)
                if p is not None and any(
                        int(g.get("y0") or 0) == y0
                        and g["x"] <= p[0] <= g["x"] + g["w"]
                        and g["z"] <= p[1] <= g["z"] + g["l"] for g in grids):
                    hit = p
                    break
            if hit is None:
                hit = self._renderer.unproject(scene_pos.x(), scene_pos.y(), 0)
            if hit is None:
                return None
            cx, cz = hit
        # Items are in the way of (and a magnet for) containers and items alike.
        ctx = PlacementContext(grids, list(self._renderer._manual_boxes)
                               + list(self._renderer._items),
                               union=self._has_layout)
        if item is not None:
            # An item always lands; what it breaks comes back as the reason.
            stack_y = over_box[1] + over_box[4] if over_box is not None else None
            pos, warns = ctx.snap_item((w, h, l, item), (cx - w / 2.0, cz - l / 2.0),
                                       y=stack_y)
            return pos, (w, h, l), True, "; ".join(warns) if warns else OK
        pos, valid, reason = ctx.snap((w, h, l, self._place_size),
                                      (cx - w / 2.0, cz - l / 2.0))
        return pos, (w, h, l), valid, reason

    # ── Hover highlight + see into the stack (view only) ─────────────────────
    #
    # J, 2026-09-26: "Can we have highlight on hover and the option to click
    # and hide boxes on top for cargo loader". With no place/paint tool armed
    # the box under the pointer lights up and the status line names it.
    # Alt+click a box hides every box resting above it; the height slider
    # hides everything whose base is at or above N; "Show all" undoes both.
    # Hidden boxes stay in the plan, the counts, save/load and every
    # placement rule; they just are not drawn, hovered or clicked.

    def _box_name(self, group) -> str:
        key = group.box_data[6]
        if isinstance(key, str):
            return self._item_def(key)["name"]
        return _("{n} SCU container").format(n=key)

    def _hover_text(self, group) -> str:
        text = self._box_name(group)
        c = self._renderer._assignments.get(group.pos_key)
        if c:
            text += "  ·  " + c
        above = [o for o in self._renderer.boxes_above(group.box_data)
                 if not self._renderer.is_hidden(o)]
        if above:
            text += "  ·  " + _("Alt+click hides the {n} above").format(n=len(above))
        return text

    def _set_hover(self, group) -> None:
        """Highlight *group* (None = none) and name it in the status line;
        the status it replaced comes back when the hover ends."""
        changed = self._renderer.set_hover(group)
        if group is None:
            if (self._hover_status is not None
                    and self._status_lbl.text() == self._hover_shown):
                self._status_lbl.setText(self._hover_status)
            self._hover_status = None
            return
        if not changed:
            return
        cur = self._status_lbl.text()
        if self._hover_status is None or cur != self._hover_shown:
            self._hover_status = cur
        self._hover_shown = self._hover_text(group)
        self._status_lbl.setText(self._hover_shown)

    def _view_alt_click(self, scene_pos) -> bool:
        """Alt+click: hide the boxes resting on the clicked one."""
        if self._drag:
            return False
        g = self._box_group_at(scene_pos)
        if g is None:
            return False
        box, name = tuple(g.box_data), self._box_name(g)
        self._set_hover(None)
        hidden = self._renderer.peel(box)
        self._refresh_hidden_ui()
        if hidden:
            self._status_lbl.setText(
                _("Hid {n} above {name}  ·  Show all brings them back").format(
                    n=len(hidden), name=name))
        else:
            self._status_lbl.setText(_("Nothing on top of {name}").format(name=name))
        if self._placing():
            self._on_view_hover(scene_pos)
        return True

    def _on_layer_slider(self, value: int) -> None:
        self._set_hover(None)
        self._renderer.set_layer_cap(value)
        self._refresh_hidden_ui()

    def _show_all(self) -> None:
        self._set_hover(None)
        self._renderer.show_all()
        self._refresh_hidden_ui()
        self._status_lbl.setText(_("Showing every box"))

    def _refresh_hidden_ui(self) -> None:
        """Slider range/position, its label, and the hidden-count indicator."""
        r = self._renderer
        levels = max(1, r._levels)
        cap = r._layer_cap
        self._layer_slider.blockSignals(True)
        self._layer_slider.setRange(1, levels)
        self._layer_slider.setValue(levels if cap is None else cap)
        self._layer_slider.blockSignals(False)
        self._layer_slider.setEnabled(levels > 1 and bool(self._current_ship))
        self._layer_lbl.setText(
            _("All layers") if cap is None
            else _("Up to layer {n}/{m}").format(n=cap, m=levels))
        n = r.hidden_count()
        active = n > 0 or cap is not None or bool(r._peeled)
        self._hidden_lbl.setText(
            (_("1 box hidden") if n == 1
             else _("{n} boxes hidden").format(n=n)) + "  ·")
        self._hidden_lbl.setVisible(active)
        self._show_all_btn.setVisible(active)

    def _on_view_hover(self, scene_pos) -> None:
        if self._drag:
            return
        if not self._placing():
            # Hover highlight is for looking; a paint brush has its own cursor.
            self._set_hover(None if self._selected_commodity is not None
                            else self._box_group_at(scene_pos))
            return
        self._set_hover(None)
        t = self._place_target(scene_pos)
        if t is None:
            self._renderer.clear_ghost()
            return
        pos, (w, h, l), valid, reason = t
        if self._place_item is not None:
            name = self._item_def(self._place_item)["name"]
            self._renderer.show_ghost(pos[0], pos[1], pos[2], w, h, l, True,
                                      warn=reason != OK)
            self._status_lbl.setText(
                (_("Click to place {name}").format(name=name) if reason == OK
                 else "⚠ " + _("Places anyway: ") + _(reason))
                + "  ·  R rotate  ·  Esc done")
            return
        self._renderer.show_ghost(pos[0], pos[1], pos[2], w, h, l, valid)
        self._status_lbl.setText(
            (_("Click to place {n} SCU").format(n=self._place_size) if valid
             else _("Can't place: ") + _(reason))
            + "  ·  R rotate  ·  Esc done")

    def _on_view_leave(self) -> None:
        if not self._drag:
            self._set_hover(None)
        if not self._drag and self._placing():
            self._renderer.clear_ghost()

    def _on_view_empty_click(self, scene_pos) -> None:
        if self._drag or not self._placing():
            return
        QTimer.singleShot(0, lambda: self._place_at(scene_pos))

    def _push_undo(self) -> None:
        self._move_undo.append((
            None if self._renderer._manual_boxes is None else list(self._renderer._manual_boxes),
            dict(self._renderer._assignments),
            list(self._renderer._items),
        ))

    def _after_manual_edit(self, msg: str) -> None:
        self._renderer.clear_ghost()
        self._sync_counts_from_boxes()
        self._render_grid()
        self._update_assignment_summary()
        self._status_lbl.setText(msg)

    def _place_at(self, scene_pos, over_box=None) -> bool:
        t = self._place_target(scene_pos, over_box=over_box)
        if t is None:
            return False
        pos, (w, h, l), valid, reason = t
        if self._place_item is not None:
            key = self._place_item
            self._push_undo()
            if self._item_def(key).get("category") == "crate":
                key = self._new_crate(key)
            new = (pos[0], pos[1], pos[2], w, h, l, key)
            self._renderer._items = list(self._renderer._items) + [new]
            shown = self._renderer.reveal_under(new)
            name = self._item_def(key)["name"]
            self._after_manual_edit(
                (_("Placed {name}").format(name=name) if reason == OK
                 else "⚠ " + _("Placed {name}: ").format(name=name) + _(reason))
                + ("  ·  " + _("hidden boxes under it shown") if shown else "")
                + "  ·  Ctrl+Z to undo")
            return True
        if not valid:
            self._status_lbl.setText(_("Can't place: ") + _(reason))
            return True
        self._push_undo()
        new = (pos[0], pos[1], pos[2], w, h, l, self._place_size)
        self._renderer._manual_boxes = list(self._renderer._manual_boxes) + [new]
        shown = self._renderer.reveal_under(new)
        self._after_manual_edit(
            _("Placed {n} SCU").format(n=self._place_size)
            + ("  ·  " + _("hidden boxes under it shown") if shown else "")
            + "  ·  Ctrl+Z to undo")
        return True

    def _remove_box(self, box: tuple) -> None:
        if is_item(box):
            items = list(self._renderer._items)
            if box not in items:
                return
            items.remove(box)
            self._push_undo()
            self._renderer._assignments.pop((box[0], box[1], box[2], box[6]), None)
            self._renderer._items = items
            self._after_manual_edit(
                _("Removed {name}  ·  Ctrl+Z to undo").format(
                    name=self._item_def(box[6])["name"]))
            return
        boxes = list(self._renderer._manual_boxes or [])
        if box not in boxes:
            return
        boxes.remove(box)
        x, y, z, w, h, l, size = box
        on_top = [o for o in boxes if o[1] == y + h and o[0] < x + w and x < o[0] + o[3]
                  and o[2] < z + l and z < o[2] + o[5]]
        if on_top:
            self._status_lbl.setText(
                _("Take the box on top off first (it is hidden: Show all)")
                if any(self._renderer.is_hidden(o) for o in on_top)
                else _("Take the box on top off first"))
            return
        self._push_undo()
        self._renderer._assignments.pop((x, y, z, size), None)
        self._renderer._manual_boxes = boxes
        self._after_manual_edit(
            _("Removed {n} SCU  ·  Ctrl+Z to undo").format(n=size))

    def _view_right_click(self, scene_pos) -> bool:
        if self._drag_right_click():
            return True
        if self._mode != "manual":
            return False
        g = self._box_group_at(scene_pos)
        if g is None:
            return False
        if self._renderer._manual_boxes is None:
            self._renderer._manual_boxes = list(self._renderer._last_boxes)
        box = tuple(g.box_data)
        QTimer.singleShot(0, lambda: self._remove_box(box))
        return True

    def _view_key(self, event) -> bool:
        if self._drag_key(event):
            return True
        if not self._placing():
            return False
        if event.key() == Qt.Key_Escape:
            self._set_place_size(None)
            self._status_lbl.setText(_("Placing done"))
            return True
        if event.key() == Qt.Key_R and not event.modifiers() & (
                Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            self._place_rot = not self._place_rot
            vp = self._view.viewport()
            self._on_view_hover(self._view.mapToScene(vp.mapFromGlobal(QCursor.pos())))
            return True
        return False

    def _drop_manual_layout(self) -> None:
        if self._drag:
            self._renderer.clear_ghost()
            self._drag = None
        self._renderer._manual_boxes = None
        self._move_undo.clear()

    @staticmethod
    def _boxes_from_payload(raw) -> list[tuple] | None:
        """Parse a saved plan's "boxes" list; None if absent or malformed."""
        if not isinstance(raw, list) or not raw:
            return None
        out = []
        try:
            for b in raw:
                size = int(b["scu"])
                x, y, z = (int(v) for v in b["pos"])
                w, h, l = (int(v) for v in b["dims"])
                if size not in CONTAINER_DIMS or \
                        sorted((w, h, l)) != sorted(CONTAINER_DIMS[size]):
                    return None
                out.append((x, y, z, w, h, l, size))
        except (KeyError, TypeError, ValueError):
            return None
        return out

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
        self._renderer._item_flags = self._item_warnings()
        for _pass in range(2):
            self._renderer.render(
                self._slots, self._bounds, self._slot_assignment,
                self._has_layout, self._current_ship,
                lambda text: self._grid_info_lbl.setText(text),
                view_width=vw, view_height=vh,
            )
            # Auto mode: the packer's boxes exist only after a render, so the
            # item warnings are re-checked against them once.
            if not self._renderer._items or self._renderer._manual_boxes is not None:
                break
            flags = self._item_warnings()
            if flags == self._renderer._item_flags:
                break
            self._renderer._item_flags = flags
        # Re-apply visibility filter after re-render
        self._apply_visibility_filter()
        self._update_items_summary()
        self._sync_crate_tabs()
        self._refresh_hidden_ui()

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

        self._refresh_capacity()

        # Update counts dict (a changed count invalidates a hand arrangement)
        new_counts = {s: self._get_count(s) for s in CONTAINER_SIZES}
        if new_counts != self._counts and self._renderer._manual_boxes is not None:
            self._drop_manual_layout()
            self._status_lbl.setText(_("Container counts changed \u2014 manual arrangement reset"))
        for s in CONTAINER_SIZES:
            self._counts[s] = new_counts[s]

        self._update_assignment()
        self._render_grid()

    def _refresh_capacity(self) -> None:
        cap = self._current_ship.get("capacity", 0) if self._current_ship else 0
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

    # -- Cargo plan save / load -----------------------------------------------

    _LOADOUT_DIR = os.path.join(os.path.expanduser("~"), "Documents", "SC Cargo Plans")
    _LOADOUT_VERSION = 1

    def _save_loadout(self) -> None:
        if not self._current_ship:
            return
        os.makedirs(self._LOADOUT_DIR, exist_ok=True)
        default_name = re.sub(r'[\\/:*?"<>|]', "_", self._current_ship["name"])
        dlg = QFileDialog(self, _("Save Cargo Plan"),
                          os.path.join(self._LOADOUT_DIR, f"{default_name}.json"),
                          _("Cargo plan files (*.json);;All files (*)"))
        dlg.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dlg.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dlg.setDefaultSuffix("json")
        if dlg.exec() != QFileDialog.DialogCode.Accepted:
            return
        files = dlg.selectedFiles()
        path = files[0] if files else ""
        if not path:
            return
        payload = self._loadout_payload()
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
            self._status_lbl.setText(_("Saved: ") + os.path.basename(path))
        except OSError as exc:
            self._status_lbl.setText(f"Save error: {exc}")

    def _loadout_payload(self) -> dict:
        """The cargo plan dict that _save_loadout writes (no file I/O)."""
        counts = {str(s): self._get_count(s) for s in CONTAINER_SIZES}
        assignments = [
            {"pos": list(k), "commodity": v}
            for k, v in self._renderer._assignments.items()
        ]
        payload = {
            "version": self._LOADOUT_VERSION,
            "ship": self._current_ship["name"],
            "rotation": self._renderer._rotation,
            "counts": counts,
            "assignments": assignments,
        }
        if self._renderer._manual_boxes is not None:
            # Hand-arranged positions (drag-and-drop). Older builds ignore it.
            payload["boxes"] = [
                {"scu": b[6], "pos": [b[0], b[1], b[2]], "dims": [b[3], b[4], b[5]]}
                for b in self._renderer._manual_boxes
            ]
        if self._renderer._items:
            # Items tab: a separate list, so older builds (and "counts") never
            # see them. name/category travel along for a machine without the
            # item data.
            items = []
            for b in self._renderer._items:
                if crate_no(b[6]) is not None:
                    continue                    # crates travel in "crates"
                d = self._item_def(b[6])
                items.append({"key": b[6], "name": d.get("name", b[6]),
                              "category": d.get("category"),
                              "pos": [b[0], b[1], b[2]], "dims": [b[3], b[4], b[5]]})
            if items:
                payload["items"] = items
        crates = self._crates_payload()
        if crates:
            # Personal crates: number, size, place in the hold and contents.
            # Older builds ignore this key (and so lose only the crates).
            payload["crates"] = crates
        return payload

    def _load_loadout(self) -> None:
        os.makedirs(self._LOADOUT_DIR, exist_ok=True)
        dlg = QFileDialog(self, _("Load Cargo Plan"),
                          self._LOADOUT_DIR,
                          _("Cargo plan files (*.json);;All files (*)"))
        dlg.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dlg.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
        dlg.setFileMode(QFileDialog.FileMode.ExistingFile)
        if dlg.exec() != QFileDialog.DialogCode.Accepted:
            return
        files = dlg.selectedFiles()
        path = files[0] if files else ""
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            self._status_lbl.setText(f"Load error: {exc}")
            return
        ship_name = payload.get("ship", "")
        if not ship_name:
            self._status_lbl.setText(_("Load error: missing ship name"))
            return
        self._pending_loadout = payload
        self._load_ship(ship_name)

    def _apply_pending_loadout(self) -> None:
        payload = self._pending_loadout
        self._pending_loadout = None
        if not payload:
            return
        # Restore rotation
        rotation = payload.get("rotation", 0)
        self._renderer.set_rotation(rotation)
        self._update_iso_info_label()
        # Reset spinbox limits to physical maxima so dynamic clamping from
        # the previous _update_fill() call doesn't prevent restoring saved values
        self._update_spinbox_limits()
        # Restore commodity assignments before rendering so they appear in the
        # first render triggered by _update_fill()
        raw_assignments = payload.get("assignments", [])
        self._renderer._assignments.clear()
        for entry in raw_assignments:
            pos = entry.get("pos")
            commodity = entry.get("commodity", "")
            if pos and len(pos) == 4 and commodity:
                self._renderer._assignments[tuple(pos)] = commodity
        # Restore container counts
        counts = payload.get("counts", {})
        for s in CONTAINER_SIZES:
            n = int(counts.get(str(s), 0))
            self._spinboxes[s].blockSignals(True)
            self._spinboxes[s].setValue(n)
            self._spinboxes[s].blockSignals(False)
        # Restore a hand arrangement, if the plan carries one that matches
        boxes = self._boxes_from_payload(payload.get("boxes"))
        if boxes is not None:
            self._counts = {s: self._get_count(s) for s in CONTAINER_SIZES}
            self._renderer._manual_boxes = boxes
        self._renderer._items = (self._items_from_payload(payload.get("items"))
                                 + self._crates_from_payload(payload.get("crates")))
        self._update_fill()
        self._freeze_if_manual()
        self._update_assignment_summary()

    def _optimize(self) -> None:
        if not self._current_ship or not self._slots:
            return
        ship_name = self._current_ship.get("name", "")
        ref = _find_reference_loadout(ship_name)
        result = ref if ref is not None else greedy_optimize_3d(self._slots)
        self._drop_manual_layout()
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
        self._freeze_if_manual()

    def _reset_containers(self) -> None:
        self._drop_manual_layout()
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
        self._freeze_if_manual()

    def _clear_containers(self) -> None:
        self._drop_manual_layout()
        for s in CONTAINER_SIZES:
            self._spinboxes[s].blockSignals(True)
            self._spinboxes[s].setValue(0)
            self._spinboxes[s].blockSignals(False)
        self._slot_assignment = []
        self._update_fill()
        self._freeze_if_manual()

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

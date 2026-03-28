"""Backward-compatible facade -- re-exports symbols for audit scripts and serves as entry point."""
import logging
import os
import sys

# -- Path setup ----------------------------------------------------------------
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')))
from shared.app_bootstrap import bootstrap_skill  # noqa: E402
bootstrap_skill(__file__)

# -- Re-export compute functions (audits import these) -------------------------
from services.dps_calculator import (  # noqa: F401
    fire_rate_rps as _fire_rate_rps,
    alpha_max as _alpha_max,
    dps_sustained as _dps_sustained,
    dmg_breakdown as _dmg_breakdown,
    compute_weapon_stats,
)

from services.stat_computation import (  # noqa: F401
    compute_shield_stats,
    compute_cooler_stats,
    compute_radar_stats,
    compute_missile_stats,
    compute_powerplant_stats_erkul,
    compute_qdrive_stats_erkul,
    compute_powerplant_stats,
    compute_qdrive_stats,
    compute_thruster_stats,
)

from services.slot_extractor import extract_slots_by_type  # noqa: F401

# -- Re-export data layer (audits import DataManager and CACHE_FILE) -----------
from data.repository import ComponentRepository as DataManager  # noqa: F401
from dps_ui.constants import CACHE_FILE  # noqa: F401

# -- Re-export UI (power audit imports PowerAllocator) -------------------------
from dps_ui.power_widget import PowerAllocatorWidget as PowerAllocator  # noqa: F401

# -- Re-export everything from constants for any code that did `from dps_calc_app import BG` etc.
from dps_ui.constants import *  # noqa: F401, F403
from dps_ui.helpers import *  # noqa: F401, F403


def main():
    from shared.crash_logger import init_crash_logging
    log = init_crash_logging("dps")
    try:
        from shared.data_utils import parse_cli_args
        p = parse_cli_args(sys.argv[1:])

        # Pre-load the cache BEFORE PySide6 starts.
        # PySide6 + Python 3.14 segfaults (0xC0000005) when a background
        # thread does heavy allocation (JSON parsing 20 MB) while Qt's
        # event loop runs concurrently.  Loading synchronously here
        # eliminates the race.
        # Use stale_ok=True so expired cache is still returned — the UI
        # can display instantly while a background refresh runs.
        from data.repository import CACHE_FILE, CACHE_TTL, CACHE_VERSION
        from data.cache import DiskCache
        _pre_cache = DiskCache(CACHE_FILE, CACHE_TTL, CACHE_VERSION)
        _preloaded = _pre_cache.load(stale_ok=True)
        _needs_refresh = not _pre_cache.is_fresh() if _preloaded else True
        log.info("Pre-loaded cache: %s (needs_refresh=%s)",
                 "OK" if _preloaded else "miss", _needs_refresh)

        from PySide6.QtWidgets import QApplication
        from shared.qt.theme import apply_theme

        app = QApplication(sys.argv)
        apply_theme(app)

        from dps_ui.app import DpsCalcApp
        window = DpsCalcApp(p["x"], p["y"], p["w"], p["h"], p["opacity"], p["cmd_file"],
                            preloaded_cache=_preloaded,
                            needs_refresh=_needs_refresh)

        window.run()
        log.info("Window shown, entering event loop")
        sys.exit(app.exec())
    except Exception:
        log.critical("FATAL crash in dps main()", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

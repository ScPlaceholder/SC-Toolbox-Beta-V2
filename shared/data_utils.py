"""
Shared data-processing helpers used across multiple tools.
"""
import logging
import time
from typing import Optional

log = logging.getLogger(__name__)


def safe_float(v, default=0.0) -> float:
    """Safely convert *v* to float; return *default* on failure."""
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# Alias matching the inline _sf() used in dps_calc_app.py et al.
_sf = safe_float


def pct_diff(a, b) -> float:
    """Absolute percentage difference between *a* and *b*.  0 if both zero."""
    if a == 0 and b == 0:
        return 0.0
    denom = max(abs(a), abs(b))
    if denom == 0:
        return 0.0
    return abs(a - b) / denom * 100.0


def parse_cli_args(argv: list, defaults: Optional[dict] = None) -> dict:
    """Parse the positional CLI args that all tool subprocesses use.

    Standard order: x y w h [custom...] opacity cmd_file
    Some skills inject custom_args between h and opacity (e.g. Trade Hub
    passes refresh_interval and max_routes).  To handle this robustly,
    x/y/w/h are parsed from the front and opacity/cmd_file from the end.
    *defaults* can override the built-in fallbacks.

    Returns a dict with keys: x, y, w, h, opacity, cmd_file, extras.
    """
    d = {"x": 100, "y": 100, "w": 1440, "h": 860, "opacity": 0.95, "cmd_file": None}
    if defaults:
        d.update(defaults)

    try:
        if len(argv) > 0: d["x"] = int(argv[0])
        if len(argv) > 1: d["y"] = int(argv[1])
        if len(argv) > 2: d["w"] = int(argv[2])
        if len(argv) > 3: d["h"] = int(argv[3])
        # cmd_file is always LAST, opacity is always SECOND-TO-LAST
        if len(argv) >= 6:
            d["opacity"]  = float(argv[-2])
            d["cmd_file"] = argv[-1]
        elif len(argv) >= 5:
            # No custom args: argv[4]=opacity, argv[5] would be cmd_file
            # but only 5 args means no cmd_file — just opacity
            d["opacity"] = float(argv[4])
    except (ValueError, IndexError):
        pass

    # Extras are anything between h (index 3) and opacity (index -2)
    if len(argv) >= 6:
        d["extras"] = argv[4:-2]
    else:
        d["extras"] = []
    return d


def retry_request(fn, retries: int = 2, backoff: float = 1.0):
    """Call *fn()* with simple exponential-backoff retry on exception.

    Returns the result of *fn()* on success, or re-raises the last exception.
    """
    delay = backoff
    last_exc = None
    for attempt in range(1 + retries):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                log.debug("retry_request: attempt %d failed (%s), retrying in %.1fs",
                          attempt + 1, exc, delay)
                time.sleep(delay)
                delay *= 2
    raise last_exc

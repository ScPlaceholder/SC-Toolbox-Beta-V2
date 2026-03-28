"""UEX Corp API client with retry, timeout, caching, and response validation.

Uses ``shared.http_client.HttpClient`` for HTTP retry/backoff and
hash-based per-URL disk cache for the 24-hour mining data.
"""
import hashlib
import json
import logging
import os
import re
import time
from typing import Dict, List, Optional, Tuple

import shared.path_setup  # noqa: E402  # centralised path config
from shared.api_config import (  # noqa: E402
    UEX_BASE_URL, MINING_LOADOUT_USER_AGENT,
    UEX_MINING_TIMEOUT, CACHE_TTL_MINING,
)
from shared.http_client import HttpClient

from models.items import (
    ATTR_CHARGE_RATE,
    ATTR_CHARGE_RATE_MODULE,
    ATTR_CHARGE_WINDOW,
    ATTR_CLUSTER,
    ATTR_DURATION,
    ATTR_EXT_POWER,
    ATTR_INERT,
    ATTR_INSTABILITY,
    ATTR_ITEM_TYPE,
    ATTR_MAX_RANGE,
    ATTR_MINING_POWER,
    ATTR_MODULE_SLOTS,
    ATTR_OPT_RANGE,
    ATTR_OVERCHARGE,
    ATTR_RESISTANCE,
    ATTR_SHATTER,
    ATTR_SIZE,
    ATTR_USES,
    CATEGORY_GADGETS,
    CATEGORY_LASERS,
    CATEGORY_MODULES,
    GadgetItem,
    LaserItem,
    ModuleItem,
)

log = logging.getLogger("MiningLoadout.api")

_HEADERS = {"User-Agent": MINING_LOADOUT_USER_AGENT, "Accept": "application/json"}
_client = HttpClient(
    UEX_BASE_URL, headers=_HEADERS,
    timeout=UEX_MINING_TIMEOUT, max_retries=3,
)

# Hash-based per-URL disk cache (24 h TTL)
_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".api_cache")
_CACHE_TTL = CACHE_TTL_MINING


def _cache_path(url: str) -> str:
    h = hashlib.sha256(url.encode()).hexdigest()[:16]
    return os.path.join(_CACHE_DIR, f"{h}.json")


def _read_cache(url: str) -> Optional[list]:
    path = _cache_path(url)
    try:
        if not os.path.exists(path):
            return None
        if time.time() - os.path.getmtime(path) > _CACHE_TTL:
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _write_cache(url: str, data: list) -> None:
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(_cache_path(url), "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError as exc:
        log.debug("Cache write failed for %s: %s", url, exc)


def _uex_get(path: str, use_cache: bool = True) -> list:
    """Fetch a UEX API endpoint with retry + disk cache.

    Returns the 'data' array from the response, or an empty list on failure.
    """
    url = f"{UEX_BASE_URL}/{path}"

    if use_cache:
        cached = _read_cache(url)
        if cached is not None:
            log.debug("Cache hit: %s", path)
            return cached

    result = _client.get_json(path)
    if not result.ok:
        log.error("API request failed for %s: %s", path, result.error)
        return []

    body = result.data
    if isinstance(body, dict):
        data = body.get("data", [])
    elif isinstance(body, list):
        data = body
    else:
        log.warning("Unexpected response type from %s: %s", path, type(body).__name__)
        return []

    if not isinstance(data, list):
        log.warning("Unexpected data type from %s: %s", path, type(data).__name__)
        return []

    if use_cache:
        _write_cache(url, data)
    return data


# ── Response parsing helpers ──────────────────────────────────────────────────

def _build_attr_map(raw: list) -> Dict[int, Dict[str, str]]:
    """Build {item_id: {attr_name: value}} from raw attribute records."""
    result: Dict[int, Dict[str, str]] = {}
    for record in raw:
        item_id = record.get("id_item", 0)
        attr_name = record.get("attribute_name", "")
        value = record.get("value") or ""
        if item_id and attr_name:
            result.setdefault(item_id, {})[attr_name] = value
    return result


def _build_price_map(raw: list) -> Dict[int, float]:
    """Build {item_id: min_buy_price} from raw price records."""
    result: Dict[int, float] = {}
    for record in raw:
        item_id = record.get("id_item", 0)
        buy = float(record.get("price_buy") or 0)
        if buy > 0 and item_id:
            result[item_id] = min(result.get(item_id, buy), buy)
    return result


def _parse_power(val: str) -> Tuple[float, float]:
    """Parse '480-2400' -> (480.0, 2400.0). Single value -> (v, v)."""
    s = str(val).strip() if val else ""
    if not s:
        return 0.0, 0.0
    m = re.match(r'(\d+\.?\d*)\s*-\s*(\d+\.?\d*)', s)
    if m:
        try:
            return float(m.group(1)), float(m.group(2))
        except ValueError:
            log.debug("Failed to parse power range: %r", s)
    try:
        v = float(s)
        return v, v
    except ValueError:
        return 0.0, 0.0


def _float_attr(attrs: Dict[int, Dict[str, str]], item_id: int, name: str) -> Optional[float]:
    """Extract a float attribute, stripping % and commas."""
    raw = (attrs.get(item_id, {}).get(name) or "").replace("%", "").replace(",", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _str_attr(attrs: Dict[int, Dict[str, str]], item_id: int, name: str) -> str:
    """Extract a string attribute."""
    return (attrs.get(item_id, {}).get(name) or "").strip()


# ── Validation ────────────────────────────────────────────────────────────────

def _validate_item_record(record: dict, kind: str) -> bool:
    """Basic validation: record must have an id and a name."""
    if not isinstance(record, dict):
        log.debug("Skipping non-dict %s record", kind)
        return False
    if record.get("id") is None:
        log.debug("Skipping %s record without id", kind)
        return False
    if not record.get("name"):
        log.debug("Skipping %s record id=%s without name", kind, record.get("id"))
        return False
    return True


# ── Main fetch function ───────────────────────────────────────────────────────

def fetch_mining_data(
    use_cache: bool = True,
) -> Tuple[List[LaserItem], List[ModuleItem], List[GadgetItem]]:
    """Fetch all mining items from UEX API and build typed model lists.

    Args:
        use_cache: Whether to use disk cache (default True, 24h TTL).

    Returns:
        Tuple of (lasers, modules, gadgets).

    Raises no exceptions — returns empty lists on total failure.
    """
    log.info("Fetching mining data from UEX API...")

    raw_lasers = _uex_get(f"items/id_category/{CATEGORY_LASERS}", use_cache)
    raw_modules = _uex_get(f"items/id_category/{CATEGORY_MODULES}", use_cache)
    raw_gadgets = _uex_get(f"items/id_category/{CATEGORY_GADGETS}", use_cache)

    raw_laser_attrs = _uex_get(f"items_attributes/id_category/{CATEGORY_LASERS}", use_cache)
    raw_module_attrs = _uex_get(f"items_attributes/id_category/{CATEGORY_MODULES}", use_cache)
    raw_gadget_attrs = _uex_get(f"items_attributes/id_category/{CATEGORY_GADGETS}", use_cache)

    raw_laser_prices = _uex_get(f"items_prices/id_category/{CATEGORY_LASERS}", use_cache)
    raw_module_prices = _uex_get(f"items_prices/id_category/{CATEGORY_MODULES}", use_cache)
    raw_gadget_prices = _uex_get(f"items_prices/id_category/{CATEGORY_GADGETS}", use_cache)

    la = _build_attr_map(raw_laser_attrs)
    ma = _build_attr_map(raw_module_attrs)
    ga = _build_attr_map(raw_gadget_attrs)

    lp = _build_price_map(raw_laser_prices)
    mp = _build_price_map(raw_module_prices)
    gp = _build_price_map(raw_gadget_prices)

    # ── Build laser models ────────────────────────────────────────────────────
    lasers: List[LaserItem] = []
    for r in raw_lasers:
        if not _validate_item_record(r, "laser"):
            continue
        iid = r["id"]
        try:
            sz = int(r.get("size") or _str_attr(la, iid, ATTR_SIZE) or 0)
        except (ValueError, TypeError):
            sz = 0
        min_p, max_p = _parse_power(_str_attr(la, iid, ATTR_MINING_POWER))
        lasers.append(LaserItem(
            id=iid,
            name=r.get("name", ""),
            size=sz,
            company=r.get("company_name", ""),
            min_power=min_p,
            max_power=max_p,
            ext_power=_float_attr(la, iid, ATTR_EXT_POWER),
            opt_range=_float_attr(la, iid, ATTR_OPT_RANGE),
            max_range=_float_attr(la, iid, ATTR_MAX_RANGE),
            resistance=_float_attr(la, iid, ATTR_RESISTANCE),
            instability=_float_attr(la, iid, ATTR_INSTABILITY),
            inert=_float_attr(la, iid, ATTR_INERT),
            charge_window=_float_attr(la, iid, ATTR_CHARGE_WINDOW),
            charge_rate=_float_attr(la, iid, ATTR_CHARGE_RATE),
            module_slots=int(ms if (ms := _float_attr(la, iid, ATTR_MODULE_SLOTS)) is not None else 2),
            price=lp.get(iid, 0),
        ))

    # ── Build module models ───────────────────────────────────────────────────
    modules: List[ModuleItem] = []
    for r in raw_modules:
        if not _validate_item_record(r, "module"):
            continue
        iid = r["id"]
        modules.append(ModuleItem(
            id=iid,
            name=r.get("name", ""),
            item_type=_str_attr(ma, iid, ATTR_ITEM_TYPE) or "Passive",
            power_pct=_float_attr(ma, iid, ATTR_MINING_POWER),
            ext_power_pct=_float_attr(ma, iid, ATTR_EXT_POWER),
            resistance=_float_attr(ma, iid, ATTR_RESISTANCE),
            instability=_float_attr(ma, iid, ATTR_INSTABILITY),
            inert=_float_attr(ma, iid, ATTR_INERT),
            charge_rate=_float_attr(ma, iid, ATTR_CHARGE_RATE_MODULE),
            charge_window=_float_attr(ma, iid, ATTR_CHARGE_WINDOW),
            overcharge=_float_attr(ma, iid, ATTR_OVERCHARGE),
            shatter=_float_attr(ma, iid, ATTR_SHATTER),
            uses=int(_float_attr(ma, iid, ATTR_USES) or 0),
            duration=_float_attr(ma, iid, ATTR_DURATION),
            price=mp.get(iid, 0),
        ))

    # ── Build gadget models ───────────────────────────────────────────────────
    gadgets: List[GadgetItem] = []
    for r in raw_gadgets:
        if not _validate_item_record(r, "gadget"):
            continue
        iid = r["id"]
        gadgets.append(GadgetItem(
            id=iid,
            name=r.get("name", ""),
            charge_window=_float_attr(ga, iid, ATTR_CHARGE_WINDOW),
            charge_rate=_float_attr(ga, iid, ATTR_CHARGE_RATE),
            instability=_float_attr(ga, iid, ATTR_INSTABILITY),
            resistance=_float_attr(ga, iid, ATTR_RESISTANCE),
            cluster=_float_attr(ga, iid, ATTR_CLUSTER),
            price=gp.get(iid, 0),
        ))

    log.info("Fetched %d lasers, %d modules, %d gadgets", len(lasers), len(modules), len(gadgets))
    return lasers, modules, gadgets

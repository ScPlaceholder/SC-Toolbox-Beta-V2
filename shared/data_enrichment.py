"""Shared component-stats enrichment logic.

Extracts the common "enrich with class / grade / hp / power / EM / IR"
block that was previously duplicated in:

* ``data.repository.ComponentRepository`` (``_index`` inner function)
* ``dps_power_audit._enrich``
* ``dps_loadout_audit.build_data_manager._index``
"""

from shared.data_utils import safe_float as _sf


def enrich_component_stats(stats: dict, raw_data: dict) -> dict:
    """Populate common display fields on *stats* from raw erkul *raw_data*.

    Uses ``dict.setdefault`` so any value already computed by a
    type-specific ``compute_*`` function is preserved.

    Fields set:
        class, grade, hp, power_draw, power_max, em_max, ir_max

    Parameters
    ----------
    stats:
        The stats dict returned by a ``compute_*`` function
        (e.g. ``compute_weapon_stats``).
    raw_data:
        The ``entry["data"]`` sub-dict from the raw erkul JSON.

    Returns
    -------
    dict
        The same *stats* dict (mutated in-place) for convenience.
    """
    stats.setdefault("class", raw_data.get("class", ""))
    stats.setdefault("grade", raw_data.get("grade", "?"))

    # Health -- may be a plain number or a dict with an "hp" key
    _hlth = raw_data.get("health", raw_data.get("hp", 0))
    if isinstance(_hlth, dict):
        _hlth = _hlth.get("hp", 0)
    stats.setdefault("hp", _sf(_hlth))

    # Power draw from resource.online.consumption
    res = raw_data.get("resource", {}) or {}
    onl = res.get("online", {}) or {}
    cons = onl.get("consumption", {}) or {}
    if not isinstance(cons, dict):
        cons = {}
    pwr_draw = _sf(cons.get("powerSegment", cons.get("power", 0)))
    stats.setdefault("power_draw", pwr_draw)
    stats.setdefault("power_max", pwr_draw)

    # EM / IR from resource.online.signatureParams
    sig = onl.get("signatureParams", {}) or {}
    em_d = sig.get("em", {}) or {}
    ir_d = sig.get("ir", {}) or {}
    stats.setdefault("em_max", _sf(em_d.get("nominalSignature", 0)))
    stats.setdefault("ir_max", _sf(ir_d.get("nominalSignature", 0)))

    return stats

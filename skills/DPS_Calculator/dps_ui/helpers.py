import re
from dps_ui.constants import _FY_SIZE_MAP, _GROUP_SHORT, group_short, pct  # noqa: F401 – re-export


def _port_label(name: str) -> str:
    s = re.sub(r"hardpoint_|_weapon$|weapon_", "", name, flags=re.I)
    s = re.sub(r"_+", " ", s).strip()
    return s.title() if s else name.replace("_", " ").title()


def _fy_slug(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s.strip())
    return s


def _fy_size(raw) -> int:
    if isinstance(raw, int):
        return raw
    s = str(raw).lower().strip()
    return _FY_SIZE_MAP.get(s, 1)


def _fy_hp_group(fy_list: list) -> dict:
    groups: dict = {}
    for hp in (fy_list or []):
        t = hp.get("type", "unknown")
        groups.setdefault(t, []).append(hp)
    return groups


def _fy_comp_name(hp: dict) -> str:
    comp = hp.get("component") or {}
    return comp.get("name") or hp.get("loadoutIdentifier") or "\u2014"


def _fy_comp_mfr(hp: dict) -> str:
    comp = hp.get("component") or {}
    mfr  = comp.get("manufacturer") or {}
    return mfr.get("name") or mfr.get("code") or ""


def fmt_sig(val) -> str:
    """Format a signature value with K suffix for thousands.

    Returns em-dash for falsy values (0, None, etc.).
    """
    if not val:
        return "\u2014"
    if val >= 1000:
        return f"{val/1000:.1f}K"
    return f"{val:.0f}"

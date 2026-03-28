"""Filtering and search logic for Craft Database blueprints.

Pure functions — no Qt imports, no side effects.
"""

from __future__ import annotations

from domain.models import Blueprint


def matches_search(bp: Blueprint, query: str) -> bool:
    if not query:
        return True
    q = query.lower()
    if q in bp.name.lower():
        return True
    if q in bp.category.lower():
        return True
    for slot in bp.ingredients:
        if q in slot.name.lower():
            return True
        for opt in slot.options:
            if q in opt.name.lower():
                return True
    return False


def filter_blueprints(
    blueprints: list[Blueprint],
    *,
    search: str = "",
    category_type: str = "",
    resource: str = "",
    mission_type: str = "",
    location: str = "",
    contractor: str = "",
    ownable_only: bool = False,
) -> list[Blueprint]:
    result = blueprints

    if search:
        result = [bp for bp in result if matches_search(bp, search)]

    if category_type:
        ct = category_type.lower()
        result = [bp for bp in result if bp.category_type.lower() == ct]

    if resource:
        r = resource.lower()
        result = [
            bp for bp in result
            if any(r in n.lower() for n in bp.ingredient_names)
        ]

    if mission_type:
        mt = mission_type.lower()
        result = [
            bp for bp in result
            if any(mt in m.mission_type.lower() for m in bp.missions)
        ]

    if location:
        loc = location.lower()
        result = [
            bp for bp in result
            if any(loc in m.locations.lower() for m in bp.missions)
        ]

    if contractor:
        c = contractor.lower()
        result = [
            bp for bp in result
            if any(c in m.contractor.lower() for m in bp.missions)
        ]

    if ownable_only:
        result = [bp for bp in result if bp.default_owned == 0]

    return result


def group_categories(categories: list[str]) -> dict[str, list[str]]:
    """Group full category paths into top-level → subcategory lists."""
    groups: dict[str, list[str]] = {}
    for cat in sorted(categories):
        parts = cat.split(" / ", 1)
        top = parts[0]
        sub = parts[1] if len(parts) > 1 else ""
        groups.setdefault(top, [])
        if sub and sub not in groups[top]:
            groups[top].append(sub)
    return groups

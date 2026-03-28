"""One-call bootstrap for skill subprocess entry points.

Replaces the 4-6 lines of ``sys.path`` boilerplate that every skill
entry script previously duplicated.  Call this **after** the one-liner
that puts the project root on ``sys.path``::

    import os, sys
    sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')))
    from shared.app_bootstrap import bootstrap_skill
    bootstrap_skill(__file__)
"""

from __future__ import annotations

import os
import sys

import shared.path_setup  # ensures PROJECT_ROOT is on sys.path


def bootstrap_skill(entry_file: str) -> str:
    """Prepare ``sys.path`` and i18n for a skill subprocess entry point.

    Parameters
    ----------
    entry_file:
        Pass ``__file__`` from the entry-point script.

    Returns
    -------
    str
        The absolute path to the skill directory (for convenience).
    """
    skill_dir = os.path.dirname(os.path.abspath(entry_file))

    # Skill dir MUST be at index 0 so local ui/, data/, services/
    # shadow the project-root ui/ (which is the launcher's UI).
    shared.path_setup.ensure_path(skill_dir, first=True)

    # Evict any cached 'ui' module from the project root so the skill's
    # own ui/ package is found on the next import.
    sys.modules.pop("ui", None)

    # ── i18n: load global + skill-specific translation catalogs ──
    from shared import i18n

    lang = os.environ.get("SC_TOOLBOX_LANG", "en")
    project_root = os.path.normpath(os.path.join(skill_dir, "..", ".."))
    i18n.init(lang, os.path.join(project_root, "locales"))

    # Auto-detect skill domain from the parent folder name (e.g. "DPS_Calculator" → "dps")
    skill_locales = os.path.join(skill_dir, "locales")
    if os.path.isdir(skill_locales):
        # Use the skill folder name lowercased as domain, or check for .mo files
        folder_name = os.path.basename(skill_dir).lower()
        # Detect actual domain from .mo/.po filenames in any language dir
        domain = _detect_domain(skill_locales) or folder_name
        i18n.register_skill(domain, skill_locales)

    return skill_dir


def _detect_domain(locale_dir: str) -> str | None:
    """Find the gettext domain name from .mo/.po files in a locales/ dir."""
    try:
        for lang_dir in os.listdir(locale_dir):
            lc = os.path.join(locale_dir, lang_dir, "LC_MESSAGES")
            if not os.path.isdir(lc):
                continue
            for fname in os.listdir(lc):
                if fname.endswith((".mo", ".po")):
                    return fname.rsplit(".", 1)[0]
    except OSError:
        pass
    return None

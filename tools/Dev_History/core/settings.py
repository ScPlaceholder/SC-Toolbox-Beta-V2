"""Settings for the Dev History tool.

Lives at ``~/.sctoolbox/dev_history/settings.json`` (same ``~/.sctoolbox/<tool>/``
convention as PlayTime).  The search engine caches its index and transcripts in
the same folder (``chronology/`` and ``transcripts/`` sub-folders), so there is no
name clash with ``settings.json``.

The file is never written automatically — defaults apply when it is absent.  Edit
it by hand to point the tool at a different branch of the corpus repo:

    {"ref": "main", "max_results": 50, "excerpts": 3}
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

_DIR = os.path.join(os.path.expanduser("~"), ".sctoolbox", "dev_history")
_SETTINGS_PATH = os.path.join(_DIR, "settings.json")

_DEFAULTS: dict[str, Any] = {
    # Git ref of ScPlaceholder/sc-dev-history to stream from.  The corpus lives
    # on the "corpus" branch until its PR merges into main.
    "ref": "corpus",
    "max_results": 50,   # rows shown in the results list
    "excerpts": 3,       # matching transcript excerpts shown per result
}


def settings_path() -> str:
    return _SETTINGS_PATH


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("dev_history: could not read %s: %s", path, exc)
        return {}


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def load_settings() -> dict:
    s = dict(_DEFAULTS)
    s.update(_read_json(_SETTINGS_PATH))
    ref = str(s.get("ref") or "").strip()
    s["ref"] = ref or _DEFAULTS["ref"]
    s["max_results"] = _clamp_int(s.get("max_results"), _DEFAULTS["max_results"], 1, 500)
    s["excerpts"] = _clamp_int(s.get("excerpts"), _DEFAULTS["excerpts"], 1, 10)
    return s


def save_settings(s: dict) -> None:
    try:
        os.makedirs(_DIR, exist_ok=True)
        tmp = _SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
        os.replace(tmp, _SETTINGS_PATH)
    except (OSError, TypeError) as exc:
        log.warning("dev_history: could not write %s: %s", _SETTINGS_PATH, exc)

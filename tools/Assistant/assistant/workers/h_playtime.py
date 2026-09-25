"""PlayTime worker: total Star Citizen play time from the Game.log backups.

Every PlayTime write (settings, summary, per-file cache, fun cache) goes
through core.settings._write_json, which is replaced with a no-op here,
so a headless scan never touches ~/.sctoolbox/playtime/. The existing
per-file cache is still READ, which keeps a rescan fast.
"""
from __future__ import annotations

import _assist_logic as L

from core import settings as _st

_writes_blocked = []
_st._write_json = lambda path, data: _writes_blocked.append(path)

from core import analytics as A          # noqa: E402
from core import log_scanner as S        # noqa: E402


def _h(sec) -> float:
    return round(float(sec or 0) / 3600.0, 1)


def playtime_summary() -> dict:
    folder = S.get_or_detect_folder()
    if not folder:
        raise L.ToolFail("could not find a Star Citizen install with Game.log files")
    s = _st.load_settings()
    sessions = S.scan(folder, recurse=bool(s.get("scan_subfolders", True)))
    cap = float(s.get("session_cap_hours", 0) or 0)
    if cap > 0:
        sessions = S.apply_cap(sessions, cap)
    a = A.build_analytics(sessions)
    summary = A.build_summary(a, s.get("time_format", "hours"), folder)
    h = a.highlights
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    res = {
        "total_hours": _h(a.total_seconds),
        "headline": summary.get("headline"),
        "sessions": a.session_count,
        "active_days": h.active_days,
        "avg_session_hours": _h(h.avg_session),
        "longest_session_hours": _h(h.longest_session.duration_seconds) if h.longest_session else None,
        "longest_session_date": h.longest_session.start.date().isoformat() if h.longest_session else None,
        "current_streak_days": h.current_streak,
        "longest_streak_days": h.longest_streak,
        "busiest_weekday": days[h.busiest_weekday[0]] if h.busiest_weekday else None,
        "busiest_hour_utc": h.busiest_hour[0] if h.busiest_hour else None,
        "first_played": h.first_session.date().isoformat() if h.first_session else None,
        "last_played": summary.get("last_played"),
        "by_channel_hours": {k: _h(v) for k, v in sorted(a.by_channel.items(), key=lambda kv: -kv[1])},
        "session_cap_hours": cap or None,
        "sc_folder": folder,
        "writes_blocked": len(_writes_blocked),
    }
    if a.session_count == 0:
        res["empty"] = True
        res["note"] = "found the folder but no timestamped Game.log sessions in it"
    return res


EXPORTS = {"playtime_summary": playtime_summary}

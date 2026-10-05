"""What a tool says about itself when the mouse rests on it.

J, 2026-10-05: "Can we also have a tool tip pop up when users hover over each tool that
summarizes its functionality?"

THE TEXT IS THE TOOL'S OWN. Each tool carries one or two plain sentences in the
``"summary"`` field of its skill.json (SkillConfig.summary), so a new tool brings
its own and nothing here has to learn about it. A tool with no summary still gets
a tooltip: its name and its hotkey.

WHAT A TOOLTIP HOLDS, in order:

    the tool's name          as on its tile
    its summary              wrapped, see WRAP
    "Hotkey: Ctrl+3"         the binding in force NOW (the caller passes it), left
                             out when the tool has no hotkey or it is switched off

and, for a window that hosts other tools as tabs (SkillConfig.tab_of: SuitMk2 is a
tab of the Toolbox Assistant), the same three lines again for each tab under
"Tab: <name>", because a hidden tool has no tile of its own to say it on.

WIDTH. Every line is cut to WRAP characters here, by hand, and the HTML form says
"do not wrap again". Qt's own tooltip wrapping picks its width from the text, so
the same summary could come out narrow on one tile and wider than the launcher on
the next. The theme draws tooltips in a fixed-width face (shared/qt/theme.py,
QToolTip), so a character cap is a width cap.

No Qt in this file: the launcher's tiles use tooltip_html(), the Assistant
window's tab buttons use tooltip_text() (they append lines of their own with a
newline, which rich text would swallow), and the tests read both.
"""
from __future__ import annotations

import html
import json
import os
import textwrap
from typing import Iterable, List, Mapping, NamedTuple, Optional, Sequence

from shared.hotkey_label import format_hotkey

# A summary longer than this is a paragraph, not a tooltip. Enforced by the tests
# over every skill.json, not here: a long one is still shown, wrapped.
SUMMARY_MAX = 160

# Characters per tooltip line. 46 of the theme's 8pt fixed-width face is about
# 290 px with the padding, inside the launcher's 400 px minimum width.
WRAP = 46


class Section(NamedTuple):
    """One tool's part of a tooltip. ``hotkey`` is already "Hotkey: Ctrl+3", or ""."""
    title: str
    summary: str
    hotkey: str


def clean(text: object) -> str:
    """One line, single spaces. A summary typed over several lines in a skill.json is one sentence."""
    return " ".join(str(text or "").split())


def wrap(text: object, width: int = WRAP) -> List[str]:
    """*text* as lines of at most *width* characters; [] for nothing."""
    return textwrap.wrap(clean(text), width=width, break_long_words=True, break_on_hyphens=False)


def read_summary(skill_dir: str) -> str:
    """The "summary" in *skill_dir*/skill.json, or "" (no file, no field, not readable).

    For a tool that needs its own or a neighbour's summary without the launcher's
    registry (the Assistant window's tab buttons).
    """
    try:
        with open(os.path.join(skill_dir, "skill.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return ""
    return clean(data.get("summary")) if isinstance(data, dict) else ""


def hotkey_line(binding: str, label: str = "Hotkey") -> str:
    """'<ctrl>+3' -> 'Hotkey: Ctrl+3'; '' when there is no binding."""
    shown = format_hotkey(binding or "")
    return "%s: %s" % (label, shown) if shown else ""


def section(name: str, summary: str = "", binding: str = "", *, hotkey_label: str = "Hotkey") -> Section:
    return Section(clean(name), clean(summary), hotkey_line(binding, hotkey_label))


def tile_sections(
    skill,
    skills: Iterable,
    *,
    hotkeys: Optional[Mapping[str, str]] = None,
    keybinds_off: Sequence[str] = (),
    disabled: Sequence[str] = (),
    hotkey_label: str = "Hotkey",
    tab_label: str = "Tab",
) -> List[Section]:
    """The sections of *skill*'s tile tooltip: the tool itself, then each tool that is a tab of it.

    *skills* is the whole registry (hidden tools included: that is where the tabs
    are). *hotkeys* maps a tool id to a binding that overrides ``skill.hotkey``,
    as LauncherWindow.update_hotkey_badges is given. A tool in *keybinds_off* has
    its hotkey switched off in Settings and shows none; a tab tool in *disabled*
    is switched off altogether and is not a tab, so it is not mentioned.
    """
    hotkeys = hotkeys or {}

    def binding(s) -> str:
        if s.id in keybinds_off:
            return ""
        return hotkeys.get(s.id, s.hotkey) or ""

    out = [section(skill.name, getattr(skill, "summary", ""), binding(skill), hotkey_label=hotkey_label)]
    for other in skills:
        if getattr(other, "tab_of", "") == skill.id and other.id != skill.id and other.id not in disabled:
            out.append(section("%s: %s" % (tab_label, other.name), getattr(other, "summary", ""),
                               binding(other), hotkey_label=hotkey_label))
    return out


def tooltip_lines(sections: Sequence[Section], width: int = WRAP) -> List[str]:
    """Every line of the tooltip, wrapped; sections are separated by one empty line."""
    lines: List[str] = []
    for sec in sections:
        part = wrap(sec.title, width) + wrap(sec.summary, width) + wrap(sec.hotkey, width)
        if not part:
            continue
        if lines:
            lines.append("")
        lines.extend(part)
    return lines


def tooltip_text(sections: Sequence[Section], width: int = WRAP) -> str:
    """Plain text, one wrapped line per line. "" only when there is nothing at all to say."""
    return "\n".join(tooltip_lines(sections, width))


def tooltip_html(sections: Sequence[Section], width: int = WRAP, *,
                 title_color: str = "", hotkey_color: str = "") -> str:
    """Rich text for QWidget.setToolTip: the same lines as tooltip_text, the name bold, the hotkey tinted.

    Every piece of text is escaped, so a "<" or "&" in a tool's name or summary is
    shown, not parsed. "" when there is nothing to say (Qt shows no bubble for "").
    """
    def span(line: str, color: str, bold: bool = False) -> str:
        text = html.escape(line, quote=False)
        if bold:
            text = "<b>%s</b>" % text
        return '<span style="color:%s">%s</span>' % (color, text) if color else text

    blocks: List[str] = []
    for sec in sections:
        rows = [span(l, title_color, bold=True) for l in wrap(sec.title, width)]
        rows += [html.escape(l, quote=False) for l in wrap(sec.summary, width)]
        rows += [span(l, hotkey_color) for l in wrap(sec.hotkey, width)]
        if rows:
            blocks.append("<br>".join(rows))
    if not blocks:
        return ""
    # white-space:pre keeps each wrapped line whole: Qt must not wrap it a second time.
    return '<div style="white-space:pre">%s</div>' % "<br><br>".join(blocks)

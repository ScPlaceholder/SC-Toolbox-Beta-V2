"""UI helper functions (palette-agnostic) for the Mission Database app."""
import re
from config import TAG_COLORS
from shared.qt.theme import P


def tag_colors(text: str):
    """Return (bg, fg) for a tag label."""
    if text in TAG_COLORS:
        return TAG_COLORS[text]
    return ("#1a2030", P.fg_dim)


def faction_initials(name: str) -> str:
    """Extract 2-letter initials from faction name."""
    words = name.split()
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    return name[:2].upper() if name else "??"


def strip_html(text: str) -> str:
    """Remove HTML tags and convert erkul-style tags to readable text."""
    if not text:
        return ""
    text = re.sub(r"<EM4>", "", text)
    text = re.sub(r"</EM4>", "", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\\n", "\n")
    return text.strip()


def fmt_uec(val) -> str:
    """Format aUEC value with comma separators."""
    if val is None:
        return "\u2014"
    try:
        return f"{int(val):,} aUEC"
    except (ValueError, TypeError):
        return str(val)


def fmt_time(minutes) -> str:
    """Format minutes into readable time."""
    if not minutes:
        return "\u2014"
    if minutes < 60:
        return f"{minutes}m"
    h = minutes // 60
    m = minutes % 60
    return f"{h}h {m}m" if m else f"{h}h"

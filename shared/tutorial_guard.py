"""Checks a tool's tutorial against the tool it describes.

A tutorial goes stale quietly: a button is renamed, a tab is dropped, and the
tutorial goes on naming the old one. Each tool's tests/test_tutorial.py uses
this to make that a failing test.

THE RULE. In a tutorial, every name set in bold (<b>...</b>) is one of:

  a UI name   text the user can read in the tool. It must be found in a string
              in the tool's own source (the files the test names), so renaming
              the button there turns the test red.
  an alias    a UI name the tutorial cannot quote letter for letter, because
              the tool builds it at run time ("INVENTORY (N)" for
              f"INVENTORY ({n})"). The test maps it to the piece of source
              text that must exist.
  prose       bold used for emphasis or for something that is not on screen
              (a key on the keyboard, a game term). The test lists these, so
              a new bold phrase has to be put on one list or the other by
              whoever writes it.

unknown_names() returns the bold phrases that are none of the three, and
unused() returns list entries the tutorial no longer contains, so the lists
cannot rot either.

WHAT IT DOES NOT DO. It does not know whether a sentence is true, only that
the names in it exist. A name that is not in bold is not checked. A UI name
matches if ANY string in the named source files contains it as whole words,
so a very short name ("Own") is a weak check.

No Qt here: the test builds the tutorial's window itself.
"""
from __future__ import annotations

import ast
import html as _html
import os
import re
from typing import Dict, Iterable, List, Sequence

_BOLD = re.compile(r"<b\b[^>]*>(.*?)</b>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def clean(text: str) -> str:
    """Tags removed, entities decoded, runs of white space made one space."""
    return _WS.sub(" ", _html.unescape(_TAG.sub("", text or ""))).strip()


def plain_text(markup: str) -> str:
    """What a reader sees of *markup*, as one line."""
    return clean(markup)


def bold_spans(markup: str) -> List[str]:
    """The text of every <b>...</b> in *markup*, in order, without repeats."""
    seen: Dict[str, None] = {}
    for m in _BOLD.finditer(markup or ""):
        t = clean(m.group(1))
        if t:
            seen.setdefault(t, None)
    return list(seen)


def source_strings(paths: Iterable[str], exclude: Iterable[str] = ()) -> List[str]:
    """Every string literal in the given .py files (and every .py under a
    given folder), cleaned the same way as the tutorial text. The literal
    parts of an f-string count; what it fills in at run time does not.

    *exclude* names files to leave out. The tutorial's own file MUST be one
    of them when it sits under a folder given here: every name it quotes is
    a string in it, so reading it would make every check pass."""
    skip = {os.path.normcase(os.path.abspath(p)) for p in exclude}
    files: List[str] = []
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, names in os.walk(p):
                dirs[:] = [d for d in dirs if d not in ("__pycache__", "tests", ".claude")]
                files.extend(os.path.join(root, n) for n in names if n.endswith(".py"))
        else:
            files.append(p)
    files = [f for f in files if os.path.normcase(os.path.abspath(f)) not in skip]
    if not files:
        raise FileNotFoundError("no source files to read: %r" % (list(paths),))
    out: List[str] = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=f)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                t = clean(node.value)
                if t:
                    out.append(t)
    return out


def is_in_source(name: str, strings: Sequence[str]) -> bool:
    """True if some source string is *name*, or contains it as whole words."""
    name = clean(name)
    if not name:
        return False
    pat = re.compile(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])")
    return any(s == name or pat.search(s) for s in strings)


def unknown_names(markup: str, strings: Sequence[str], prose: Iterable[str] = (),
                  aliases: Dict[str, str] | None = None) -> List[str]:
    """Bold phrases of *markup* that are not a UI name, an alias or prose.
    An alias whose source text is missing is unknown too."""
    prose = {clean(p) for p in prose}
    aliases = {clean(k): v for k, v in (aliases or {}).items()}
    bad = []
    for name in bold_spans(markup):
        if name in prose:
            continue
        if name in aliases:
            if not is_in_source(aliases[name], strings):
                bad.append("%s (alias for %r, which is not in the source)" % (name, aliases[name]))
            continue
        if not is_in_source(name, strings):
            bad.append(name)
    return bad


def unused(markup: str, prose: Iterable[str] = (), aliases: Dict[str, str] | None = None) -> List[str]:
    """Entries of the prose and alias lists that are not bold in *markup* any more."""
    have = set(bold_spans(markup))
    return [p for p in list(prose) + list(aliases or {}) if clean(p) not in have]


def problems(markup: str, strings: Sequence[str], prose: Iterable[str] = (),
             aliases: Dict[str, str] | None = None) -> List[str]:
    """Everything wrong, as sentences; [] when the tutorial and its lists agree with the source."""
    out = ["bold in the tutorial, but not in the tool's source and not listed as prose: %r" % n
           for n in unknown_names(markup, strings, prose, aliases)]
    out += ["listed in the test, but no longer bold in the tutorial (remove it from the list): %r" % n
            for n in unused(markup, prose, aliases)]
    return out

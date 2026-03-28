#!/usr/bin/env python3
"""
Extract translatable strings and generate a single combined .pot template.

Usage:
    python tools/extract_strings.py

Output:
    locales/SC_Toolbox_Source.pot   — auto-updating source (regenerated every run)

This file is the ONLY file the script ever writes.  Translator .po files
(e.g. ``locales/de/LC_MESSAGES/sc_toolbox.po``) are NEVER read, modified,
or deleted by this script.

Translators open ``SC_Toolbox_Source.pot`` in Poedit to create their .po
file.  When new strings appear in the .pot, Poedit's "Update from POT
file" merges them into the existing .po WITHOUT losing any prior work.

Auto-expansion:
    Any new folder added under ``skills/`` is automatically scanned.
    No manual registration is needed.

Requirements:
    - Python 3.10+ (uses ast module — no external dependencies)
"""
from __future__ import annotations

import ast
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Directories to scan for translatable strings (launcher + shared)
LAUNCHER_DIRS = [
    PROJECT_ROOT / "ui",
    PROJECT_ROOT / "core",
    PROJECT_ROOT / "shared",
]

# Function names that mark translatable strings
MARKER_FUNCS = {"_", "s_", "_t", "N_"}


def discover_all_source_dirs() -> list[Path]:
    """Return all directories to scan: launcher dirs + every skill folder."""
    dirs = list(LAUNCHER_DIRS)

    skills_root = PROJECT_ROOT / "skills"
    if skills_root.is_dir():
        for entry in sorted(skills_root.iterdir()):
            if entry.is_dir() and not entry.name.startswith((".", "__")):
                dirs.append(entry)

    return dirs


def extract_strings_from_file(filepath: Path) -> list[tuple[int, str]]:
    """Extract (line_number, string) tuples from a Python file."""
    try:
        source = filepath.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(filepath))
    except (SyntaxError, UnicodeDecodeError) as exc:
        print(f"  WARN: skipping {filepath}: {exc}")
        return []

    results: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func = node.func
        func_name = None
        if isinstance(func, ast.Name) and func.id in MARKER_FUNCS:
            func_name = func.id
        elif isinstance(func, ast.Attribute) and func.attr in MARKER_FUNCS:
            func_name = func.attr

        if not func_name:
            continue

        if not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            results.append((node.lineno, arg.value))

    return results


def collect_strings(source_dirs: list[Path]) -> dict[str, list[tuple[str, int]]]:
    """Collect all translatable strings from source directories.

    Returns: {string: [(relative_filepath, lineno), ...]}
    """
    strings: dict[str, list[tuple[str, int]]] = {}

    for src_dir in source_dirs:
        if not src_dir.exists():
            continue
        for pyfile in sorted(src_dir.rglob("*.py")):
            parts = pyfile.parts
            if any(p.startswith((".", "__")) for p in parts):
                continue

            extracted = extract_strings_from_file(pyfile)
            rel = pyfile.relative_to(PROJECT_ROOT)
            for lineno, text in extracted:
                if text not in strings:
                    strings[text] = []
                strings[text].append((str(rel).replace("\\", "/"), lineno))

    return strings


def escape_po(s: str) -> str:
    """Escape a string for .po file format."""
    s = s.replace("\\", "\\\\")
    s = s.replace('"', '\\"')
    s = s.replace("\n", "\\n")
    s = s.replace("\t", "\\t")
    return s


def generate_pot(strings: dict[str, list[tuple[str, int]]]) -> str:
    """Generate the combined .pot file content."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M+0000")

    header = textwrap.dedent(f"""\
        # ============================================================
        # SC_Toolbox — Combined Translation Source (auto-generated)
        # ============================================================
        #
        # This file is regenerated automatically.  DO NOT translate
        # strings in this file — use Poedit to create a .po copy.
        #
        # Translator workflow:
        #   1. Open this .pot in Poedit → creates your .po file
        #   2. Translate strings and save
        #   3. When this .pot updates with new strings, open your
        #      .po in Poedit and click Catalogue → Update from POT
        #      file.  All your existing translations are preserved.
        #
        #, fuzzy
        msgid ""
        msgstr ""
        "Project-Id-Version: SC_Toolbox 1.2\\n"
        "Report-Msgid-Bugs-To: \\n"
        "POT-Creation-Date: {now}\\n"
        "PO-Revision-Date: YEAR-MO-DA HO:MI+ZONE\\n"
        "Last-Translator: FULL NAME <EMAIL@ADDRESS>\\n"
        "Language-Team: LANGUAGE <LL@li.org>\\n"
        "Language: \\n"
        "MIME-Version: 1.0\\n"
        "Content-Type: text/plain; charset=UTF-8\\n"
        "Content-Transfer-Encoding: 8bit\\n"

    """)

    entries: list[str] = []
    for msgid, locations in sorted(strings.items()):
        refs = " ".join(f"{f}:{ln}" for f, ln in locations)
        entry = f"#: {refs}\n"
        entry += f'msgid "{escape_po(msgid)}"\n'
        entry += 'msgstr ""\n'
        entries.append(entry)

    return header + "\n".join(entries) + "\n"


def main() -> None:
    source_dirs = discover_all_source_dirs()
    print(f"Scanning {len(source_dirs)} directories...")

    for d in source_dirs:
        print(f"  {d.relative_to(PROJECT_ROOT)}")

    strings = collect_strings(source_dirs)

    if not strings:
        print("No translatable strings found.")
        return

    pot_content = generate_pot(strings)

    # Write the auto-updating source file
    out_dir = PROJECT_ROOT / "locales"
    out_dir.mkdir(parents=True, exist_ok=True)
    pot_file = out_dir / "SC_Toolbox_Source.pot"
    pot_file.write_text(pot_content, encoding="utf-8")

    print(f"\n{len(strings)} unique strings -> locales/SC_Toolbox_Source.pot")
    print()
    print("This file is safe to regenerate — it NEVER touches .po files.")
    print("Share SC_Toolbox_Source.pot with translators. They open it in")
    print("Poedit to create or update their translation (.po) files.")


if __name__ == "__main__":
    main()

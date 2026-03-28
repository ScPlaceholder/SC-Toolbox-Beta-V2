#!/usr/bin/env python3
"""
Compile .po files to .mo binary format for gettext.

Usage:
    python tools/compile_mo.py              # compile all .po files under locales/
    python tools/compile_mo.py de           # compile just German

No external dependencies — uses Python's built-in struct module.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = PROJECT_ROOT / "locales"


def parse_po(po_path: Path) -> list[tuple[bytes, bytes]]:
    """Parse a .po file and return (msgid, msgstr) byte pairs."""
    entries: list[tuple[str, str]] = []
    lines = po_path.read_text(encoding="utf-8").splitlines()

    msgid_parts: list[str] = []
    msgstr_parts: list[str] = []
    reading: str | None = None

    def _flush():
        mid = "".join(msgid_parts)
        mstr = "".join(msgstr_parts)
        if mid:  # skip empty msgid (header handled separately)
            entries.append((mid, mstr))

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("msgid "):
            if reading is not None:
                _flush()
            msgid_parts = [_unquote(stripped[6:])]
            msgstr_parts = []
            reading = "id"
        elif stripped.startswith("msgstr "):
            msgstr_parts = [_unquote(stripped[7:])]
            reading = "str"
        elif stripped.startswith('"') and stripped.endswith('"'):
            val = _unquote(stripped)
            if reading == "id":
                msgid_parts.append(val)
            elif reading == "str":
                msgstr_parts.append(val)
        elif not stripped or stripped.startswith("#"):
            if reading is not None:
                _flush()
                msgid_parts = []
                msgstr_parts = []
                reading = None

    if reading is not None:
        _flush()

    # Convert to bytes
    result: list[tuple[bytes, bytes]] = []
    for mid, mstr in entries:
        result.append((mid.encode("utf-8"), mstr.encode("utf-8")))
    result.sort(key=lambda pair: pair[0])
    return result


def _unquote(s: str) -> str:
    """Remove surrounding quotes and unescape PO escape sequences."""
    s = s.strip()
    if s.startswith('"') and s.endswith('"'):
        s = s[1:-1]
    # Unescape in correct order
    s = s.replace("\\n", "\n")
    s = s.replace("\\t", "\t")
    s = s.replace('\\"', '"')
    s = s.replace("\\\\", "\\")
    return s


def write_mo(messages: list[tuple[bytes, bytes]], mo_path: Path) -> None:
    """Write a .mo file from sorted (msgid, msgstr) byte pairs."""
    n = len(messages)

    # Collect all msgids and msgstrs
    ids = b""
    strs = b""
    id_offsets: list[tuple[int, int]] = []
    str_offsets: list[tuple[int, int]] = []

    for mid, mstr in messages:
        id_offsets.append((len(mid), len(ids)))
        ids += mid + b"\x00"
        str_offsets.append((len(mstr), len(strs)))
        strs += mstr + b"\x00"

    # .mo layout:
    # - header (28 bytes)
    # - id table (n * 8 bytes)
    # - str table (n * 8 bytes)
    # - id strings
    # - str strings
    header_size = 28
    id_table_offset = header_size
    str_table_offset = id_table_offset + n * 8
    ids_start = str_table_offset + n * 8
    strs_start = ids_start + len(ids)

    # Header
    data = struct.pack(
        "Iiiiiii",
        0x950412DE,      # magic
        0,               # version
        n,               # number of strings
        id_table_offset, # offset of id table
        str_table_offset,# offset of str table
        0,               # hash table size
        0,               # hash table offset
    )

    # ID length/offset table
    for length, offset in id_offsets:
        data += struct.pack("ii", length, ids_start + offset)

    # Str length/offset table
    for length, offset in str_offsets:
        data += struct.pack("ii", length, strs_start + offset)

    # String data
    data += ids
    data += strs

    mo_path.parent.mkdir(parents=True, exist_ok=True)
    mo_path.write_bytes(data)


def compile_language(lang_dir: Path) -> None:
    """Compile all .po files in a language directory."""
    lc = lang_dir / "LC_MESSAGES"
    if not lc.is_dir():
        return

    for po_file in lc.glob("*.po"):
        mo_file = po_file.with_suffix(".mo")
        messages = parse_po(po_file)
        write_mo(messages, mo_file)
        print(f"  {po_file.relative_to(PROJECT_ROOT)} -> {len(messages)} messages")


def main() -> None:
    args = sys.argv[1:]

    if args:
        # Compile specific languages
        for lang in args:
            lang_dir = LOCALES_DIR / lang
            if lang_dir.is_dir():
                print(f"Compiling {lang}...")
                compile_language(lang_dir)
            else:
                print(f"ERROR: No locale directory for '{lang}'")
                sys.exit(1)
    else:
        # Compile all languages
        if not LOCALES_DIR.is_dir():
            print("No locales/ directory found.")
            return

        count = 0
        for lang_dir in sorted(LOCALES_DIR.iterdir()):
            if lang_dir.is_dir() and not lang_dir.name.startswith("."):
                lc = lang_dir / "LC_MESSAGES"
                if lc.is_dir() and list(lc.glob("*.po")):
                    print(f"Compiling {lang_dir.name}...")
                    compile_language(lang_dir)
                    count += 1

        if count:
            print(f"\nDone! {count} language(s) compiled.")
        else:
            print("No .po files found to compile.")


if __name__ == "__main__":
    main()

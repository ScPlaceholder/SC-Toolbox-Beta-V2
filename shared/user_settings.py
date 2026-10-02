"""Where a tool's user settings live, and the one-time move out of the install folder.

Velopack replaces the install folder (%LOCALAPPDATA%/SC_Toolbox/current) wholesale on every update, so a
settings file kept beside the code is thrown away by every release. Worse, releases 2.2.17 to 2.4.0 SHIPPED
the developer's own settings files inside the package, so an update did not merely reset a user's choices,
it replaced them with someone else's (every tool hotkey disabled, UI scale 1.5, a blank UEX key).

Settings now live in ~/.sctoolbox/<tool>/, outside anything an update touches (the convention Battle Buddy,
Mission Database and the shared settings already use).

    path = settings_path("trade_hub", "config.json")
    data = load_json(path, legacy=os.path.join(SKILL_DIR, "trade_hub_config.json"))

MIGRATION. When the new file does not exist yet, the old in-folder file is read once, and the next save
lands at the new path. But an installed user's old file is very often not theirs: it is the copy a release
put there. So a legacy file whose bytes exactly match one we SHIPPED is treated as "never customised" and
ignored, and the tool starts from its own defaults. Anything else is the user's own work and is carried over.
"""
import hashlib
import json
import logging
import os

log = logging.getLogger("sctoolbox.user_settings")

ROOT = os.path.join(os.path.expanduser("~"), ".sctoolbox")

# sha256 of every settings file found inside the full release packages 2.2.17, 2.3.0, 2.3.1 and 2.4.0.
# Older packages were not on disk when this was written, so a file shipped only by 2.2.16 or earlier is
# not recognised and would be migrated as if the user had chosen it. Keyed by the legacy file name.
SHIPPED = {
    'battle_buddy_settings.json': {
        '922df5385933c5f4647c4801d67469ea0c14d96d5fb8c7bcb264ad44b468fa1f',
    },
    'cargo_loader_config.json': {
        '074cbcefc55c45c20e10521b64ebeb29cb33064d339f5d7d10a7d7ab9c03dda2',
    },
    'mining_loadout_config.json': {
        '0d6c72efc0417b0adf078c4feda598cbb70302b41a80f26d803990fc7bbcb0bd',
    },
    'mining_signals_config.json': {
        '651ee20bbe34799875fe9d87c5842abaacb2501d4bc4f9b7eeeaf7aae4c574f9',
    },
    'mission_db/settings.json': {
        '53e1dccfc2b4b2f4467b2abd79727da3bf6b31dac01d80d6c05026abf98a7960',
    },
    'skill_launcher_settings.json': {
        '6036e8ed5343294e32dc57f018df09c725ab22783dba645e150d87abfa9bba69',
        'a643b894561bba519cdff8880179f2ec52e2fb34beaa1c8ed600e20465cc9e01',
    },
    'trade_hub_config.json': {
        '16c71de392f832d24c448ff55f27bebe680689c18acdc1f889077a20255fdbd9',
        '2a534e19512e8e96ccc722ad96b16fb56ef6af711ac398262cb3d52d1368e92f',
        '891fc4be4f2f1da3ba1a85d638ef95f062973e0f552758571b6c269ba36348f3',
    },
    'uex_settings.json': {
        '59583c113930a4eaaaa8081e853c955e85adf023b436cd40b98b438e6038f8ef',
    },
}


def settings_path(tool, filename="settings.json"):
    """~/.sctoolbox/<tool>/<filename>. The folder is created on first save, not here."""
    return os.path.join(ROOT, tool, filename)


def is_shipped_copy(path, name=None):
    """True when the file at path is byte-identical to a copy a release shipped."""
    try:
        with open(path, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return False
    return digest in SHIPPED.get(name or os.path.basename(path), ())


def in_installed_copy(path):
    """True inside a Velopack install (.../SC_Toolbox/current/...). A developer's checkout holds the very file
    a release was built from, so there a shipped-identical file IS the owner's settings and must be kept."""
    parts = [p.lower() for p in os.path.normpath(os.path.abspath(path)).split(os.sep)]
    return any(a == "sc_toolbox" and b == "current" for a, b in zip(parts, parts[1:]))


def source_path(path, legacy=None, name=None):
    """The file to READ: the new one if it exists, else a customised legacy file, else None (use defaults)."""
    if os.path.isfile(path):
        return path
    if legacy and os.path.isfile(legacy):
        if in_installed_copy(legacy) and is_shipped_copy(legacy, name):
            log.info("Ignoring %s: it is the copy a release shipped, not the user's settings", legacy)
            return None
        log.info("Migrating settings from %s to %s", legacy, path)
        return legacy
    return None


def load_json(path, legacy=None, default=None, name=None):
    """Read settings, migrating from legacy once. Returns default (or {}) when there is nothing to read."""
    src = source_path(path, legacy, name)
    if src is None:
        return {} if default is None else default
    try:
        with open(src, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("Could not read settings %s: %s", src, exc)
        return {} if default is None else default


def save_json(path, data):
    """Write atomically to the NEW path, creating its folder."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def launcher_settings(root):
    """(path, legacy) for the launcher's settings; root is the toolbox folder. Every reader uses this pair, so
    a tool that only READS (hotkey labels, the Assistant) sees exactly what the launcher sees."""
    return settings_path("launcher"), os.path.join(root, "skill_launcher_settings.json")

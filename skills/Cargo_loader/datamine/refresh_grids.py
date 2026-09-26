"""Headless refresh of the scunpacked cargo grid data.

Re-runs datamine/rebuild_grids.js (Node.js) against a fresh scunpacked-data
ships.json and rewrites datamine/cargo_grids_scunpacked.json, which the
loader reads when cargo_loader_config.json has {"grid_source": "scunpacked"}.
No GUI, no WingmanAI, no PySide6 required.

Usage:
  python refresh_grids.py [ships.json-or-url] [XYZmap]

  ships.json-or-url  Path to a scunpacked-data ships.json (download from
                     https://github.com/StarCitizenWiki/scunpacked-data),
                     or an http(s) URL to fetch it from. Defaults to the
                     ships.json already sitting in this directory.
  XYZmap             Optional axis mapping permutation (e.g. XZY). Defaults
                     to the derived winner (currently width=X, height=Z,
                     length=Y).

Requires node on PATH.

Attribution: Ship data: StarCitizenWiki/scunpacked-data.
Star Citizen content (c) Cloud Imperium Games.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
CACHE_FILE = os.path.join(SKILL_DIR, ".cargo_cache.json")
OUT_FILE = os.path.join(HERE, "cargo_grids_scunpacked.json")
REBUILD_JS = os.path.join(HERE, "rebuild_grids.js")
DEFAULT_SHIPS = os.path.join(HERE, "ships.json")
DEFAULT_URL = "https://raw.githubusercontent.com/StarCitizenWiki/scunpacked-data/main/ships.json"


def obtain_ships(spec: str | None) -> str:
    if spec and spec.startswith(("http://", "https://")):
        print("downloading %s ..." % spec)
        urllib.request.urlretrieve(spec, DEFAULT_SHIPS)
        return DEFAULT_SHIPS
    if spec:
        if not os.path.exists(spec):
            raise SystemExit("ships.json not found: %s" % spec)
        return spec
    if os.path.exists(DEFAULT_SHIPS):
        return DEFAULT_SHIPS
    print("no local ships.json; downloading %s ..." % DEFAULT_URL)
    urllib.request.urlretrieve(DEFAULT_URL, DEFAULT_SHIPS)
    return DEFAULT_SHIPS


def main(argv: list[str]) -> int:
    ships = obtain_ships(argv[1] if len(argv) > 1 else None)
    map_str = argv[2] if len(argv) > 2 else None

    if not os.path.exists(CACHE_FILE):
        raise SystemExit(
            "loader cache %s not found; open the Cargo Loader once with the "
            "default source so it can build the baseline cache" % CACHE_FILE
        )
    node = shutil.which("node")
    if not node:
        raise SystemExit("node is required on PATH to run rebuild_grids.js")

    cmd = [node, REBUILD_JS, "convert", ships, CACHE_FILE]
    if map_str:
        cmd.append(map_str)
    cmd.append(OUT_FILE)
    print("running: %s" % " ".join(cmd))
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        raise SystemExit("rebuild_grids.js failed with exit code %d" % proc.returncode)

    with open(OUT_FILE, encoding="utf-8") as fh:
        data = json.load(fh)
    ships_out = data.get("ships", [])
    converted = sum(
        1 for s in ships_out
        if (s.get("provenance") or {}).get("source") == "scunpacked-data"
    )
    print("done: %d ships total (%d converted from scunpacked, %d carried over)" % (
        len(ships_out), converted, len(ships_out) - converted))
    if grid_source_hint() != "scunpacked":
        print('note: set {"grid_source": "scunpacked"} in %s to make the loader use this file' % (
            os.path.join(SKILL_DIR, "cargo_loader_config.json")))
    return 0


def grid_source_hint() -> str:
    try:
        with open(os.path.join(SKILL_DIR, "cargo_loader_config.json"), encoding="utf-8") as fh:
            return json.load(fh).get("grid_source", "sc_cargo_space")
    except (OSError, json.JSONDecodeError):
        return "sc_cargo_space"


if __name__ == "__main__":
    sys.exit(main(sys.argv))

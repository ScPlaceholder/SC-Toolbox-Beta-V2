"""training_shots.py - the frames the eyes LOOKED AT, kept only if the pilot opts in, exportable as one zip
(J 2026-09-24: "a training screenshot export where it will zip users agent screenshots and export them").

What is kept, and what is not:
  - ONLY frames the vision model actually described (a routine glance or a deliberate look), each with what the model
    said about it. Never the change-gate frames the eyes skim every few seconds, and never the desktop: the eyes
    only capture while StarCitizen.exe is in front.
  - The JPEG the model saw (glance_crop, ~768 px wide, ~40-80 KB), not a full-resolution frame.
  - OFF by default. The launch notice is where the pilot turns it on ("keep_training_shots" in settings), and the
    setting is re-read on every shot, so switching it off stops keeping at once with no restart.
  - Rolling cap (MAX_SHOTS): the oldest shot is deleted when a new one would pass it.

Why: these pairs (frame -> what the eyes said) are how the eyes get better at being present and making fewer
mistakes. They are NOT used to train anything to play the game. NOTICE below is the one text both the launch popup
and the exported README use, so the promise cannot drift between the two.

    python training_shots.py --selftest
"""
from __future__ import annotations

import json
import os
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional

MAX_SHOTS = 500
NOTICE_VERSION = 1          # bump when NOTICE changes, so every pilot sees the new wording once
NOTICE_TITLE = "SuitMk2 - what the companion is, and what it is not"
NOTICE = (
    "Elah and Montaigne are NARRATORS. They watch and listen, and they talk. That is all they can do.\n\n"
    "They have no way to press keys, move the mouse, use a controller, inject input, read or change game memory or "
    "files, or alter the game in any way. They give no unfair advantage: everything they know comes from the game's "
    "own log file, the game's sound, and what is on your screen, which you can already see and hear.\n\n"
    "Training screenshots (optional, off unless you tick the box):\n"
    "When the companion's eyes look closely at the screen, SuitMk2 can keep that small screenshot and what the eyes "
    "thought it showed, on this PC only, up to {max} of them. Nothing is uploaded. You can export them as a zip from "
    "the SuitMk2 window if you choose to share them.\n\n"
    "They are used ONLY to make the companion better at noticing what is happening, so it is more present and makes "
    "fewer mistakes. They are never used to train a bot to play the game for you or for anyone else."
).format(max=MAX_SHOTS)


class ShotStore:
    """Thread-safe: the eyes loop and a deliberate look can both keep a shot."""

    def __init__(self, root: Path, enabled: Callable[[], bool], max_shots: int = MAX_SHOTS,
                 now: Callable[[], float] = time.time):
        self.root, self._enabled, self.max_shots, self._now = Path(root), enabled, max_shots, now
        self.index = self.root / "index.jsonl"
        self._lock = threading.Lock()
        self.kept = 0
        self.errors = 0

    def enabled(self) -> bool:
        try:
            return bool(self._enabled())
        except Exception:
            return False                     # cannot tell -> do not keep

    def keep(self, jpeg: bytes, meta: dict) -> Optional[Path]:
        """Keep one looked-at frame. -> its path, or None when keeping is off or it failed (never raises: a
        failure here must not cost the companion its eyes)."""
        if not jpeg or not self.enabled():
            return None
        try:
            with self._lock:
                self.root.mkdir(parents=True, exist_ok=True)
                t = self._now()
                name = time.strftime("%Y%m%d_%H%M%S", time.localtime(t)) + f"_{int(t * 1000) % 1000:03d}.jpg"
                path = self.root / name
                n = 1
                while path.exists():
                    path = self.root / f"{name[:-4]}_{n}.jpg"
                    n += 1
                tmp = path.with_suffix(".jpg.tmp")
                tmp.write_bytes(jpeg)
                os.replace(tmp, path)
                row = {"file": path.name, "t": round(t, 3)}
                row.update({k: v for k, v in meta.items() if k in ("reason", "scene", "confidence", "notable")})
                with self.index.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                self.kept += 1
                self._prune()
                return path
        except Exception:
            self.errors += 1
            return None

    def _rows(self) -> list:
        if not self.index.exists():
            return []
        rows = []
        for line in self.index.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows

    def _prune(self) -> None:
        rows = [r for r in self._rows() if (self.root / r.get("file", "")).exists()]
        drop, keep = rows[:-self.max_shots] if len(rows) > self.max_shots else [], rows[-self.max_shots:]
        for r in drop:
            try:
                (self.root / r["file"]).unlink()
            except OSError:
                pass
        if drop or len(keep) != len(self._rows()):
            tmp = self.index.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep), encoding="utf-8")
            os.replace(tmp, self.index)

    def count(self) -> int:
        return sum(1 for r in self._rows() if (self.root / r.get("file", "")).exists())

    def export(self, out_zip: Path, app_version: str = "") -> int:
        """Zip every kept shot + manifest.jsonl + README.txt (the same NOTICE). -> number of shots exported."""
        with self._lock:
            rows = [r for r in self._rows() if (self.root / r.get("file", "")).exists()]
            out_zip = Path(out_zip)
            out_zip.parent.mkdir(parents=True, exist_ok=True)
            tmp = out_zip.with_suffix(out_zip.suffix + ".tmp")
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:     # JPEGs do not compress further
                for r in rows:
                    z.write(self.root / r["file"], "shots/" + r["file"])
                z.writestr("manifest.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
                z.writestr("README.txt",
                           f"SuitMk2 training screenshots\nexported {time.strftime('%Y-%m-%d %H:%M:%S')}"
                           f"{' - app ' + app_version if app_version else ''}\n{len(rows)} shots. Each line of "
                           f"manifest.jsonl is one shot: the file, when, why the eyes looked (reason), and what "
                           f"they thought it showed (scene, confidence, notable).\n\n{NOTICE}\n")
            os.replace(tmp, out_zip)
            return len(rows)

    def clear(self) -> int:
        """Delete every kept shot. -> how many were deleted."""
        with self._lock:
            n = 0
            for r in self._rows():
                try:
                    (self.root / r["file"]).unlink()
                    n += 1
                except (OSError, KeyError):
                    pass
            if self.index.exists():
                self.index.unlink()
            return n


def default_store() -> ShotStore:
    """The store the app uses: under the SuitMk2 settings dir, gated LIVE on the pilot's setting."""
    import settings as st
    return ShotStore(st.DIR / "training_shots", enabled=lambda: bool(st.load().get("keep_training_shots")))


def _selftest() -> int:
    import tempfile
    ok = True

    def case(name, cond):
        nonlocal ok
        ok &= bool(cond)
        print(("  PASS  " if cond else "  FAIL  ") + name)
    with tempfile.TemporaryDirectory() as d:
        flag = [False]
        clock = [1_700_000_000.0]

        def tick():
            clock[0] += 1.5
            return clock[0]
        s = ShotStore(Path(d) / "shots", enabled=lambda: flag[0], max_shots=3, now=tick)
        case("OFF by default: nothing is kept", s.keep(b"jpeg", {"reason": "glance"}) is None and s.count() == 0)
        flag[0] = True
        p = s.keep(b"\xff\xd8one", {"reason": "arrival", "scene": "station", "confidence": 0.8,
                                    "notable": "a hangar, or maybe a cargo bay", "secret": "x"})
        case("ON: the frame is kept", p is not None and p.read_bytes() == b"\xff\xd8one")
        row = s._rows()[0]
        case("index carries what the eyes said, and ONLY the listed fields",
             row["notable"] == "a hangar, or maybe a cargo bay" and "secret" not in row)
        for i in range(4):
            s.keep(f"f{i}".encode(), {"reason": "glance"})
        case("rolling cap: never more than max_shots, oldest dropped",
             s.count() == 3 and not p.exists() and len(list((Path(d) / "shots").glob("*.jpg"))) == 3)
        flag[0] = False
        case("switching OFF stops keeping at once", s.keep(b"late", {}) is None and s.count() == 3)
        z = Path(d) / "out" / "export.zip"
        n = s.export(z, "test")
        with zipfile.ZipFile(z) as zz:
            names = zz.namelist()
            readme = zz.read("README.txt").decode("utf-8")
        case("export zips every shot + manifest + README", n == 3 and "manifest.jsonl" in names
             and sum(x.startswith("shots/") for x in names) == 3)
        case("README carries the same promise as the popup", NOTICE in readme and "never used to train a bot" in readme)
        case("no .tmp left behind", not list(Path(d).rglob("*.tmp")))
        bad = ShotStore(Path(d) / "x", enabled=lambda: 1 / 0)
        case("a broken setting reads as OFF, never raises", bad.keep(b"j", {}) is None)
        case("clear deletes them all", s.clear() == 3 and s.count() == 0)
    print("training_shots selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)

"""Tests must write NOTHING outside `tmp_path`.

A fixture poisoned a production cache in this tree on 2026-09-27: an injected
fake returned one row, the loader cached it unconditionally, and the next real
run reported a clean board computed from that single fake row. So this file
does not take the rule on trust — it hashes the package tree, exercises every
path that could plausibly write, and re-hashes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pico import selftest as st
from pico.anim import ChannelMap, Clip, ClipLibrary
from pico.rig import Rig, Skeleton, default_skeleton_path

PKG = Path(st.__file__).resolve().parent


def tree_digest() -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    for p in sorted(PKG.rglob("*")):
        if p.is_dir() or "__pycache__" in p.parts:
            continue
        h.update(p.relative_to(PKG).as_posix().encode())
        h.update(p.read_bytes())
        n += 1
    return h.hexdigest(), n


def test_the_digest_can_see_a_change(tmp_path):
    """Prove the instrument works before trusting what it reports.

    A hash function that ignored its input would report "unchanged" forever,
    and that is precisely the reading this file exists to produce.
    """
    before, n = tree_digest()
    assert n >= 15, "only %d files hashed; the walk is not seeing the package" % n
    scratch = PKG / "__hygiene_probe.tmp"
    scratch.write_text("x", encoding="utf-8")
    try:
        after, n2 = tree_digest()
        assert after != before and n2 == n + 1
    finally:
        scratch.unlink()
    assert tree_digest() == (before, n)


def test_a_full_exercise_writes_nothing_into_the_package():
    before = tree_digest()
    sk = Skeleton.load()
    cm = ChannelMap(sk)
    lib = ClipLibrary.load_dir(skeleton=sk, channel_map=cm)
    rig = Rig(sk, debug=True)
    for name in lib:
        clip = lib[name]
        for i in range(21):
            rig.slot_transforms(clip.sample(clip.duration * i / 20.0), st.SKIN_DRAKE)
    assert st.main(["--quiet"]) == 0
    assert tree_digest() == before, "the package tree changed during a test run"


def test_the_shipped_skeleton_is_not_rewritten_by_loading_it():
    p = default_skeleton_path()
    before = p.read_bytes()
    for _ in range(3):
        Skeleton.load()
    assert p.read_bytes() == before


def test_write_clip_lands_only_in_tmp_path(write_clip, tmp_path):
    p = write_clip("probe", {
        "name": "probe", "duration": 1.0, "loop": False, "channel": "BASE",
        "tracks": {"body": [{"t": 0.0, "rot": 0}, {"t": 1.0, "rot": 1}]}})
    assert tmp_path in p.parents
    assert PKG not in p.parents
    assert json.loads(p.read_text(encoding="utf-8"))["name"] == "probe"
    Clip.load(p)  # loading a tmp clip must not touch the package either


def test_the_clip_directory_holds_only_anim_files():
    stray = [p.name for p in (PKG / "clips").iterdir() if p.suffix != ".anim"]
    assert not stray, (
        "%s in the clip directory — ClipLibrary globs *.anim, so anything else "
        "here is invisible to it and will drift" % stray)

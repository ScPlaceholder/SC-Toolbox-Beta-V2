"""Shared fixtures for the Pico rig runtime tests.

NOTHING HERE WRITES OUTSIDE `tmp_path`. Tests that need a file on disk build it
under pytest's `tmp_path`; the shipped skeleton and clips are only ever READ.
A fixture that reaches a production path poisons a cache and the next real run
reports a clean board computed from the fake — that happened in this tree on
2026-09-27 and it is the reason this paragraph exists.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# `tools/Pico` — the import root for the `pico` package.
PICO_ROOT = Path(__file__).resolve().parents[2]
if str(PICO_ROOT) not in sys.path:
    sys.path.insert(0, str(PICO_ROOT))

from pico.anim import ChannelMap, ClipLibrary  # noqa: E402
from pico.rig import Rig, Skeleton, default_skeleton_path  # noqa: E402


@pytest.fixture(scope="session")
def skeleton_dict() -> dict:
    """The raw JSON. Deep-copied per use by `skeleton_variant`."""
    return json.loads(default_skeleton_path().read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def skeleton() -> Skeleton:
    return Skeleton.load()


@pytest.fixture()
def rig(skeleton: Skeleton) -> Rig:
    return Rig(skeleton)


@pytest.fixture(scope="session")
def channels(skeleton: Skeleton) -> ChannelMap:
    return ChannelMap(skeleton)


@pytest.fixture(scope="session")
def clips(skeleton: Skeleton, channels: ChannelMap) -> ClipLibrary:
    return ClipLibrary.load_dir(skeleton=skeleton, channel_map=channels)


@pytest.fixture()
def skeleton_variant(skeleton_dict: dict):
    """`skeleton_variant(mutate) -> Skeleton`, built in memory, never on disk."""
    def make(mutate) -> Skeleton:
        d = json.loads(json.dumps(skeleton_dict))
        mutate(d)
        return Skeleton.from_dict(d)
    return make


@pytest.fixture()
def write_clip(tmp_path: Path):
    """`write_clip(name, dict) -> Path` inside tmp_path. The only writer."""
    def make(name: str, body: dict) -> Path:
        d = tmp_path / "clips"
        d.mkdir(exist_ok=True)
        p = d / (name + ".anim")
        p.write_text(json.dumps(body), encoding="utf-8")
        return p
    return make

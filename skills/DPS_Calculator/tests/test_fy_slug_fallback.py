"""Tests for the FleetYards slug fallback used by the thruster section.

FleetYards has no page for many game-class variants ("drak-vulture-teach",
Collector skins, "rsi-apollo-medivac-tier-2") and names a few ships without the
manufacturer prefix ("razor"). Without a fallback the thruster section stays
empty for those ships (measured 2026-09-25: 17 of 40 random class names 404'd).
"""

import os
import sys
import threading

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.repository import _fy_slug_candidates, ComponentRepository  # noqa: E402


def test_exact_slug_is_tried_first():
    assert _fy_slug_candidates("aegs-sabre")[0] == "aegs-sabre"


def test_variant_suffixes_are_trimmed_in_order():
    c = _fy_slug_candidates("rsi-apollo-medivac-tier-2")
    assert c[:3] == ["rsi-apollo-medivac-tier-2", "rsi-apollo-medivac-tier", "rsi-apollo-medivac"]


def test_never_trims_below_two_tokens_but_drops_manufacturer_last():
    c = _fy_slug_candidates("misc-razor")
    assert c == ["misc-razor", "razor"]
    assert "misc" not in c


def test_capped():
    assert len(_fy_slug_candidates("a-b-c-d-e-f-g-h-i")) <= 6


class _FakeApi:
    def __init__(self, pages):
        self.pages = pages
        self.asked = []

    def fetch_hardpoints(self, slug):
        self.asked.append(slug)
        return self.pages.get(slug, [])


class _NoCache:
    def get(self, slug):
        return None

    def put(self, slug, data):
        self.put_key = slug


def _run(repo, name):
    done = threading.Event()
    out = {}

    def on_done(groups):
        out["g"] = groups
        done.set()

    repo.fetch_fy_hardpoints(name, on_done=on_done)
    assert done.wait(5)
    return out["g"]


def _repo(pages):
    repo = ComponentRepository.__new__(ComponentRepository)
    repo._fy_api = _FakeApi(pages)
    repo._fy_cache = _NoCache()
    return repo


def test_fetch_falls_back_to_base_variant():
    thr = [{"category": "main_thrusters", "name": "t"}]
    repo = _repo({"drak-vulture": thr})
    g = _run(repo, "drak-vulture-teach")
    assert g.get("main_thrusters") == thr
    assert repo._fy_api.asked == ["drak-vulture-teach", "drak-vulture"]
    assert repo._fy_cache.put_key == "drak-vulture-teach"   # cached under what the window asks for


def test_fetch_gives_empty_when_nothing_resolves():
    repo = _repo({})
    assert _run(repo, "rsi-zeus-es") == {}

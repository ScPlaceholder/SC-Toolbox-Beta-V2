"""Capture store, size cap, import-as-proposals, label discipline."""
from __future__ import annotations

import pytest
from PIL import Image

from devmode import api, captures, labels, paths, train
from devmode.tests.conftest import render_value, tiny


def _solid(i: int, size=(60, 30)) -> Image.Image:
    return tiny(color=(i % 256, (i * 7) % 256, (i * 13) % 256), size=size)


def test_dev_root_honours_env(dev_root):
    assert api.dev_root() == dev_root
    assert not paths.is_inside_install(dev_root)


def test_capture_toggle_persists():
    assert api.capture_enabled() is False
    api.set_capture_enabled(True)
    assert api.capture_enabled() is True
    api.set_capture_enabled(False)
    assert api.capture_enabled() is False


def test_add_capture_proposals_and_stats():
    a = api.add_capture(_solid(1), "signal", {"onnx": "7,080", "tesseract": "7080", "paddle": None}, {})
    b = api.add_capture(_solid(2), "signal", {"onnx": "7080", "tesseract": "7030"}, {"reader": "7080"})
    c = api.add_capture(_solid(3), "hud", {"onnx": "0.61"}, {})
    d = api.add_capture(_solid(4), "hud", {"onnx": "garbage!"}, {})
    rows = {r["id"]: r for r in api.list_captures(limit=10)}
    assert (rows[a]["proposed"], rows[a]["proposal_source"], rows[a]["status"]) == ("7080", "consensus", "proposed")
    assert (rows[b]["proposed"], rows[b]["proposal_source"]) == ("7080", "reader")   # 1-1 split: no consensus
    assert (rows[c]["proposed"], rows[c]["proposal_source"]) == ("0.61", "reader")   # lone engine
    assert (rows[d]["proposed"], rows[d]["status"]) == (None, "unlabeled")
    for r in rows.values():
        assert r["label"] is None                       # a proposal is never a label
    st = api.capture_stats()
    assert st["count"] == 4 and st["bytes"] > 0 and st["cap_bytes"] == 500 * 1024 * 1024
    assert api.label_stats() == {"unlabeled": 1, "proposed": 3, "confirmed": 0, "rejected": 0}
    assert api.label_stats("signal_rgb_inv")["proposed"] == 2    # family filter
    # exact duplicate -> same id, no new row
    assert api.add_capture(_solid(1), "signal_inv", {}, {}) == a
    assert api.capture_stats()["count"] == 4


def test_screenshot_is_refused():
    with pytest.raises(captures.ScreenshotRefusedError):
        api.add_capture(Image.new("RGB", (1920, 1080)), "signal", {}, {})
    assert api.capture_stats()["count"] == 0


def test_unknown_kind_rejected():
    with pytest.raises(ValueError):
        api.add_capture(_solid(1), "refinery", {}, {})


def test_cap_eviction_oldest_unconfirmed_first_never_confirmed():
    ids = [api.add_capture(_solid(i), "signal", {}, {}, created=1000.0 + i) for i in range(6)]
    one = api.capture_stats()["bytes"] // 6
    api.confirm(ids[0], "1234")                  # oldest, but confirmed
    api.reject(ids[3])                           # rejected goes before older unlabeled
    api.set_capture_cap(one * 6 + one // 2)      # room for 6.5
    new = api.add_capture(_solid(50), "signal", {}, {})
    left = {r["id"] for r in api.list_captures(limit=50)}
    assert ids[0] in left, "confirmed capture was evicted"
    assert ids[3] not in left, "rejected capture should be evicted first"
    assert new in left and ids[1] in left
    api.add_capture(_solid(51), "signal", {}, {})
    left = {r["id"] for r in api.list_captures(limit=50)}
    assert ids[1] not in left and ids[0] in left, "next victim is the oldest unconfirmed"
    assert api.capture_stats()["bytes"] <= api.capture_stats()["cap_bytes"]


def test_cap_full_of_confirmed_refuses_instead_of_evicting():
    a = api.add_capture(_solid(1), "signal", {}, {})
    b = api.add_capture(_solid(2), "signal", {}, {})
    api.confirm(a, "1111")
    api.confirm(b, "2222")
    api.set_capture_cap(api.capture_stats()["bytes"])
    with pytest.raises(captures.CapFullError):
        api.add_capture(_solid(3), "signal", {}, {})
    assert {r["id"] for r in api.list_captures()} == {a, b}


def test_import_folder_values_are_proposals_only(tmp_path):
    src = tmp_path / "live_samples"
    src.mkdir()
    render_value("7,080").save(src / "sig_115439_901_7080.png")
    render_value("10,110").save(src / "sig_115440_743_10620.png")    # reader was WRONG
    render_value("21,200").save(src / "sig_090406_206_none.png")
    render_value("5").save(src / "sig_090407_206_ab!c.png")          # junk value
    progress = []
    n = api.import_folder(str(src), "signal", progress=lambda f, m: progress.append(f))
    assert n == 4 and progress and progress[-1] == 1.0
    by_val = {r["proposed"]: r for r in api.list_captures()}
    assert by_val["7080"]["proposal_source"] == "import"
    assert by_val["10620"]["status"] == "proposed" and by_val["10620"]["label"] is None
    assert by_val[None]["status"] == "unlabeled"
    assert api.label_stats("signal")["confirmed"] == 0
    # proposals never reach training or scoring
    assert labels.confirmed_rows("signal", "all") == []
    samples, _counts, cap_ids = train.build_dataset("signal")
    assert samples == [] and cap_ids == []
    # re-import is idempotent
    assert api.import_folder(str(src), "signal") == 0


def test_confirm_reject_and_stats():
    a = api.add_capture(_solid(1), "signal", {"onnx": "7080", "tess": "7080"}, {})
    b = api.add_capture(_solid(2), "hud", {}, {})
    api.confirm(a, "7,080")
    assert api.get_capture(a)["label"] == "7080" and api.get_capture(a)["status"] == "confirmed"
    with pytest.raises(ValueError):
        api.confirm(b, "12a")
    with pytest.raises(ValueError):
        api.confirm(a, "1.5")                   # '.' is not a signal character
    api.confirm(b, "62%")
    api.reject(a)
    ra = api.get_capture(a)
    assert ra["status"] == "rejected" and ra["label"] is None
    assert api.label_stats() == {"unlabeled": 0, "proposed": 0, "confirmed": 1, "rejected": 1}
    assert [r["id"] for r in api.list_captures(status="confirmed")] == [b]
    with pytest.raises(KeyError):
        api.confirm("nope", "1")
    with pytest.raises(ValueError):
        api.list_captures(status="maybe")


def test_list_captures_paging_newest_first():
    ids = [api.add_capture(_solid(i), "signal", {}, {}, created=100.0 + i) for i in range(5)]
    page1 = [r["id"] for r in api.list_captures(limit=2)]
    page2 = [r["id"] for r in api.list_captures(limit=2, offset=2)]
    assert page1 == [ids[4], ids[3]] and page2 == [ids[2], ids[1]]

"""Benchmark, activate/revert gate, export zip."""
from __future__ import annotations

import importlib.util
import json
import zipfile

import pytest
from PIL import Image

from devmode import activate, api, bench, kinds, split, train
from devmode.tests.conftest import render_value


def _fake_bench(scores: dict):
    """benchmark() replacement: accuracy by model role, fixed n."""
    def fake(kind, model_path=None, *, captures=None):
        role = "candidate" if model_path else "current"
        return {"accuracy": scores[role], "n": scores.get("n", 10), "per_class": {}, "model": role}
    return fake


def _make_candidate(kind="signal"):
    p = train.candidate_path(kind)
    p.write_bytes(b"not really onnx")
    p.with_suffix(".json").write_text(json.dumps({"charClasses": "0123456789@",
                                                  "trainCaptureIds": []}), encoding="utf-8")
    return p


def test_activate_refuses_worse_and_equal_accepts_better_then_revert(monkeypatch):
    stock = kinds.stock_model_path("signal")
    stock_sha = train.file_sha256(stock) if stock.is_file() else None
    _make_candidate()
    monkeypatch.setattr(bench, "heldout_captures", lambda kind, ex=(): [{}] * 10)

    monkeypatch.setattr(bench, "benchmark", _fake_bench({"current": 0.9, "candidate": 0.8}))
    assert api.compare("signal")["better"] is False
    assert api.activate("signal") is False and api.active_model_path("signal") is None

    monkeypatch.setattr(bench, "benchmark", _fake_bench({"current": 0.9, "candidate": 0.9}))
    assert api.activate("signal") is False, "equal is not better"

    monkeypatch.setattr(bench, "benchmark", _fake_bench({"current": 0.9, "candidate": 0.95}))
    c = api.compare("signal")
    assert c["better"] is True and c["current"]["accuracy"] == 0.9
    assert api.activate("signal") is True
    ap = api.active_model_path("signal")
    assert ap and api.dev_root() in train.paths.Path(ap).parents
    assert train.paths.Path(ap).read_bytes() == b"not really onnx"
    if stock_sha:
        assert train.file_sha256(stock) == stock_sha, "shipped model must never change"

    api.revert("signal")
    assert api.active_model_path("signal") is None
    api.revert("signal")                                   # idempotent, never raises
    api.revert("hud_rgb")


def test_activate_needs_enough_heldout(monkeypatch):
    _make_candidate()
    monkeypatch.setattr(bench, "heldout_captures", lambda kind, ex=(): [])
    monkeypatch.setattr(bench, "benchmark", _fake_bench({"current": 0.1, "candidate": 1.0, "n": 3}))
    res = api.compare("signal")
    assert res["better"] is False and "held-out" in res["reason"]
    assert api.activate("signal") is False


def test_compare_without_candidate(monkeypatch):
    monkeypatch.setattr(bench, "benchmark", _fake_bench({"current": 0.5, "candidate": 0.9}))
    res = api.compare("hud")
    assert res["candidate"] is None and res["better"] is False


def test_unscorable_candidate_is_not_activated(monkeypatch):
    _make_candidate()
    real = bench.benchmark

    def only_current(kind, model_path=None, *, captures=None):
        if model_path:
            return real(kind, model_path, captures=captures)      # garbage bytes -> error
        return {"accuracy": 0.0, "n": 10, "per_class": {}, "model": "stock"}
    monkeypatch.setattr(bench, "benchmark", only_current)
    monkeypatch.setattr(bench, "heldout_captures", lambda kind, ex=(): [])
    res = api.compare("signal")
    assert res["better"] is False and "error" in res["candidate"]
    assert api.activate("signal") is False


@pytest.mark.skipif(importlib.util.find_spec("onnxruntime") is None, reason="onnxruntime missing")
def test_benchmark_scores_only_confirmed_heldout_with_stock_model():
    held = []
    v = 100
    while len(held) < 4:
        if split.is_heldout("hud", str(v)):
            held.append(str(v))
        v += 1
    for i, lab in enumerate(held):
        cid = api.add_capture(render_value(lab, jitter=i), "hud", {}, {})
        api.confirm(cid, lab)
    # proposed-only capture with a held-out value must NOT be scored
    api.add_capture(render_value(held[0], jitter=9), "hud", {"a": held[0], "b": held[0]}, {})
    r = api.benchmark("hud")
    assert r["n"] == 4
    assert 0.0 <= r["accuracy"] <= 1.0
    assert r["model"] == str(kinds.stock_model_path("hud"))
    assert set(r["per_class"]) <= set("0123456789.%")


def test_export_preview_matches_zip_and_holds_crops_only(tmp_path):
    api.set_game_info(resolution="2560x1440", hud_colour="#44ccff")
    # misread: reader said 7030, human says 7080
    a = api.add_capture(render_value("7,080"), "signal", {"onnx": "7030", "tess": "7030"}, {"reader": "7030"})
    # no_read
    b = api.add_capture(render_value("9,999"), "signal", {"onnx": None}, {"reader": None})
    # corrected: reader unknown, one engine wrong
    c = api.add_capture(render_value("0.61"), "hud", {"onnx": "0.61", "tess": "0.67"}, {"user_path": "C:/Users/x"})
    # correct and agreed -> not a failure, not exported
    d = api.add_capture(render_value("62%"), "hud", {"onnx": "62%", "tess": "62%"}, {"reader": "62%"})
    # unconfirmed failure-looking capture -> not exported
    api.add_capture(render_value("5,555"), "signal", {"onnx": "5550"}, {"reader": "5550"})
    for cid, lab in ((a, "7080"), (b, "9999"), (c, "0.61"), (d, "62%")):
        api.confirm(cid, lab)

    prev = api.export_preview()
    reasons = {p.get("capture_id"): p["reason"] for p in prev if p.get("capture_id")}
    assert reasons == {a: "misread", b: "no_read", c: "corrected"}
    zpath = api.export_zip(str(tmp_path / "out"))
    with zipfile.ZipFile(zpath) as zf:
        names = sorted(zf.namelist())
        assert names == sorted(p["file"] for p in prev)
        sizes = {i.filename: i.file_size for i in zf.infolist()}
        for p in prev:
            assert sizes[p["file"]] == p["size"], p["file"]
        for n in names:
            if n.endswith(".png"):
                with zf.open(n) as fh:
                    w, h = Image.open(fh).size
                assert w < 1280 or h < 720
                assert n.startswith("crops/")
        lines = [json.loads(x) for x in zf.read("labels.jsonl").decode("utf-8").splitlines()]
        settings = json.loads(zf.read("settings.json"))
    assert {l["capture_id"] for l in lines} == {a, b, c}
    assert all("user_path" not in l["meta"] for l in lines)
    assert settings["game_resolution"] == "2560x1440" and settings["hud_colour"] == "#44ccff"
    assert settings["toolbox_version"]
    assert set(settings["models"]) == set(kinds.KINDS)
    assert settings["counts"]["exported"] == 3
    blob = json.dumps(settings) + json.dumps(lines)
    assert str(api.dev_root()) not in blob and "C:/Users" not in blob, "no local paths in the zip"


def test_export_empty_is_valid(tmp_path):
    prev = api.export_preview()
    assert [p["file"] for p in prev] == ["labels.jsonl", "settings.json"]
    z = api.export_zip(str(tmp_path))
    with zipfile.ZipFile(z) as zf:
        assert sorted(zf.namelist()) == ["labels.jsonl", "settings.json"]


def test_active_model_is_under_dev_root_only():
    d = train.model_dir("hud")
    assert api.dev_root() in d.parents
    assert activate.active_model_path("hud") is None

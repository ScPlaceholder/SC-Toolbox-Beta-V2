"""Held-out split, glyph extraction, synth, training data, engine."""
from __future__ import annotations

import importlib.util
import subprocess

import pytest

from devmode import api, engine, glyphs, labels, split, synth, train
from devmode.tests.conftest import render_value


def _labels_by_side(fam: str, n_each: int = 3, digits: int = 3):
    held, tr = [], []
    v = 10 ** (digits - 1)
    while len(held) < n_each or len(tr) < n_each:
        s = str(v)[:digits]
        (held if split.is_heldout(fam, s) else tr).append(s)
        v += 1
    return held[:n_each], tr[:n_each]


def test_split_is_deterministic_and_grouped_by_value():
    vals = [str(v) for v in range(1000, 3000)]
    first = [split.is_heldout("signal", v) for v in vals]
    assert first == [split.is_heldout("signal", v) for v in vals]
    frac = sum(first) / len(first)
    assert 0.15 < frac < 0.25, frac
    assert split.split_of("signal", None) is None


def test_confirmed_split_exposed_and_heldout_never_extracted():
    held, tr = _labels_by_side("hud")
    ids = {}
    for i, lab in enumerate(held + tr):
        cid = api.add_capture(render_value(lab, jitter=i % 3), "hud", {}, {})
        api.confirm(cid, lab)
        ids[cid] = lab
    # same label on a second capture lands on the same side
    twin = api.add_capture(render_value(held[0], jitter=5), "hud", {}, {})
    api.confirm(twin, held[0])
    sides = {r["id"]: r["split"] for r in api.list_captures(status="confirmed", limit=50)}
    assert sides[twin] == "heldout"
    assert {sides[c] for c, lab in ids.items() if lab in held} == {"heldout"}
    assert {sides[c] for c, lab in ids.items() if lab in tr} == {"train"}

    n = api.extract_glyphs("hud")
    assert n > 0, "synthetic HUD crops should segment"
    heldout_ids = {r["id"] for r in labels.confirmed_rows("hud", "heldout")}
    all_g = api.list_glyphs("hud")
    assert all_g and not ({g["capture_id"] for g in all_g} & heldout_ids)
    assert api.extract_glyphs("hud") == 0          # idempotent

    for g in all_g:
        api.approve_glyph(g["id"])
    samples, counts, cap_ids = train.build_dataset("hud")
    assert samples and not (set(cap_ids) & heldout_ids)
    st = api.glyph_stats("hud")
    assert set("0123456789.%") <= set(st)
    assert sum(v["approved"] for v in st.values()) == len(all_g)


def test_pending_and_rejected_glyphs_do_not_train():
    _held, tr = _labels_by_side("hud", 1, 4)
    lab = tr[0]
    cid = api.add_capture(render_value(lab), "hud", {}, {})
    api.confirm(cid, lab)
    api.extract_glyphs("hud")
    gs = api.list_glyphs("hud")
    assert gs
    api.reject_glyph(gs[0]["id"])
    samples, counts, _ = train.build_dataset("hud")
    assert samples == []                           # nothing approved yet
    for g in gs[1:]:
        api.approve_glyph(g["id"])
    samples, counts, _ = train.build_dataset("hud")
    assert len(samples) == len(gs) - 1
    # relabelling the capture drops its glyphs
    api.confirm(cid, "9.99")
    assert api.list_glyphs("hud") == []


def test_glyph_ids_unknown():
    with pytest.raises(KeyError):
        api.approve_glyph("missing")


def test_synth_balances_up_to_target_with_fallback_seeds():
    res = api.generate_synth("signal", 12)
    # no approved glyphs: digits come from sc_templates, '@' from the blacklist icon
    assert set(res) == set("0123456789@")
    assert all(v == 12 for v in res.values()), res
    files = synth.synth_files("signal_inv")          # shared pool with signal
    assert all(len(v) == 12 for v in files.values())
    # rgb pools get no template fallback -> digits stay 0 and training names them
    res_rgb = api.generate_synth("signal_rgb", 5)
    assert res_rgb["3"] == 0


def test_train_refuses_without_torch(monkeypatch):
    monkeypatch.setattr(engine, "torch_status", lambda: {"installed": False, "version": None, "location": None})
    with pytest.raises(train.TorchMissingError):
        api.train("signal")


def test_train_refuses_thin_classes(monkeypatch):
    monkeypatch.setattr(engine, "torch_status", lambda: {"installed": True, "version": "x", "location": "y"})
    api.generate_synth("signal", 3)
    with pytest.raises(train.NotEnoughDataError) as ei:
        api.train("signal")
    assert "'0'=3" in str(ei.value)


def test_torch_status_when_absent(monkeypatch):
    monkeypatch.setattr(engine, "_base_torch", lambda: None)
    assert api.torch_status() == {"installed": False, "version": None, "location": None}


def test_install_torch_without_pip_is_a_clear_error_and_runs_nothing(monkeypatch):
    monkeypatch.setattr(engine, "_pip_available", lambda: False)

    def boom(*a, **k):
        raise AssertionError("pip must not be launched")
    monkeypatch.setattr(subprocess, "Popen", boom)
    with pytest.raises(engine.TorchInstallError) as ei:
        api.install_torch()
    assert "pip" in str(ei.value)


def test_builders_are_lifted_from_the_original_trainers():
    from devmode import train_worker
    for kind, (src, fn, _kw) in train.BUILDERS.items():
        f = train_worker.load_builder(str(train.paths.TOOL_DIR / src), fn)
        assert callable(f), kind


@pytest.mark.skipif(importlib.util.find_spec("torch") is None,
                    reason="PyTorch is not importable in this interpreter; training smoke skipped")
def test_train_smoke_writes_candidate_outside_install(monkeypatch):
    monkeypatch.setenv("SC_DEVMODE_EPOCHS", "1")
    api.generate_synth("signal", 30)
    path = api.train("signal", progress=lambda f, m: None)
    p = train.paths.Path(path)
    assert p.is_file() and p.parent == api.dev_root() / "models" / "signal"
    assert not train.paths.is_inside_install(p)
    import onnxruntime as ort
    s = ort.InferenceSession(str(p), providers=["CPUExecutionProvider"])
    assert s.get_outputs()[0].shape[-1] == 11
    # no held-out set yet -> never activated
    assert api.compare("signal")["better"] is False
    assert api.activate("signal") is False
    assert api.active_model_path("signal") is None

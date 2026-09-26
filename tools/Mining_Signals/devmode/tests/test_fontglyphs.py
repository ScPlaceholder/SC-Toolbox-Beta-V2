"""Glyphs rendered from the game font: pool, sizing, colour, synth seeds,
and the hard guarantee that they never reach the benchmark."""
from __future__ import annotations

import collections
import inspect
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from devmode import api, bench, db, fontglyphs, glyphs, kinds, labels, segment, split, synth, train
from devmode.tests.conftest import render_value


def _train_labels(fam: str, n: int, digits: int = 4, start: int = 1000) -> list[str]:
    out, v = [], start
    while len(out) < n:
        s = str(v)[:digits]
        if not split.is_heldout(fam, s):
            out.append(s)
        v += 1
    return out


def _heldout_labels(fam: str, n: int, start: int = 1000) -> list[str]:
    out, v = [], start
    while len(out) < n:
        if split.is_heldout(fam, str(v)):
            out.append(str(v))
        v += 1
    return out


def _real_glyphs(kind: str, n_caps: int = 3, fg=(40, 230, 120), bg=(12, 20, 30)) -> list[dict]:
    """Confirm train-split captures, cut and approve their glyphs."""
    fam = kinds.family(kind)
    for i, lab in enumerate(_train_labels(fam, n_caps)):
        cid = api.add_capture(render_value(lab, fg=fg, bg=bg, jitter=i), kind, {}, {})
        api.confirm(cid, lab)
    assert api.extract_glyphs(kind) > 0
    gs = api.list_glyphs(kind, source="capture")
    for g in gs:
        api.approve_glyph(g["id"])
    return gs


def _tile(g) -> np.ndarray:
    with Image.open(g["image_path"]) as im:
        return np.asarray(im)


# ── rendering into the pool ─────────────────────────────────────────────

def test_grey_render_queues_pending_font_glyphs_sized_like_real_ones():
    n = api.render_font_glyphs("hud", "9", 20, seed=1)
    assert n == 20
    gs = api.list_glyphs("hud", source="font")
    assert len(gs) == 20
    assert {g["status"] for g in gs} == {"pending"}
    assert {g["char"] for g in gs} == {"9"}
    assert {g["source"] for g in gs} == {"font"} and {g["capture_id"] for g in gs} == {""}
    assert api.list_glyphs("hud", source="capture") == []
    for g in gs:
        t = _tile(g)
        assert t.shape == (28, 28) and t.dtype == np.uint8       # grey kinds: 28x28 L
        assert (t[0] == 255).all() and (t[:, 0] == 255).all()     # the extractor's white pad
        assert Path(g["image_path"]).is_relative_to(api.dev_root() / "glyphs")
    fonts = collections.Counter(g["font"] for g in gs)
    assert fonts["furore.otf"] == 17                             # 85% of 20
    # stats split by source, totals unchanged in meaning
    st = api.glyph_stats("hud")["9"]
    assert st["pending"] == 20 and st["font"]["pending"] == 20 and st["capture"]["pending"] == 0


# ── which font, per region family ───────────────────────────────────────

def test_hud_renders_in_furore_and_the_scanner_has_no_matching_font_by_default():
    hud = api.region_font("hud_rgb")
    assert hud["family"] == "hud" and hud["font"] == "furore"
    sig = api.region_font("signal")
    assert sig["font"] is None and "No matching font bundled" in sig["reason"]
    for kind in ("signal", "signal_inv", "signal_rgb", "signal_rgb_inv"):
        with pytest.raises(fontglyphs.NoMatchingFontError, match="No matching font bundled"):
            api.render_font_glyphs(kind, "9", 5)
        assert api.list_glyphs(kind) == []


def test_region_font_is_a_persisted_setting():
    out = api.set_region_font("signal", "quantico", lookalike_share=0.0)
    assert out["font"] == "quantico" and out["lookalike_share"] == 0.0
    assert api.region_font("signal_rgb")["font"] == "quantico"
    assert api.region_font("hud")["font"] == "furore"            # the other family is untouched
    assert api.render_font_glyphs("signal", "9", 6, seed=11) == 6
    assert {g["font"] for g in api.list_glyphs("signal", source="font")} == {"quantico.ttf"}
    # shared pool: signal_inv sees the same glyphs
    assert len(api.list_glyphs("signal_inv", char="9", source="font")) == 6
    api.set_region_font("signal", None)
    with pytest.raises(fontglyphs.NoMatchingFontError):
        api.render_font_glyphs("signal", "9", 1)
    with pytest.raises(ValueError):
        api.set_region_font("signal", "comic-sans")
    with pytest.raises(ValueError):
        api.set_region_font("scanner", "furore")
    with pytest.raises(ValueError):
        api.set_region_font("hud", "furore", lookalike_share=0.9)


def test_hud_render_and_decimal_point():
    assert api.render_font_glyphs("hud", ".", 6, seed=2) == 6
    assert api.render_font_glyphs("hud", "%", 4, seed=3) == 4
    tiles = [_tile(g) for g in api.list_glyphs("hud", source="font")]
    assert len(tiles) == 10 and all(t.shape == (28, 28) for t in tiles)


def test_font_plan_is_mostly_the_family_font_and_exact():
    plan = fontglyphs.font_plan(100, "furore")
    c = collections.Counter(plan)
    assert c["furore"] == 85 and c["orbitron"] == c["quantico"] == c["jura"] == 5
    assert fontglyphs.font_plan(40, "furore", lookalike_share=0.0) == ["furore"] * 40
    only_furore = {"furore": Path("furore.otf")}
    assert fontglyphs.font_plan(10, "furore", only_furore) == ["furore"] * 10
    with pytest.raises(fontglyphs.FontRenderError):
        fontglyphs.font_plan(10, "furore", {"jura": Path("jura.ttf")})


@pytest.mark.parametrize("char,count", [("A", 5), ("@", 5), ("12", 5), ("9", 0), ("9", 2001),
                                        ("9", True), (",", 5)])
def test_bad_requests_are_refused(char, count):
    with pytest.raises(ValueError):
        api.render_font_glyphs("signal", char, count)
    assert api.list_glyphs("signal") == []


@pytest.mark.parametrize("kind", ["signal_rgb", "signal_rgb_inv", "hud_rgb"])
def test_colour_kinds_refuse_without_approved_real_glyphs(kind):
    api.set_region_font("signal", "furore")        # so the scanner kinds get past the font check
    with pytest.raises(fontglyphs.NeedRealGlyphsError) as ei:
        api.render_font_glyphs(kind, "3", 5)
    assert "approved REAL glyphs" in str(ei.value)
    assert api.list_glyphs(kind) == []


def test_pending_real_glyphs_do_not_count_as_colour_source():
    fam = "hud"
    lab = _train_labels(fam, 1)[0]
    cid = api.add_capture(render_value(lab), "hud_rgb", {}, {})
    api.confirm(cid, lab)
    api.extract_glyphs("hud_rgb")                                 # pending, not approved
    with pytest.raises(fontglyphs.NeedRealGlyphsError):
        api.render_font_glyphs("hud_rgb", "3", 5)


def test_colour_render_takes_the_real_glyphs_colours():
    fg, bg = (40, 230, 120), (12, 20, 30)
    real = _real_glyphs("hud_rgb", fg=fg, bg=bg)
    looks = fontglyphs.colour_looks("hud_rgb")
    assert looks and all(lk["fg"][1] > lk["fg"][0] and lk["fg"][1] > lk["fg"][2] for lk in looks)
    assert api.render_font_glyphs("hud_rgb", "9", 12, seed=5) == 12
    font = api.list_glyphs("hud_rgb", source="font")
    fgs = []
    for g in font:
        t = _tile(g)
        assert t.shape == (28, 28, 3)
        lk = fontglyphs._look(t)
        assert lk is not None
        r, gg, b = lk["fg"]
        assert gg > r and gg > b, lk                             # green ink, like the real ones
        assert sum(lk["bg"]) < sum(lk["fg"])                     # same (upright) polarity
        fgs.append(lk["fg"])
    real_fg = np.mean([lk["fg"] for lk in looks], axis=0)
    assert np.abs(np.mean(fgs, axis=0) - real_fg).max() < 50, (np.mean(fgs, axis=0), real_fg)
    real_t = np.stack([_tile(g) for g in real]).astype(float)
    font_t = np.stack([_tile(g) for g in font]).astype(float)
    # overall colour of the tiles is in the same neighbourhood
    assert np.abs(real_t.mean(axis=(0, 1, 2)) - font_t.mean(axis=(0, 1, 2))).max() < 45


def test_inverted_kind_shares_the_pool_and_inverts_at_load_like_real_tiles():
    api.set_region_font("signal", "furore")        # about the pool, not the font choice
    _real_glyphs("signal_rgb")
    assert kinds.pool("signal_rgb_inv") == kinds.pool("signal_rgb")
    assert api.render_font_glyphs("signal_rgb_inv", "7", 4, seed=6) == 4
    font = api.list_glyphs("signal_rgb", char="7", source="font")
    assert len(font) == 4                                        # stored in the shared pool
    real = api.list_glyphs("signal_rgb", source="capture")
    for g in (font[0], real[0]):
        t = _tile(g)
        up = segment.to_input([t], "signal_rgb")
        inv = segment.to_input([t], "signal_rgb_inv")
        assert np.allclose(inv, 1.0 - up)                        # one inversion, at load


# ── training and synth ──────────────────────────────────────────────────

def test_approved_font_glyphs_train_and_pending_ones_do_not():
    api.render_font_glyphs("hud", "9", 5, seed=7)
    samples, counts, caps = train.build_dataset("hud")
    assert counts["9"] == 0
    for g in api.list_glyphs("hud", source="font")[:3]:
        api.approve_glyph(g["id"])
    samples, counts, caps = train.build_dataset("hud")
    assert counts["9"] == 3 and caps == []                       # font glyphs claim no capture
    assert train.source_counts("hud")["font"] == 3


def test_synth_seeds_real_first_then_font_then_stock():
    real = _real_glyphs("hud")
    real_chars = {g["char"] for g in real}
    missing = next(c for c in "0123456789" if c not in real_chars)
    api.render_font_glyphs("hud", missing, 8, seed=8)
    for g in api.list_glyphs("hud", source="font"):
        api.approve_glyph(g["id"])
    some_real = next(iter(sorted(real_chars)))
    api.render_font_glyphs("hud", some_real, 6, seed=9)
    for g in api.list_glyphs("hud", char=some_real, source="font"):
        api.approve_glyph(g["id"])

    seeds = api.synth_seeds("hud")
    assert seeds[missing] == {"real": 0, "font": 8, "stock": 0}
    n_real = sum(1 for g in real if g["char"] == some_real)
    assert seeds[some_real] == {"real": n_real, "font": 6, "stock": 0}
    nobody = next(c for c in "0123456789" if c not in real_chars and c != missing)
    assert seeds[nobody]["real"] == seeds[nobody]["font"] == 0 and seeds[nobody]["stock"] >= 1

    res = api.generate_synth("hud", 20)
    assert res[missing] == 20 - 8                                # font glyphs count toward target
    assert res[some_real] == 20 - n_real - 6
    assert res[nobody] == 20                                     # stock fallback still works


def test_seed_picker_prefers_real_seeds():
    import random
    real, font = ["r1", "r2"], [f"f{i}" for i in range(8)]
    pick = synth._seed_picker(real, font, random.Random(0))
    draws = [pick() for _ in range(400)]
    share = sum(d.startswith("r") for d in draws) / len(draws)
    assert 0.42 < share < 0.6                                    # >= REAL_SEED_MIN_SHARE (0.5), not the 20% raw share
    pick = synth._seed_picker(real, [], random.Random(0))
    assert {pick() for _ in range(10)} == {"r1", "r2"}
    pick = synth._seed_picker([], font, random.Random(0))
    assert all(pick().startswith("f") for _ in range(10))


# ── the hard guarantee: font glyphs never reach the benchmark ───────────

def _setup_heldout_and_font(kind="hud"):
    fam = kinds.family(kind)
    held = _heldout_labels(fam, 3)
    for i, lab in enumerate(held):
        cid = api.add_capture(render_value(lab, jitter=i), kind, {}, {})
        api.confirm(cid, lab)
    for ch in "0123456789":
        api.render_font_glyphs(kind, ch, 3, seed=ord(ch))
    for g in api.list_glyphs(kind, source="font"):
        api.approve_glyph(g["id"])
    return held


def test_font_glyphs_never_enter_the_heldout_set_or_the_benchmark(monkeypatch):
    held = _setup_heldout_and_font("hud")
    font = api.list_glyphs("hud", source="font")
    font_ids = {g["id"] for g in font}
    font_files = {str(Path(g["image_path"]).resolve()) for g in font}
    assert len(font) == 30

    cases = bench.heldout_captures("hud")
    assert sorted(c["label"] for c in cases) == sorted(held)
    assert not ({c["id"] for c in cases} & font_ids)
    cap_root = (api.dev_root() / "captures").resolve()
    for c in cases:
        p = Path(c["image_path"]).resolve()
        assert p.is_relative_to(cap_root) and str(p) not in font_files
        assert c["status"] == "confirmed" and c["split"] == "heldout"

    # Score the stock model and watch every image the benchmark cuts.
    stock = kinds.stock_model_path("hud")
    if not stock.is_file():
        pytest.skip("stock signal model not present in this checkout")
    seen = []
    real_cut = segment.glyphs_for

    def spy(img, kind, label):
        seen.append(label)
        return real_cut(img, kind, label)
    monkeypatch.setattr(segment, "glyphs_for", spy)
    opened = []
    real_open = Image.open

    def open_spy(fp, *a, **k):
        opened.append(str(Path(fp).resolve()))
        return real_open(fp, *a, **k)
    monkeypatch.setattr(bench.Image, "open", open_spy)
    res = api.benchmark("hud")
    assert res["n"] == len(held)
    assert sorted(seen) == sorted(held)
    assert opened and all(Path(p).is_relative_to(cap_root) for p in opened)
    assert not (set(opened) & font_files)


def test_benchmark_refuses_anything_that_is_not_a_heldout_capture(monkeypatch):
    _setup_heldout_and_font("hud")
    g = api.list_glyphs("hud", source="font")[0]
    smuggled = {"id": g["id"], "image_path": g["image_path"], "label": g["char"],
                "status": "confirmed", "split": "heldout"}
    stock = kinds.stock_model_path("hud")
    if not stock.is_file():
        pytest.skip("stock signal model not present in this checkout")
    with pytest.raises(bench.BenchmarkError, match="not a capture file"):
        bench.benchmark("hud", captures=[smuggled])
    # A real capture that is on the TRAIN side is refused too.
    lab = _train_labels("hud", 1)[0]
    cid = api.add_capture(render_value(lab), "hud", {}, {})
    api.confirm(cid, lab)
    tr = next(c for c in labels.confirmed_rows("hud", "train") if c["id"] == cid)
    with pytest.raises(bench.BenchmarkError, match="held-out"):
        bench.benchmark("hud", captures=[tr])


def test_benchmark_code_has_no_route_to_the_glyph_table():
    src = inspect.getsource(bench)
    assert "FROM glyphs" not in src and "list_glyphs" not in src
    assert not hasattr(bench, "glyphs") and not hasattr(bench, "fontglyphs")
    held_src = inspect.getsource(bench.heldout_captures)
    assert "confirmed_rows" in held_src                           # captures table only


def test_font_glyph_forged_onto_a_heldout_capture_is_refused():
    held = _setup_heldout_and_font("hud")
    hid = next(c["id"] for c in labels.confirmed_rows("hud", "heldout"))
    g = api.list_glyphs("hud", source="font")[0]
    with db.LOCK, db.connect() as con:
        con.execute("UPDATE glyphs SET capture_id=? WHERE id=?", (hid, g["id"]))
    with pytest.raises(glyphs.GlyphIntegrityError):
        glyphs.approved_font_glyphs("hud")
    with pytest.raises(glyphs.GlyphIntegrityError):
        train.build_dataset("hud")
    assert held


def test_export_never_ships_font_glyphs():
    _setup_heldout_and_font("hud")
    rows = api.export_preview()
    font_names = {Path(g["image_path"]).name for g in api.list_glyphs("hud", source="font")}
    assert not ({Path(r["file"]).name for r in rows} & font_names)


def test_old_database_gains_the_source_column(tmp_path, monkeypatch):
    import sqlite3
    root = tmp_path / "old"
    root.mkdir()
    con = sqlite3.connect(str(root / "devmode.sqlite3"))
    con.executescript(
        "CREATE TABLE glyphs (id TEXT PRIMARY KEY, pool TEXT NOT NULL, capture_id TEXT NOT NULL,"
        " char TEXT NOT NULL, pos INTEGER NOT NULL, file TEXT NOT NULL, status TEXT NOT NULL,"
        " created REAL NOT NULL);"
        "INSERT INTO glyphs VALUES ('g1','training_data_user_sig','c1','5',0,'x.png','approved',0);")
    con.commit()
    con.close()
    monkeypatch.setenv("SC_DEVMODE_ROOT", str(root))
    rows = api.list_glyphs("signal")
    assert rows and rows[0]["source"] == "capture" and rows[0]["font"] is None


def test_missing_font_file_is_a_reason_not_a_crash(monkeypatch):
    monkeypatch.setattr(fontglyphs, "available_fonts", lambda: {})
    rf = api.region_font("hud")
    assert rf["font"] is None and "missing" in rf["reason"]
    with pytest.raises(fontglyphs.NoMatchingFontError, match="missing"):
        api.render_font_glyphs("hud", "9", 3)

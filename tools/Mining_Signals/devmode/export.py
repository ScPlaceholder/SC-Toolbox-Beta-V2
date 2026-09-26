"""Failure zip for J: the user's hard cases, human-labelled.

Contents (and nothing else):
  crops/<family>/<capture_id>.png   confirmed captures the pipeline got wrong
  labels.jsonl                      one line per crop: human label + what the engines said
  settings.json                     game resolution, HUD colour, toolbox version,
                                    active/stock model hashes, counts

Only CONFIRMED captures are exported (their label is human truth), and only
when they are failures:
  misread    the live reader returned a value and it was wrong
  no_read    the live reader returned nothing
  corrected  the reader was right or unknown, but the proposal or an engine
             disagreed and the human corrected it
Crops only: anything screen-sized is skipped. No absolute paths, no
usernames: meta is reduced to scalar fields whose names are not path-like.

export_preview() and export_zip() are built from the same plan, so the
preview lists exactly the zip's entries (metadata rows carry reason
"metadata").
"""
from __future__ import annotations

import json
import logging
import time
import zipfile
from collections import Counter
from pathlib import Path
from typing import Optional

from . import activate, captures, db, kinds, labels, paths, train

log = logging.getLogger(__name__)

_META_DROP = ("path", "user", "name", "dir", "file", "host")


def _reason(cap: dict) -> Optional[str]:
    label = cap["label"]
    meta = cap["meta"] or {}
    fam = cap["family"]
    eng = [v for v in (kinds.normalize_value(fam, x) for x in (cap["engines"] or {}).values()) if v]
    reader_known, reader = False, None
    if cap["proposal_source"] == "import" or meta.get("imported"):
        reader_known, reader = True, cap["proposed"]
    elif "reader" in meta:
        reader_known, reader = True, kinds.normalize_value(fam, meta.get("reader"))
    if reader_known:
        if reader is None:
            return "no_read"
        if reader != label:
            return "misread"
    elif not eng and cap["proposed"] is None:
        return "no_read"
    if (cap["proposed"] and cap["proposed"] != label) or any(v != label for v in eng):
        return "corrected"
    return None


def _clean_meta(meta: dict) -> dict:
    out = {}
    for k, v in (meta or {}).items():
        if any(tok in str(k).lower() for tok in _META_DROP):
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[str(k)] = v
    return out


def _toolbox_version() -> Optional[str]:
    pp = paths.REPO_ROOT / "pyproject.toml"
    try:
        import tomllib  # noqa: WPS433
        return tomllib.loads(pp.read_text(encoding="utf-8")).get("project", {}).get("version")
    except (OSError, ValueError, ImportError) as exc:
        log.warning("devmode: toolbox version unknown: %s", exc)
        return None


def _model_hashes() -> dict:
    out = {}
    for k in kinds.KINDS:
        stock = kinds.stock_model_path(k)
        act = activate.active_model_path(k)
        out[k] = {
            "active": act is not None,
            "active_sha256": train.file_sha256(Path(act)) if act else None,
            "stock_sha256": train.file_sha256(stock) if stock.is_file() else None,
        }
    return out


def _plan() -> tuple[list[dict], bytes, bytes]:
    rows: list[dict] = []
    for fam in ("signal", "hud"):
        rows.extend(labels.confirmed_rows(fam, "all"))
    items, lines = [], []
    res, colours = Counter(), Counter()
    for cap in rows:
        reason = _reason(cap)
        if reason is None:
            continue
        if captures.looks_like_screenshot(cap["width"], cap["height"]):
            log.warning("devmode: export skipped screen-sized capture %s", cap["id"])
            continue
        src = Path(cap["image_path"])
        if not src.is_file():
            continue
        arc = f"crops/{cap['family']}/{cap['id']}.png"
        items.append({"file": arc, "kind": cap["kind"], "reason": reason, "size": src.stat().st_size,
                      "capture_id": cap["id"], "label": cap["label"], "_src": str(src)})
        meta = _clean_meta(cap["meta"])
        lines.append(json.dumps({
            "file": arc, "capture_id": cap["id"], "kind": cap["kind"], "family": cap["family"],
            "label": cap["label"], "reason": reason, "proposed": cap["proposed"],
            "proposal_source": cap["proposal_source"], "engines": cap["engines"],
            "width": cap["width"], "height": cap["height"], "meta": meta,
        }, ensure_ascii=False))
        if meta.get("resolution"):
            res[str(meta["resolution"])] += 1
        col = meta.get("hud_colour") or meta.get("hud_color")
        if col:
            colours[str(col)] += 1
    st = db.read_state()
    settings = {
        "format": "mining_signals_devmode_failures/1",
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "game_resolution": st.get("game_resolution") or (res.most_common(1)[0][0] if res else None),
        "hud_colour": st.get("hud_colour") or (colours.most_common(1)[0][0] if colours else None),
        "toolbox_version": _toolbox_version(),
        "models": _model_hashes(),
        "counts": {
            "exported": len(items),
            "by_reason": dict(Counter(i["reason"] for i in items)),
            "labels": {fam: labels.label_stats(fam) for fam in ("signal", "hud")},
        },
    }
    labels_bytes = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    settings_bytes = json.dumps(settings, indent=2).encode("utf-8")
    return items, labels_bytes, settings_bytes


def export_preview() -> list[dict]:
    items, lb, sb = _plan()
    out = [{k: v for k, v in i.items() if not k.startswith("_")} for i in items]
    out.append({"file": "labels.jsonl", "kind": None, "reason": "metadata", "size": len(lb)})
    out.append({"file": "settings.json", "kind": None, "reason": "metadata", "size": len(sb)})
    return out


def export_zip(dest_dir: str) -> str:
    items, lb, sb = _plan()
    d = Path(dest_dir)
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"mining_signals_failures_{time.strftime('%Y%m%d_%H%M%S')}.zip"
    tmp = dest.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for i in items:
            zf.write(i["_src"], i["file"])
        zf.writestr("labels.jsonl", lb)
        zf.writestr("settings.json", sb)
    tmp.replace(dest)
    return str(dest)


def set_game_info(resolution: Optional[str] = None, hud_colour: Optional[str] = None) -> None:
    changes = {}
    if resolution is not None:
        changes["game_resolution"] = resolution
    if hud_colour is not None:
        changes["hud_colour"] = hud_colour
    if changes:
        db.write_state(**changes)

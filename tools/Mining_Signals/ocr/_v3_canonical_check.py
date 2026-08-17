"""End-to-end no-regression test for canonical signature captures.

Runs `_signal_recognize_pil` on the two canonical full-screen captures
that v2 was known to read correctly (16,960 and 11,520) and compares
the result before/after swapping v3 in for v2.

Strategy: the production code paths read the v2 ONNX off disk by
filename. We:
  1. Read v2 result with the existing v2 file in place.
  2. Back up v2 to a side path, copy v3 -> v2, reload, re-read.
  3. If the swap is reverted (caller's choice), put v2 back.

This script is non-destructive in the failure case — it always
restores v2 unless --keep-swap is passed.
"""
from __future__ import annotations

import argparse
import importlib
import shutil
import sys
from pathlib import Path

PROD = Path(r"C:\Users\_user\AppData\Local\SC_Toolbox\current\tools\Mining_Signals")
WINGMAN = Path(
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI\custom_skills"
    r"\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
)

V2_ONNX = PROD / "ocr" / "models" / "model_signal_rgb_cnn_v2.onnx"
V2_DATA = PROD / "ocr" / "models" / "model_signal_rgb_cnn_v2.onnx.data"
V2_JSON = PROD / "ocr" / "models" / "model_signal_rgb_cnn_v2.json"
V2_BACKUP = PROD / "ocr" / "models" / "model_signal_rgb_cnn_v2_pre_panel_aug.onnx"
V2_BACKUP_DATA = PROD / "ocr" / "models" / "model_signal_rgb_cnn_v2_pre_panel_aug.onnx.data"
V2_BACKUP_JSON = PROD / "ocr" / "models" / "model_signal_rgb_cnn_v2_pre_panel_aug.json"

V3_ONNX = PROD / "ocr" / "models" / "model_signal_rgb_cnn_v3.onnx"
V3_DATA = PROD / "ocr" / "models" / "model_signal_rgb_cnn_v3.onnx.data"
V3_JSON = PROD / "ocr" / "models" / "model_signal_rgb_cnn_v3.json"

CAP1 = WINGMAN / "training_data_panels" / "user_20260418_154408" / "region2" / "cap_20260418_160452_795.png"
CAP2 = WINGMAN / "training_data_panels" / "user_20260418_154408" / "region2" / "cap_20260418_155500_306.png"

CAP1_GT = 16960
CAP2_GT = 11520


def add_paths():
    sys.path.insert(0, str(PROD))
    sys.path.insert(0, str(PROD / "ocr"))
    sys.path.insert(0, str(PROD / "ocr" / "sc_ocr"))


def force_reload_api():
    """Reload sc_ocr.api so any cached ONNX session is dropped."""
    for mod in list(sys.modules):
        if mod.startswith("sc_ocr") or mod.startswith("ocr.sc_ocr") or mod.startswith("ocr"):
            sys.modules.pop(mod, None)


def recognize(cap_path: Path) -> tuple[int | None, str]:
    """Run end-to-end signature recognition on a capture file."""
    force_reload_api()
    add_paths()
    try:
        from sc_ocr import api as sc_api  # type: ignore
    except Exception:
        try:
            import ocr.sc_ocr.api as sc_api  # type: ignore
        except Exception as e:
            return None, f"import failed: {e}"

    from PIL import Image
    img = Image.open(cap_path).convert("RGB")
    fn = getattr(sc_api, "_signal_recognize_pil", None)
    if fn is None:
        return None, "no _signal_recognize_pil"
    try:
        # Pass region=None — caller has already provided the cropped
        # signature panel image (these captures ARE the panel-region
        # crops, not full-screen frames).
        val = fn(img, region=None)
        return val, "ok"
    except Exception as e:
        import traceback
        return None, f"exception: {e}\n{traceback.format_exc()}"


def swap_v3_in():
    if V2_ONNX.exists() and not V2_BACKUP.exists():
        shutil.copy2(V2_ONNX, V2_BACKUP)
    if V2_DATA.exists() and not V2_BACKUP_DATA.exists():
        shutil.copy2(V2_DATA, V2_BACKUP_DATA)
    if V2_JSON.exists() and not V2_BACKUP_JSON.exists():
        shutil.copy2(V2_JSON, V2_BACKUP_JSON)
    if V3_ONNX.exists():
        shutil.copy2(V3_ONNX, V2_ONNX)
    # external-data file (only present when ONNX was exported with .data sidecar)
    if V3_DATA.exists():
        shutil.copy2(V3_DATA, V2_DATA)
    elif V2_DATA.exists():
        # v3 has no sidecar; v2 had one — remove the stale one so the
        # new v2 ONNX uses inline weights.
        V2_DATA.unlink()
    if V3_JSON.exists():
        shutil.copy2(V3_JSON, V2_JSON)


def restore_v2():
    if V2_BACKUP.exists():
        shutil.copy2(V2_BACKUP, V2_ONNX)
    if V2_BACKUP_DATA.exists():
        shutil.copy2(V2_BACKUP_DATA, V2_DATA)
    elif V2_DATA.exists() and not V2_BACKUP_DATA.exists():
        # backup never had a sidecar; remove any leftover v3 sidecar
        V2_DATA.unlink()
    if V2_BACKUP_JSON.exists():
        shutil.copy2(V2_BACKUP_JSON, V2_JSON)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--keep-swap", action="store_true",
                   help="leave v3 in place after the test")
    args = p.parse_args()

    if not V3_ONNX.exists():
        print(f"FATAL: {V3_ONNX} missing")
        return 1

    print("=== BEFORE swap (current v2 in production) ===")
    cap1_v2, msg1 = recognize(CAP1)
    cap2_v2, msg2 = recognize(CAP2)
    print(f"  CAP1 ({CAP1.name}, GT={CAP1_GT}): result={cap1_v2!r} ({msg1})")
    print(f"  CAP2 ({CAP2.name}, GT={CAP2_GT}): result={cap2_v2!r} ({msg2})")

    print("\nSwapping v3 -> v2 path (with backup)...")
    swap_v3_in()
    print(f"  backup at: {V2_BACKUP}")

    try:
        print("\n=== AFTER swap (v3 model in production v2 path) ===")
        cap1_v3, msg1 = recognize(CAP1)
        cap2_v3, msg2 = recognize(CAP2)
        print(f"  CAP1 ({CAP1.name}, GT={CAP1_GT}): result={cap1_v3!r} ({msg1})")
        print(f"  CAP2 ({CAP2.name}, GT={CAP2_GT}): result={cap2_v3!r} ({msg2})")
    finally:
        if not args.keep_swap:
            print("\nRestoring v2...")
            restore_v2()
        else:
            print("\n--keep-swap: v3 remains in production v2 path")

    cap1_pre_ok  = (cap1_v2 == CAP1_GT)
    cap1_post_ok = (cap1_v3 == CAP1_GT)
    cap2_pre_ok  = (cap2_v2 == CAP2_GT)
    cap2_post_ok = (cap2_v3 == CAP2_GT)

    print("\n=== verdict ===")
    print(f"  CAP1 GT={CAP1_GT}: pre-swap={'OK' if cap1_pre_ok else 'FAIL'} ({cap1_v2}), post-swap={'OK' if cap1_post_ok else 'FAIL'} ({cap1_v3})")
    print(f"  CAP2 GT={CAP2_GT}: pre-swap={'OK' if cap2_pre_ok else 'FAIL'} ({cap2_v2}), post-swap={'OK' if cap2_post_ok else 'FAIL'} ({cap2_v3})")

    return 0 if (cap1_post_ok and cap2_post_ok) else 2


if __name__ == "__main__":
    sys.exit(main())

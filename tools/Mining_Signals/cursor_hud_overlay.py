"""Cursor-tracking overlay box sized like the mining HUD crop.

A frameless, always-on-top, click-through window draws a red rectangle
outline that follows the mouse cursor in real time. Useful for eyeballing
where the SCAN RESULTS panel should sit in a capture region.

Usage:
    python cursor_hud_overlay.py
    python cursor_hud_overlay.py --w 500 --h 260
    python cursor_hud_overlay.py --anchor top-left
    python cursor_hud_overlay.py --w 380 --h 200 --border 2

Press ESC to quit. Ctrl+C in the launching terminal also works.

Notes:
  * The window is click-through (WS_EX_TRANSPARENT), so it does NOT
    intercept mouse clicks — the game underneath stays interactive.
  * Default size 400x220 matches the typical mining HUD crop derived
    from ui/calibration_dialog.py defaults (mineral row x=20, mass/
    resistance/instability extending to x=340, rows ending at y=178).
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import tkinter as tk
from ctypes import wintypes


# ── Win32 plumbing ──────────────────────────────────────────────────────────
user32 = ctypes.windll.user32


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


def get_cursor_pos() -> tuple[int, int]:
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


VK_ESCAPE = 0x1B


def escape_pressed() -> bool:
    # High bit (0x8000) set => key is currently down this poll.
    return bool(user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)


# Extended window styles for click-through, always-on-top, no-activate.
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000


def make_click_through(hwnd: int) -> None:
    """Mark the window as layered + transparent so clicks pass through."""
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style |= (
        WS_EX_LAYERED
        | WS_EX_TRANSPARENT
        | WS_EX_TOOLWINDOW
        | WS_EX_NOACTIVATE
    )
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


# ── Overlay ─────────────────────────────────────────────────────────────────
def run(width: int, height: int, anchor: str, border: int) -> None:
    root = tk.Tk()
    root.overrideredirect(True)
    root.wm_attributes("-topmost", True)

    # Use a key color for the background and mark it transparent. The
    # red outline rectangle and label text are NOT this color, so they
    # remain visible while the interior of the box is see-through.
    KEY = "magenta"
    root.configure(bg=KEY)
    try:
        root.wm_attributes("-transparentcolor", KEY)
    except tk.TclError:
        # Non-Windows fallback: window will be opaque magenta. Still
        # functional; just not pretty.
        pass

    cvs = tk.Canvas(
        root,
        width=width,
        height=height,
        bg=KEY,
        highlightthickness=0,
        bd=0,
    )
    cvs.pack()

    # Red outline rectangle, transparent fill.
    half = max(1, border // 2)
    cvs.create_rectangle(
        half,
        half,
        width - half,
        height - half,
        outline="red",
        width=border,
        fill="",
    )

    # Live coordinate label in the top-left of the box.
    label = cvs.create_text(
        border + 6,
        border + 4,
        anchor="nw",
        text="",
        fill="red",
        font=("Consolas", 10, "bold"),
    )

    # Realise the window so winfo_id() returns a valid HWND.
    root.update_idletasks()
    hwnd = root.winfo_id()
    try:
        make_click_through(hwnd)
    except Exception as exc:
        print(f"warning: could not set click-through: {exc}", file=sys.stderr)

    def offset_for(cx: int, cy: int) -> tuple[int, int]:
        """Translate cursor (cx, cy) into window top-left (wx, wy)."""
        if anchor == "top-left":
            return cx, cy
        if anchor == "top-right":
            return cx - width, cy
        if anchor == "bottom-left":
            return cx, cy - height
        if anchor == "bottom-right":
            return cx - width, cy - height
        # default = center
        return cx - width // 2, cy - height // 2

    def tick() -> None:
        if escape_pressed():
            root.destroy()
            return
        cx, cy = get_cursor_pos()
        wx, wy = offset_for(cx, cy)
        root.geometry(f"{width}x{height}+{wx}+{wy}")
        cvs.itemconfigure(
            label,
            text=f"x={wx} y={wy}  {width}×{height}",
        )
        root.after(16, tick)  # ~60 Hz

    root.after(0, tick)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass


def main() -> None:
    p = argparse.ArgumentParser(
        description="Cursor-tracking red-box overlay sized for the mining HUD.",
    )
    p.add_argument(
        "--w",
        type=int,
        default=400,
        help="Box width in pixels (default: 400)",
    )
    p.add_argument(
        "--h",
        type=int,
        default=220,
        help="Box height in pixels (default: 220)",
    )
    p.add_argument(
        "--anchor",
        choices=[
            "center",
            "top-left",
            "top-right",
            "bottom-left",
            "bottom-right",
        ],
        default="center",
        help=(
            "Where the cursor sits inside the box (default: center). "
            "Use 'top-left' if you're aiming a capture region's origin."
        ),
    )
    p.add_argument(
        "--border",
        type=int,
        default=3,
        help="Outline thickness in pixels (default: 3)",
    )
    args = p.parse_args()
    run(args.w, args.h, args.anchor, args.border)


if __name__ == "__main__":
    main()

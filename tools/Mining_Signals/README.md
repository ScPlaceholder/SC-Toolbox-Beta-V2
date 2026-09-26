# Mining Signals — Setup & Region Guide

Mining Signals reads your Star Citizen mining HUD live and shows you the rock's
**signature → mineral matches** and **breakability recommendations**. Before it
can read anything, you point it at two on-screen panels **once**. This guide
covers that — especially how the region selectors now work.

> Looking for how the OCR engine itself works? See `MINING_SIGNALS_WRITEUP.md`.

---

## Quick start

1. Have Star Citizen running and be **actively scanning a rock** so the panels are on screen.
2. Click **Set Scanning Region** and frame the signature pill (see below).
3. Click **Set Mining HUD Region** and frame the SCAN RESULTS panel (see below).
4. Click **Start Scan**.

---

## The two regions you set

You set **two** capture regions, each with its own button:

| Button | Panel to frame | What MUST be inside the box |
|---|---|---|
| **Set Scanning Region** | The **signature scanner** (the radial-menu pill) | The **location-pin icon** *and* the **4–5 digit signal value** — e.g. `📍 11,565` |
| **Set Mining HUD Region** | The **SCAN RESULTS** panel | The **SCAN RESULTS** title, the **mineral name**, and the **MASS**, **RESISTANCE**, and **INSTABILITY** rows |

### ⚠️ Mining HUD region: stop before COMPOSITION
Draw the box from just **above** the **SCAN RESULTS** title down to just **below**
the **INSTABILITY** row. **Do NOT include the COMPOSITION section** (the
mineral-percentage breakdown underneath) — its text confuses the reader and drags
the row detection to the wrong place.

---

## What's new: live confirmation while you select

The selector now **shows you whether the box is framed correctly *before* you let
go.** As you click-and-drag, it runs the real finders on the live game underneath
your selection and draws **ghost outlines** on the elements it locates:

- **Amber outlines** = still searching / only some elements found yet.
- **Green outlines** = **every** expected element was found. You're good.
- A **status line** tracks progress and shows the resolution it detected:
  - `Searching for HUD regions — frame the panel…`
  - `Detecting… 2 region(s) found`
  - `✓ All regions detected — release to confirm`
  - `…   ·   Game: 1920×1080 (game.log)` — the resolution it's scaling the finders to.

**Rule of thumb: wait for green, then release the mouse to save.** If it stays
amber, nudge the box so the *whole* panel sits inside it.

---

## ⏳ You may have to wait a moment for the boxes to populate

The ghost outlines do **not** appear the instant you start dragging. This is
normal — give it a beat:

1. **First-time warm-up (~1 second).** The very first time you open a selector it
   builds its templates once. Start your box and pause a second; the outlines
   begin appearing.
2. **The signature pill needs a few frames to confirm.** For the **Set Scanning
   Region** step especially, **hold the box steady over the pill** for a moment —
   the icon detector averages a few frames before it's confident enough to turn
   green. Keep still and let it settle.

So the flow is: **frame the panel → hold steady → wait for the outlines to appear
and turn green → release.**

> You don't need to worry about the dim overlay or the ghost boxes interfering —
> the selection window is invisible to screen capture, so the finders only ever
> see the clean game underneath.

---

## Two things that trip people up

- **The real game panel must be on screen *while* you select.** The selector reads
  the *live* game, not a saved image. If the panel isn't showing, the outlines can
  never turn green because there's nothing to detect.
  - For **Set Scanning Region**, open the signature scanner so the `📍 value` pill is visible.
  - For **Set Mining HUD Region**, be scanning a rock so the **SCAN RESULTS** panel is up.
- **After you save, give the readout a few scan ticks.** Once both regions are set
  and the panels are on screen, the value still needs a few frames of agreement
  before it displays (it will not show a number it isn't confident about). A brief
  **"Scanning… Please Wait"** before the value and mineral matches appear is
  expected, not a bug.

---

## If you change resolution or HUD scale, redo the regions

The capture boxes are saved in **screen pixels**, and the HUD moves and resizes
when you change the game's **resolution** or **HUD scale**. After any such change,
re-run **Set Scanning Region** and **Set Mining HUD Region** — the finders
auto-detect your new resolution (shown in the status line) and will re-confirm
green at the new size.

<p align="center">
  <a href="https://robertsspaceindustries.com/community-hub/post/sc-toolbox-v2-is-released-42-testers-and-counting-aUYFfLH5ecHkh">
    <img src="assets/cig_staff_pick.png" alt="CIG Staff Pick" width="600">
  </a>
</p>

<p align="center">
  <strong>We got featured by CIG!!! Thank you everyone! We wouldn't be here if it wasn't for your testing, feedback and support!!!</strong>
</p>

<p align="center">
  <img src="assets/sc_toolbox_logo.png" alt="SC Toolbox" width="128">
</p>

<h1 align="center">SC Toolbox</h1>

<p align="center">
  A lightweight desktop overlay suite for <strong>Star Citizen</strong>.<br>
  Nine gameplay tools — always on top, one hotkey away, no alt-tab required.
</p>

<p align="center">
  <a href="https://github.com/ScPlaceholder/SC-Toolbox-Beta-V2/releases/latest">
    <img src="https://img.shields.io/github/v/release/ScPlaceholder/SC-Toolbox-Beta-V2?label=Download&style=for-the-badge" alt="Download">
  </a>
  <a href="https://discord.gg/D3hqGU5hNt">
    <img src="https://img.shields.io/badge/Discord-Join-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Discord">
  </a>
</p>

---

## Download & Install

**[Download the latest installer](https://github.com/ScPlaceholder/SC-Toolbox-Beta-V2/releases/latest)** — no Python required, everything is bundled.

1. Download `SC_Toolbox_Setup_X.Y.Z.exe`
2. Run the installer (no admin rights needed — installs per-user)
3. Launch from the desktop shortcut or Start Menu
4. First launch: press **Shift + `** to open the launcher, then click any tile or use its hotkey

---

## What's New in v2.3.1

- **Mining Signals now scans correctly on machines other than the developer's.** This is the fix most likely to matter if the scanner has been unreliable for you. HUD panels were normalised against the wrong reference height (`REF_H` 541, from an older training set) and only ever rescaled *upward*, so live 448x670 captures skipped normalisation entirely. On a different resolution, DPI or HUD scale the panel title outgrew the anchor templates, the pose solver lost lock, and values were read off a bad pose. Now calibrated to the native 670 and normalised in **both** directions. Measured on 22 real labelled panels: 2.0x rescaled went from 0% to 77% correct, native held at 100%, and the off-native band tightened from a ragged 0-86% to a steady 77-91%. Accuracy is now effectively **scale-independent**.
- **Local signature OCR fallback** — the scanner degrades gracefully instead of failing outright when the primary reader is unavailable. (Thanks [@garrett-williams](https://github.com/garrett-williams).)
- **The installer reports the version it is actually installing.** It had been showing `v2.2.16` while installing 2.3.x. The version is now derived from a single source at build time.
- **Installer privacy** — the build no longer embeds the build machine's Windows username, from two independent causes (a staging scrub that had been silently failing to run, and compiler-emitted debug metadata).

**Known limit, stated plainly:** the remaining Mining Signals errors are systematic rather than random. Resampling softens glyph edges and the digit model still misreads a few fragile shapes — 5 vs 6, decimal placement, thin 1s. Frame-to-frame consensus cannot fix an error that is identical in every frame, so accuracy plateaus around 75% on the hardest panels. That is the next thing being worked on.

<details>
<summary>Earlier: What's New in v2.2.8</summary>


- **RGB CNN voter for Mining Signals** — the SC_OCR pipeline now runs an RGB-trained CNN (plus a per-channel-inverted variant) as the primary digit reader, with the original grayscale CNN, CRNN, and Tesseract as fallback voters. Stops losing thin-stroke digits like 9s in low-contrast frames and reads faster on the high-confidence path.
- **Glyph reviewer drag-select + augmentation cascade** — the training-data review tool (`scripts/review_glyphs.py`) now supports bounding-box drag-select for picking multiple glyphs in one sweep, and automatically moves a sample's augmented siblings (`aug_*.png`) when you re-classify the original.
- **Stability fixes** — multi-recipe adaptive binarization for wide signature spans, equal-width splitter when blobs survive binarization, glyph-height-proportional comma masking, and a hysteresis gate on signal-value acceptance to suppress single-frame OCR jitter.
- **Bundled-runtime fixes** — `scipy` now correctly declared in the Mining_Signals runtime requirements (was silently missing). Paddle sidecar pip uses `--only-binary=:all:` to avoid the Rust-toolchain-required `python-bidi` sdist on Python 3.13.

</details>

---

## Tools

| Hotkey | Tool | Description | Data Source |
|--------|------|-------------|------------|
| Shift+1 | **DPS Calculator** | Ship loadout viewer & DPS calculator with power allocator | erkul.games, fleetyards.net |
| Shift+2 | **Cargo Loader** | 3D isometric cargo grid viewer & container optimizer | sc-cargo.space |
| Shift+3 | **Mission Database** | Browse missions, crafting blueprints & mining resources | scmdb.net |
| Shift+4 | **Mining Loadout** | Mining laser, module & gadget optimizer | uexcorp.space |
| Shift+5 | **Market Finder** | Searchable catalog of all purchasable items with buy/sell locations | uexcorp.space |
| Shift+6 | **Trade Hub** | Trade route calculator for single-hop & multi-leg routes | uexcorp.space |
| Shift+7 | **Craft Database** | Crafting recipe browser with material requirements | scmdb.net |
| Shift+8 | **Battle Buddy** | Real-time HUD overlay — tracks kills, deaths, and inventory from game logs | Star Citizen game log |
| — | **Mining Signals** | Live screen overlay reading signal scan %, mass, resistance, and instability from the SCAN RESULTS panel — powered by the new **SC_OCR** engine | Screen capture (SC_OCR + Tesseract) |

Press **Shift + `** to toggle the launcher window.

### SC_OCR — purpose-built for Star Citizen

Mining Signals previously relied on stock Tesseract, which struggled with the SC HUD's sparse digits, anti-aliased glyphs, and varying background luminance. v2.2.6 introduces **SC_OCR**: a CNN-based reader trained on actual in-game captures. Highlights:

- **Adaptive (locally-windowed) thresholding** — handles bright vs dark backgrounds without hand-tuning
- **Position-based row finder with lock cache** — once a row is found, subsequent frames lock onto it for stability and speed
- **Multi-frame averaging** — smooths out transient OCR noise from animated panels
- **Glyph-level confidence** — per-digit classification scores, with an optional live "Glyph Reader" debug view
- **Self-curating training pipeline** — captures, auto-labels (with consensus voting), quarantines contaminated samples, and stages new data for review

You don't need to do anything to use SC_OCR — it ships pre-trained inside Mining Signals.

---

## Screenshots

<p align="center">
  <img src="assets/screenshots/launcher.png" alt="SC Toolbox Launcher" width="320"><br>
  <em>The main launcher — click any tile or use the global hotkey to open a tool</em>
</p>

<p align="center">
  <img src="assets/screenshots/battle_buddy.png" alt="Battle Buddy" width="800"><br>
  <em>Battle Buddy — real-time in-game HUD showing equipped weapons, ammo counts, and consumables parsed live from game logs</em>
</p>

<p align="center">
  <img src="assets/screenshots/mining_signals.png" alt="Mining Signals" width="800"><br>
  <em>Mining Signals — OCR overlay that identifies ore types and signal strengths directly from your ship's scanner</em>
</p>

<p align="center">
  <img src="assets/screenshots/dps_calculator.png" alt="DPS Calculator" width="800"><br>
  <em>DPS Calculator — live ship loadout viewer with weapon DPS, sustained fire, shield, hull, and power data</em>
</p>

<p align="center">
  <img src="assets/screenshots/cargo_loader.png" alt="Cargo Loader" width="800"><br>
  <em>Cargo Loader — 3D isometric cargo grid for the Drake Caterpillar (576 SCU) with auto-optimize and commodity assignment</em>
</p>

<p align="center">
  <img src="assets/screenshots/mission_database.png" alt="Mission Database" width="800"><br>
  <em>Mission Database — searchable browser for 1,381 missions across all systems, factions, and mission types</em>
</p>

<p align="center">
  <img src="assets/screenshots/mining_loadout.png" alt="Mining Loadout" width="800"><br>
  <em>Mining Loadout — configure and compare mining lasers, modules, and gadgets for any ship</em>
</p>

<p align="center">
  <img src="assets/screenshots/market_finder.png" alt="Market Finder" width="800"><br>
  <em>Market Finder — browse 272 ships and items with buy prices, cargo capacity, crew, and all purchase locations</em>
</p>

<p align="center">
  <img src="assets/screenshots/trade_hub.png" alt="Trade Hub" width="800"><br>
  <em>Trade Hub — multi-leg mixed freight route calculator showing 293 routes for a C2 Hercules Starlifter with profits up to 12.6M aUEC per run</em>
</p>

<p align="center">
  <img src="assets/screenshots/craft_database.png" alt="Craft Database" width="800"><br>
  <em>Craft Database — browse 1,040 craftable items with full material requirements and mission unlock conditions</em>
</p>

---

## Features

- **Always-on-top overlay** — stays visible over Star Citizen
- **Global hotkeys** — toggle any tool without alt-tabbing
- **Customizable keybinds** — rebind all hotkeys in Settings
- **Live data** — prices, loadouts, and missions pulled from community APIs
- **Local caching** — fast startup with automatic background refresh
- **WingmanAI integration** — works as a voice-activated WingmanAI skill (optional)

---

## Requirements

- Windows 10 or 11 (64-bit)
- Internet connection (for live game data)

---

## Manual Install (Advanced)

If you prefer to run from source instead of the installer:

1. Install Python 3.10+
2. Run `INSTALL_AND_LAUNCH.bat` (installs dependencies and launches)
3. Or manually: `pip install -r requirements.txt` then `python skill_launcher.py`

---

## Data Sources & Credits

- [erkul.games](https://erkul.games) — DPS calculator data, weapon stats ([Patreon](https://patreon.com/erkul))
- [uexcorp.space](https://uexcorp.space) — Market prices, trade routes, ship data, mining equipment
- [scmdb.net](https://scmdb.net) — Mission database, crafting blueprints, mining resources
- [fleetyards.net](https://fleetyards.net) — Ship hardpoint data
- [sc-cargo.space](https://sc-cargo.space) — Cargo grid layouts

---

## Community

- [Discord](https://discord.gg/D3hqGU5hNt) — Bug reports, feedback, and discussion

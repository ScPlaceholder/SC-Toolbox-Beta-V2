# Mining Signals — what we built

## The problem

Star Citizen's mining loop hides three numbers behind a particle storm. When you scan a rock, the game flashes a panel with **MASS**, **RESISTANCE**, and **INSTABILITY** — three values you need to choose the right laser power, the right gadgets, and the right crew layout. Nailing those choices is the difference between a clean break and a cratered rock with no payout. There's a separate **signature scanner** on the radial menu that shows a 4–5 digit signal value for every rock in range; that number maps deterministically (via Mort13's community spreadsheet) to a list of candidate minerals.

Both panels live on a **fullscreen-borderless game HUD**. We can't ask the game what the values are. We have to read them off the screen, in real time, while the player is moving, while the rock is flickering, while sunlight or asteroid glare changes what "background" means frame to frame, while the SC font's 5-vs-6 ambiguity makes single OCR engines flip between adjacent values, while the displayed value changes when the player aims at a new rock — and we have to do all of this without ever showing a wrong number, without flickering the overlay, and without pegging a CPU core that the game wants for itself. Tick budget: 500 ms, with a 1 Hz default scan rate.

That constraint is what shaped everything below.

## What ships

The tool runs as a Qt-based companion app (`ui/app.py`, 5,771 lines) that watches the screen at ~2 Hz while the player mines. It scans **two completely different in-game panels in parallel** and produces **two overlays**:

- **Signature scanner panel** — the radial menu showing "UNKNOWN — 11,565" with the location-pin icon. Region configured as `ocr_region` in `mining_signals_config.json`.
- **SCAN RESULTS panel** — the rectangular ship-laser scan panel showing mineral name + MASS / RESISTANCE / INSTABILITY values. Region configured as `hud_region`.

The **scan bubble** (`ui/scan_bubble.py`) is a frameless always-on-top widget that floats next to the user-defined region. It shows:
- "Scanning… Please Wait" placeholder while gates are armed.
- A coloured pill matching the rarity of the matched mineral (rarity → colour map in `scan_bubble.py:22-31`) once the signal value resolves.
- Multiple matches stacked when one signature value collides between minerals — the `show_matches` path at `scan_bubble.py:143`.
- Live HUD readings (M / R / I) when the scan-results panel is up but no signal match is available yet.
- **Topmost enforcement via raw `ctypes.windll.user32.SetWindowPos(... HWND_TOPMOST ...)`** at `scan_bubble.py:262-276` — Qt's stay-on-top hint loses against fullscreen-borderless games. A Qt-only widget gets buried the first time the game window repaints.

The **break bubble** (`ui/break_bubble.py`, 1,010 lines) is the breakability assistance panel. It consumes the live HUD `mass / resistance / instability` reads and the configured ship loadouts and recommends laser settings, gadget choices, charge-decay timing (min throttle, time-to-window, est crack time), substitute crew reallocations, etc. Its rendering is fingerprinted (`_breakability_signature` at `break_bubble.py:42`) so jittery sub-unit OCR reads don't cause flickering re-renders.

Per scan tick:
1. Capture both regions with `mss`.
2. **Anchor-gate** each region (a cheap polarity-canonical NCC check) to decide whether the corresponding panel is on screen.
3. If at least one gate fires, dispatch the heavy scan(s) to a persistent `ThreadPoolExecutor` — signal scan and HUD scan run **concurrently** in two futures.
4. Wait with timeouts (6 s for signal, 3 s for HUD).
5. Push results through rolling consensus windows.
6. Marshal results back to the UI thread via `QMetaObject.invokeMethod(... Qt.QueuedConnection ...)` to update the bubbles.

## The OCR pipeline at a glance

Three observations up front:

- The pipeline has been **rewritten three times.** Git log shows `Initial local snapshot` (Tesseract-only) → `Signal OCR: add PaddleOCR as third engine` → `Auto-harvest + online learning for ONNX digit CNN`, then a wholesale move to a custom NumPy + ONNX engine called `sc_ocr`.
- The legacy 3-engine code was removed from the default path, but a **fast/legacy engine-mode toggle** (`ocr/engine_mode.py`, 2026-09) re-wires it back in on demand: `MINING_SIGNALS_OCR_MODE=legacy` env var or the `ocr_engine_mode` config key routes scans to the original Tesseract A + B + PaddleOCR sidecar vote (`screen_reader.extract_number`, ~1.2 s/scan), while a reconstructed Tesseract+Paddle HUD line (`onnx_hud_reader._legacy_scan_hud`) covers the mass/resistance/instability panel. Default is `fast` (SC-OCR, ~23 ms/scan); any unknown value falls back to `fast`. The Scanner tab has a "Legacy OCR" checkbox; the config is re-read every tick so the toggle takes effect on the next scan.
- The current pipeline lives under `ocr/sc_ocr/` and consists of **19 modules totalling ~10,000 lines of pure-NumPy + ONNX code**.

## Anchoring the panel on screen

Two completely different anchors per panel, because the two panels have completely different visual signatures:

**Signature panel** uses NCC against the location-pin **icon** (`ocr/sc_ocr/signal_anchor.py`). Templates come from `training_data_blacklist/` — and the docstring at `signal_anchor.py:13-15` notes the elegant repurposing: *"The blacklist directory was originally created so glyph extraction could REJECT icon-shaped tiles. We re-purpose those same icon PNGs as positive ANCHOR templates here. One-stop registration."* Templates are **multi-scale** (target widths 16, 20, 24, 28, 32, 36, 40 px) so different render resolutions match. Key quirk: the matcher returns the **leftmost** position whose score crosses threshold — not the global maximum — because SC digit shapes (specifically `9` and `,0` pairings) NCC-correlate higher against the icon than the icon itself does. Picking the leftmost peak filters that out.

**SCAN RESULTS panel** uses NCC against the rendered title text "SCAN RESULTS" (`ocr/sc_ocr/scan_results_match.py`). Templates were built offline by `scripts/build_scan_results_template.py` from annotated panel captures and live in `ocr/sc_templates/scan_results.npz`. Search is restricted to the top 42% of the capture region (`scan_results_match.py:84`) to prevent matching against MASS-row digits whose vertical strokes correlate with parts of the title at small scales. Score threshold 0.40 calibrated against 120 captures: **99.2% match at 0.40, 98.3% at 0.50, 95.0% at 0.60.**

Once the title is anchored, **three tiers** locate each value row:
- **Tier A (preferred):** per-row NCC label match for "MASS:", "RESISTANCE:", "INSTABILITY:" using templates in `ocr/sc_templates/labels.npz`.
- **Tier B (fallback):** horizontal-projection band detection in the strip below the title.
- **Tier C (fallback of fallback):** fixed proportional offsets from the title's height (`_ROW_OFFSET_MULTS` at `onnx_hud_reader.py:935`).

There is **also** a fully Tesseract-free path `_find_label_rows_by_hud_grid` (`onnx_hud_reader.py:1385-1504`) that detects the two horizontal HUD chrome separator lines bracketing the panel via `_find_panel_lines`. Those lines have a distinctive ≥80% column-fill signature (`onnx_hud_reader.py:306-307`) that lets the code reject decorative rules and use fixed fractional row positions when label-NCC has failed and projection bands are noisy. Four redundant ways to find the same three rows — any one can succeed independently.

## Five OCR engines (plus a font-template tiebreaker)

The mature pipeline runs as many as five independent engines per scan and votes them:

1. **Custom 28×28 digit CNN** — `ocr/models/model_cnn.onnx`, the primary fast path. Mort13's "trained CNN model (3 KB graph + 1.7 MB weights, 13 char classes, 100% validation accuracy)" per the file header. Char classes `0123456789.-%`.
2. **Inverted-polarity sibling CNN** — `model_cnn_inv.onnx`, same architecture, trained with augmentation that pixel-inverts every glyph. Used as the **decorrelated peer voter** in the dual-polarity scheme described below.
3. **CRNN (Conv + BiLSTM + CTC)** — `model_crnn.onnx`. Trained with `ocr/train_crnn.py` for value-crop and mineral-name reads. Architecture: five conv stages collapsing height to 1, then two-layer bidirectional LSTM, linear → CTC. Alphabet expanded to digits + punctuation + uppercase + lowercase + space + parens so it can read both `"32.17"` and `"BERYL (RAW)"` from the same head.
4. **Tesseract** — both stock `eng` and a fine-tuned **`eng_sc` LSTM** (from the SC-Datarunner-UEX project, in `ocr/tessdata/eng_sc.traineddata`). Tesseract is auto-downloaded from UB-Mannheim on first use if not present (`screen_reader.py:30-150`).
5. **PaddleOCR** — runs under a **separate Python 3.13 embedded interpreter** as a subprocess daemon (`ocr/paddle_daemon.py`), spawned and talked to over stdin/stdout via length-prefixed JSON by `ocr/paddle_client.py`. The reason: paddlepaddle has no Python 3.14 wheels yet, but the main app has to run on 3.14, so the IPC sidecar is the only way to use it. The daemon loads PaddleOCR once (~5–20 s, models stay resident) and services requests in a tight loop. First inference is ~3–5 s, subsequent ones <1 s on CPU.

Plus a sixth deterministic voter — **Furore-font templates** (`ocr/templates_furore.py`, 301 lines) — pre-rendered from `furore.otf` at 11 different sizes into `ocr/models/furore_templates.npz`. Used as a tiebreaker on small-extraction-mode crops where the neural engines disagree.

## The dual-polarity CNN voter — the single cleverest piece

The SC HUD font has a structural 5-vs-6 ambiguity that any single classifier misclassifies in **predictable** ways. The fix lives at `sc_ocr/api.py:2348-2423`:

- For each Tesseract-segmented digit position, the system runs the **original-polarity CNN** on the bright-on-dark glyph AND the **inverted-polarity CNN** on the same glyph after pixel inversion.
- Because the two models have **zero shared weights** AND see opposite polarities, their errors decorrelate strongly.
- If the two CNNs agree on **every** digit, the result overrides Tesseract.
- If they disagree on **any** digit, the function returns `None` and the caller falls through to Tesseract's vote.

The motivation, per the comment block at `sc_ocr/api.py:2175-2189`: the SC font's 5-vs-6 differs by a single stroke segment that anti-aliasing eats at HUD scale. Two independently-trained classifiers seeing inverted pixels almost never make the same mistake, so **disagreement between them is itself a high-value signal** that says "ambiguous case, defer to Tesseract." That asymmetry is what makes the voter useful — it's not just two competing strings, it's a per-position confidence built out of decorrelation.

The same primary+secondary CNN pattern is used on HUD value crops at `sc_ocr/api.py:1605-1657`, where the secondary always runs (even when the primary has high confidence) so the live diagnostics viewer can show both heads' classifications side by side for debugging.

## Preprocessing — every step is a fix for a specific failure

The repo has more than 200 `_debug_*`, `debug_*`, and `_chroma_*` PNGs at the root, named things like `_chroma__sample_light_iron_impossible_resistance.png`, `_check_inst.png`, `_bin_debug_cleaned_d20.png`. These are saved by the throttled debug-save path in `onnx_hud_reader.py:94-154` (every 5 s on the hot path so disk writes don't slow scanning) and they are persistent because the OCR pipeline went through several preprocessing iterations, each chasing a specific failure mode:

- **Polarity canonicalisation via minority-class rule** (`sc_ocr/api.py:303-339`). The naive rule `median > 130 → invert` gets fooled by bright sky backgrounds where text is brighter than the mostly-bright background. Instead, Otsu-split into two classes and treat the smaller class as the text. Always normalise to bright-on-dark.
- **Adaptive (local) binarisation** (`sc_ocr/api.py:342-415`). Replaces global Otsu (which assumes a bimodal histogram and breaks on bright sandy/asteroid backgrounds) with a Gaussian-windowed local threshold. Block size 31, C=15. Tests `pixel > local_mean + C` because text is bright (inverse of the document-OCR convention).
- **Max-of-RGB grayscale instead of luma** (introduced by commit `496619b`). Chromatic aberration on the SC HUD smears coloured strokes 1–2 px; PIL's `convert("L")` blends channels with weights and spatially blurs them, dropping NCC scores from **0.85+ down to 0.55**. Per-pixel max preserves the brightest channel and keeps each glyph shape recognisable.
- **Two-tier vertical-density mask** (`onnx_hud_reader.py:443-460`). A strict density floor (25% of row height) catches stroke columns; a permissive floor (3 px) catches dots and decimal points; the strict-hot mask is dilated by ±6 px and ANDed with the permissive-hot mask to recover dots that sit adjacent to digits without admitting random noise.
- **Polarity-flip-after-binarisation specifically for Tesseract** (`onnx_hud_reader.py:2854-2862`). Even though we standardise to bright-on-dark for the CNNs, Tesseract's PSM 6/7/8 modes return empty string on white-on-black input because their document classifier rejects it. Empirically: on the "382.36" instability crop, pre-flip → empty; post-flip → "382.36". So we own the polarity end-to-end and flip back specifically for Tesseract.

Each step has a corresponding `_debug_*.png` in the repo as a regression fixture — the file naming pattern (`_chroma__sample_light_iron_impossible_*.png` and friends) lets us replay any past failure mode against new pipeline changes.

## Character segmentation — the named guards

`_segment_glyphs` in `sc_ocr/api.py:507-653` is doing more than vertical-projection segmentation:

- Min span width 2 px, lowered from 3 because narrow `1`s and `.`s were being dropped.
- **Right-anchored span filter for label-text intrusion**: find the largest gap between adjacent spans; if it's >1.4× the median *or* >8 px absolute, discard everything left of it as label leakage. Without this, "MASS: 27.43" can produce phantom digits from tail-end artifacts of the M-A-S-S strokes.
- **Leading-narrow-drop with two guards**: any leading span narrower than 80% of the median width is dropped — *unless* `_looks_like_one(0)` (height/width ≥ 2.0, the actual aspect ratio of `1` glyphs in this font) *or* `_looks_full_height(0)` (full row height, the actual signature of a `0` next to a `%`). Without the guards, the width-only check was eating real `1`s and `0`s.
- A previous "split merged-digit spans" pass was **removed** because it sliced `%` in half — the `%` glyph is naturally about 1.6× a digit's width, which the split heuristic interpreted as "two merged digits."

Each guard is a fix for a real bug. The code visibly remembers the bugs it has been taught to avoid.

## Why we built our own OCR

We didn't set out to. The pipeline went Tesseract → Tesseract + PaddleOCR voter → custom — driven by a set of constraints that don't reduce.

**Hard 500 ms scan budget.** Tesseract subprocess spawn on Windows is 50–100 ms; the original three-PSM × three-scale matrix was 9 calls/scan, blowing 450–900 ms wall-clock and pegging a CPU core at 1 Hz scanning. PaddleOCR's *warm* inference is ~9 s — longer than the entire scan budget. Anything in the primary path has to finish in ~30 ms, and nothing off-the-shelf does.

**A font no document OCR has seen.** SC's HUD uses Furore. Default Tesseract `eng` on a clean "499" crop at 4× upscale returns `"43%"`. PaddleOCR splits "SCAN RESULTS" into multiple regions inconsistently. Both are trained on document scans, where digits look very different from a glowing thin-stroke sci-fi UI font rendered through chromatic aberration and bloom.

**Tiny rendered glyphs** (12–24 px depending on resolution and FOV). Document OCRs are tuned for 30+ px text and degrade sharply below that. We deliberately train at the actual rendered size.

**A 5-vs-6 structural font ambiguity.** Furore's `5` and `6` differ by a single stroke segment that gets eaten by anti-aliasing at HUD scale. Any single classifier flips between them frame to frame. Off-the-shelf OCRs give you a string out; they don't give you the per-digit confidence breakdown or the architectural seams to plug a second decorrelated classifier in beside the first.

**Domain knowledge has nowhere to go.** The full set of valid signature values is finite and known (Mort13's spreadsheet). When a scan ties between an in-spreadsheet candidate and a not-in-spreadsheet candidate, we want the in-spreadsheet one. Generic OCRs have no API for "here's the lexicon, prefer it" — Tesseract has user-words but only as a coarse hint at the *word* level, not at the per-digit-position level we needed.

**Polarity and preprocessing have to be controlled end-to-end.** Tesseract's PSM 6/7/8 modes return empty string on white-on-black input because their document classifier rejects it. Our CNNs need bright-on-dark. We need to flip polarity *between* the engines — which means we have to own the preprocessing pipeline.

**Online learning has to be live.** When three engines agree, we want to push that sample as a gradient step *and hot-swap the live model during play*. You can't hot-swap Tesseract — its model is loaded inside its subprocess and the only knob is fine-tuning offline and shipping a new traineddata. PaddleOCR is similar. ONNX + a PyTorch sidecar makes the swap a pointer move under the GIL.

**Shipping size.** Tesseract is ~30 MB on disk; PaddleOCR is hundreds of MB once you include detection + recognition models. Our entire primary CNN is **1.7 MB of weights** plus a 3 KB graph.

**Resource pressure on the player's machine.** PaddleOCR uncapped uses every CPU core it can find, dragging SC's frame rate. We had to cap `OMP_NUM_THREADS=2` at sidecar startup before paddlepaddle imports, just to keep the game playable.

**Wheel-availability accidents.** As of April 2026, paddlepaddle has no Python 3.14 wheels. The main app runs on 3.14. So even *using* PaddleOCR required spawning a 3.13 embed and IPCing into it. That's a fine workaround for a third voter; it's a non-starter for a primary read path.

Any one of those, you could engineer around. All ten together, you can't.

### What we tried, in order

1. **Tesseract-only.** First commit. Hallucinated on the SC font, blew the latency budget on Windows due to subprocess spawn, and gave us no per-character confidence to feed consensus logic.
2. **Add `eng_sc`** — a fine-tuned Tesseract LSTM from the SC-Datarunner-UEX project. This was the version of "modify an existing OCR" we tried hardest. It *helped* — `"43%"` became `"499"` on the canonical test crop. But fine-tuning the LSTM didn't fix the subprocess-spawn cost, didn't give us per-digit confidence, didn't give us a way to inject the in-spreadsheet lexicon as a tiebreaker, and didn't give us the seam to plug a second polarity-inverted classifier in beside the first. We still ship `eng_sc.traineddata` and use it on the slow path, but it couldn't be the primary.
3. **Add PaddleOCR as a third voter.** Decorrelated from Tesseract — useful as a vote — but its 9 s warm inference means it can never be on the critical path. It runs as an async background voter whose result the *next* scan reads from a 30 s cache. Useful as a third opinion on disagreement, useless as a primary read.
4. **Build the custom pipeline.** Start with a 28×28, 13-class digit CNN trained on the actual Furore font at the actual HUD scale. Train an inverted-polarity sibling for dual-polarity voting. Add a CRNN with a custom alphabet for whole-value and mineral-name reads. Pre-render the Furore font at 11 sizes for a deterministic template-NCC tiebreaker. Keep Tesseract and PaddleOCR around as decorrelated voters on the slow path, where their 50–100 ms (and 9 s) latencies are amortised across the consensus window.

## How we landed on `sc_ocr` — the decision moment

There was one specific moment when we admitted out loud that the existing pipeline was structurally wrong rather than just incomplete.

The original `ocr/onnx_hud_reader.py` had grown organically over months. By the time it had three engines and three row-finding tiers and a separate light-HUD branch, the file was **3,926 lines** in one module, unmaintainable: every new failure mode required squeezing another conditional into a function that was already doing six things, and the consensus logic was scattered across the file rather than centralised.

The thing that tipped it wasn't a feature we couldn't add — it was a **bug we couldn't isolate.** A flicker between **11,565 and 11,655** in the signal scanner. We could see in the debug PNGs that the OCR was reading both values across consecutive frames. We knew the fix had to live in the consensus layer. But the consensus layer was three different ad-hoc retry loops in three different functions, none of which knew about the others, and threading the fix through all three would break some other invariant we'd lose track of.

That was the moment we stopped patching `onnx_hud_reader.py` and started writing `ocr/sc_ocr/` from scratch. The old file is still in the tree, disabled behind `if False:` blocks, kept as a reference for the failure modes it caught — because we genuinely *do* still consult it when a new failure mode shows up that we suspect was already solved once.

The new design started from a different premise: **OCR is the easy part. The architecture around the OCR is the hard part.** Once we accepted that, the shape of `sc_ocr` fell out almost mechanically.

## Why `sc_ocr` is the right shape

Three structural properties make it fit-for-purpose in ways no library could, and they're properties no off-the-shelf OCR can give you because they're not OCR properties — they're *system* properties.

**1. Engines are voters, not architecture.** In a normal OCR-using app, the OCR is the architecture and your code wraps it. In `sc_ocr`, the architecture is the consensus stack — `anchor → row-find → segment → vote → consensus → lock` — and engines plug into the *vote* step as interchangeable peers. Adding a sixth engine would be a 30-line file. Removing one is a one-line change. The pipeline does not depend on any of them being present, and crucially, none of them sees the consensus logic — so we can reason about consensus correctness without reasoning about engine internals.

**2. Per-digit-position voting is a first-class concept.** No off-the-shelf OCR exposes this. Tesseract returns a string and a word-level confidence; PaddleOCR returns a string and a region-level confidence. Neither lets you say "engine A and engine B agree on positions 0,1,2 but disagree on position 3, so accept A's answer for the agreed positions and abstain on position 3." The dual-polarity voter only works because `sc_ocr` *segments first, then votes per glyph.* That inverts the normal OCR data flow and lets the dual-polarity disagreement become a high-value signal rather than just two competing strings to pick between.

**3. Consensus, fingerprinting, and locking are baked into the API, not bolted on.** Every value the pipeline returns has gone through a 5-frame rolling consensus buffer with a parallel crop-fingerprint buffer, then through a sticky 2-of-3 majority, then through a lock cache that skips OCR entirely on stable values until the crop fingerprint drifts. That's not an afterthought wrapped around a third-party OCR — it's the spine of the module. Off-the-shelf OCRs return a fresh string per call and leave consensus as your problem; `sc_ocr` makes consensus the contract.

## The robustness layer — ~30 named mechanisms

The OCR engines are necessary but not sufficient. On top of them sits a stack of consensus, hysteresis, and locking layers — every one of which exists because some specific flicker or wrong-read happened in real use.

**Per-field rolling consensus buffer (5 frames per field) plus a parallel crop-fingerprint buffer** (`sc_ocr/api.py:42-77`). The fingerprint is an 8×24 NCC fingerprint (`_CROP_FP_W=24, _CROP_FP_H=8`). A new value never displays without majority agreement.

**Sticky 2-of-3 majority** (`_consensus_value` at `sc_ocr/api.py:248-291`). Until a value appears at least twice in the buffer, the previously-displayed stable value is held.

**Field-value lock cache** (`sc_ocr/api.py:204-237`). Once a field reads the same value across **all 5 frames** (`_LOCK_VALUE_AGREEMENT = _LOCK_WINDOW`) **AND** the mean pairwise crop NCC ≥ 0.85, that value is locked for the duration the panel is visible. Subsequent scans skip OCR entirely for that field. The lock is invalidated when the **current crop's NCC against the stored fingerprint drops below 0.65** — lower than 0.85 to avoid dropping good locks on transient noise but high enough that a real row-geometry drift will release the lock.

**Signal scanner consensus** (`sc_ocr/api.py:80-129`). 6-frame rolling buffer (`_SIGNAL_BUFFER_LEN`). A new value swap requires **4 consecutive identical reads** (`_SIGNAL_AGREEMENT_REQ=4`, raised from 2 because 2 was letting the 5-vs-6 OCR ambiguity flip between 11,565 and 11,655).

**`_KNOWN_SIGNAL_VALUES` lexicon tiebreaker.** The full set of valid signature values from the mining chart is loaded into a Python set. The voter prefers in-set candidates over out-of-set ones even before majority is reached. Per the comment at `sc_ocr/api.py:108-121`: *"if among the 6 PSM × scale Tesseract variants two produce 17020 (a known Silicon × 4-rocks value) and one produces 17011 (not in any known table), the voter returns 17020 even before majority is reached. This kills the dominant flicker pattern outright."*

**Anchor gate hysteresis** (`app.py:3933-3972`). Strict floor 0.74 for "fresh enter" — empty screens score up to 0.72, the false-positive zone. Relaxed floor 0.55 for "stay locked" once the gate has confirmed. The gate stays True until 3 consecutive sub-strict ticks drop it. Brief frame-to-frame score wobble (chromatic aberration shifts, anti-alias differences) doesn't flip the gate to None and force the bubble to flicker into the "Scanning Please Wait" placeholder.

**Gate state machine and `active_both`** (`app.py:5357-5482`). Four states: `armed`, `active_signature`, `active_hud`, `active_both`. When both signal AND HUD anchors fired in the same tick, the previous if/elif chain only set ONE flag, and the state handler then nuked the OTHER side's cached values on every tick. That caused the break bubble (which depended on cached HUD values) to flicker off whenever the signal scanner was on screen. The fix introduces the distinct `active_both` state so the cleanup logic preserves both sides. **And** the cleanup is gated on `_state_changed = state != _prev_state` — teardown is a transition event, not a per-tick event. Without that, even with `active_both` correct, the per-tick `self._scan_bubble.hide()` race produced visible flicker.

**Scan-bubble lifecycle.** Both `show_scanning` and `show_matches` compute a content fingerprint (`_last_scan_fp`, `_last_match_fp`) and **early-return** if the fingerprint matches the previous call. Without this, `_maybe_show_scanning` re-fired every 500 ms with the same data, destroying and recreating every QLabel each time, producing a visible 2 Hz flicker. **Stale-match clearing**: at `app.py:5278-5319`, `_on_signal_no_value` increments a streak counter; only when the signal scan returns no value 3 consecutive times does the scan bubble clear its matches. Handles the case where the signature panel disappears but the anchor's hysteresis keeps `sig_present=True` — without this, the bubble froze on the last match indefinitely.

**Stopped averaging consecutive signal reads** (commit `b20e5bb`). For a 5-digit signature like 7680, the ±max(50, 5%) tolerance window is 384 wide. A single outlier averaging two consistent 7680s with a stray 7860 produced **7770**, which exact-matched **Agricium's** signature — a different rock entirely. Removed the average; relies on agreement-count consensus instead.

**Async PaddleOCR via background cache** (`onnx_hud_reader.py:3367-3404`, commit `6264076`). Paddle takes ~9 s per warm inference, far longer than the 5 s scan budget. The pipeline runs Paddle fire-and-forget in a background `ThreadPoolExecutor`; the *next* scan reads the cached result and uses it as the third voter. Cache TTL 30 s, min interval between dispatches 15 s to prevent CPU pegging.

**Multi-frame averaging for HUD wiggle** (commit `518fe41`). Beyond per-field consensus, the HUD pipeline uses **7-frame averaging + rolling-window majority vote** to defeat sub-pixel HUD jitter. A new mineral name only displaces an existing locked one when its vote count ≥ 2 — protects against fuzzy-match collisions where "Borase" and "Beryl" both show up in the candidate list.

**Light-vs-dark HUD polarity dispatch** (`onnx_hud_reader.py:3690-3713`). `if float(np.median(gray)) > 130: light_result = _scan_light(img)`. The light path goes through PaddleOCR exclusively because the dark path's fixed-threshold heuristics break on light backgrounds. Panel visibility for the light path is detected by looking for "scan" and "results" anywhere in the region list — paddle sometimes splits "SCAN RESULTS" into two regions.

**CPU-pinning the PaddleOCR daemon** (`paddle_daemon.py:56-60`). Env vars `OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `OPENBLAS_NUM_THREADS` are capped to 2 BEFORE importing any numeric libs. Without the caps, a single PaddleOCR inference uses all available CPU cores and pegs user machines at 90% CPU during scanning.

**Per-region label cache** (`onnx_hud_reader.py:53-58`). Tesseract label OCR is ~500 ms (3 subprocess spawns); labels don't move within a rock, so the cache is keyed by region geometry with a 60 s TTL, invalidated on `panel_visible=False`.

**Per-scan panel-lines cache** at `onnx_hud_reader.py:317-334`. The three-row call (mass/resist/instab) inside one scan shares the same gray array; the cache keyed by `id(gray) + shape` keeps repeated calls free.

**Calibration JSON** at `%LOCALAPPDATA%\SC_Toolbox\sc_ocr\calibration.json`. User-confirmed crop coordinates per HUD region. When present, the runtime **skips detection entirely** for that region (commit `ac6872d` "Trust user calibration unconditionally").

That's about thirty mechanisms. There are others. Each has a commit attached to the failure mode it addresses.

## The training pipeline — every glyph hand-captured

The training infrastructure is roughly the same scale as the inference engine, and **every training sample was captured and labelled by a human in front of the actual game.** No scraped dataset, no public corpus to fine-tune on. The pipeline was built around the player playing.

### Capture

`scripts/dual_capture.py` puts two draggable always-on-top frameless rectangles on screen — one over the SCAN RESULTS panel, one over the signature panel — with global hotkeys that **don't suppress the keypress**, so SPACE and B capture the underlying region while the game still receives the input normally. Captures land in `training_data_panels/<session>/region1/` and `region2/`. Overlay positions, sizes, and toggles persist between sessions in `.dual_capture_state.json` because dual-capture is iterative — the player keeps refining overlay alignment across sessions and shouldn't have to re-fit the rectangles every time.

The whole tool exists for one reason: **it has to be possible to grab labelled data without leaving the game**, because the moment you alt-tab the HUD looks different and the value you wanted to capture is gone. That single requirement is what justifies the global-hotkey-without-suppression plumbing, the persistent state, and the always-on-top frameless rectangles — there isn't an off-the-shelf tool that does that.

### Label

Captures sit unlabelled in `training_data_panels/` until they go through one of the GUI labellers — `scripts/label_pending.py` or `scripts/label_crops_gui.py`. The labeller shows each captured panel image and the user types the values they can see — `mass: 27.43`, `resistance: 0.61`, `instability: 382.36`. That's the only ground truth the system ever gets. **Every digit the model knows about, a human typed.**

### Extract

`scripts/extract_labeled_glyphs.py` walks the labelled corpus, runs each panel through the same segmentation logic the inference pipeline uses, and pairs each character of the typed label with the corresponding segmented glyph in the panel image. Per-class PNGs land in `training_data_pending_review/<digit>/<source>_<n>.png`. This is where the labelling effort multiplies — one hand-typed three-digit value yields three labelled glyphs, one signature read yields four or five.

### Review

Pending glyphs aren't trusted until a human looks at them. `scripts/stage_for_review.py` batches pending glyphs for review; `scripts/review_glyphs.py` is the accept/reject GUI; `scripts/promote_reviewed.py` moves accepted glyphs into `training_data_clean/<digit>/` where they become eligible for training.

Why review at all, when the glyph was already labelled? Because the segmentation step is fallible. A merged digit pair, a stray pixel from a particle effect, a row that landed half-off-frame — these all produce glyphs that *correspond* to the right character in the typed label but visually aren't actually that character. Promoting them to training data would teach the model that `8` sometimes looks like `83`. The review pass exists to catch that.

### Prune

Bad glyphs that survived review still surface eventually — usually because they cause regressions in the validation accuracy on the next training run. `scripts/quarantine_contaminated_glyphs.py` pulls them out of the clean set, and `scripts/blacklist_manager.py` catalogues them in `training_data_blacklist/`.

The blacklist isn't just "deleted samples." It's a *positive* corpus of known-bad shapes the model should learn to **reject**, used during training as negative examples. And, as covered earlier, the same blacklist directory does double duty as the source of icon templates for the signal-anchor NCC matcher. **One directory, three uses: training reject pool, anchor template source, and audit trail of every false positive we've found.**

### Clean and split

`scripts/filter_training_data.py` dedupes near-identical crops (the labeller will produce many similar samples from one play session — adjacent frames with the same value), balances class counts, and writes train/val splits into `training_data_split/`. The directories `training_data_user_panel/`, `training_data_user_panel_inv/`, and `training_data_user_sig/` hold per-source slices — panel-captured samples in original polarity, panel-captured samples augmented to inverted polarity (for training the inverted-polarity sibling CNN), and signal-scanner-captured samples — kept separate so a regression in one source can be diagnosed without touching the others.

### Synthetic supplementation

`ocr/synth_data.py` (471 lines) fabricates whole-string labelled crops for the CRNN by concatenating real labelled single-glyph crops left-to-right with random spacing and augmentation, then bootstraps the missing characters (`.`, `%`) via PIL renderings of the actual Furore font with extra augmentation. The CRNN couldn't have been trained without this — there was no source of labelled multi-digit strings, only labelled per-glyph crops, and the CRNN reads sequences. So we built the sequences from the glyphs we had.

### Pretraining

`ocr/pretrain_crnn.py` (1,483 lines) pretrains the CRNN on **SVHN (600K real-world digit images) + MJSynth (9M synthetic text words) + SynthText (800K synthetic scene-text images)**, all filtered to examples whose ground truth is exclusively in the SC alphabet. Streaming download — every dataset is downloaded in chunks, re-encoded to the 32-tall canvas format, used for training, then deleted. **No dataset is ever fully on disk.** This gives the CRNN a strong general "what digits look like" prior; the SC-specific fine-tune on top of that is what teaches it to read Furore at HUD scale.

### Iteration

The training logs in the repo root tell the story directly:

- **9 fine-tuning runs:** `_finetune.log`, `_finetune_874.log`, `_finetune_broad_base.log`, `_finetune_clean.log`, `_finetune_furore.log`, `_finetune_multires.log`, `_finetune_v2.log` through `_finetune_v8.log`.
- **6 pretraining runs:** `_pretrain.log`, `_pretrain_big.log`, `_pretrain_furore.log`, `_pretrain_gpu.log`, `_pretrain_v2.log`, `_pretrain_v3.log`.
- **2 dedicated CRNN training runs:** `_crnn_train_v2.log`, `_crnn_train_v3.log`.
- **8 versioned CRNN model snapshots in the repo:** `model_crnn_20260416_225235_val78`, `..._20260417_002114_val50`, `..._011352_val81`, `..._073242_val76`, `..._091915_val83`, `..._211548_val54`, plus `model_crnn_furore`, `model_crnn_furore_smalltext`, `model_crnn_large_clean_val54`, `model_crnn_smalltext_val76`. Each snapshot is a checkpoint we kept so a regression can be rolled back by copying it over the canonical name and re-exporting to ONNX.

The validation accuracies in the filenames (50, 54, 76, 78, 81, 83) are *the visible record of the curve climbing*. We didn't go straight to a good model. Each accuracy number is one full loop of *capture session → label → review → train → evaluate against held-out user-captured panels → regress → root-cause the regression (usually a class of glyph that was under-represented or a contamination pattern in the clean set) → capture another session targeting that weakness → re-label → re-review → re-train.*

### Online learning

Once the model was in user hands, the training loop didn't stop — it just moved into the player's machine. `ocr/online_learner.py` and `ocr/digit_reservoir.py` keep going during real play.

When all three engines unanimously agree on a digit, the crop goes into a per-class **Algorithm R reservoir** at `%LOCALAPPDATA%/SC_Toolbox/digit_reservoir/{0-9}/`. Phase 1: always add until full (`MAX_PER_CLASS=50`). Phase 2: accept with probability `MAX/n_seen` and replace a random existing sample. Classic reservoir sampling, so the long-tail distribution stays representative no matter how many samples the user has produced.

Simultaneously, the same crop goes onto a thread-safe queue that a background daemon thread processes one **Adam gradient step at LR 1e-4** at a time. Every 50 steps it re-exports the PyTorch model to ONNX (atomic write + rename) and **hot-swaps it into the live inference session** with `onnx_hud_reader.hot_swap_model` — Python's GIL makes the session-pointer swap atomic.

The online-learned model lives in `%LOCALAPPDATA%`; the shipped model in the app dir is **never modified**, so updates don't clobber learned weights and `reset_to_pretrained()` is a one-line deletion. Graceful degradation: if PyTorch isn't installed, all online-learning methods become no-ops, but the reservoir still collects samples for offline retraining. A killswitch env var `SC_TOOLBOX_AUTO_COLLECT` gates ALL training-data writes, so disabling collection in one place stops every disk write at once.

The user's machine literally trains the model on the user's specific monitor / HDR profile / DPI setup, drifting it toward their distribution without catastrophic forgetting. **The capture-label-review-train loop never ends** — it just becomes a closed loop running inside the player's session.

### What that means in aggregate

Every glyph in the training set was put there by hand. The capture tool is a hand-built capture tool. The labeller is a hand-built labeller. The reviewer is a hand-built reviewer. The blacklist is a hand-curated blacklist. Each tool exists because the previous step's output couldn't be trusted to be ground truth without a human pass on top, and each tool was built specifically for the shape of error its predecessor produced. That's where the time really went — not into the OCR algorithms (those are well-understood), but into the *infrastructure for a human to teach a model what a Star Citizen digit looks like, fast enough that the human can keep doing it for the months it takes to converge.*

## The breakability calculator

`services/breakability.py` (1,316 lines) is a pure-Python port of Mort13's BreakabilityChart calculations. Core math:

```
mass = power × (1 − effective_resistance) / C_MASS
required_power = (mass × C_MASS) / (1 − effective_resistance)
```

The mass constant `C_MASS = 0.20` (community standard ÷5; Mort13's original 0.175 undershoots required power by **14%** and gives false "can break" verdicts on borderline rocks — see comments at `breakability.py:30-35`).

The calculator does much more than the formula:

- **Charge profile simulation** (`breakability.py:131-160`, commit `80f3047`). Dynamic charge mechanics reverse-engineered from SCMDB's Mining Solver. Two constants — `decay_rate = mass × 0.02`, `capacity = mass × 10` — were derived from **4 SCMDB screenshots at mass 5789** across different rock compositions. Computes `min_throttle_pct`, `time_to_window_sec`, `time_in_window_sec`, `est_total_time_sec`.
- **Multi-laser subset search** (`breakability.py:361`). Enumerates non-empty laser subsets, smallest-first, returns the minimum-laser config that breaks the rock.
- **Greedy fallback for fleets >12 turrets** (`breakability.py:305-358`). O(n²) instead of O(2^n).
- **Crew reallocation** (`breakability.py:223-280`, commit `f01bb52`). If a MOLE turret is empty, pull a player from a non-mining ship to fill it — but only one reassignment per pool member per rock, and donor mining ships get excluded from the calc.
- **Gadget recommendation** that respects the `gadget_quantities` config (Okunis, BoreMax, OptiMax, Sabir, Stalwart, Waveshift) and the `always_use_best_gadget` toggle.
- **Cluster mode** for fleet/team/cluster configurations.

The break bubble's `_breakability_signature` (`break_bubble.py:42-100`) hashes 20+ inputs (rounded to dampen jitter) so re-renders triggered by sub-unit OCR bounce don't tear down the widget tree.

## Caching and state files

A handful of files in the project root keep the system fast and resumable across sessions:

- `.mining_chart_cache.json` (240 KB): mining location data fetched from scmdb.net, TTL 24 hours (`mining_chart_data.py:33-35`). Hammering the community resource on every app start would be antisocial.
- `.refinery_distance_cache.json`: refinery-to-station distance lookups; distances are stable, so cache once.
- `.dual_capture_state.json`: persists overlay positions, sizes, and toggles between dual-capture sessions.
- `.signals_cache.json`: cached signal table.

## A small piece of design economy

The `training_data_blacklist/` directory was originally created so glyph extraction could **reject** icon-shaped tiles (so they didn't pollute the digit training set as false positives). The same icon PNGs are now also used as the **positive anchor templates** for the signature panel — one-stop registration. Add a new icon variant to one directory and it simultaneously improves anchor matching AND prevents that variant from leaking into digit training. That kind of double duty is threaded through the codebase — the same `training_data_panels/` directory feeds both the labelled-string pool for the CRNN and the per-glyph extraction pipeline for the digit CNN; the same Furore font asset feeds the synth data generator, the template tiebreaker, and the validation rendering used to spot-check the learned model.

## What made it hard

If you stripped this down to one sentence, it would be: ***every error mode has a named, commit-tracked countermeasure.*** The pipeline isn't "OCR plus a UI." It's a stack of perhaps 30 small mechanisms — adaptive thresholds, fingerprint locks, dual-polarity voters, hysteresis floors, async background voters, sidecar Python interpreters for wheel-availability mismatches, hot-swappable models, three-tier row finders — each of which exists because at some point a specific bug surfaced in real use (a flicker, a mis-classification, a frozen bubble, a wrong rock recommendation) and was traced to its root and fixed *there*, not papered over with a retry loop one layer up.

Most of those fixes look small in the diff. The cleverness is in the *choice* — figuring out which layer the problem actually belonged to, and what the smallest principled countermeasure was. The dual-polarity voter, the in-spreadsheet tiebreaker, the `active_both` gate state, the leftmost-anchor rule, the repurposed blacklist, the polarity-flip-after-binarisation specifically for Tesseract — those all came from sitting with a bad behaviour, finding the *actual* asymmetry that caused it, and exploiting that asymmetry as the fix. That's the part that took the time.

The result is an OCR system that's been forced into honesty by the constraints of running against a moving, particle-effect-occluded, chromatically-aberrated, sometimes-sunlit, fullscreen-borderless game HUD at 1 Hz on a CPU, with the user able to look anywhere at any time. And underneath it, a training infrastructure built around the only person who could ever produce ground truth for that HUD — the player playing the game — and rigorous enough about that human's labour that the model that drops out the other side has learned the actual shapes the actual game renders, on the actual machine the player owns.

That's the work.

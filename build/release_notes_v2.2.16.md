# SC Toolbox v2.2.16

## Mining Signals — full-body pose tracking + resolution-aware scanning

A major Mining Signals upgrade. The scanner now treats the SCAN RESULTS HUD panel and the SIGNATURE panel as single rigid bodies — pose-solved "skeletons" rather than per-field guesses — and adapts to your game resolution. The result is steadier locks, far less wander, and accurate reads across display/render resolutions.

### Full-body / rigid-pose tracking
- **One rigid pose per panel.** Each panel is solved as a single rigid body (icon-anchored pose), so the overlay holds together instead of individual fields drifting.
- **Predictive position tracking.** Rigid when the panel is still, smoothly follows when it moves (e.g. as you turn) — no more fresh/reject churn or wandering locks.
- **Scale lock.** Once solved, the panel's geometry is fixed, so reads stay stable through brief occlusion or jitter.

### Resolution-aware detection
- **Game-resolution detection + region normalization.** The capture is rescaled so the HUD renders at the expected size regardless of your display/render resolution — narrows the search and improves lock rate.
- Capture is upscaled once, up front, so the overlay no longer flips resolution mid-scan.

### Signature scanner
- Tightened pill finder (fixes under-detection), a signature consistency reflex, and lexicon hygiene for cleaner readings.
- `signal_solve` geometry core with a pill fallback when the world-model data is absent.

---

🤖 Generated with [Claude Code](https://claude.com/claude-code)

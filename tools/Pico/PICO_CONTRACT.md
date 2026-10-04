# PICO PAL — the interface contract

**Status: the SEAM, not the design.** J wrote the architecture (2026-09-27); this fixes the
boundaries between the pieces so several agents can build against one interface instead of inventing
four incompatible ones. Where J decided something, it is quoted. Where I decided something, it says
so. Where nobody has decided, it says OPEN and does not pretend otherwise.

## Why this file exists before any code

Four agents at an architecture produce four interfaces. The expensive version of that is not a merge
conflict, it is three correct components that do not compose. So: the data model and the formats are
frozen here first, the pieces are built against them, and disagreements come back to this file rather
than being resolved privately inside one component.

---

## 1. The layers, and who owns what

```
  A. RIG RUNTIME      bones, transforms, .anim parsing, channel blending
                      PURE LOGIC. No Qt, no I/O, headless-testable. Owns pico/rig.py, pico/anim.py
  B. RENDERER         draws a posed rig with its skin attachments
                      Qt. Consumes A's output. Owns pico/render.py
  C. EVENT MAPPER     Game.log -> triggers -> state machine
                      No Qt. Owns pico/events.py
  D. SKIN FORMAT      how a skin is authored and bound to bones. Owns pico/skin.py + the schema
```

**A is upstream of everything.** B, C and D consume its types and never redefine them.

---

## 2. Bones

⛔⛔ **SUPERSEDED 2026-09-27 by `pico_master_rig_pack_v1`.** J shipped a real rig pack whose
`rig/pico_skeleton.json` is, in his words, *"the stable naming contract"*. **THAT FILE IS THE
AUTHORITY AND THIS ONE DOES NOT RESTATE IT.** Earlier drafts of this section copied a bone list out of
his prose sketch; a copied list is a fork waiting to drift, and I have spent today finding forks.
Read the JSON. What follows is only what the JSON does not say.

*Pico_Master v1: 23 bones, 26 slots, canvas 2048x2048 origin top-left.*

★ **AND THE COPY I HELD WAS ALREADY WRONG.** My draft listed `jacket_L`, `jacket_R`, `collar` and
`belt` as BONES, because J's prose sketch listed them alongside real bones. In the actual rig they are
**SLOTS** — `jacket_front`/`jacket_back`/`belt_gear` hang off the `body` bone. Bones carry transforms;
slots carry artwork and z-order. Conflating them would have produced an animation format that tries
to keyframe a jacket directly, which is exactly the coupling J's whole architecture exists to prevent.
The rig pack caught my error two hours after I wrote it. Had I not compared, it would have shipped.

⇒ **SLOTS are not bones.** A slot is `(name, bone, z)`. z-order runs shadow 0 → body 10 → legs 20 →
  belly 40 → head 50 → visor 60 → flippers 70 → sleeves 72 → jacket_front 80 → headwear 90 →
  props 100 → fx_front 200. The animation format keyframes BONES ONLY; a skin swaps what is in a SLOT.

⇒ **CONSTRAINTS exist and my draft had none.** `head [-18,18]`, `body [-12,12]`,
  `flipper [-55,70]`, `foot [-18,18]` degrees, plus *"Character and FX must remain clipped to host
  viewport."* Clamp at pose time, and **clamp loudly in debug** — a silently clamped keyframe makes an
  animation look subtly wrong with nothing in the data to explain it.
  ⚠ Note `flipper` is ASYMMETRIC (-55 to +70). Do not "tidy" that into +/-70.

⇒ **ART CONTRACT, J's words:** *"Every raster attachment is exported on a 2048x2048 transparent master
  canvas. Do not trim individual PNGs during authoring."* The reason is the good part — it makes every
  manufacturer attachment land on the same pivot with no per-skin offsets. Runtime trimming/atlasing
  is allowed AFTER binding, never before.

⚠ **AND THE 12-FRAME SHEET IS NOT PRODUCTION ART.** J: *"The accompanying generated master sheet is a
  visual reference, not pixel-perfect separated source art. The exact transparent layers should be
  cut/redrawn to the stable master canvas before production binding."* So the sheet I measured and
  handed to the listener agent is reference-grade. That is fine for the listener, which is a separate
  tool playing frames, and it is NOT a source for Pico's slots.

A bone is `(name, parent, rest_x, rest_y, rest_rot, rest_scale)`. A pose is a sparse map
`{bone_name: (dx, dy, drot, dscale)}` of DELTAS FROM REST, never absolutes — so an animation that
touches only `flipper_L` leaves every other bone alone and can be layered over another.

### ⛔ TRANSFORM SPACE — decide it here, once, and write down which you chose

This is the thing that cost 16 hours in July on the Loom rig, recorded in
`BrAi/loom/LOCKED_ANIMATION_RIGGING.md`: rotations composed in the WRONG SPACE produced a rig where
"every arm flings UP — a breathing idle came out hands-by-the-head."

Pico is 2D, so the specific Mixamo tilt bug cannot recur. The general failure absolutely can:
parent rotation applied in the wrong order, or scale folded in before rotation instead of after.
**Compose child = parent_matrix @ local_matrix, with local = translate @ rotate @ scale, and state it
in a docstring.** If a future reader cannot tell from the code which space a delta is in, that is the
bug arriving.

---

## 3. The `.anim` format

J's own example is the spec:

```
0.00  torso rotation   0°
0.15  torso rotation  -8°
0.30  left_flipper    +24°
0.45  body_y          -12px
0.60  right_flipper   +28°
```

Frozen as JSON, because it must be diffable, hand-editable and machine-generated:

```json
{
  "name": "idle_breathe",
  "duration": 2.4,
  "loop": true,
  "channel": "BASE",
  "tracks": {
    "torso":     [{"t": 0.00, "rot": 0}, {"t": 1.20, "rot": -3}, {"t": 2.40, "rot": 0}],
    "flipper_L": [{"t": 0.00, "rot": 0}, {"t": 1.20, "rot": 2},  {"t": 2.40, "rot": 0}]
  }
}
```

- Keys carry only the fields they change (`x`, `y`, `rot`, `scale`); absent fields hold.
- `t` is seconds from clip start. Interpolation is linear unless a key says `"ease": "in"|"out"|"inout"`.
- A looping clip MUST have its last key equal its first on every track, or it pops. **Assert this at
  load time and name the offending track** — a silent pop is the kind of defect people describe as
  "it feels wrong" and nobody can locate.

---

## 4. Channels and blending

J: *"We can layer animations rather than treating every combination as a unique animation."*

```
BASE   whole-body motion            idle_breathe, idle_dance, wave, death_flop
HEAD   head/neck only               look_right, neutral
FACE   eyes, beak, visor            blink, sleepy, squint
PROP   what is in prop_anchor       hold_mobiglas, none
```

**Precedence is by specificity, not by layer order:** FACE beats HEAD beats BASE *on the bones each
owns*. A channel only ever writes the bones in its own set, so two channels cannot fight over one
bone. Declare each channel's bone set as data, and **assert the sets are disjoint at load** — if they
ever overlap, blending becomes order-dependent and the bug is invisible until two clips coincide.

`SKIN` is NOT a channel. It is an attachment set, and it never produces bone deltas.

---

## 5. Skins

J: *"dance.anim doesn't know that Pico is wearing Drake clothes."* That is the invariant. An
animation must never name a skin, and a skin must never name an animation.

A skin is a map from bone name to drawable attachments:
```json
{ "name": "DRAKE",
  "attach": { "torso": ["drake_jacket_body"], "jacket_L": ["drake_jacket_flap_l"],
              "head": ["drake_cap"], "prop_anchor": [] } }
```
Bones a skin does not mention simply have nothing attached. **A skin referencing a bone the skeleton
does not have is a load error, not a silent skip** — silently dropping it produces a penguin missing
one sleeve and no explanation.

Manufacturer *behaviour profiles* (J: Drake sloppier timing, Origin polishing the visor, ARGO
inspecting tools, Anvil saluting) are a separate map of timing multipliers and idle-picker weights.
They change WHICH clip plays and HOW FAST, never the clip contents.

---

## 6. Events

C turns `Game.log` lines into triggers. The interface is one callback, `on_trigger(name, payload)`.

J's example state machine:
```
quantum_start → quantum_brace → quantum_watch → quantum_exit_wobble → idle
```

⚠ **SHIP-FOLLOWING SKINS ARE NOT YET PROVEN POSSIBLE.** Measured 2026-09-27 against both live logs:
manufacturer prefixes are abundant (`RSI_Aurora`, `DRAK_Corsair`, `AEGS_Sabre_Raven`, `ORIG_m80`), so
ship→manufacturer is trivial. But those are `ShipATCDataManager` entries for ships in the AREA, and
across both logs there are **123 lines naming the player and ZERO tying the player to a vehicle**.
Both logs are `gamerules="SC_Frontend"` — menu sessions — so that absence is **uninformative**, not a
refutation. Needs one log from a session where a ship was actually boarded.
⇒ **Build user-selected Pico first.** It works regardless. Ship-following hangs off a single
`resolve_current_ship()` that is allowed to return None, and everything downstream must cope.

✅ **AND J CHOSE USER-SELECTED, 2026-09-27, before seeing the measurement:** *"I would let the player
customize their Pico and then it sticks to that outfit."* So sticky user choice is THE behaviour, and
ship-following is not a v1 requirement at all. The measurement above and his preference agree, which
is luck rather than planning — record both, because if he later wants ship-following the log question
is still open and still unanswered.

---

## 7. The discriminating tests

★★ **J ARRIVED AT THE SAME FIRST TEST INDEPENDENTLY, FROM THE ART SIDE.** His pack's "First proof"
reads: *"Bind Base Pico, then Drake. Test: 1. idle_breathe 2. blink 3. look_left/right 4. wave
5. dance 6. death_flop. If both skins run the six clips with identical animation data, freeze the rig
and proceed with remaining skins."* That is my test 1 (idle_breathe first) and my test 2 (a skin swap
must change nothing) written by someone who had not read this file. Convergence from two directions is
the strongest evidence either of us has that the tests are the right ones — so they are now BOTH the
engineering gate and J's acceptance criterion, and "freeze the rig" is his stated trigger.


From the July rig work, and it is the most valuable line in this file:

> *"DISCRIMINATING TEST: a breathing idle MUST retarget to arms-DOWN. Arms up = the object tilt is
> leaking. Do not trust a 'taunt looks like a taunt' check — arms-up looked plausibly taunt-ish and
> hid the bug. Test the case that can only pass if right."*

⛔⛔ **AND THE RATIONALE BELOW IS WRONG FOR AN AUTOMATED CHECK. MEASURED AND REFUTED 2026-09-27**
by the rig-runtime build, which ran 5 mutations across 3 clips and tabulated hand drift:

```
                    idle_breathe        dance          wave
contract            332 / 23.0      289 /  91.1    275 /  110.5
order_SRT           327 / 26.9      212 / 198.6    150 /  257.8
reversed_compose    265 / 82.8     -395 / 773.9   -821 / 1151.8
parent_relative     314 / 24.3      259 /  96.6    242 /  111.4
drop_parent_rot     349 /  6.0      349 /  14.0    349 /    0.0
accumulating        139 / 620.0      63 / 923.9    139 /  232.4
```
An arms-down FLOOR catches **2 of 5 on idle_breathe, 3 of 5 on dance, 4 of 5 on wave.** For a
numeric bound the expressive clip is MORE sensitive, not less — it AMPLIFIES the fault.

★ THE JULY LESSON IS ABOUT A **HUMAN** CHECK AND I OVER-GENERALISED IT. There it is correct, and for
  the stated reason: a person cannot hold dance's expected pose in their head, so "arms-up looked
  plausibly taunt-ish" and hid the bug. A numeric bound with a rest reference has no such limit. The
  failure was transplanting a rule about EYES onto a rule about ASSERTIONS without re-deriving it.

✅ `idle_breathe` REMAINS the fixture, for a better argument than mine: its envelope is **derivable
  from the clip before running anything** — 1.5° on spine_upper over a 340 px arm is ~23 px at the
  hands, so a band of [12, 25] is defensible in advance. dance's 91 px is a number nobody could
  predict, so any band around it encodes today's output rather than an expectation. **A tight band on
  a predictable clip beats a loose band on an unpredictable one.** That is the real principle;
  "test the boring clip" was a corollary that happened to hold for the wrong reason.
⇒ And the correction is pinned in live assertions (`test_gate.py`), not left as prose, so it cannot
  quietly rot back into the version written here first.

Applied here:

1. **`idle_breathe` must keep the flippers DOWN.** A transform-space or rotation-order bug produces a
   splayed penguin, and a splayed penguin still reads as "some animation" — plausibly a wave. The
   low-motion idle is the discriminating case precisely because it is boring. Do not validate the rig
   on `idle_dance`; an expressive clip hides everything.
2. **A skin swap mid-clip must not move a single bone.** Same animation, same frame, DRAKE vs base:
   assert the pose maps are identical. This is what proves the animation/skin separation is real
   rather than merely intended.
3. **Two channels on disjoint bone sets must commute.** Apply HEAD-then-FACE and FACE-then-HEAD, assert
   the same pose. If they differ, the sets overlap and precedence is silently order-dependent.
4. **A looping clip, played twice round, must return to its exact starting pose.** Catches drift from
   accumulating deltas instead of recomputing from rest.

---

## 8. Open, and not to be silently decided

- Authoring: nobody has said how `.anim` files get MADE. Hand-written JSON does not scale to J's list.
- The existing 12-frame sheet is raster, not a rig. Turning it into bone-bound pieces is a real art
  task and has not been scoped.
- Frame rate / tick source: Qt timer vs render-driven. Owner A decides and writes it here.
- ~~Whether the Assistant's listener penguin becomes Pico's first consumer.~~ **ANSWERED by J,
  2026-09-27: "So this is another tool entirely from the listener."** They are separate tools. The
  listener widget is NOT disposable and NOT a Pico prototype; Pico must not depend on it and must not
  try to absorb it. Two animations of the same character in two tools is the accepted design, not an
  oversight to consolidate later.

---

## 9. Settings UI — J's requirement, quoted

*"The user clicks on the tool and it has a setup for the pico and a prompt to right click on the pico
to re-open the settings. The settings box should open every time the tool is first launched so users
can't forget how to customize their pico."*

Three separable requirements, and the third is the one that is easy to get subtly wrong:

1. A setup/customise dialog, reachable from the tool.
2. **Right-click on Pico himself re-opens it.** So Pico is an interactive widget, not decoration —
   owner B must accept mouse events on the character, and that constrains the window flags if he is
   a frameless always-on-top overlay.
3. **The settings box opens on FIRST LAUNCH, every time.** Read that literally: "every time the tool
   is first launched" means first launch of the TOOL, i.e. once per app start — not once ever, and
   not on every show/hide of the panel. His stated reason is discoverability: *"so users can't forget
   how to customize their pico."*
   ⚠ Do NOT quietly turn this into a one-time "don't show again" flag. That is the obvious
     engineering instinct and it defeats the reason he gave. If it should become dismissible, that is
     his call to make later, not a default to assume.

---

## SPRITE PATH (2026-10-01) — a second renderer beside the rig, not a replacement

J, 2026-10-01: rendered key-frame loops over interpolated in-betweens ("Left is better"), then
"continue with the Pico Pals tool... wiring in the Drake Pico". So Pico can now run from WHOLE-BODY
LOOPS with the expression baked into each frame, while the bone rig (A, B, D) stays untouched.

```
  events.MoodSource  ->  MoodReading.mood  ->  pico/sprites.LoopChooser  ->  sprite_pal.py (Qt)
  (layer C, shared)                            (pure logic, no Qt)           (draws, nothing else)
```

* Loops live per outfit: `BrAi/_forJ/VNCCS/pico_anim_sequences/` (Drake) and
  `pico_anim_sequences_<brand>/`. The `.webp` is the desktop copy — transparent, every loop on one
  shared canvas so Pico does not jump between loops. The `.gif` is the flattened review copy.
* `MOOD_LOOPS` in `pico/sprites.py` maps each `face.DEFAULT_MOODS` name to a small pool of loops.
  A mood with no loop on disk is refused at load, never substituted at play time.
* **He rests (J, 2026-10-04: "He can be idle at times", "He also doesn't need to loop an animation
  8,000 times", "if you pull out a weapon he should still periodically idle").** An animation plays
  `ACT_PASSES` times, then he stands still for `REST_S` seconds; with a weapon out he holds it still for
  `HOLD_STILL_S`, puts it away for one idle, and takes it out again. A mood change or a game event ends
  a rest at once. Standing still is frame 0 of a loop (the same neutral stand in every outfit folder),
  so it needs no art: `LoopChooser.resting` asks, `sprite_pal.py` draws that one frame.
* **OPEN for J, my picks:**
  1. UNKNOWN plays `confused`. There is no blank-faced loop, and "I can't tell" is the honest claim.
  2. `sprite_pal` treats a Game.log untouched for 15 min as "game not running" -> UNKNOWN. Without
     it, the first live run showed a dancing penguin and "feed=live" from a log 4.8 DAYS old,
     because FeedState's staleness is deliberately undecided (section on FeedState in events.py).
  3. The pools themselves: which loops count as calm, alert, hurt, happy, startled, irritated.
* Flight/event loops (docking, quantum_jump, crash, fuel_low, ...) are NOT wired yet. They want
  Game.log EVENTS, not moods, and that mapping is the next piece.

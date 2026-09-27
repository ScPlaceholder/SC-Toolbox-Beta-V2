"""build_manifest.py — derive the asset manifest from the skeleton, never from memory.

⛔ THE SKELETON IS THE NAMING AUTHORITY AND THIS FILE RESTATES NOTHING. Slot names, their bones
   and their z-order are read from pico_skeleton.json at run time. The rig runtime already works
   this way (its commit message: "no bone list is restated in code"), and a manifest that hardcoded
   26 slot names would be a second copy of the truth, free to drift the first time a slot is renamed.

★ THE FIRST BATCH IS NOT "SOME IMAGES", IT IS J'S OWN FREEZE GATE. PICO_CONTRACT.md quotes him:
     "Bind Base Pico, then Drake. Test: 1. idle_breathe 2. blink 3. look_left/right 4. wave
      5. dance 6. death_flop. If both skins run the six clips with identical animation data,
      freeze the rig and proceed with remaining skins."
  So BASE + DRAKE is the whole first generation run. It is not a sample I invented to be careful
  with his money -- it is the smallest set that can answer the question he wants answered, and the
  remaining manufacturers are gated behind it passing. Generating all ~150 before the gate would
  pay for art that a failed gate might invalidate.

⚠ WHAT THIS FILE DOES NOT DO: it does not call an image API and it cannot spend anything. It emits
  JSON. The generator is a separate step, deliberately, so the manifest can be reviewed and the
  whole pipeline dry-run with the paid call stubbed.

    python build_manifest.py                 # write asset_manifest.json for BASE + DRAKE
    python build_manifest.py --skins ALL     # every manufacturer (gated: run the freeze gate first)
    python build_manifest.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKELETON = os.path.join(HERE, "pico", "rigdata", "pico_skeleton.json")
OUT = os.path.join(HERE, "assets", "asset_manifest.json")

# ── which slots belong to the CHARACTER and which to a WARDROBE ──────────────────────────
#
# A manufacturer skin does not redraw the penguin. Base Pico owns his own body; DRAKE owns the
# clothes laid over it. Splitting them here is what stops the generator being asked for "a Drake
# beak", which is the prompt that produces a penguin wearing a penguin.
# ⚠ Derived from the slot NAMES, and the mapping is stated rather than inferred, so an unrecognised
#   slot is a loud KeyError at build time instead of a silently mis-kinded asset at generation time.
SLOT_KIND = {
    "shadow":          ("base", "fx"),
    "body_back":       ("base", "body"),
    "backpack":        ("wardrobe", "prop"),
    "leg_L":           ("base", "body"),
    "leg_R":           ("base", "body"),
    "foot_L":          ("base", "body"),
    "foot_R":          ("base", "body"),
    "belly":           ("base", "body"),
    "shirt_or_hoodie": ("wardrobe", "jacket"),
    "jacket_back":     ("wardrobe", "jacket"),
    "head_base":       ("base", "head"),
    "eye_L":           ("base", "expression"),
    "eye_R":           ("base", "expression"),
    # ⛔ THIS WAS ("wardrobe", "headwear") AND IT WOULD HAVE LEFT PICO FACELESS.
    #   J's own rig/layer_manifest.json — which I did not have until he sent the master pack —
    #   lists "visor" under BASE, beside body, belly, head, beak and flippers. The style sheet
    #   says the same thing in pictures: the visor IS his face, sixteen expressions, always
    #   present. Classified as wardrobe, BASE would never have generated one and every single
    #   manufacturer skin would have generated its own.
    # ★★ AND MY "INDEPENDENT ORACLE" COULD NOT HAVE CAUGHT IT. The selftest's ANATOMY set is
    #   written out by hand precisely so a mutation to SLOT_KIND cannot fool it — and I wrote
    #   that set from the same wrong belief, so it omitted the visor too. Both sides agreed and
    #   both were wrong. The oracle was independent of the CODE and not independent of ME.
    #   A test whose expectation I author cannot find an error in my UNDERSTANDING; only an
    #   artefact from someone else can. That is the sharpest version of today's lesson.
    "visor":           ("base", "expression"),
    "beak":            ("base", "head"),
    "flipper_L":       ("base", "body"),
    "flipper_R":       ("base", "body"),
    "sleeve_L":        ("wardrobe", "sleeve"),
    "sleeve_R":        ("wardrobe", "sleeve"),
    "jacket_front":    ("wardrobe", "jacket"),
    "belt_gear":       ("wardrobe", "belt"),
    "headwear":        ("wardrobe", "headwear"),
    "prop_L":          ("wardrobe", "prop"),
    "prop_R":          ("wardrobe", "prop"),
    "prop_world":      ("wardrobe", "prop"),
    "fx_front":        ("base", "fx"),
}

# Manufacturers, in the order the contract's gate names them. BASE is not a manufacturer; it is
# the penguin, and every wardrobe hangs on it.
GATE_SKINS = ["BASE", "DRAKE"]
# The full roster from J's style sheet, which lists 14 manufacturers. I had 9 and had invented
# the list from memory of Star Citizen rather than reading his sheet; MIRAI, NAUTILUS, TUMBRIL,
# GATAC, ESPERIA and BANU were simply missing.
ALL_SKINS = ["BASE", "DRAKE", "RSI", "ANVIL", "AEGIS", "ORIGIN", "CRUSADER", "ARGO",
             "MISC", "MIRAI", "NAUTILUS", "TUMBRIL", "GATAC", "ESPERIA", "BANU"]

# ⛔ NEGATIVES ARE NOT DECORATION. J's pipeline notes fight one specific failure: the generator
#   drawing the whole character instead of the one part. asset_validate.py's coverage rule catches
#   it after the fact; these try to stop it happening. Kept identical across every part prompt so a
#   reject can never be blamed on an inconsistent negative.
# ⛔ THE TEXT NEGATIVE AND THE REMOVED "stencilled lettering" BOTH DATE FROM THE FIRST REAL IMAGE,
#   2026-09-27, AND I CAUSED THE DEFECT I THEN HAD TO FIX. The DRAKE flavour asked for "stencilled
#   lettering". The generator duly produced lettering and spelled it "DRRKE" and "WERPLANSTARY".
#   Image models cannot spell and I had invited them to try.
# ★ asset_validate CANNOT SEE THIS. Alpha 255 spread, coverage 21.6% inside the headwear band, zero
#   edge bleed — it PASSED. A structurally perfect asset with gibberish stamped across it, and no
#   mechanical check in this pipeline will ever catch that. It is exactly why the plan was one image
#   before twenty-six.
# ★ THIS LIST CAME FROM THE DESIGNER, NOT FROM ME, 2026-09-27. J pointed me at the ChatGPT
#   conversation where the reference art was made and told me to ask what it actually used. Two
#   things I would never have written myself:
#     - the part list is ENUMERATED (skull, eyes, beak, hair, neck) rather than summarised. My
#       "no head, no face" felt complete and was not; a generator that draws a beak has not drawn
#       a "head" by any definition it is using.
#     - "the inside or opening of the garment must be genuinely empty and transparent where the
#       head would go". A hat whose interior is filled looks perfect in isolation and cannot be
#       worn. No check I own measures the INSIDE of a shape.
#   ⚠ And: a drawn checkerboard is not transparency. Worth saying in the prompt because the
#     reference sheets are all rendered ON checkerboard, so the style examples actively invite it.
#   ⚠ "No visor" specifically: from the same quality gate — "a hat containing even part of
#     Pico's skull OR VISOR fails". My list covered head, face, body and mannequin and never
#     named the visor, which is the one part of Pico that is ALWAYS present and therefore the
#     part a generator is most likely to draw into a hat. The gap was invisible because my
#     list LOOKED complete.
NEGATIVES = (
             "No penguin. No head. No skull. No face. No eyes. No beak. No visor. No hair. No neck. "
             "No body. No other clothing. No mannequin. No figure wearing it. "
             "Nothing but the single item, floating, centred, on a fully transparent background. "
             "No drop shadow extending outside the object. No ground plane. No backdrop. "
             "No background gradient of any kind. No border. NO CHECKERBOARD PATTERN — a drawn "
             "checkerboard is not transparency. "
             "NO TEXT, no letters, no words, no logos, no writing of any kind anywhere; "
             "branding is composited separately from official logos. "
             "The inside or opening of the garment must be genuinely empty and transparent where "
             "the character's head or body would eventually go.")

# ⛔⛔ THIS SAID "Flat vector game art, clean bold outlines" UNTIL 2026-09-27, AND IT WAS IN ALL
#   26 PROMPTS. J's style sheet (assets/reference/pico_style_sheet_2026-09-27.jpg) shows Pico is
#   nothing of the kind: a soft-shaded 3D render, glossy, toy-like, with rounded forms and gentle
#   specular highlights. I invented a house style and shipped it into every prompt, and the first
#   real image came back in the wrong medium entirely.
# ★ NO CHECK I OWN COULD HAVE CAUGHT THAT. asset_validate measures alpha, coverage, edges and
#   dimensions — all of which a flawlessly-executed wrong-style asset passes. Style is a reference
#   image and a human, which is what the sheet now supplies.
STYLE = ("Soft-shaded stylised 3D render, glossy toy-like surfaces, rounded forms, gentle specular "
         "highlights, clean studio lighting from upper left, saturated sci-fi palette, readable at "
         "small size. Matches a collectible-figure look, NOT flat vector and NOT cel-shaded.")

MFR_FLAVOUR = {
    "BASE":     "",
    # ⛔ THIS SAID "orange and gunmetal" AND DRAKE IS BLACK AND INDUSTRIAL YELLOW.
    #   J sent the target render 2026-09-27 (assets/reference/drake_penguin_target_2026-09-27.jpg)
    #   after asking why my cap looked nothing like his example. I had built two theories about
    #   RENDERING — that the prose was fighting the reference, then that the grit words were the
    #   villain — spent two paid images testing them, and was wrong both times. The answer was a
    #   plain fact about the COLOURWAY that I had invented and never checked against anything.
    # ★ An orange leather cap and a black-and-yellow industrial one look completely different at
    #   identical treatment. Most of the gap he was pointing at was colour, not style, and I went
    #   looking in the hardest place first because that is where I had been working.
    # ⚠ AND THE WEATHERING IS CORRECT. Scratched paint, worn edges, scuffed panels are all in his
    #   target. "scrappy, worn edges" was never the problem; my second theory was as wrong as the
    #   first, and I nearly deleted the one part of this line that was right.
    "DRAKE":    "Drake Interplanetary: black and industrial yellow, scratched and scuffed paint, "
                "worn edges, exposed fasteners and buckles, hazard striping, nothing precious.",
    "AEGIS":    "Aegis Dynamics: military, hard angles, matte olive and steel, painted hazard chevrons.",
    "ORIGIN":   "Origin Jumpworks: luxury, white and gold, seamless panels, polished.",
    "ANVIL":    "Anvil Aerospace: utilitarian military, olive drab, rivets, patch pockets.",
    "MISC":     "MISC: Xi'an-influenced curves, teal and bone, smooth organic shells.",
    "CRUSADER": "Crusader Industries: clean civilian aerospace, blue and white, rounded.",
    "RSI":      "Roberts Space Industries: institutional, navy and silver, formal.",
    "ARGO":     "ARGO Astronautics: blunt industrial workhorse, yellow and black hazard striping.",
}

PART_PROMPT = {
    "body": "the {part} of a small cartoon penguin character, side-neutral game sprite part",
    "head": "the {part} of a small cartoon penguin character",
    "expression": "a single {part} for a cartoon penguin character",
    "jacket": "a {mfr_short} jacket piece ({part}) sized to fit a small round penguin torso",
    "sleeve": "a single {mfr_short} jacket {part} sized for a short penguin flipper",
    "belt": "a {mfr_short} utility belt with pouches, sized for a small round penguin waist",
    # ⚠ A CAP, NOT A HELMET. I generated DRAKE headwear as a combat helmet; the style sheet shows
    #   a soft peaked CAP worn over a jacket. The manufacturer skins are CLOTHING, not armour, and
    #   the slot name "headwear" was generic enough to let me pick the wrong garment entirely.
    "headwear": "a soft peaked {mfr_short} cap sized to sit on a small penguin head",
    "prop": "a {mfr_short} handheld {part} prop, sized for a small penguin to hold",
    "fx": "a soft {part} element",
}


def load_slots(path=SKELETON):
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    slots = d.get("slots") or []
    if not slots:
        raise SystemExit("REFUSING: the skeleton declares no slots — nothing to build a manifest from")
    unknown = [s["name"] for s in slots if s["name"] not in SLOT_KIND]
    if unknown:
        # ⚠ LOUD, not skipped. A slot nobody classified would otherwise get a default kind and a
        #   coverage band that means nothing, and the failure would surface as a mysterious reject.
        raise SystemExit("REFUSING: %d slot(s) in the skeleton have no kind mapping: %s\n"
                         "  Add them to SLOT_KIND — do not let them default."
                         % (len(unknown), ", ".join(unknown)))
    return slots, d.get("canvas")


def build(skins):
    slots, canvas = load_slots()
    assets = []
    for skin in skins:
        for s in slots:
            owner, kind = SLOT_KIND[s["name"]]
            if skin == "BASE" and owner != "base":
                continue
            if skin != "BASE" and owner != "wardrobe":
                continue
            part = s["name"].replace("_L", " left").replace("_R", " right").replace("_", " ")
            short = skin.title() if skin != "BASE" else ""
            body = PART_PROMPT[kind].format(part=part, mfr_short=short).strip()
            flavour = MFR_FLAVOUR.get(skin, "")
            assets.append({
                "skin": skin,
                "slot": s["name"],
                "bone": s["bone"],
                "z": s["z"],
                "kind": kind,
                "path": "skins/%s/%s.png" % (skin, s["name"]),
                "prompt": " ".join(x for x in (body + ".", flavour, STYLE, NEGATIVES) if x),
            })
    return {
        "generated_from": os.path.basename(SKELETON),
        "canvas": canvas,
        "skins": skins,
        "gate": ("BASE + DRAKE is J's freeze gate from PICO_CONTRACT.md. Run the six clips on both "
                 "before generating any other manufacturer."),
        "count": len(assets),
        "assets": assets,
    }


def _selftest():
    fails = []
    slots, _ = load_slots()
    if len(slots) != len(SLOT_KIND):
        fails.append("skeleton has %d slots, SLOT_KIND maps %d — they must agree"
                     % (len(slots), len(SLOT_KIND)))

    m = build(GATE_SKINS)
    base = [a for a in m["assets"] if a["skin"] == "BASE"]
    drake = [a for a in m["assets"] if a["skin"] == "DRAKE"]
    if not base or not drake:
        fails.append("the gate must produce BOTH skins, got base=%d drake=%d" % (len(base), len(drake)))

    # ⛔⛔ THIS CHECK WAS INERT FOR TWENTY MINUTES AND MUTATION TESTING IS THE ONLY REASON I KNOW.
    #   It used to assert the two skins were DISJOINT. Moving "beak" from base to wardrobe — the
    #   exact defect it was written to catch, because it makes the generator draw "a Drake beak" —
    #   keeps them perfectly disjoint. The slot simply moves to the other side. Measured: the
    #   mutant produced 13 base + 13 drake and the suite PASSED.
    # ★ Disjointness was the wrong PROPERTY. The risk is not "a slot in both skins", it is
    #   "an ANATOMY slot inside a manufacturer skin". [[a-correct-rule-can-guard-a-branch-nothing-takes]]
    # ★★ AND THE ORACLE MUST NOT COME FROM SLOT_KIND. Deriving "what counts as anatomy" from the
    #   table under test makes the check circular: the same mutation that moves the beak also moves
    #   the definition, and it passes again. So the anatomy list is written out HERE, independently,
    #   and this test fails if the two ever disagree. A test that reads its expectation from the
    #   code it is testing cannot fail. [[a-differential-test-with-a-broken-control-scores-perfect]]
    ANATOMY = {
        "shadow", "body_back", "leg_L", "leg_R", "foot_L", "foot_R", "belly",
        "head_base", "eye_L", "eye_R", "beak", "visor", "flipper_L", "flipper_R",
        "fx_front",
    }
    intruders = sorted({a["slot"] for a in drake} & ANATOMY)
    if intruders:
        fails.append("manufacturer skin DRAKE contains anatomy slot(s) %s — that asks the generator "
                     "for 'a Drake beak', which produces a penguin wearing a penguin" % intruders)
    missing = sorted(ANATOMY - {a["slot"] for a in base})
    if missing:
        fails.append("BASE is missing anatomy slot(s) %s — the penguin would render incomplete"
                     % missing)

    # every asset must carry the negatives and a band-able kind
    import importlib.util
    spec = importlib.util.spec_from_file_location("av", os.path.join(HERE, "asset_validate.py"))
    av = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(av)
    for a in m["assets"]:
        if "No penguin." not in a["prompt"]:
            fails.append("%s/%s lost the negatives" % (a["skin"], a["slot"]))
            break
    unknown_kind = sorted({a["kind"] for a in m["assets"]} - set(av.COVERAGE))
    if unknown_kind:
        fails.append("kind(s) %s have no coverage band in asset_validate.COVERAGE — the validator "
                     "would fall back to 'unknown', whose band is almost meaningless" % unknown_kind)

    # paths must be unique, or one generation silently overwrites another
    paths = [a["path"] for a in m["assets"]]
    if len(paths) != len(set(paths)):
        fails.append("duplicate output path(s) — a later asset would overwrite an earlier one")

    print("build_manifest selftest:", "PASS" if not fails else "FAIL",
          "(%d checks, %d asset(s) in the gate: %d base + %d drake)"
          % (6, len(m["assets"]), len(base), len(drake)))
    for f in fails:
        print("   -", f)
    return 1 if fails else 0


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--skins", default="GATE", help="GATE (BASE+DRAKE, the default) or ALL")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return _selftest()

    skins = ALL_SKINS if a.skins.upper() == "ALL" else GATE_SKINS
    m = build(skins)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(m, fh, indent=2, ensure_ascii=False)
    print("wrote %s" % a.out)
    print("  %d asset(s) across %s" % (m["count"], ", ".join(skins)))
    by = {}
    for x in m["assets"]:
        by[x["skin"]] = by.get(x["skin"], 0) + 1
    for k, v in by.items():
        print("     %-9s %d" % (k, v))
    if skins is GATE_SKINS:
        print("  gate: %s" % m["gate"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

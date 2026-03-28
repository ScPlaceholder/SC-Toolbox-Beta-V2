"""
Canonical ship SCU database — single source of truth.

Values are from trade_hub_app.py (most complete list) with corrections:
  - Hull D: 6912 (confirmed)
  - Perseus: 96, Hammerhead: 40 (were 0 in route_engine.py)
  - Avenger Titan: 8 (game-accurate; removed ambiguous "titan": 12)
  - Ironclad: 2204, Ironclad Assault: 1440
  - Starfarer: 291, Merchantman: 2880
  - A2 Hercules: 234, Valkyrie: 90, Fortune: 12
"""
from typing import List, Tuple

# lowercase ship name -> SCU capacity
SHIP_PRESETS: dict = {
    # 1-3 SCU
    "reliant tana": 1,
    "100i": 2, "125a": 2, "cutter rambler": 2, "cutter scout": 2,
    "f7c hornet mk i": 2, "hornet": 2, "mpuv cargo": 2, "mpuv": 2,
    "redeemer": 2, "reliant sen": 2,
    "aurora es": 3, "aurora ln": 3, "aurora lx": 3, "aurora mr": 3, "aurora": 3,
    # 4-11 SCU
    "325a": 4, "350r": 4, "c8 pisces": 4, "c8x pisces expedition": 4, "pisces": 4,
    "cutter": 4, "mustang alpha": 4, "mustang": 4, "paladin": 4,
    "135c": 6, "aurora cl": 6, "reliant kore": 6, "reliant": 6,
    "salvation": 6, "syulen": 6,
    "300i": 8, "avenger titan": 8, "avenger titan renegade": 8, "avenger": 8,
    "intrepid": 8,
    # 12-23 SCU
    "315p": 12, "clipper": 12, "cutlass blue": 12, "cutlass red": 12,
    "fortune": 12, "misc fortune": 12, "srv": 12, "vulcan": 12, "vulture": 12,
    "mpuv tractor": 16, "zeus mk ii mr": 16, "zeus mr": 16,
    "600i touring": 20,
    # 24-63 SCU
    "nomad": 24,
    "valkyrie liberator edition": 30,
    "apollo medivac": 32, "apollo triage": 32, "apollo": 32,
    "golem": 32, "prospector": 32, "prowler utility": 32, "prowler": 32,
    "shiv": 32, "zeus mk ii es": 32, "zeus es": 32,
    "freelancer dur": 36, "freelancer mis": 36,
    "600i executive edition": 40, "600i executive": 40,
    "hammerhead": 40,
    "400i": 42,
    "600i explorer": 44, "600i": 44,
    "cutlass black": 46, "cutlass": 46,
    # 64-127 SCU
    "c1 spirit": 64, "spirit": 64, "galaxy": 64, "galaxy base": 64,
    "galaxy cargo": 576, "galaxy cargo module": 576,
    "golem ox": 64, "hull a": 64, "nautilus": 64,
    "freelancer": 66,
    "corsair": 72, "drake corsair": 72,
    "retaliator": 74, "retaliator bomber": 74,
    "constellation phoenix": 80, "constellation phoenix emerald": 80, "phoenix": 80,
    "valkyrie": 90,
    "constellation andromeda": 96, "andromeda": 96, "connie": 96,
    "constellation aquila": 96, "aquila": 96,
    "mole": 96, "perseus": 96, "starlancer tac": 96,
    "mercury star runner": 114, "msr": 114,
    "freelancer max": 120,
    # 128-299 SCU
    "expanse": 128, "zeus mk ii cl": 128, "zeus cl": 128,
    "constellation taurus": 174, "taurus": 174,
    "asgard": 180, "reclaimer best in show edition": 180, "reclaimer bis": 180,
    "raft": 192, "argo raft": 192,
    "starlancer max": 224,
    "a2 hercules starlifter": 234, "a2 hercules": 234, "a2": 234,
    "crucible": 240, "odyssey": 252,
    "hermes": 288, "rsi hermes": 288,
    # 291-576 SCU
    "starfarer": 291, "starfarer gemini": 291,
    "genesis starliner": 300, "genesis": 300,
    "railen": 320,
    "orion": 384, "890 jump": 388, "890jump": 388,
    "liberator": 400, "reclaimer": 420,
    "carrack": 456, "carrack expedition": 456,
    "endeavor": 500,
    "hull b": 512,
    "m2 hercules starlifter": 522, "m2 hercules": 522, "m2": 522,
    "arrastra": 576, "caterpillar": 576, "polaris": 576,
    # 696-1374 SCU
    "c2 hercules starlifter": 696, "c2 hercules": 696, "c2": 696,
    "kraken privateer": 768,
    "pioneer": 1000,
    "idris-m": 1168, "idris m": 1168,
    "idris-p": 1374, "idris p": 1374, "idris": 1374,
    # Capital
    "ironclad assault": 1440,
    "ironclad": 2204,
    "merchantman": 2880, "banu merchantman": 2880,
    "kraken": 3792,
    "hull c": 4608,
    "javelin": 5400,
    "hull d": 6912,
    "hull e": 12000,
}


def scu_for_ship(name: str) -> int:
    """Return SCU capacity for a ship name.

    Tries exact match first, then longest partial match.  Returns 0 if unknown.
    """
    if not name:
        return 0
    key = name.lower().strip()
    # Exact match
    if key in SHIP_PRESETS:
        return SHIP_PRESETS[key]
    # Best overlap match (how much of the query actually matched)
    best_key = ""
    best_scu = 0
    best_overlap = 0
    tied = False
    for preset, scu in SHIP_PRESETS.items():
        if preset in key:
            overlap = len(preset)
        elif key in preset:
            overlap = len(key)
        else:
            continue
        if overlap < 3:
            continue  # Skip very short partial matches (e.g. "sv" matching "srv")
        if overlap > best_overlap:
            best_overlap = overlap
            best_key = preset
            best_scu = scu
            tied = False
        elif overlap == best_overlap and len(preset) == len(best_key) and scu != best_scu:
            tied = True
    return 0 if tied else best_scu


# Display-cased ship names for UI dropdowns (from trade_hub_app.py QUICK_SHIPS)
# TODO: generate QUICK_SHIPS from SHIP_PRESETS programmatically
QUICK_SHIPS: List[Tuple[str, str]] = [
    ("",                              "-- No Ship Cap --"),
    # 1-3 SCU
    ("Reliant Tana",                  "Reliant Tana (1 SCU)"),
    ("100i",                          "100i (2 SCU)"),
    ("125a",                          "125a (2 SCU)"),
    ("Cutter Rambler",                "Cutter Rambler (2 SCU)"),
    ("Cutter Scout",                  "Cutter Scout (2 SCU)"),
    ("F7C Hornet Mk I",              "F7C Hornet Mk I (2 SCU)"),
    ("MPUV Cargo",                    "MPUV Cargo (2 SCU)"),
    ("Redeemer",                      "Redeemer (2 SCU)"),
    ("Reliant Sen",                   "Reliant Sen (2 SCU)"),
    ("Aurora ES",                     "Aurora ES (3 SCU)"),
    ("Aurora LN",                     "Aurora LN (3 SCU)"),
    ("Aurora LX",                     "Aurora LX (3 SCU)"),
    ("Aurora MR",                     "Aurora MR (3 SCU)"),
    # 4-11 SCU
    ("325a",                          "325a (4 SCU)"),
    ("350r",                          "350r (4 SCU)"),
    ("C8 Pisces",                     "C8 Pisces (4 SCU)"),
    ("C8X Pisces Expedition",         "C8X Pisces Expedition (4 SCU)"),
    ("Cutter",                        "Cutter (4 SCU)"),
    ("Mustang Alpha",                 "Mustang Alpha (4 SCU)"),
    ("Paladin",                       "Paladin (4 SCU)"),
    ("135c",                          "135c (6 SCU)"),
    ("Aurora CL",                     "Aurora CL (6 SCU)"),
    ("Reliant Kore",                  "Reliant Kore (6 SCU)"),
    ("Salvation",                     "Salvation (6 SCU)"),
    ("Syulen",                        "Syulen (6 SCU)"),
    ("300i",                          "300i (8 SCU)"),
    ("Avenger Titan",                 "Avenger Titan (8 SCU)"),
    ("Avenger Titan Renegade",        "Avenger Titan Renegade (8 SCU)"),
    ("Intrepid",                      "Intrepid (8 SCU)"),
    # 12-23 SCU
    ("315p",                          "315p (12 SCU)"),
    ("Clipper",                       "Clipper (12 SCU)"),
    ("Cutlass Blue",                  "Cutlass Blue (12 SCU)"),
    ("Cutlass Red",                   "Cutlass Red (12 SCU)"),
    ("Fortune",                       "Fortune (12 SCU)"),
    ("SRV",                           "SRV (12 SCU)"),
    ("Vulcan",                        "Vulcan (12 SCU)"),
    ("Vulture",                       "Vulture (12 SCU)"),
    ("MPUV Tractor",                  "MPUV Tractor (16 SCU)"),
    ("Zeus Mk II MR",                 "Zeus Mk II MR (16 SCU)"),
    ("600i Touring",                  "600i Touring (20 SCU)"),
    # 24-63 SCU
    ("Nomad",                         "Nomad (24 SCU)"),
    ("Valkyrie Liberator Edition",    "Valkyrie Liberator Edition (30 SCU)"),
    ("Apollo Medivac",                "Apollo Medivac (32 SCU)"),
    ("Apollo Triage",                 "Apollo Triage (32 SCU)"),
    ("Golem",                         "Golem (32 SCU)"),
    ("Prospector",                    "Prospector (32 SCU)"),
    ("Prowler Utility",               "Prowler Utility (32 SCU)"),
    ("Shiv",                          "Shiv (32 SCU)"),
    ("Zeus Mk II ES",                 "Zeus Mk II ES (32 SCU)"),
    ("Freelancer DUR",                "Freelancer DUR (36 SCU)"),
    ("Freelancer MIS",                "Freelancer MIS (36 SCU)"),
    ("600i Executive Edition",        "600i Executive Edition (40 SCU)"),
    ("Hammerhead",                    "Hammerhead (40 SCU)"),
    ("400i",                          "400i (42 SCU)"),
    ("600i Explorer",                 "600i Explorer (44 SCU)"),
    ("Cutlass Black",                 "Cutlass Black (46 SCU)"),
    # 64-127 SCU
    ("C1 Spirit",                     "C1 Spirit (64 SCU)"),
    ("Galaxy",                        "Galaxy Base (64 SCU)"),
    ("Galaxy Cargo",                  "Galaxy Cargo Module (576 SCU)"),
    ("Golem Ox",                      "Golem Ox (64 SCU)"),
    ("Hull A",                        "Hull A (64 SCU)"),
    ("Nautilus",                      "Nautilus (64 SCU)"),
    ("Freelancer",                    "Freelancer (66 SCU)"),
    ("Corsair",                       "Corsair (72 SCU)"),
    ("Retaliator",                    "Retaliator (74 SCU)"),
    ("Retaliator Bomber",             "Retaliator Bomber (74 SCU)"),
    ("Constellation Phoenix",         "Constellation Phoenix (80 SCU)"),
    ("Constellation Phoenix Emerald", "Constellation Phoenix Emerald (80 SCU)"),
    ("Valkyrie",                      "Valkyrie (90 SCU)"),
    ("Constellation Andromeda",       "Constellation Andromeda (96 SCU)"),
    ("Constellation Aquila",          "Constellation Aquila (96 SCU)"),
    ("MOLE",                          "MOLE (96 SCU)"),
    ("Perseus",                       "Perseus (96 SCU)"),
    ("Starlancer TAC",                "Starlancer TAC (96 SCU)"),
    ("Mercury Star Runner",           "Mercury Star Runner (114 SCU)"),
    ("Freelancer MAX",                "Freelancer MAX (120 SCU)"),
    # 128-299 SCU
    ("Expanse",                       "Expanse (128 SCU)"),
    ("Zeus Mk II CL",                 "Zeus Mk II CL (128 SCU)"),
    ("Constellation Taurus",          "Constellation Taurus (174 SCU)"),
    ("Asgard",                        "Asgard (180 SCU)"),
    ("Reclaimer Best In Show Edition","Reclaimer BIS (180 SCU)"),
    ("RAFT",                          "RAFT (192 SCU)"),
    ("Starlancer MAX",                "Starlancer MAX (224 SCU)"),
    ("A2 Hercules Starlifter",        "A2 Hercules Starlifter (234 SCU)"),
    ("Crucible",                      "Crucible (240 SCU)"),
    ("Odyssey",                       "Odyssey (252 SCU)"),
    ("Hermes",                        "Hermes (288 SCU)"),
    # 291-576 SCU
    ("Starfarer",                     "Starfarer (291 SCU)"),
    ("Starfarer Gemini",              "Starfarer Gemini (291 SCU)"),
    ("Genesis Starliner",             "Genesis Starliner (300 SCU)"),
    ("Railen",                        "Railen (320 SCU)"),
    ("Orion",                         "Orion (384 SCU)"),
    ("890 Jump",                      "890 Jump (388 SCU)"),
    ("Liberator",                     "Liberator (400 SCU)"),
    ("Reclaimer",                     "Reclaimer (420 SCU)"),
    ("Carrack",                       "Carrack (456 SCU)"),
    ("Carrack Expedition",            "Carrack Expedition (456 SCU)"),
    ("Endeavor",                      "Endeavor (500 SCU)"),
    ("Hull B",                        "Hull B (512 SCU)"),
    ("M2 Hercules Starlifter",        "M2 Hercules Starlifter (522 SCU)"),
    ("Arrastra",                      "Arrastra (576 SCU)"),
    ("Caterpillar",                   "Caterpillar (576 SCU)"),
    ("Polaris",                       "Polaris (576 SCU)"),
    # 696-1374 SCU
    ("C2 Hercules Starlifter",        "C2 Hercules Starlifter (696 SCU)"),
    ("Kraken Privateer",              "Kraken Privateer (768 SCU)"),
    ("Pioneer",                       "Pioneer (1,000 SCU)"),
    ("Idris-M",                       "Idris-M (1,168 SCU)"),
    ("Idris-P",                       "Idris-P (1,374 SCU)"),
    # Capital
    ("Ironclad Assault",              "Ironclad Assault (1,440 SCU)"),
    ("Ironclad",                      "Ironclad (2,204 SCU)"),
    ("Merchantman",                   "Merchantman (2,880 SCU)"),
    ("Kraken",                        "Kraken (3,792 SCU)"),
    ("Hull C",                        "Hull C (4,608 SCU)"),
    ("Javelin",                       "Javelin (5,400 SCU)"),
    ("Hull D",                        "Hull D (6,912 SCU)"),
    ("Hull E",                        "Hull E (12,000 SCU)"),
]

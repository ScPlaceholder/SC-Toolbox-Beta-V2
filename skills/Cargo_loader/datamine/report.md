# Cargo grid diff - scunpacked-data 4.10.1-LIVE.12660092 vs loader cache

Source commit: `e96132078ae6a1a5f62a183fb1523dc006dcfddb`. 1 SCU cell = 1.25 m.

## Axis mapping evidence (all 6 permutations, ships present in both sources)

| mapping (w,h,l <- files) | exact shape matches | grids matched | total-SCU matches | ships compared |
|---|---|---|---|
| X,Z,Y | 64 | 193 | 86 | 87 |
| Z,X,Y | 24 | 120 | 86 | 87 |
| Y,Z,X | 16 | 113 | 86 | 87 |
| Z,Y,X | 6 | 64 | 86 | 87 |
| Y,X,Z | 5 | 68 | 86 | 87 |
| X,Y,Z | 5 | 57 | 86 | 87 |

Chosen mapping: **width=X, height=Z, length=Y** (file axes).

## Summary counts

- MATCH_ALL (grid shapes + total SCU agree): **64**
- SCU_ONLY (total SCU agrees, grid shapes differ): **22**
- DIFF (SCU differs): **1**
- only in loader (sc-cargo.space): **18**
- only in scunpacked: **39**

## SCU_ONLY

### 600i Touring
- loader SCU 20 (1x2x4, 1x2x4, 1x2x1, 1x2x1)
- files  SCU 20 (4x2x1, 4x2x1, 1x2x1, 1x2x1)
- files MaxSize per grid: null, null, null, null

### Caterpillar
- loader SCU 576 (5x2x6, 5x1x4, 1x2x4, 6x4x4, 5x4x1, 1x2x4, 6x4x4, 5x4x1, 1x2x4, 6x4x4, 5x4x1, 1x2x4, 6x4x4, 5x4x1)
- files  SCU 576 (5x3x4, 5x2x2, 4x4x6, 4x2x1, 1x4x5, 4x4x6, 4x2x1, 1x4x5, 4x4x6, 4x2x1, 1x4x5, 4x4x6, 4x2x1, 1x4x5)
- files MaxSize per grid: null, null, null, null, null, null, null, null, null, null, null, null, null, null

### Corsair
- loader SCU 72 (4x3x6)
- files  SCU 72 (4x2x9)
- files MaxSize per grid: null

### Cutter
- loader SCU 4 (1x2x2)
- files  SCU 4 (2x2x1)
- files MaxSize per grid: null

### Fortune
- loader SCU 16 (1x2x3, 1x2x3, 2x1x2)
- files  SCU 16 (2x1x2, 2x1x3, 2x1x3)
- files MaxSize per grid: null, null, null

### Freelancer
- loader SCU 66 (1x3x2, 1x3x2, 2x3x9)
- files  SCU 66 (2x3x9, 2x3x1, 2x3x1)
- files MaxSize per grid: null, null, null

### Freelancer DUR
- loader SCU 36 (1x3x2, 1x3x2, 2x3x4)
- files  SCU 36 (2x3x4, 2x3x1, 2x3x1)
- files MaxSize per grid: null, null, null

### Freelancer MAX
- loader SCU 120 (1x3x2, 1x3x2, 4x3x9)
- files  SCU 120 (4x3x9, 2x3x1, 2x3x1)
- files MaxSize per grid: null, null, null

### Freelancer MIS
- loader SCU 36 (1x3x2, 1x3x2, 2x3x4)
- files  SCU 36 (2x3x4, 2x3x1, 2x3x1)
- files MaxSize per grid: null, null, null

### Hull-A
- loader SCU 64 (2x4x4, 2x4x4)
- files  SCU 64 (2x2x4, 2x2x4, 2x2x4, 2x2x4)
- files MaxSize per grid: null, null, null, null

### Hull-C
- loader SCU 4608 (12x6x8, 12x6x8, 12x6x8, 12x6x8, 12x6x8, 12x6x8, 12x6x8, 12x6x8)
- files  SCU 4608 (8x6x8, 8x6x8, 8x6x8, 8x6x8, 8x6x8, 8x6x8, 8x6x8, 8x6x8, 4x6x8, 4x6x8, 4x6x8, 4x6x8, 4x6x8, 4x6x8, 4x6x8, 4x6x8)
- files MaxSize per grid: null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null

### Idris-M
- loader SCU 1326 (4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 18x2x4, 14x2x5, 2x2x1, 5x2x1, 2x2x1)
- files  SCU 1326 (4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 1x2x5, 1x2x2, 1x2x2, 14x2x5, 18x2x4)
- files MaxSize per grid: null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null

### Idris-P
- loader SCU 1374 (4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 18x2x4, 14x2x5, 2x2x1, 5x2x1, 2x2x1, 3x2x4, 3x2x4)
- files  SCU 1374 (4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 4x4x4, 1x2x5, 1x2x2, 1x2x2, 14x2x5, 18x2x4, 3x2x2, 3x2x2, 3x2x2, 3x2x2)
- files MaxSize per grid: null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null, null

### MOTH
- loader SCU 224 (4x4x6, 4x4x6, 4x2x4)
- files  SCU 224 (4x2x4, 4x4x6, 6x4x4)
- files MaxSize per grid: null, null, null

### Mustang Alpha
- loader SCU 4 (1x1x4)
- files  SCU 4 (4x1x1)
- files MaxSize per grid: null

### Railen
- loader SCU 640 (4x4x8, 4x4x8, 4x4x8, 4x4x8, 2x4x8, 2x4x8)
- files  SCU 640 (4x2x8, 4x2x8, 4x4x8, 4x4x8, 4x4x8, 4x4x8)
- files MaxSize per grid: null, null, null, null, null, null

### Reclaimer
- loader SCU 420 (5x2x7, 5x2x7, 5x2x5, 5x2x5, 3x3x2, 3x3x3, 3x3x3, 3x3x2, 3x3x2, 3x3x3, 3x3x3, 3x3x2)
- files  SCU 420 (3x3x2, 3x3x2, 3x3x2, 3x3x2, 3x3x3, 3x3x3, 3x3x3, 3x3x3, 7x2x5, 7x2x5, 5x2x5, 5x2x5)
- files MaxSize per grid: null, null, null, null, null, null, null, null, null, null, null, null

### Reliant Kore
- loader SCU 6 (1x1x3, 1x1x3)
- files  SCU 6 (3x1x1, 3x1x1)
- files MaxSize per grid: null, null

### Starfarer
- loader SCU 291 (5x5x7, 2x2x2, 2x5x5, 2x2x2, 2x5x5)
- files  SCU 291 (5x5x7, 5x5x2, 2x2x2, 5x5x2, 2x2x2)
- files MaxSize per grid: null, null, null, null, null

### Starfarer Gemini
- loader SCU 291 (5x5x7, 2x2x2, 2x5x5, 2x2x2, 2x5x5)
- files  SCU 291 (5x5x7, 5x5x2, 2x2x2, 5x5x2, 2x2x2)
- files MaxSize per grid: null, null, null, null, null

### Syulen
- loader SCU 6 (3x1x2)
- files  SCU 6 (1x1x1, 1x1x1, 1x1x1, 1x1x1, 1x1x1, 1x1x1)
- files MaxSize per grid: null, null, null, null, null, null

### Zeus Mk II CL
- loader SCU 128 (5x3x8, 1x2x2, 1x2x2)
- files  SCU 128 (5x3x8, 2x2x1, 2x2x1)
- files MaxSize per grid: null, null, null

## DIFF

### Vulture
- loader SCU 13 (1x1x1, 2x2x3)
- files  SCU 12 (2x2x3)
- files MaxSize per grid: null

## Only in loader (not in scunpacked ships.json)

- 600i Explorer
- Arrastra [Concept]
- Aurora (Mk II) [Cargo]
- Aurora CL (Mk I)
- Aurora ES (Mk I)
- Aurora LN (Mk I)
- Aurora LX (Mk I)
- Aurora MR (Mk I)
- F7C Hornet (Mk I)
- Galaxy [Base Concept]
- Galaxy [Cargo Concept]
- Hull-D [Concept]
- Hull-E [Concept]
- Liberator [Concept]
- Merchantman [Concept]
- Pioneer [Concept]
- Retaliator
- Zeus Mk II MR [Concept]

## Only in scunpacked (not in loader cache)

- ARGO MOLE Alliance
- Aegis Avenger Titan Renegade
- Aegis Idris-P Wikelo War Special
- Aegis Reclaimer 2949 Best In Show Edition
- Aegis Reclaimer Teach's Special
- Anvil Asgard Wikelo War Special
- Anvil Carrack Expedition
- Argo MOLE Teach's Special
- Argo RAFT Wikelo Work Special
- C.O. Mustang CitizenCon 2948 Edition
- Corsair PYAM Exec
- Crusader A2 Hercules Starlifter Wikelo War Special
- Crusader C1 Spirit Wikelo Special
- Crusader Intrepid Wikelo Work Special
- Cutlass Black PYAM Exec
- Drake Caterpillar Pirate
- Drake Clipper Wikelo War Special
- Drake Vulture Teach's Special
- Esperia Prowler Utility Wikelo Work Special
- Greycat UTV
- MISC Fortune Teach's Special
- MISC Fortune Wikelo Special
- MISC Starfarer Teach's Special
- MISC Starlancer MAX Wikelo Work Special
- MISC Starlancer TAC Wikelo War Special
- Origin 600i
- Origin 600i 2951 BIS
- Origin 600i Executive Edition
- RSI Apollo Triage Wikelo Sneak Special
- RSI Aurora Mk I  LX
- RSI Aurora Mk I CL
- RSI Aurora Mk I ES
- RSI Aurora Mk I LN
- RSI Aurora Mk I MR
- RSI Aurora Mk I SE
- RSI Constellation Phoenix Emerald
- RSI Constellation Taurus Wikelo War Special
- RSI Zeus Mk II ES Wikelo Work Special
- Syulen PYAM Exec

_Attribution: Ship data: StarCitizenWiki/scunpacked-data. Star Citizen content (c) Cloud Imperium Games._
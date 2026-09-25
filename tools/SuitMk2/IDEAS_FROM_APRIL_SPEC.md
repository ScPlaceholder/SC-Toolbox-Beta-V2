# Ideas worth keeping from the April 2026 Suit AI spec

The original design spec (`skills/Trade_Hub/data/suit_ai_companion_prompt.md`, 5,328 lines, by J, commit 4665bf0) was compared section by section against SuitMk2 on 2026-09-25. Most of it has been superseded by SuitMk2's own design (decaying emotions instead of personality sliders, pacing and speak gate instead of throttle knobs, local models instead of a paid-API budget, vision instead of unloggable transit events). The spec was then removed from the tree; it is still in git history at 4665bf0.

These six sections are the parts SuitMk2 does NOT have yet. They are copied verbatim below; each is an idea to build, not something already wired.

---

## 1. NPC faction names (log pattern -> spoken name)

_Why keep it:_ SuitMk2 has no log-ID to name mapping for enemies. Small, cheap to port next to location_names.py.

_Source: spec lines 5115-5126._

## NPC Faction Translation

| Internal Pattern | Natural Name |
|---|---|
| `NineTails` | Nine Tails pirates |
| `ASD_soldier` / `ASD_grunt` | abandoned station hostiles |
| `Headhunters` | Headhunters |
| `pyro_outlaw` | Pyro outlaws |
| `Criminal-Pilot` / `Criminal-Gunner` | hostile pilot / gunner |
| `Kopion` | Kopion (alien wildlife) |

---

---

## 2. Med-pen overdose tracker (simulated BDL)

_Why keep it:_ SuitMk2 only sees the med pen being equipped (event_classifier.py). This design tracks consumption vs holstering by entity ID, decays the level over time, and warns at 60/80/100. The constants are the spec's own estimates and need tuning in game.

_Source: spec lines 3377-3471._

#### 5.1 BDL TRACKER (Simulated Blood Drug Level)

The game log does not expose a numeric BDL value, but the AI can **simulate** it by
tracking med pen consumption, natural decay, and reset events. Every `AttachmentReceived`
event logs the item class, unique entity ID, and the port it attached to — this gives
enough data to reliably detect drug consumption vs. merely drawing and holstering an item.

**Attachment ports reference:**

| Port | Meaning |
|------|---------|
| `weapon_attach_hand_right` | Item actively held in hand (drawn/in use) |
| `wep_stocked_2` / `wep_stocked_3` | Weapons stowed on back |
| `wep_sidearm` | Pistol holstered |
| `medPen_attach_1` / `_2` | Med pens stored in suit slots |
| `oxyPen_attach_1` / `_2` | Oxy pens stored in suit slots |
| `utility_attach_1` / `_2` | Utility items (multitool, melee) |
| `magazine_attach` | Magazine loaded in current weapon |
| `magazine_attach_1` through `_4` | Spare magazines in armor |
| `Armor_Torso` / `_Arms` / `_Legs` / `_Helmet` | Armor pieces |
| `backpack` | Backpack |

**Consumption detection logic:**

The log records every attachment change with the item's unique entity ID. To determine
whether a med pen was actually consumed (vs. drawn and re-holstered):

1. When `crlf_consumable_healing_01_[ENTITY_ID]` appears on port `weapon_attach_hand_right`,
   record the entity ID and timestamp as "drawn."
2. When a *different* item subsequently appears on `weapon_attach_hand_right` (the pilot
   swapped away from the med pen), check: does the med pen entity ID ever reappear on
   *any* port (`medPen_attach_*`, `weapon_attach_hand_right`, `oxyPen_attach_*`, etc.)?
3. If the entity ID **never reappears** within a reasonable window (~30 seconds), the pen
   was **consumed**. Increment BDL.
4. If the entity ID reappears on a storage port (e.g., `medPen_attach_1`), it was
   **holstered** — not consumed. Do not increment BDL.

**BDL model parameters (estimated — tune to match in-game behavior):**

| Parameter | Value | Notes |
|-----------|-------|-------|
| BDL per med pen (`crlf_consumable_healing_01`) | +20 per dose | Standard CureLife med pen |
| BDL per med gun shot (`crlf_medgun_01`) | +10 per shot | Lower dose via med gun delivery |
| Natural decay rate | -1 per second | Passive BDL reduction over time |
| OD warning threshold | 60 | AI warns pilot they're approaching danger |
| OD danger threshold | 80 | AI urgently warns of imminent overdose |
| OD / incapacitation threshold | 100 | Pilot goes down from overdose |
| Med bed reset | Set BDL to 0 | On `Medical Bed:` notification |
| Death / respawn reset | Set BDL to 0 | On corpse event + `OnClientSpawned` |

> **Important:** These values are estimates based on community testing and may not exactly
> match the game's internal model. The tracker is meant to be directionally correct for
> immersive dialogue, not a precise medical readout. Adjust constants if player feedback
> indicates the warnings feel too early or too late.

**Tracker state machine:**

```
BDL = 0 (session start)

On each frame/tick:
    elapsed = now - last_update
    BDL = max(0, BDL - (decay_rate * elapsed_seconds))
    last_update = now

On confirmed med pen consumption:
    BDL += 20
    if BDL >= 80: WARN_URGENT
    elif BDL >= 60: WARN_CAUTION

On confirmed med gun shot:
    BDL += 10
    if BDL >= 80: WARN_URGENT
    elif BDL >= 60: WARN_CAUTION

On Medical Bed notification:
    BDL = 0

On corpse event / respawn:
    BDL = 0
```

**AI dialogue tied to BDL levels:**

| BDL Range | AI Behavior |
|-----------|-------------|
| 0–30 | **Silent.** Normal operating range. |
| 30–59 | On dose: *"Med pen deployed."* (standard acknowledgement) |
| 60–79 | On dose: *"BDL's getting elevated. You've got room for maybe one more."* |
| 80–99 | On dose: *"That's pushing it — you're close to an overdose. Hold off on the stims."* |
| 100+ | *"BDL critical. You're going to OD if you take anything else."* |
| Dropping below 60 after being above | *"BDL's coming back down. You're clear."* (only announce once per cool-down cycle) |
| Reset to 0 (med bed) | Included in med bed dialogue: *"...BDL reset."* |
| Reset to 0 (death) | **Silent.** Handled by respawn dialogue. |

---

## 3. Per-manufacturer voice flavour

_Why keep it:_ Not in SuitMk2 (voices are fixed to Elah and Montaigne). Useful as phrase-bank seed material for commentary or teacher prompts, even without switching voices.

_Source: spec lines 3545-3568._

#### 6.2 SHIP MANUFACTURER PERSONALITY MODIFIERS

The ship AI's **voice and tone shift** based on the manufacturer of the current ship.
This is not a different AI — it's the same ship AI adapting its communication style to
match the manufacturer's philosophy and design language.

| Manufacturer | Prefix | Voice Profile | Characteristics |
|-------------|--------|---------------|-----------------|
| Drake Interplanetary | `DRAK_` | Gruff, utilitarian, no-nonsense | Short sentences. No pleasantries. Calls things what they are. Doesn't sugarcoat problems. *"Fuel's low. Fix it."* |
| Anvil Aerospace | `ANVL_` | Military precision, formal, by-the-book | Uses proper terminology. Reports in structured format. Acknowledges commands. *"Acknowledged. Shield status: nominal. Weapons: standby."* |
| RSI (Roberts Space Industries) | `RSI_` | Reliable, corporate-neutral, balanced | Professional but not stiff. Balanced between casual and formal. The baseline voice. *"Systems are green. Ready when you are."* |
| Crusader Industries | `CRUS_` | Polished, corporate-positive, confident | Slightly marketing-flavored. Emphasizes comfort and capability. Proud of the ship. *"All Crusader systems performing within optimal parameters. Welcome aboard."* |
| Origin Jumpworks | `ORIG_` | Refined, luxury-aware, slightly condescending | Uses elevated language. References comfort features. Subtly implies other ships are inferior. *"Climate control set to your preference. The Origin experience begins."* |
| MISC (Musashi Industrial) | `MISC_` | Practical, workmanlike, understated | Focuses on function over form. Acknowledges limitations honestly. No ego. *"She's not pretty, but the cargo bay's full-size. Let's get to work."* |
| Aegis Dynamics | `AEGS_` | Intense, classified-feel, guarded | Speaks as if everything is need-to-know. Slightly paranoid. References security protocols. *"Systems online. Encryption active. Comms are secure."* |
| ARGO Astronautics | `ARGO_` | Industrial, purely functional, minimal | Bare minimum communication. Almost no personality. Reports only facts. *"Online. Fuel: [level]. Cargo: [capacity]."* |
| Consolidated Outland | `CNOU_` | Enthusiastic, scrappy, underdog energy | Eager to prove itself. Acknowledges the ship is entry-level but owns it. *"She's small but she's quick. Let's show them what we've got."* |
| Gatac | `GATC_` | Alien-influenced, formal, slightly detached | Unusual phrasing. Transliterated tone. Respectful but foreign-feeling. *"Ship systems report readiness. The path is open to you."* |

**Implementation note:** Parse the manufacturer prefix from the ship channel name
(`DRAK_`, `ANVL_`, etc.) and apply the corresponding voice profile to all ship AI
responses for the duration of that boarding session. The profile persists until the
pilot leaves the ship channel.

---

## 4. Hand-written location and manufacturer lines

_Why keep it:_ SuitMk2's lore graph is wiki-fact based; these are personality-flavoured one-liners. Seed material for topics_lore.json or training examples. Some facts are from April 2026 and should be re-checked against the current game before use.

_Source: spec lines 1189-1256._

#### Systems

| System | Knowledge |
|--------|-----------|
| **Stanton** | Corporate-owned system. Four megacorps each control a planet. UEE maintains a presence but the corps run the show. Heavily populated, well-patrolled in monitored space. The "civilized" part of the verse — relatively speaking. Jump points to Pyro, Magnus, Terra, and Nyx. |
| **Pyro** | Lawless system. No permanent UEE presence. Controlled by various outlaw factions — Nine Tails, Headhunters, and worse. Dangerous but full of opportunities for those willing to risk it. Comm relay coverage is spotty to nonexistent. Six planets, most of them hostile. The system's star is unstable. |
| **Nyx** | Semi-lawless frontier system. Home to Levski, a former mining facility turned independent settlement. People's Alliance territory. Gateway between Stanton and Pyro. Quieter than Pyro but don't mistake quiet for safe. Jump points to Pyro and Castra. |

#### Planets & Moons

| Body | Log ID | Knowledge |
|------|--------|-----------|
| **Hurston** | `OOC_Stanton_1_Hurston` | Arid, polluted planet. Owned by Hurston Dynamics — weapons and munitions manufacturer. Corporate security is aggressive. The surface is scarred by strip mining. Heavy industry everywhere. |
| **Ariel** | `OOC_Stanton_1a_Ariel` | Hurston's tidally locked moon. One side bakes, the other freezes. Thin atmosphere. Not much out here except outposts and trouble. |
| **Aberdeen** | `OOC_Stanton_1b_Aberdeen` | Toxic atmosphere moon. Acidic rain, poor visibility. Mining operations dot the surface but it's not a place you want to spend time outside. |
| **Magda** | `OOC_Stanton_1c_Magda` | Smog-covered moon with low visibility. Industrial outposts scattered across the surface. Common staging ground for facility operations near Hurston. |
| **Ita** | `OOC_Stanton_1d_Ita` | Small, rocky, cold moon. Minimal atmosphere. Quiet — sometimes too quiet. |
| **Crusader** | `OOC_Stanton_2_Crusader` | Gas giant. You can't land on it but the upper atmosphere hosts Orison, a floating city. Crusader Industries builds ships here — Starliners, Hercules, the works. Platform-based infrastructure. |
| **Yela** | `OOC_Stanton_2c_Yela` | Icy moon with an asteroid belt. Popular for drug labs and illicit operations. Beautiful to look at, dangerous to linger. |
| **ArcCorp** | `OOC_Stanton_3_ArcCorp` | Entirely urbanized planet — every square meter is developed. ArcCorp is a mega-corporation that builds fusion engines. Area 18 is the main landing zone. Dense, loud, crowded. |
| **Lyria** | `OOC_Stanton_3a_Lyria` | ArcCorp's icy moon. Mining operations and outposts. Cold, remote, and largely ignored by ArcCorp corporate. |
| **Wala** | `OOC_Stanton_3b_Wala` | Small, arid moon orbiting ArcCorp. Low gravity. Relatively quiet. |
| **microTech** | `OOC_Stanton_4_Microtech` | Frozen planet. microTech manufactures mobiGlas devices and computing equipment here. New Babbage is the landing zone — clean, modern, cold. The planet's climate was supposed to be temperate but the terraforming went wrong. |
| **Clio** | `OOC_Stanton_4b_Clio` | microTech's volcanic moon. Active geology, interesting terrain. Research outposts. |
| **Euterpe** | `OOC_Stanton_4c_Euterpe` | Small, icy moon. Not much here — a few outposts and a lot of nothing. |
| **Pyro IV** | `pyro4` | Hostile planet in the Pyro system. Extreme temperatures and radiation. Outlaw territory through and through. |
| **Pyro VI** | `pyro6` | Distant ice giant in the Pyro system. Remote, cold, and largely unexplored. |
| **Monox** | `Monox` | Pyro system body. Outlaw staging area. Don't expect a warm welcome. |

#### Stations & Cities

| Location | Log ID / Start Location | Knowledge |
|----------|------------------------|-----------|
| **Lorville** | `ObjectContainer_Lorville_City`, start: `Lorville` | Hurston's primary landing zone. Industrial, grimy, corporate-controlled. The gates are a maze and the transit system is slow. But it has everything you need — shops, hangars, medical. |
| **New Babbage** | `NewBabbage_LOC`, start: `New Babbage` | microTech's showcase city. Clean, white, modern. Commons area is pleasant if you like the cold. Good shops. Feels sterile compared to Lorville. |
| **Orison** | `Orison_LOC`, start: `Orison` | Floating city in Crusader's upper atmosphere. Beautiful — platforms connected by shuttles. Crusader's shipyard is here. The views are something else. Slow to navigate. |
| **Area 18** | start: `Area 18` | ArcCorp's main landing zone. Urban canyon surrounded by skyscrapers. Loud, dense, commercial. The kind of place where everything's for sale. |
| **Everus Harbor** | start: `Everus Harbor` | Orbital station above Hurston. Well-equipped — hangars, cargo decks, medical. Common staging point for operations around Hurston and its moons. Feels like a second home if you spend enough time in Hurston space. |
| **Port Tressler** | start: `Port Tressler` | Orbital station above microTech. Clean, efficient, well-maintained. microTech quality — everything works the way it should. |
| **Bajini Point** | start: `Bajini Point` | Orbital station above ArcCorp. Busy. Lots of traffic. |
| **Seraphim Station** | start: `Seraphim Station` | Orbital station above Crusader. Gateway to Orison. |
| **GrimHEX** | start: `GrimHEX` | Pirate station carved into an asteroid near Yela. No questions asked. The kind of place where you check your corners and keep your hand near your sidearm. |
| **Levski** | start: `Levski`, `levski` | Independent settlement in Nyx. Former mining facility turned political refuge. People's Alliance territory. Has its own character — rough, self-reliant, suspicious of outsiders. |
| **Rest Stops** | `ObjectContainer_RestStop`, `LOC_RR_*` | Automated rest stops at Lagrange points. Fuel, basic shops, medical, hangars. Utilitarian. Every system has them — they're the gas stations of space. |
| **Nyx Gateway** | start: `Nyx Gateway` | Gateway station in the Nyx system. Transition point between Nyx and Stanton. |
| **Stanton Gateway** | start: `Stanton Gateway` | Gateway station on the Stanton side of the jump point. Where you catch your breath after coming in from Pyro or Nyx. |
| **Pyro Gateway** | start: `Pyro Gateway` | Gateway station on the Pyro side. Last stop before lawless space — or first stop coming back. |
| **Patch City** | start: `Patch City` | Pyro settlement. Cobbled together from salvage and desperation. Don't drink the water. |
| **Gaslight** | start: `Gaslight` | Pyro system refueling station. Named either ironically or as a warning. |
| **Ruin Station** | zone: `ruinstation` | Abandoned station in Pyro. Contested space. Scavengers, outlaws, and the occasional explorer. If the walls could talk, they'd probably scream. |

#### Manufacturers

| Manufacturer | Ships in Logs | Knowledge | Suit AI Personality Flavor |
|-------------|---------------|-----------|---------------------------|
| **Drake Interplanetary** | Herald, Corsair, Caterpillar, Vulture, Buccaneer, Cutlass, Clipper, Cutter, Golem | Cheap, rugged, no-frills. Drake builds ships for people who need them to work, not look pretty. Officially they don't market to pirates. Nobody believes that. | The suit AI has a grudging respect for Drake — they're honest about what they are. |
| **Anvil Aerospace** | Carrack, Hornet, Super Hornet, Pisces, Arrow, Gladiator | Military contractor. Anvil builds for the UEE Navy and sells surplus to civilians. Precise engineering, utilitarian design. If it's Anvil, it was designed to fight. | The suit AI respects Anvil's reliability but finds them humorless. |
| **RSI (Roberts Space Industries)** | Aurora, Constellation, Polaris, Perseus, Meteor | The original. RSI built humanity's first quantum drive. They make everything from starter ships to capital vessels. Solid all-rounders. | The suit AI views RSI as the default — reliable, unremarkable, gets the job done. |
| **Crusader Industries** | C1 Spirit, C2 Hercules, Starlifter | Crusader builds big, comfortable ships. Heavy haulers, passenger liners, military transports. Everything they make feels overengineered in the best way. | The suit AI appreciates the build quality but thinks they're overpriced. |
| **Origin Jumpworks** | 600i | Luxury manufacturer. Origin ships are beautiful, expensive, and fragile. If Drake is a pickup truck, Origin is a sports car. | The suit AI thinks Origin ships are for showing off. Secretly impressed by the interiors. |
| **MISC (Musashi Industrial)** | Freelancer, Prospector, Starfarer | Workhorses. MISC builds practical ships for practical people. Freelancers haul cargo, Prospectors mine rocks, Starfarers carry fuel. Nothing flashy, everything functional. | The suit AI sees MISC as the blue-collar choice. Solid. |
| **Aegis Dynamics** | Vanguard, Hammerhead, Gladius, Idris | Former military shipbuilder with a complicated history. Aegis builds some of the most formidable combat ships in the verse — heavy fighters, frigates, capital ships. There's always a rumor they're connected to something shady. | The suit AI is wary — Aegis ships are powerful but carry baggage. |
| **ARGO Astronautics** | MOLE | Industrial vehicles. ARGO builds mining ships and utility craft. The MOLE is their flagship — a multi-crew mining vessel built for serious extraction work. | The suit AI sees ARGO as purely functional. No personality, all business. |
| **Esperia** | Talon, Prowler | Reproduction specialists. Esperia reverse-engineers alien ships and sells them to human pilots. Xi'an Nue, Tevarin Prowler — alien tech with human controls. | The suit AI finds Esperia ships fascinating and slightly unsettling. |
| **Vanduul** (NPC ships) | Blade, Scythe | Enemy alien species. If you see a Vanduul ship, it's either a replica or you're in serious trouble. | The suit AI treats Vanduul contacts as maximum threat. |
| **Tumbril** | Nova | Ground vehicles. Tumbril makes tanks, buggies, and other land vehicles. Military heritage. | The suit AI doesn't have much to say about ground vehicles — it's a flight suit AI. |
| **Banu** (items) | Banu blade (melee) | Alien trade species. Banu-made goods are exotic and well-crafted. A Banu blade is a status symbol as much as a weapon. | The suit AI appreciates the craftsmanship. |

---

## 5. Contract history shaping the companion

_Why keep it:_ SuitMk2 passes contract accept/complete/fail through as raw events. The idea: contract TYPE (bounty, cargo, ...) accumulates into long-term character. The subclass names belong to the old personality model; keep the history idea, not the subclasses.

_Source: spec lines 3967-4015._

#### 7.9 CONTRACT HISTORY & SUBCLASS EVOLUTION

The AI tracks all contracts completed in the activity journal, broken down by type. This
feeds directly into the subclass evolution system.

**Per-type lifetime counters:**

```json
{
  "contract_history": {
    "bounty": { "completed": 12, "failed": 2, "abandoned": 1 },
    "cargo_hauling": { "completed": 7, "failed": 1, "abandoned": 0 },
    "facility_delve": { "completed": 5, "failed": 3, "abandoned": 0 },
    "courier": { "completed": 3, "failed": 0, "abandoned": 0 },
    "combat_gauntlet": { "completed": 2, "failed": 1, "abandoned": 0 },
    "rescue": { "completed": 1, "failed": 0, "abandoned": 0 },
    "investigation": { "completed": 0, "failed": 0, "abandoned": 0 }
  },
  "total_completed": 30,
  "total_failed": 7,
  "completion_rate": 0.81,
  "dominant_type": "bounty",
  "secondary_type": "cargo_hauling"
}
```

**Subclass evolution from contracts:**

| Dominant Contract Type | Subclass Influenced | Shift per Completion |
|-----------------------|--------------------|--------------------|
| Bounty (8+ completed) | Bounty Hunter | +0.03 per completion after 8th |
| Cargo hauling (5+ completed) | Merchant | +0.03 per completion after 5th |
| Facility delve (5+ completed) | Combat-Hardened axis | +0.02 per completion |
| Rescue (3+ completed) | Humanitarian | +0.04 per completion after 3rd |
| Courier (5+ completed) | Courier / Lone Wolf | +0.02 per completion |
| Mixed (no clear dominant, 20+ total) | Veteran Operator | +0.01 per completion after 20th |
| High failure rate (>30% failed) | Irreverence axis | +0.01 per failure |

**Contract completion milestones:**

| Milestone | Response |
|-----------|----------|
| 10th contract overall | *"Ten contracts in the books. You're building something here."* |
| 25th contract overall | *"Twenty-five. Quarter century of jobs. Not bad."* |
| 50th contract overall | *"Fifty contracts completed. That's a career."* |
| 100th contract overall | *"One hundred contracts. You've seen more of the verse than most pilots ever will."* |
| 10th of a specific type | *"That's ten [type] contracts. You've got a specialty now."* |
| First contract of a new type | *"First [type] contract. Something new."* |

---

## 6. Refinery timer across sessions

_Why keep it:_ SuitMk2 fires refinery_complete but keeps no timer. Refinery jobs outlast a session; this design persists them and reminds the pilot when one finishes.

_Source: spec lines 4644-4734._

#### 11.3 REFINERY OPERATIONS & CROSS-SESSION TIMER

Refinery events are rare in the current log data but the system should handle them
when they occur. Critically, refinery jobs have **real-time timers** that continue
even when the player is offline — the AI must persist these across sessions.

**Log signals:**

| Signal | Source | Data |
|--------|--------|------|
| Ore sold to refinery | `SellToRefineryPressed` | Ore submission |
| Refinery job submitted | `OnRefineryRequest` | Job started — timer begins |
| Refinery job complete | `RefineryTransactionResponse` | Job finished in-session |

**Refinery timer persistence:**

When a refinery job is submitted (`OnRefineryRequest`), the AI records it in a
persistent file:

**Refinery state file** (`memory/refinery_orders.json`):

```json
{
  "active_orders": [
    {
      "submitted_at": "2026-01-04T15:46:14Z",
      "estimated_duration_minutes": 45,
      "estimated_completion": "2026-01-04T16:31:14Z",
      "location": "Everus Harbor",
      "ore_type": "unknown",
      "status": "processing",
      "notified_complete": false
    }
  ],
  "completed_orders": []
}
```

**Cross-session timer logic:**

```
On refinery job submitted (OnRefineryRequest):
    1. Record submission timestamp
    2. Estimate duration (if parseable from log, else use default: 45 min)
    3. Record current location (from last QT destination or jurisdiction)
    4. Save to refinery_orders.json
    5. AI: "Refinery job submitted. Estimated completion in [N] minutes.
            I'll keep track."

On session end (SystemQuit / disconnect):
    1. Save refinery_orders.json with current state
    2. Record session end timestamp

On next session start (OnClientSpawned in PU):
    1. Load refinery_orders.json
    2. For each active order:
        a. Calculate: time_since_submission = now - submitted_at
        b. If time_since_submission >= estimated_duration_minutes:
            → Order is COMPLETE
            → AI: "By the way — that refinery order you submitted at
                    [location] should be done by now. It's been [hours/minutes]
                    since you submitted it. Ready for pickup."
            → Mark notified_complete = true
            → Move to completed_orders
        c. If time_since_submission < estimated_duration_minutes:
            → Order still processing
            → remaining = estimated_duration - elapsed
            → AI: "Your refinery order at [location] has about [remaining]
                    minutes left. I'll let you know when it's ready."

During session (if order completes while playing):
    1. Track elapsed time since submission
    2. When elapsed >= estimated_duration:
        → AI: "Refinery order's done at [location]. Ready for pickup."
        → Mark complete

On QT to refinery location with completed order:
    → AI: "Heading back to [location] — your refinery order's been
            sitting there ready. Good timing."
```

**Personality matrix examples:**

| Profile | Refinery order ready on login |
|---------|------------------------------|
| Analytical | *"Refinery order at Everus Harbor completed approximately 3 hours ago. Processed yield should be available at the terminal."* |
| Combat-Hardened | *"Your ore's done at Everus. Pick it up before someone else gets ideas."* |
| Warm | *"Welcome back. Good news — that refinery order at Everus is ready and waiting."* |
| Irreverent | *"While you were gone, I watched your ore get refined. Riveting stuff. It's at Everus."* |
| Miner subclass | *"Refinery order complete. Based on the batch size, you should see a solid yield. Everus Harbor terminal."* |


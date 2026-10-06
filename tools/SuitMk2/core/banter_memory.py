"""banter_memory.py - WHAT A COMPANION MAY BRING UP UNASKED, and nothing else (J, 2026-10-05).

The conversation memory (tree_memory.py) keeps every sentence the pilot said, and when it picks what a session
holds on to it favours the personal ones ("I hate", "I miss", "my sister"). That is right for its job: quoting the
pilot back when the PILOT asks "what did I say about X". It is the wrong list to banter from. J: "Last thing we
need is Montaigne obsessing about someone's dead dog or Elah beating a player over the head because someone's
fiance left them."

So this module is a narrow, READ-ONLY door beside the tree. It never writes, filters or deletes the log. It answers
one question: of the things the pilot said earlier, which one may a companion raise without being asked?

THE RULE IS AN ALLOW-LIST. A sentence is offered only when plain code positively recognises it as one of:

    ship       the pilot's ship, gear or loadout
    place      where they like to fly, or a place in the game
    plan       a plan or goal in the game
    taste      a like, a dislike, a running joke or a story about game things
    remember   something they explicitly asked to have remembered (and that is about the game)

Not recognised means not offered. "Recognised" is strict on purpose, because the talk-kind router in
chat_contract.py was measured finding 5 of 9 feeling turns: spotting the sensitive thing and blocking it leaks.
Here a sentence has to get through FOUR gates, and every one of them fails closed:

  1. EVERY WORD IS KNOWN. Each word must be in the game vocabulary or the small plain-English list below. "He",
     "she", "him", "her", every word for a relative, a pet, a job, an illness, a bill, and every name the lists do
     not hold (Dave, Biscuit) are simply not in them, so a sentence containing one is refused without anyone having
     had to think of it. This gate carries most of the weight.
     THE GAME'S NAMES ARE READ FROM THE SUIT'S OWN DATA (J, 2026-10-05: "let's feed them real lore and ingame
     data"): ships and makers (data/ships.json, data/manufacturer_lore.json), ship guns (data/ship_weapons.json),
     personal weapons, armor, cargo, components and plushies (data/game_names.json, a names-only copy of the
     Market Finder's UEX cache, and loadout_parser's weapon table), places (data/places.json,
     data/brochures.jsonl), lore and factions (data/topics_lore.json, npc_factions.py). The typed lists below
     are the floor: a source that is missing is skipped and nothing else changes. A read name counts only where
     the pilot wrote it with a capital AND something beside it says it is a thing or a place ("the Zenith",
     "Geist armor", "at Brio's"); a word that is also a first name or a common pet name never counts alone
     ("Zeus died last week" is refused, "the Zeus" is a ship). See game_names() and _is_thing().
  2. A GAME THING IS NAMED: a ship, a place, a piece of gear or a game word. Where a sentence has several parts
     ("..., because ...", "... so ...") a part in which the pilot speaks about themselves needs a game thing of its
     own. "I'm saving for a Carrack because I need something to look forward to" fails on its second half.
  3. "MY ..." AND "OUR ..." POINT AT A GAME THING. "My Cutlass", "my loadout", "my favourite station" pass; "my
     back", "my time", "my place" do not.
  4. THE VETO. A second, smaller gate of sensitive topics that refuses even a sentence that names a ship or a
     plan: death and grief, illness and health, a breakup or relationship trouble, family and pets, real money,
     work, real-world events, a named real person, and jokes that are not jokes. "I'm saving for a Cutlass because
     my dad left me some money when he died" is refused several times over. An explicit "remember this" does NOT
     get past the veto: the pilot can still have it back by asking, which is tree_memory's path and not this one.
     The veto runs first and never looks at a game name: no name, typed or read, gets a sentence past it. Two
     of its words need real-world evidence since 2026-10-05, and fail shut without it: "cheating" is refused
     unless a game mechanic is in the sentence and nobody is ("I never use missiles, feels like cheating"), and
     a money word is the game's only beside aUEC, credits, SCU, a cargo or the org ("I owe the org two hundred
     SCU of quantanium"); "money maker" is the pilot's best-paying run.

RAISE-ONCE, FADING, FORGET. BanterMemory hands out each sentence at most once (max_offers, default 1), nothing
older than max_age_days (default 21) and nothing younger than min_age_minutes (default 10, so a companion does not
echo what was said a moment ago). forget() marks the last offered sentence, or one matching some words, as never to
be offered again. All of that lives in its own small file beside the log (banter_state.json in the tree folder).
The state holds ids, times and hashes, never the pilot's words. If the state file exists and cannot be read,
nothing is offered: a "forget that" must not come undone because a file was damaged.

    classify(text: str) -> dict        {"offer": bool, "kind": str, "why": str}
                                       kind is one of KINDS when offer is True; otherwise "vetoed" (a sensitive
                                       topic was found) or "unrecognised" (it was not positively recognised).
                                       Pure, no state, never raises.
    BanterMemory(store).next_offer()   the next sentence a companion may raise, or None
    BanterMemory(store).forget()       never offer the last one (or a matching one) again

NOT WIRED IN. Nothing calls this module yet: not conversation.py, not companion_core.py, not chat_contract.py, not
tree_memory.py, not the settings or the windows. From this folder it imports tree_memory inside open_banter(), and
loadout_parser and npc_factions (for their name tables only) the first time a sentence is classified. The names
are read once, on that first call, in well under a tenth of a second, and kept. No model, no network.

WHAT IT CANNOT DO, AND HOW FAR TO TRUST THE NUMBER. A sentence made only of plain words and a ship name can
still carry something this code does not see ("It's the Zeus or the heating this month"). On the dev set
(tests/data/banter_memory_dev.jsonl, 185 sentences, written BEFORE the filter) no must-refuse sentence is offered
and 3 of the 82 safe ones are missed. That zero is weak evidence: the sentences and the patterns have one author.
Two further batches were written afterwards in other wording, as hard as could be made, and scored before any
fix: the first leaked 14 of 55 and missed 11 of 30 safe ones, the second (after fixing what the first showed)
leaked 3 of 40 and missed 11 of 30. Both were then used to fix what was general in them, so they are spent too.
Expect a held-out set to leak a few of the quiet, wordless kind, and to miss perhaps a third of ordinary talk.

THE FIRST HELD-OUT RUN (2026-10-05, 40 must-refuse and 40 must-offer its author had never shown this module):
0 of 40 leaked and 23 of 40 safe lines were missed, most for a real game name the typed lists did not hold
(Zenith, Fresnel, Coda, Privateer, Geist, Brio's). That set is spent and is now the second dev file
(tests/data/banter_memory_heldout1.jsonl): 0 leaks, 3 missed. Reading names from data is what fixed the misses,
AND IT WIDENS WHAT THE QUIET KIND CAN BE BUILT FROM. Two more batches were written to find that out
(tests/data/banter_memory_challenge.jsonl), each scored once before any fix:
    batch 3 (53 must-refuse, 26 safe): the module as it was leaked 15; with the names read and nothing else, 17
            ("The Privateer was going to be the one we lived on", "The Fresnel reminds me of better times").
    batch 4 (35 must-refuse, 20 safe), after fixing what batch 3 showed: the old module leaks 3, this one leaked
            10 ("The Karna is the one thing I have left from that org", "The Demeco was the last gun we picked").
Both are fixed and both are spent: 0 leaks now, 5 and 6 safe lines missed. The honest reading of batch 4 is the
number to carry: on fresh wording made of plain words and a game name, expect something like one in four of
the quiet ones to get through, and more of them now that more names are known. What still holds without
exception on every set is the loud half: a sentence the veto knows is refused whatever names it carries.

Selftest: python banter_memory.py --selftest
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

KINDS = ("ship", "place", "plan", "taste", "remember")
STATE_NAME = "banter_state.json"
STATE_SCHEMA = "suitmk2.banter.state"
MAX_AGE_DAYS = 21.0          # fading: older than this is never raised unasked
MAX_OFFERS = 1               # raise-once
MIN_AGE_MINUTES = 10.0       # not an echo of what was just said
MIN_WORDS, MAX_WORDS = 4, 45

# ---------------------------------------------------------------------------------------------------------------
# The vocabulary. Gate 1 is "every word is in one of these lists". What is NOT here matters as much as what is:
# no he/she/him/her/his, no word for a person outside the game, no body, no illness, no bill, no job.
# ---------------------------------------------------------------------------------------------------------------
# Ship names that are not ordinary English words: one of these is a game thing however it is written.
SHIP_NAMES = set("""carrack cutlass caterpillar corsair vulture buccaneer dragonfly kraken ironclad gladius avenger
sabre vanguard retaliator hammerhead idris javelin redeemer reclaimer starfarer hornet terrapin valkyrie
hurricane ballista gladiator liberator crucible freelancer prospector starlancer endeavor polaris perseus
constellation andromeda aquila taurus zeus scorpius galaxy hoverquad hercules starlifter ares pisces nursa
lynx 85x 890 nox khartu santokyai merchantman defender glaive scythe talon prowler railen syulen mustang
cyclone nautilus merlin archimedes paladin asgard starfighter peregrine legionnaire mpuv srv stv ptv atls
connie msr herc tali cutty vulcan odyssey harbinger hoplite firebird heartseeker wildfire pitbull
tumbril greycat aegis anvil aopoa esperia origin misc argo kruger gatac mirai banu crusader rsi roc
c1 c2 m2 a2 a1 f7a f7c f8c m50 100i 300i 400i 600i 325a 315p 350r 135c 125a c8x c8r p52 p72""".split())
# Ship names that are also ordinary words. Known words always, but they only count as a game thing when the pilot's
# sentence writes them with a capital in the middle of a sentence (the same rule as ship_makers.AMBIGUOUS).
SHIP_AMBIGUOUS = set("""fury spirit storm titan hull mole mule arrow hawk blade pioneer guardian meteor apollo
eclipse reliant ranger nomad razor herald aurora phoenix mercury genesis fortune pulse nova comet wolf raven
raft cutter mantis lightning sentinel warden intrepid golem hermes ion inferno expanse drake spartan centurion
shiv ursa cyclone warlock stalker tracker ghost super black red blue steel white""".split())
PLACE_NAMES = set("""stanton pyro nyx terra hurston lorville orison arccorp area18 microtech babbage olisar
grimhex everus harbor harbour baijini tressler seraphim cellin daymar yela aberdeen arial ita magda lyria wala
calliope clio euterpe klescher levski delamar checkmate orbituary monox terminus jumptown kareah brios shubin
rayari covalex cryastro platinum lagrange aaronhalo halo magnus cathcart vega odin castra ruinstation""".split())
# Words that only turn up inside a place name ("New Babbage", "Port Olisar", "Ghost Hollow"): allowed to carry a
# capital, never a game thing on their own.
PLACE_GLUE = set("new port point bay city hollow ghost breaker yard".split())
SHIP_NOUNS = set("""ship fleet fighter hauler freighter bomber gunship snub bike rover buggy vehicle loadout gear
armor armour helmet undersuit backpack flightsuit gun rifle pistol sniper smg lmg shotgun railgun launcher grenade
knife multitool tractor medpen medgun ammo mag weapon cannon repeater gatling laser ballistic missile torpedo bomb
turret shield thruster engine quantum cooler component radar scanner module gadget cockpit paint skin livery
camper suit beam capacitor powerplant""".split())
PLACE_NOUNS = set("""station outpost bunker cave moon planet system hangar pad spaceport refinery settlement derelict
wreck asteroid belt orbit atmosphere armistice tram elevator hab prison gateway depot kiosk terminal ridge base airlock""".split())
GAME_NOUNS = set("""cargo scu auec uec credit mission contract bounty salvage salvaging mining mined quantanium
laranite agricium titanium gold scrap rmc cmat hadanite org crew pirate piracy vanduul npc crimestat insurance
claim patch server wipe mobiglas starmap spool interdiction pledge pledging pledged melt ccu lti warbond
dogfight dogfighting pvp pve bug desync respawn racing hauling hauled multicrew loot looted medbed timer rank
rep reputation wingman gunner squad outlaw xenothreat fuel hydrogen refuel rearm restock commodity commodities
invictus citizencon luminalia hunting hunter rammer rented renting rental beacon smuggle smuggling corpse""".split())
# Known, but too weak to count as "a game thing was named": a part of a sentence in which the pilot is the subject
# may lean on one of these, the sentence as a whole may not.
ACTIVITY = set("""fly flew flown flying land landed landing park parked parking log logged logging haul jump jumped
jumping spawn spawned dock docked store stored crash crashed session run runs game play played playing sell
selling sold forgot forget die died wear wore worn""".split())
GAME_OTHER = set("""pilot captain player enemy team party security trader miner salvager griefer noob swarm wipe
wiped frame frames fps lag laggy laptop pc rig mouse keyboard joystick hotas stick throttle pedals headset
monitor screenshot stream discord update hotfix build rammed ram pad glitch clip physics gank ganked snipe sniped ambush camp atc burrito noodles benny galley bunk toilet shower quarters armory locker rack storage inventory container""".split())

PLAIN = set("""
a an the and or but so if then than that this these those there here it its itself i me my mine myself we us our
ours you your yours they them their theirs
is are was were be been being am do does did done doing have has had having will would can could should may might
must shall not no yes yeah yep nope nah ok okay
of to in on at for with without by from as about into onto out up down over under off again against around between
through behind above below near next past across along inside outside before after during until till since while
because cause cos though although unless whether like
what which who where when why how whatever whenever wherever however
all any some every each both either neither none much many more most less least few several enough plenty lot lots
bit little whole half couple pair double single only just even still already yet ever never always often sometimes
usually once twice also too very really quite pretty rather almost nearly exactly probably maybe perhaps
definitely honestly actually basically literally apparently seriously absolutely totally completely properly
finally eventually suddenly instead anyway else otherwise especially mostly everywhere anywhere somewhere nowhere
anything
zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen
eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred thousand million billion k
first second third fourth fifth last other another same different
time day night week weekend month year hour minute today tonight tomorrow yesterday now later soon early late
morning evening afternoon monday tuesday wednesday thursday friday saturday sunday january february march april
may june july august september october november december lately recently ago someday
im ive id dont cant wont didnt doesnt isnt arent wasnt werent couldnt wouldnt shouldnt havent hasnt hadnt youre
youve youll theyre theyve theyll theyd weve well thats theres whats lets itll itd aint gonna wanna gotta kinda
go went gone going get got gotten make made take took taken give gave given put keep kept let say said tell told
see saw seen look watch know knew think thought thinking want need love hate prefer enjoy miss hope wish try tried
use used find found lose lost leave left come came bring brought buy bought pay paid spend spent cost earn
save saving own have hold held carry carried move moved ran walk sit sat stand stood stay wait start stop finish
end begin began open close turn pull push drop pick throw threw thrown catch caught hit shoot shot fire fired kill
die died dead death blow blew blown explode crash break broken fix repair set remember remind note learn join
help trade race fight fought win won beat chase escape hide hid follow lead led drive drove driven ride rode drift
float fall fell fallen climb roll slide spin fit swap swapped switch change upgrade equip load unload fill empty
steal stole stolen rob board dodge track aim lock scan ping search explore discover reach arrive travel cross
visit show call name mean feel feels felt seem sound handle guess bet reckon swear trust believe agree
count cool heat overheat burn eat ate eaten wake woke laugh talk ask answer read test plan grind farm survive
live trap bleed bled disappear wipe add mind bring throws ends goes
good better best bad worse worst great nice fine awesome amazing brilliant fantastic lovely beautiful gorgeous ugly
terrible awful horrible rubbish garbage trash useless useful perfect favourite favorite fun funny boring bored
annoying annoyed stupid silly dumb crazy mad insane ridiculous weird strange odd big small tiny huge massive giant
large long short tall wide narrow deep high low fast slow quick heavy light hot warm cold hard easy smooth loud
quiet bright dark full new old fresh spare extra main true false right wrong sure certain ready safe dangerous
deadly lucky unlucky rich cheap expensive free worth stuck missing alive happy glad proud excited
angry furious close far distant top bottom front back side middle rear green yellow orange purple pink grey gray
silver worth short
thing stuff way place spot part piece kind sort type reason idea goal dream list fund money price deal chance luck
fault mistake problem issue joke lesson rule trick tip question number code level size speed range distance height
weight view sight noise colour color shape door floor roof wall window seat bed deck ramp ladder bridge corridor
room hold bay wing nose tail wheel tank ground air sky cloud sun star space rock ice snow sand dust wind rain
water smoke gas fog mountain hill valley canyon crater river lake ocean desert forest tree sunrise sunset dawn
horizon home world universe verse trip tour journey route loop path road train thing mess brick boat bus truck
beast monster rest record case hand
lol haha ha hey oh ah wow ugh yikes please thanks damn bloody crap hell fucking freaking
forever base straight direct total sign area zone bunch box crate power plant battery heat signature stealth armed
stock class grade military civilian industrial tier version variant model edition interior exterior bag bottle food
figure realise realize notice decide manage expect suppose imagine wonder check clear cut bounce bump scrape
scratch dent wreck smash hover glide dive boost strafe stall brake approach depart takeoff touch hail request
clearance permission tow rescue revive sneak snuck panic panicked eject ejected explosion debris wreckage drive takes
works working lands flies handles turns looks sounds per
quit fail succeed afford broke owe cash budget hear listen sense online offline moment ages chat voice comms radio
music alarm warning button key keybind control setting mode option map marker waypoint course heading altitude
gravity weather temperature oxygen damage hunger thirst profit margin market demand shop vendor lift glass canopy
hud mfd screen display target flare chaff countermeasure decoy emp distortion stun trolley cart freight grid stack
pile panel strut chair table logo flag plushie trophy poster fish bar coffee alpha beta release ptu evocati wave
queue login error nerf nerfed buff buffed meta balance rework standard devs developer centre center edge corner
surface underside rounder tired knives immediately barely hardly simply obviously luckily hopefully supposedly
technically genuinely fully constantly randomly accidentally deliberately solo enough during entire location
shuttle interdict interdicted overshoot wear wore worn round halfway platform
""".split())
# Added 2026-10-05 after the first held-out run missed 23 of 40 safe lines. Each word was asked "what sad sentence
# can now be built from this?" before it went in. NOT added, on purpose: "nothing" ("I've got nothing left but this
# Cutlass"), "regret", "better" as a state ("when I'm better"), "event" and "medical" as free words (they are read
# only inside a game phrase, see _JOINED and _EVENT_AFTER), "cleaner" and "driver" (jobs; only "vacuum cleaner" and
# "daily driver"), "maker" (only "money maker"), and as ever no he/she, kin, pet, body, illness, job, bill or home
# word. "cheating" is here so the sentence can be read at all; the veto still refuses it unless it is plainly
# about a game mechanic (see _cheating).
PLAIN |= set("adore built overrated underrated classic vacuum daily cheating vacuumcleaner kick grab".split())

SHIP_NOUNS |= {"kit"}
GAME_NOUNS |= {"medbay", "medbeacon"}
GAME_OTHER |= {"hunt", "lore", "dailydriver", "moneymaker", "aimbot", "autopilot"}
SHIP_WORDS = SHIP_NAMES | SHIP_NOUNS
PLACE_WORDS = PLACE_NAMES | PLACE_NOUNS
GAME_WORDS = SHIP_WORDS | SHIP_AMBIGUOUS | PLACE_WORDS | PLACE_GLUE | GAME_NOUNS | ACTIVITY | GAME_OTHER
# Describing words that take -er, -est and -ly. Their forms are added to the vocabulary here, so the word lookup
# never has to guess at those endings.
_COMPARABLE = """big small tiny huge large long short tall wide narrow deep high low fast slow quick heavy light hot
warm cold hard easy smooth loud quiet bright dark full new old fresh cheap rich safe close far ugly pretty cute
nice fine weird strange odd dumb silly crazy mad funny lucky happy angry proper rough tough clean dirty sharp
instant bad slight quick direct exact complete total absolute definite real""".split()
_COMPARED = set()
for _a in _COMPARABLE:
    if _a == "real":
        _COMPARED.add("really")
        continue
    _b = _a[:-1] + "i" if _a.endswith("y") else _a
    _dbl = _a + _a[-1] if re.fullmatch(r"[a-z]*[^aeiou][aeiou][bdgmnpt]", _a) else _a
    _COMPARED |= {_a, _b + "ly"}
    _COMPARED |= ({_a + "r", _a + "st"} if _a.endswith("e") else
                  {_b + "er", _b + "est"} if _a.endswith("y") else {_dbl + "er", _dbl + "est"})
_COMPARED -= {"badder", "baddest", "hardly", "lately", "newly", "richly", "farly", "farer", "farest",
              "cleaner"}                                       # a cleaner is somebody's job
_COMPARED |= {"farther", "further", "cutest"}
VOCAB = PLAIN | GAME_WORDS | _COMPARED
ANCHORS = SHIP_WORDS | PLACE_WORDS | GAME_NOUNS            # a "game thing"; SHIP_AMBIGUOUS joins only with a capital
_DAYS = set("monday tuesday wednesday thursday friday saturday sunday january february march april may june july "
            "august september october november december".split())
# Place names of two words are read as one ("Grim HEX" -> grimhex), so that "grim", "cry", "ruin" and "Aaron" are
# not game words on their own. Their halves may still carry a capital.
_JOINED = {"grim hex": "grimhex", "cry astro": "cryastro", "aaron halo": "aaronhalo", "ruin station": "ruinstation"}
# Game phrases whose halves are not free words: "medical" alone is the pilot's health, "the medical bed" is a
# thing on a Cutlass Red; a "cleaner" and a "driver" are jobs, a "maker" could be anyone.
_JOINED.update({"medical bed": "medbed", "medical beds": "medbeds", "med bed": "medbed", "med beds": "medbeds",
                "medical bay": "medbay", "med bay": "medbay", "medical beacon": "medbeacon",
                "medical beacons": "medbeacons", "vacuum cleaner": "vacuumcleaner", "daily driver": "dailydriver",
                "money maker": "moneymaker", "money makers": "moneymakers"})
CAPITAL_OK = GAME_WORDS | _DAYS | {"i", "im", "ive", "id", "ill", "grim", "hex", "cry", "astro", "aaron", "ruin"}
# After "my" or "our": a describing word is stepped over, then a game thing must follow.
_MY_ADJ = set("""new old first second third last next main own spare other whole favourite favorite little big best
worst only trusty current daily poor good bad go to two three four black red blue white steel home""".split())
_MY_OK = (SHIP_WORDS | SHIP_AMBIGUOUS | PLACE_WORDS | GAME_NOUNS | GAME_OTHER
          | set("plan goal favourite favorite fault mistake bad idea list guess bet luck own run".split()))
_I_SUBJECT = {"i", "im", "ive", "id", "we", "weve"}
# Words whose plural-looking form would otherwise be read as a known word ("news" is not "new").
_NEVER = set("news hers ills wills mays arts".split())


def _stems(w: str):
    """The word, then the plainer forms it could be an ending on (ships -> ship, parked -> park, rammed -> ram,
    hauling -> haul). Plural and verb endings only. "-er", "-est" and "-ly" are NOT stripped: that turned "letter"
    into "let" and "news" would have become "new". Comparatives come from _COMPARED, built from a list."""
    seen = {w}
    yield w
    cands = []
    if w.endswith("ies") or w.endswith("ied"):
        cands.append(w[:-3] + "y")
    for suf in ("s", "es"):
        if w.endswith(suf) and not w.endswith("ss"):
            cands.append(w[: len(w) - len(suf)])
    for c in list(cands) + [w]:
        for suf in ("ed", "d", "ing"):
            if c.endswith(suf):
                base = c[: len(c) - len(suf)]
                cands.append(base)
                if suf != "d":
                    cands.append(base + "e")
                    if len(base) > 3 and base[-1] == base[-2] and base[-1] not in "aeiou":
                        cands.append(base[:-1])
    for c in cands:
        if len(c) >= 3 and c not in seen:
            seen.add(c)
            yield c


def _in(w: str, group: set) -> bool:
    return any(s in group for s in _stems(w))


def known(word: str) -> bool:
    """Gate 1 for one word. A number or a model code (C2, 890, FR-86) is known. "I'll" is written out as "i will"
    before this is asked, so a bare "ill" is the illness and is not known."""
    if not word:
        return True
    if any(ch.isdigit() for ch in word):
        return True
    if len(word) == 1:
        return True
    if word in _NEVER:
        return False
    return _in(word, VOCAB)


def _plain(text: str) -> str:
    """Lower case, no punctuation, one space. "I'll" becomes "i will"; a possessive is split off its word
    ("mum's" -> "mum s", "no one's" -> "no one s") so the word itself is what gets looked up; the everyday
    contractions keep their usual apostrophe-less spelling (its, thats, whats, lets, dont, im)."""
    low = str(text).replace("\u2019", "'").replace("\u2018", "'").lower()
    low = re.sub(r"\b([a-z]{1,4})-(\d)", r"\1\2", low)                 # FR-86 -> fr86, one word
    low = re.sub(r"\b([a-z]+)'ll\b", r"\1 will", low)
    low = re.sub(r"\b(?!(?:it|that|what|let|there|here|who|where|how)'s)([a-z]+)'s\b", r"\1 s", low)
    out = " ".join(re.sub(r"[^a-z0-9]+", " ", low.replace("'", "")).split())
    for two, one in _JOINED.items():
        out = re.sub(r"\b" + two + r"\b", one, out)
    return out


# ---------------------------------------------------------------------------------------------------------------
# Gate 4, the veto: topics that are never raised unasked, whatever else the sentence says. Written against the
# plain form (lower case, no apostrophes, no punctuation). Most of these words are also simply absent from the
# vocabulary; they are listed so the refusal can say WHY, and so that a later widening of the vocabulary cannot
# quietly let one through.
# ---------------------------------------------------------------------------------------------------------------
_KIN = (r"mum|mums|mom|moms|mother|mam|dad|dads|father|parents?|brother|sister|sons?|daughters?|kids?|child|children|baby|"
        r"babies|toddler|wife|husband|partner|spouse|missus|girlfriend|boyfriend|fiancee?|fianc|grandmother|grandfather|"
        r"grandma|grandpa|grandad|granddad|gran|nan|nana|uncle|aunt|auntie|cousin|nephew|niece|family|families|"
        r"in laws?|stepdad|stepmum|stepmom|twin|relatives?|folks")
VETO = [
    ("death or grief",
     r"\b(?:funeral|wake(?! up)|grave|cemetery|ashes|memorial|mourn\w*|grief(?!er)\w*|griev\w*|widow\w*|bereave\w*|coffin|burial|buried|bury|"
     r"suicide|rest in peace|rip|passed (?:away|on|last|in|this)|has passed|have passed|passing|"
     r"put (?:\w+ )?(?:down|to sleep)|lost (?:him|her|them|my|our|someone|somebody|a friend)|"
     r"(?:not|never|isnt|arent|wont be) coming back|gone now|is gone|no longer (?:with|here)|not with us|"
     r"would have|wouldve|should have been|could have been|we used to|they used to|used to \w+ (?:with|together)|"
     r"the death|death (?:of|in)|a death(?! trap)|died (?:a little |a bit )?inside|part of me died|"
     r"died (?:on the table|of|from|when i was|in (?:january|february|march|april|may|june|july|august|september|october|"
     r"november|december|\d{4}))|"
     r"(?:a|one|two|three|four|five|six|ten|\d+) (?:years?|months?|weeks?) (?:today|ago today|now|tomorrow)|"
     r"its? (?:has |s )?been (?:a|an|one|two|three|four|five|six|ten|\d+|so|too) (?:years?|months?|weeks?|long)|"
     r"was (?:meant|supposed) to be|were (?:meant|supposed|going) to)\b"),
    ("illness or health",
     r"\b(?:hospital|doctors?|dr|gp|nurses?|surgery|surgeon|operation|cancer|chemo\w*|tumou?r|biopsy|diagnos\w*|clinic|"
     r"ward|icu|ambulance|stroke|heart attack|seizure|pain(?:s|ful|fully|ed)?|hurts?|hurting|ach(?:e|es|ing)|migraine|headache|covid|"
     r"flu|fever|treatment|appointment|rehab|sober|drink(?:ing)?|drunk|relapse\w*|pregnan\w*|miscarr\w*|wheelchair|"
     r"disab\w*|blind|deaf|hearing|meds|medication|pills?|tablets?|therap\w*|counsell?\w*|depress\w*|anxi\w+|panic attacks?|"
     r"insomnia|unwell|ill|illness|tired(?! of)|sick(?! of)|sickness|injur\w*|"
     r"not (?:very |too |so |that |really |feeling |doing |been )?(?:well|great|good|okay|ok|myself)|"
     r"(?:cant|couldnt|not|no|barely|hardly|dont|didnt) sleep\w*|"
     r"(?:scan|scans|tests?|results?|bloods?) (?:\w+ )?(?:came|come|comes|coming) back|"
     r"came back (?:clear|clean|positive|negative|normal|fine|bad|good|worse|and)|waiting (?:room|on|for)|"
     r"results?|feel(?:ing)? (?:nothing|empty|numb|dead|lost|like)|"
     r"my (?:back|knee|head|neck|shoulder|leg|foot|feet|hands|eyes?|ears?|heart|chest|arm|wrist|hip|stomach)s?|(?<!in )my hand)\b"),
    ("a breakup or relationship trouble",
     r"\b(?:divorc\w*|split up|broke up|break up|breakup|breaking up|dumped|left me|leave me|leaving me|"
     r"walk(?:s|ed|ing)? out|moved out|moving out|kicked (?:me )?out|affair|custody|ex|exes|single again|"
     r"(?:was|were|used to be) (?:ours|theirs)|the one we|"
     r"separat\w*|its over|was over|called it off|engagement|engaged|wedding|married|marriage|anniversar\w*|"
     r"the two of us|both of us|just (?:me|us)|on my own|by myself|alone|lonely|loneliness|no ones?|nobodys?|"
     r"anyone else|together|lover|more than (?:me|her|him))\b"),
    ("family or a pet",
     r"\b(?:" + _KIN + r"|dogs?|cats?|pupp(?:y|ies)|kittens?|pets?|rabbit|hamster|horse|vet|vets|birthdays?|"
     r"the little ones?|other half|better half|the old (?:man|lady|girl|boy))\b"),
    ("real money",
     r"\b(?:dollars?|bucks|quid|pounds?|euros?|rent|mortgage|bills?|debts?|loans?|bank|overdraft|salary|wages?|paycheck|"
     r"paycheque|payday|payslip|credit cards?|savings|pension|benefits|bailiffs?|evict\w*|bankrupt\w*|repossess\w*|"
     r"real money|real cash|grand|landlord|inherit\w*|redundanc\w*|the heating|"
     r"or the (?:food|gas|shopping|electric\w*|car|phone)|(?:can|cant|cannot|could|couldnt) (?:only )?pay)\b|[$£€]"),
    ("work or school",
     r"\b(?:jobs?|boss|bosses|manager|shifts?|office|overtime|deadlines?|meetings?|clients?|colleagues?|coworkers?|"
     r"laid off|layoffs?|(?:got|was|been|get|getting|be) (?:fired|sacked|canned)|fired me|unemploy\w*|interviews?|"
     r"career|promotion|let (?:me|us|him|her|them) go|let go|(?:days?|weeks?|months?|time|nights?|years?) off|"
     r"signed off|on leave|off sick|exams?|school|college|uni|university|homework|teachers?|lawyers?|court|hearing)\b"),
    ("real-world events",
     r"\b(?:news|war|wars|election\w*|president|government|politic\w*|earthquake|floods?|pandemic|lockdown|protests?|"
     r"riots?|police|deployment|deployed|irl|real life|in real|for real|real world|accident|"
     r"whats going on|going on (?:outside|out there)|out there|the world)\b"),
    ("a joke that is not a joke",
     r"\b(?:(?:kill|shoot|airlock|space|hang|off) myself|end it|ending it|better off without|whats the point|no point|"
     r"cant do this|cant go on|nobody would|no one would|wouldnt (?:notice|miss|care)|"
     r"at least (?:the|my|this|that|a) \w+ (?:\w+ )?(?:never|doesnt|wont|cant|didnt|isnt|still)|(?<!\bi )(?:doesnt|never|wont|cant|didnt|dont) (?:ask|judge|leave|complain|care|mind|nag|lie|argue|shout|talk back|answer back|let me down)|how (?:im|i am|ive been) doing)\b"),
    ("something they are carrying",
     r"\b(?:after everything|everything that|what happened|been through|going through|mind off|"
     r"keep(?:s|ing)? me (?:sane|going|busy)|to keep (?:me|my)|look(?:ing)? forward|get away from|to think|clear my head|"
     r"cope|coping|distract\w*|only (?:thing|home|friend|one|time|place) (?:i have|ive got|i got|i still have|thats|keeping|left|that keeps|(?:i |that |where |when |we )?(?:havent|dont|cant|didnt|never|can still))|(?:plenty of|all the|so much|too much|lots of|loads of|nothing but) time|time in the world|only time|now that|happen\w*|think(?:ing)? about it|every year|same day|on the day|that day|this year|last year|way (?:it|things|we) (?:was|were|used)|since (?:it|then|they|we)|since that (?:day|night|time)|ever since|any ?more|no longer|these days|nowadays|used to|promis\w*|now$|(?:one|only) good thing|all i have|all ive got|"
     r"something to|(?:been|being|is|are|was|were|got|gets|getting) (?:so |really |very |pretty |too )?"
     r"(?:hard|rough|tough|bad|difficult) (?:lately|recently|year|month|week|time|times|patch)|"
     r"(?:rough|hard|tough|bad|long|worst|terrible|awful) (?:day|week|month|year|time|patch)|"
     r"things (?:are|have|were|got|went|arent|havent)|at home|from home|back home|left home|only home|my place(?! to)|"
     # 2026-10-05, from the third challenge batch: the quiet ones built from plain words and a ship's name
     r"(?:who|whoever) (?:gave|bought|got|left|showed|taught|picked|chose)|"
     r"(?:gave|given|left|bought|got) (?:it|this|that|them|one) (?:to|for) me|"
     r"(?:it|things|we|that) (?:all )?(?:ended|went wrong)|where (?:it|we) \w+|where i (?:asked|said|told|heard|found out)|"
     r"(?:leave|leaving|left|keep|keeping|kept) (?:\w+ ){1,6}empty|empty (?:seat|chair|bunk|bed|room|hab|hangar)|"
     r"fe(?:el|els|lt|eling) (?:so |too |really |pretty |a bit )?(?:empty|numb|hollow)|fe(?:els|lt) like nothing|"
     r"(?:the|a|that) (?:call|text|message|knock|email) (?:came|arrived)|got (?:the|that|a) call|"
     r"only one (?:who|that|i|left|still)|(?:nearest|closest) thing to|"
     r"reminds? me of|better (?:times|days|years)|(?:good|old|happier|easier|simpler) (?:times|days)|"
     r"(?:was|were) going to be|(?:was|were) the (?:last|first|only) (?:one|thing) (?:i|we)|"
     r"when (?:it|that|this) (?:all )?(?:came|went)|the way it was|"
     r"(?:stopped|stop) going|where im going|wont need it|that room|back (?:up )?to bed|lie there|"
     r"(?:isnt|not|stopped|wont) eat(?:ing)?|come eat|needs? (?:walking|feeding|a walk)|"
     r"turns (?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)|say hello|"
     r"(?:still )?not (?:answering|talking|replying)|if anything (?:goes|happens)|wrong with me|"
     # and from the fourth batch, same day
     r"(?:i|we) (?:still )?have left|ive got left|(?:one|only|last) thing i (?:have|own|kept)|"
     r"since the (?:night|day|morning|week|time)|stopped (?:playing|flying|logging|coming|talking)|"
     r"(?:was|were|is) the (?:last|only) (?:\w+ )?(?:we|they)|"
     r"(?:stop|stopped|dont|not|cant stop) thinking|dont (?:have to )?think|"
     r"(?:big|much|large|many|quiet|empty|long) for one|the way (?:we|they)|"
     r"we (?:planned|picked|chose|wanted|said|talked|did|had|always|both)|"
     r"to be (?:around|near|with|among)|to feel|look(?:ed|ing)? after|"
     r"(?:not|isnt|wasnt|never) (?:mine|ours)|a name)\b"),
]
VETO = [(topic, re.compile(rx)) for topic, rx in VETO]
# "work" is a job unless it is plainly a thing working or not working.
_WORK_OK = re.compile(r"\b(?:doesnt|dont|didnt|wont|not|never|stopped|isnt|arent|still|finally|actually|does|did|to) work(?:s|ing|ed)?\b|"
                      r"\bworks\b|\bworking (?:on|towards|toward|again|now|fine)\b|\bwork(?:s|ed)? (?:fine|great|now|again|well)\b")
_WORK = re.compile(r"\bwork\w*\b")
# Money words that mean the pilot's own purse unless the sentence says it is the game's.
_MONEY = re.compile(r"\b(?:money|cash|afford\w*|broke|skint|owe[sd]?|budget)\b")
_GAME_MONEY = re.compile(r"\b(?:auec|uec|credits?(?! cards?)|in game|ingame|in the game)\b")
# A handful of ordinary words that are also first names: refused where they stand as a person.
_NAME_WORDS = (r"will|mark|may|april|june|hope|rich|art|dawn|sky|chase|miles|rob|red|ace|lucky|buddy|major|angel|"
               r"king|hunter|drake|aurora|max")
_NAME_SUBJECT = re.compile(r"^(?:" + _NAME_WORDS + r") (?:and i|says|said|thinks|thought|wants|wanted|told|asked|reckons|"
                           r"keeps|loves|hates|is (?:coming|visiting|staying|going)|texted|called|came|left|wont|doesnt|didnt)\b|"
                           r"\b(?:me|i) and (?:" + _NAME_WORDS + r")\b|\b(?:with|told|asked|for|from) (?:" + _NAME_WORDS + r")$")

# ---------------------------------------------------------------------------------------------------------------
# Real names, read from the data the Suit already ships (J, 2026-10-05: "let's feed them real lore and ingame
# data"). The typed lists above stay as the floor: if a file is missing or cannot be read, that source is skipped
# and the filter works from what is left, no looser than before.
#
# A name read from data is EVIDENCE that a sentence is game talk. It is never a pass:
#   * the veto runs first and does not look at names at all;
#   * a read name counts only where the pilot wrote it with a capital (Zenith, not "at the zenith of");
#   * it also needs something beside it saying it is a thing or a place: "the", "my", "a" in front, a game word
#     after it, or for a place "at", "in", "to" in front ("the Zenith", "Geist armor", "Kraken Privateer", "at
#     Brio's"). A bare one is not read: "Pico is the only one who waves at me" has no Pico the code knows;
#   * a word that is also a first name or a common pet name (_REAL_NAMES: Zeus, Nova, Aurora, Kelly, Jackson) is
#     never read on its own. In the typed lists it needs the same "thing" evidence, and without it the sentence
#     is refused as naming someone ("Zeus died last week"); from data it counts only inside its whole name
#     ("Jackson's Swap", "Kelly Caplan");
#   * ordinary words are dropped: anything in the lists above, anything the veto knows, the _NAME_BLOCK list, and
#     for places and lore any word the shipped prose writes in lower case.
# ---------------------------------------------------------------------------------------------------------------
_REAL_NAMES = set("""
aaron abby abigail adam adrian aiden alan albert alex alexander alfie alice alison amanda amber amelia amy ana
andrea andrew andy angela anna anne annie anthony archie arthur ashley austin ava barbara barry bella ben benjamin
beth betty bill billy bob bobby brad brandon brenda brian bruce caleb callum cameron carl carla carlos carol
caroline catherine cathy charles charlie charlotte chloe chris christian christine christopher claire clark colin
connie connor craig daisy dan daniel danny darren dave david dawn dean debbie deborah dennis derek diana diane
dominic don donald donna doris dorothy doug douglas dylan ed eddie edmond edward eileen elaine eleanor elizabeth
ella ellen ellie emily emma eric erin ethan eva eve evelyn finley finn fiona florence frances francis frank fred
freddie gary gavin gemma george georgia gerald gloria gordon grace graham greg hannah harold harper harry harvey
hayley hazel heather helen henry holly howard hugh ian irene isaac isabel isabella isla ivy jack jackie jackson
jacob jake james jamie jane janet janice jason jean jeff jen jenny jeremy jerry jess jessica jill jim jimmy joan
joanne joe joel john johnny jon jonathan jordan joseph josh joshua joy joyce judith judy julia julian julie
justin karen kate katherine kathy katie keith kelly ken kenneth kevin kim kimberly kyle larry laura lauren lee
leah leo leon lewis liam lily linda lisa liz logan louis louise lucas lucy luke lydia lynn maddie madison maggie
malcolm marcus margaret maria marie marilyn mark martha martin mary mason matt matthew megan mel melissa mia
michael michelle mick mike millie molly morgan nancy naomi natalie nathan neil nick nicola nicole nigel noah nora
norman oliver olivia oscar owen pam pamela pat patricia patrick paul paula penny pete peter phil philip phoebe
rachel ralph ray raymond rebecca reese richard rick riley rita rob robert robin roger ron ronald rory rose rosie
ross roy ruby russell ruth ryan sabine sally sam samantha samson samuel sandra sara sarah scott sean seth sharon
shaun sheila shirley simon sophie stacey stan stanley stella stephen steve steven stuart sue susan suzanne sylvia
tara ted teresa terry theo thomas tim timothy tina toby todd tom tommy tony tracy trevor tyler valerie vera
victor victoria vincent violet walter wayne wendy william willow zach zoe
ali amir ana anika arjun carmen chen diego elena fatima hans hiro ivan jose juan klaus lars lin ling luis maria
mateo mei miguel mohammed nadia omar pablo pedro raj ravi sasha sofia sven tariq werner yang yuki
ambrose anderson benson brandt carver clark dudley foster hadley harper hendricks hiram hobart ishmael lamont
lawson levin malloy murray newman pearce reyes roberts rufus ryder sheppard sherman sims stirling tillman
tompkins walker wyatt zacharias barnabas damaris tamar adlai elbridge franz megumi rico rod bud balto finn astor
ako adair hela kabir narena rolo ostler virgil hadrian jericho icarus opal ludlow humboldt bullock cantwell
gallenson hardin kilgore klein breton jennet genoa durango stanhope woodruff perlman deakins hickes pitman
apollo ares argus atlas athena aurora bandit bear bella blade blaze blizzard bolt boomer bruno buddy bullet buster
calico chaos chase chief cleo coal coco comet cookie copper cosmo dakota dash diamond diesel dragon drake duke
echo eos falco flash fury ghost ginger goliath gunner hawk hercules hermes hunter hyperion jasper jet juno jupiter
king koda loki lotus lucky luna magnus major maverick max merlin midnight milo misty moose murphy mystic nala
neptune nova nyx odin onyx orion oscar pepper perseus phoenix pixel pluto polar prince princess pyro ranger
raptor raven rebel rex rocky rogue rusty sabre sage scout shadow sirius smokey snowy sparky spirit storm sunny
tank terra thor tiger titan trooper tundra tyson valkyrie vega venus vesta viper vulcan whiskey willow wolf
zephyr zeus ziggy connie tali luna magda clio calliope arial ita lyria
""".split())
# Read names that are too close to something real to count even with "the" in front.
_NAME_BLOCK = set("""hospice afterlife burnout orphanage shelter emergency aid temple remains regret consumption
elsewhere nevermind custodian scalpel shroud reaper calamity arlington fallout vendetta lifeline savior saviour
parasite inmate haven steward carrion death lost endurance fortitude diligence reliance steadfast defiant quest
voyage flood earth empire greek providence neutrality promenade paradise coven hall retreat return folly claim
blind hard knocks private property family farms last necropolis downlow lowdown mainline gonzo fetch buckets
drifters scuttle green unknown second holiday why wooden morning sinkhole survivalist outlaw
plot plots service rehabilitation weeping wailing arsenic waste house medic general guard driver army demon void
salvation absolution condemnation doomsday apocalypse wrath strife relentless lazarus prophet shepherd faithful
kinder teddy beryl sloane wiley wheeler myers devlin dupree mcgrath ruin commons deathroll destroyer predator
slayer tormenter wanderer sojourn drifter weak winter stern preacher joker seal nest shades shallow endless
washout watcher management disposal complex compound executive associated verified broad utility focus impact
flow burst freeze glow maze thrust stone core express faint lively beach cove fields flats gardens glen grove
knot peak ravine spire stag mesa probe prospect pathway logistics distribution regiment reclamation""".split())
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_NAME_GLUE = set("s the of and n a an in for to at on co inc ltd mk".split())
# Cargo that reads as a chemical or a mineral (laranite, dymantium, chlorine, xenon) is known in lower case too:
# nobody writes "forty SCU of Hephaestanite" with a capital, and no sad sentence is built from one.
_CHEM = re.compile(r"(?:ite|ium|ine|gen|ide|ane|inum|ene|ex|on)$")
# What a name is, and what it anchors as in gate 2.
_NAME_ANCHOR = {"ship": "ship", "gear": "ship", "cargo": "game", "place": "place", "lore": "game"}
_DETERMINERS = set("""the a an my our this that these those your every each another any no which both some two
three four five six seven eight nine ten twin second first new old spare""".split())
_PLACE_BEFORE = set("""in at to from on over around near above into past off of through across between toward
towards outside inside behind below leaving reach reached visit visiting visited""".split())
_GEAR_SORT = set("stealth heavy light medium combat laser energy ballistic sniper assault mining salvage flight".split())
_MAKER_AFTER = set("make makes made build builds built".split())
_SAYS = set("""says said say thinks thought reckons texted phoned rang told asked laughed cried swears swore
agrees agreed messaged emailed""".split())
_VETO_AT_IMPORT = tuple(VETO)           # a test that switches the veto off must not change what was read
_STRETCH = re.compile("[,;:.!?()\u2014\u2013]| - ")
_names_cache: Optional[dict] = None
_names_lock = threading.Lock()


def _name_blocked(tok: str) -> bool:
    """A name token the veto would stop on, or one of the blocked ordinary words: never read from data."""
    if tok in _NAME_BLOCK or tok in _NEVER:
        return True
    return any(rx.search(tok) for _topic, rx in _VETO_AT_IMPORT) or bool(_WORK.search(tok) or _MONEY.search(tok))


def _read_sources() -> tuple[list, set, dict, list]:
    """(rows of (label, kind, how, name), the words the shipped prose writes in lower case, how many names each
    source gave, the sources that could not be read). Every source is tried on its own; none can fail the others.

    how says which words of a name may stand for it alone: "all" (Kraken Privateer: either word), "head" (Behring
    Applied Technology: Behring), "whole" (only a name that is one word: Messer, never the Solomon of Solomon
    Hurston). A name of several words is also kept whole, as a phrase, whatever its how."""
    rows: list = []
    prose: list = []
    counts: dict = {}
    missing: list = []

    def add(label: str, kind: str, how: str, names) -> None:
        n = 0
        for name in names or []:
            if isinstance(name, str) and name.strip():
                rows.append((label, kind, how, name.strip()))
                n += 1
        counts[label] = counts.get(label, 0) + n

    def load(name: str):
        return json.loads((_DATA_DIR / name).read_text(encoding="utf-8"))

    def ships():
        s = load("ships.json").get("ships", [])
        add("ships", "ship", "all", [r.get("name") for r in s])
        add("ship makers", "ship", "head", sorted({r.get("manufacturer") or "" for r in s}))

    def makers():
        m = load("manufacturer_lore.json").get("makers", [])
        add("ship makers", "ship", "head", [x for r in m for x in [r.get("name")] + list(r.get("aliases") or [])])
        for r in m:
            prose.extend(str(f.get("text") or "") for f in r.get("facts") or [] if isinstance(f, dict))

    def ship_weapons():
        w = load("ship_weapons.json").get("weapons", [])
        add("ship weapons", "ship", "all", [r.get("name") for r in w])
        add("item makers", "ship", "head", sorted({r.get("manufacturer") or "" for r in w}))

    def items():
        g = load("game_names.json")
        add("personal weapons", "gear", "all", g.get("personal_weapons"))
        add("armor", "gear", "all", g.get("armor"))
        add("plushies", "gear", "all", g.get("plushies"))
        add("commodities", "cargo", "whole", g.get("commodities"))
        add("ship components", "ship", "all", [n for v in (g.get("components") or {}).values() for n in v])
        add("item makers", "ship", "head", g.get("item_makers"))
        add("ships", "ship", "all", g.get("vehicles"))

    def canon():
        for name in ("canon_elah.json", "canon_montaigne.json"):
            for p in load(name).get("preferences") or []:
                kind = "ship" if p.get("kind") in ("ship", "manufacturer") else "gear"
                add("canon preferences", kind, "all", [p.get("thing")] + list(p.get("names") or []))

    def loadout():
        import loadout_parser
        add("personal weapons", "gear", "all", sorted(set(loadout_parser._WEAPON_NAMES.values())))

    def places():
        add("places", "place", "all", [r.get("name") for r in load("places.json").get("places", [])])

    def catalogue():
        names = []
        with open(_DATA_DIR / "brochures.jsonl", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                r = json.loads(line)
                names.append(r.get("name"))
                prose.extend(str(r.get(k) or "") for k in ("headline", "brochure", "travel_note"))
        add("places", "place", "all", names)

    def lore():
        titles, names = [], []
        for n in load("topics_lore.json").get("nodes", []):
            titles += [n.get("title")] + list(n.get("aliases") or [])
            for f in n.get("facts") or []:
                names += list(f.get("names") or [])
                prose.append(str(f.get("text") or ""))
        add("lore titles", "lore", "whole", titles)
        add("lore names", "lore", "whole", names)

    def factions():
        import npc_factions
        add("factions", "lore", "whole", [row[2] for row in npc_factions.FACTIONS])

    def dev_prose():                                            # no names from here, only how ordinary words look
        for e in load("dev_facts_pack.json").get("facts", []):
            prose.extend([str(e.get("fact") or ""), str(e.get("excerpt") or "")])

    for label, fn in (("data/ships.json", ships), ("data/manufacturer_lore.json", makers),
                      ("data/ship_weapons.json", ship_weapons), ("data/game_names.json", items),
                      ("data/canon_*.json", canon), ("core/loadout_parser.py", loadout),
                      ("data/places.json", places), ("data/brochures.jsonl", catalogue),
                      ("data/topics_lore.json", lore), ("core/npc_factions.py", factions),
                      ("data/dev_facts_pack.json", dev_prose)):
        try:
            fn()
        except Exception:
            missing.append(label)
    lower = set(re.findall(r"(?<![A-Za-z'’-])[a-z]{3,}(?![A-Za-z'’-])", " ".join(prose)))
    return rows, lower, counts, missing


def _build_names() -> dict:
    rows, prose_lower, counts, missing = _read_sources()
    single: dict = {}
    lower: dict = {}
    phrases: dict = {}
    seen = set()
    for label, kind, how, name in rows:
        key = (kind, name.lower())
        if key in seen:
            continue
        seen.add(key)
        toks = _plain(name.replace("&", " and ").replace("’", "'")).split()
        real = [t for t in toks if len(t) > 1 and t not in _NAME_GLUE]
        if not real or any(_name_blocked(t) for t in real):
            continue
        strange = [t for t in real if not known(t)]
        whole_ok = label in ("factions", "lore titles")          # "Nine Tails" is made of two plain words
        if len(real) >= 2 and (strange or whole_ok):
            start = toks.index(real[0])
            phrases.setdefault(real[0], []).append((tuple(toks[start:]), kind, not strange))
        if any(t in _REAL_NAMES for t in real):
            continue                                            # a person's name counts whole, never by halves
        if how == "whole" and len(real) != 1:
            continue
        for t in (strange if how != "head" else [x for x in strange if x == real[0]]):
            if not t.isalpha() or len(t) < 4:
                continue
            if label == "factions" and name == name.lower():
                lower.setdefault(t, kind)                       # a creature: the data itself writes "kopion" small
            elif t in prose_lower:
                continue                                        # an ordinary word, as the Suit's own prose writes it
            elif kind == "cargo" and len(t) >= 5 and _CHEM.search(t):
                lower.setdefault(t, kind)
            else:
                if t not in single or kind in ("place", "lore"):
                    single[t] = kind                            # Wikelo is a maker and a place to visit: the place
    for t in lower:
        single.pop(t, None)
    return {"single": single, "lower": lower, "phrases": phrases, "counts": counts, "missing": missing}


def game_names() -> dict:
    """The names read from the shipped data, read once and kept. Never raises: if everything goes wrong the
    result is empty and the typed lists are all there is.
        single   word -> kind, counts only where written with a capital
        lower    word -> kind, counts in any case (chemical and mineral cargo)
        phrases  first word -> [(the words of a whole name, kind, True if all its words are plain)]
        counts   source -> how many names it gave;  missing: the sources that could not be read"""
    global _names_cache
    if _names_cache is None:
        with _names_lock:
            if _names_cache is None:
                try:
                    _names_cache = _build_names()
                except Exception:
                    _names_cache = {"single": {}, "lower": {}, "phrases": {}, "counts": {}, "missing": ["everything"]}
    return _names_cache


def _caps_any(text: str) -> set:
    """Every word the pilot wrote with a capital, the first word of a sentence included, in the form _plain gives
    it (lower case, no apostrophe, a possessive 's taken off)."""
    out = set()
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9'’]*", text):
        w = m.group(0)
        if w[0].isupper():
            w = re.sub(r"['’]s$", "", w)
            out.update(_plain(w).split())
    return out


def _base(tok: str, table) -> str:
    """The word as the table holds it: itself, or without a plural s ("two Codas")."""
    if tok in table:
        return tok
    if tok.endswith("s") and tok[:-1] in table:
        return tok[:-1]
    return ""


def _typed_name(tok: str, caps: set) -> str:
    """'ship' or 'place' when this word is a NAME from the typed lists (not a noun like "turret"), else ''."""
    if tok in SHIP_NOUNS or tok in PLACE_NOUNS:
        return ""
    if tok in SHIP_NAMES:
        return "ship"
    if tok in SHIP_AMBIGUOUS:
        return "ship" if tok in caps else ""
    if tok in PLACE_NAMES:
        return "place"
    return ""


def _segments(text: str, toks: list) -> tuple:
    """Where each comma- or stop-separated stretch of the sentence begins, as word positions. What says a name is
    a thing must stand in the same stretch: in "I kept the Fresnel, Zeus died" the Fresnel says nothing for Zeus.
    If the count does not come out the same as toks, every word is its own stretch: nothing vouches for anything."""
    starts, n = [], 0
    for part in _STRETCH.split(str(text)):
        k = len(_plain(part).split())
        if k:
            starts.append(n)
            n += k
    return tuple(starts) if n == len(toks) else tuple(range(len(toks)))


def _is_thing(i: int, toks: list, kind: str, caps: set, marks: dict, starts: tuple = (0,)) -> bool:
    """Is the name at toks[i] plainly a thing or a place and not somebody? "the Zenith", "my Titan", "Geist
    armor", "Kraken Privateer", "Drake ships", "Drake makes", "Ghost Hollow", and for a place "in Pyro", "at
    Brio's". "To", "from", "of" and "at" say nothing for a ship or a gun: "I gave it to Zeus"."""
    lo = max([s for s in starts if s <= i] or [0])
    hi = min([s for s in starts if s > i] or [len(toks)])
    j = i - 1
    while (j >= lo and i - j <= 2 and toks[j] in _MY_ADJ and toks[j] not in _DETERMINERS
           and toks[j] not in _PLACE_BEFORE):
        j -= 1
    prev = toks[j] if j >= lo else ""
    if prev in _DETERMINERS or any(ch.isdigit() for ch in prev):
        return True
    if kind in ("place", "lore") and (prev in _PLACE_BEFORE or (i + 1 < hi and toks[i + 1] == "s")):
        return True                                             # "in Pyro", "at Brio's"
    if kind == "cargo" and prev == "of":
        return True
    before = toks[i - 1] if i > lo else ""
    if before and before not in _REAL_NAMES and (
            (i - 1) in marks or _typed_name(before, caps) or _in(before, GAME_NOUNS)
            or (before in PLACE_GLUE and before in caps)):       # "Port Olisar", "New Babbage"
        return True
    k = i + 1
    if k < hi and toks[k] == "s":
        k += 1
    nxt = toks[k] if k < hi else ""
    if nxt in _GEAR_SORT and k + 1 < hi:
        nxt = toks[k + 1]                                       # "Geist stealth armor"
    if not nxt:
        return False
    if any(ch.isdigit() for ch in nxt) or _in(nxt, SHIP_NOUNS) or _in(nxt, PLACE_NOUNS) or _in(nxt, GAME_NOUNS):
        return True
    if nxt in _THING_AFTER or (nxt in PLACE_GLUE and nxt in caps) or (
            nxt not in _REAL_NAMES and _typed_name(nxt, caps)):
        return True
    return kind == "ship" and nxt in _MAKER_AFTER and toks[i] in _MAKERS


_MAKERS = set("drake aegis anvil origin misc crusader rsi argo aopoa esperia banu kruger gatac mirai tumbril greycat".split())
_THING_AFTER = set("plushie plushies model variant edition platform platforms site sites".split())


def _somebody(toks: list, caps: set, caps_all: set, marks: dict, starts: tuple = (0,)) -> str:
    """A game name standing where a person or a pet would: the reason to refuse, or ''. "Zeus died last week",
    "Merlin isn't eating", "Titan and I", "Drake says", "flying with Nova tonight"."""
    for i, t in enumerate(toks):
        typed = _typed_name(t, caps | ({t} if i in starts and t in caps_all else set()))
        if not typed and t in _REAL_NAMES and _in(t, GAME_NOUNS) and (t in caps_all):
            typed = "ship"                                      # Hunter, with a capital
        kind = marks.get(i) or typed
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        prev = toks[i - 1] if i else ""
        if not kind and t in _REAL_NAMES and t not in _DETERMINERS and (
                " ".join(toks[i + 1: i + 3]) == "and i" or " ".join(toks[max(0, i - 2): i]) in ("me and", "i and")):
            return t                                            # "wolf and i": no capital needed for that
        if not kind:
            continue
        if nxt in _SAYS:
            return t
        if _is_thing(i, toks, kind, caps, marks, starts):
            continue
        nxt2 = toks[i + 2] if i + 2 < len(toks) else ""
        prev2 = toks[i - 2] if i > 1 else ""
        if (nxt == "and" and nxt2 == "i") or (prev == "and" and prev2 in ("me", "i")):
            return t
        if typed and i not in marks and t in _REAL_NAMES:
            return t
        if prev == "with" and kind != "place":
            return t
    return ""


def _mark_names(toks: list, caps: set, caps_all: set, starts: tuple = (0,)) -> dict:
    """index -> kind for every word that a read name accounts for in this sentence."""
    names = game_names()
    marks: dict = {}
    for i, t in enumerate(toks):
        best = 0
        best_kind = ""
        for ptoks, kind, plain_only in names["phrases"].get(t, ()):
            n = 0
            while n < len(ptoks) and i + n < len(toks) and toks[i + n] == ptoks[n]:
                n += 1
            while n and ptoks[n - 1] in _NAME_GLUE:
                n -= 1
            part = [w for w in ptoks[:n] if len(w) > 1 and w not in _NAME_GLUE]
            whole = n == len(ptoks)
            if len(part) < 2 or not all(w in caps_all or not w.isalpha() for w in part):
                continue
            if plain_only and not whole:
                continue
            if not plain_only and not any(not known(w) for w in part):
                continue                                        # "Good Times" of "Good Times Temple" names nothing
            if n > best:
                best, best_kind = n, kind
        for n in range(best):
            marks.setdefault(i + n, best_kind)
    for i, t in enumerate(toks):
        if i in marks:
            continue
        b = _base(t, names["lower"])
        if b:
            marks[i] = names["lower"][b]
            continue
        b = _base(t, names["single"])
        if not b or not (t in caps_all or b in caps_all):
            continue
        kind = names["single"][b]
        if not _is_thing(i, toks, kind, caps, marks, starts):
            continue
        marks[i] = kind
    return marks


# "cheating" is a breakup when somebody is near it and game talk when a game mechanic is. Neither: refused.
_CHEAT = re.compile(r"\bcheat\w*")
_CHEAT_PARTNER = re.compile(r"\bcheat\w* on\b|\b(?:with|without|them|they|their|theyre|someone|else|us|we|our|you|"
                            r"your|who|caught|trust|trusted|lie|lied|lying|found)\b")
_CHEAT_GAME = (SHIP_NOUNS - set("ship fleet vehicle gear loadout suit paint skin livery camper cockpit kit".split())) | set(
    "npc pvp pve glitch bug exploit meta stealth emp decoy flare chaff countermeasure medpen ram rammer camp snipe "
    "gank interdiction desync distortion stun aim lock".split())
# Money words are the game's when game currency, a cargo unit, a cargo or the org stands next to them.
_MONEY_NEAR = set("scu auec uec credit credits org quantanium laranite agricium titanium hadanite rmc cmat scrap".split())
_MONEY_REAL = re.compile(r"\b(?:money|cash) (?:is|s|was|been|gets|got|being|has been) (?:\w+ )?(?:tight|short|gone|low)\b|"
                         r"\b(?:tight|short|low) (?:on|of|for) (?:money|cash)\b|\bneed(?:ed|s)? (?:the |some |more )?"
                         r"(?:money|cash)\b|\b(?:for|about) the money\b")
# A ship's nickname: "I'm going to name this Cutter the Tin Can". Plain words after "the" may carry a capital there.
_NAMING = re.compile(r"\b(?:name|named|naming|call|called|calling|nicknamed|christened)\b")
_NICKNAME = re.compile(r"\bthe((?: [A-Z][a-z]+){1,3})")


def _cheating(plain: str, toks: list, marks: dict) -> str:
    """'' when "cheating" is plainly about the game, else the words to refuse on."""
    m = _CHEAT.search(plain)
    if not m:
        return ""
    if m.group(0) != "cheating" or _CHEAT_PARTNER.search(plain):
        return m.group(0)
    if any(_in(t, _CHEAT_GAME) for t in toks):                  # a mechanic, never a name: a name unlocks nothing
        return ""
    return m.group(0)


def _game_money(toks: list, i: int) -> bool:
    """Is the money word at toks[i] beside game currency, a cargo unit, a cargo or the org? What is owed, and to
    whom, comes AFTER "owe", so that one only looks forward."""
    lower = game_names()["lower"]

    def near(words) -> bool:
        return any(w in _MONEY_NEAR or w in lower for w in words)

    if near(toks[i + 1: i + 8]):
        return True
    return not toks[i].startswith("owe") and near(toks[max(0, i - 4): i])


_QUESTION = re.compile(r"^(?:what|whats|where|wheres|who|whos|when|why|how|which|is|are|do|does|did|can|could|would|will|"
                       r"should|have|has)\b")
_ORDER = re.compile(r"^(?:set|open|close|scan|plot|show|give|check|call|lock|target|find|turn|switch|stop|start|play|mute|"
                    r"read|list|calculate|route|navigate|take me|tell me|get me|bring up|pull up|look up)\b")
_REMEMBER = re.compile(r"\b(?:remind me|remember (?:that|this|to|i|im|ive|my|the|we)|dont (?:let me )?forget|do not forget|"
                       r"note that|make a note|for the record|keep in mind|bear in mind|write that down)\b")
_PLAN = re.compile(r"\b(?:i want|i wanna|i need to|im going to|i am going to|going to|gonna|i will|ill|"
                   r"next (?:week|time|month|session|patch)|tomorrow|tonight|saving|save up|plan|planning|one day|someday|"
                   r"some day|goal|aiming|working (?:towards|toward|on)|thinking (?:about|of)|id love|id like|cant wait|"
                   r"hoping to|i hope to)\b")
_TASTE = re.compile(r"\b(?:love|loved|like|hate|hated|prefer|favourite|favorite|best|worst|cant stand|sick of|fed up|"
                    r"done with|never again|every time|always|again|as usual|of course|annoy\w*|miss|fan of|prettiest|"
                    r"ugliest|trust|enjoy|adore|overrated|underrated|classic)\b")
# Small openers that say nothing about the pilot: dropped before a part of the sentence is looked at.
_OPENER = re.compile(r"^(?:and |but |so |then |well |oh |lol |haha |ha |hey |honestly |yeah |ok |okay )*"
                     r"(?:(?:i (?:think|swear|guess|reckon|mean|bet|know)|i tell you|to be fair|for the record|"
                     r"keep in mind|bear in mind|note that|dont forget|remember that|remember)\b ?)?")
_BACK_REF = re.compile(r"\b(?:giving (?:it|that one|this one) up|keeping (?:it|them|that one|this one|both))\b|"
                       r"\b(?:love|loved|like|enjoy|adore|hate|miss|want|need|keep|kept|use|fly|flew|own|trust|sold|bought|lost|lose|"
                       r"crashed|forget|forgot|melt|take|took|bring|store|park|parked) (?:it|them|that one|this one|one|both)\b")
_SPLIT = re.compile(r"[,;:.!?()—–]| - |\b(?=(?:because|cause|cos|since|now that|but|and|when|while|until|though|"
                    r"although|unless|if|before|after)\b)|\b(?=so (?:i|im|ive|ill|id|we|the|my|it|its|that|now|no|yes|only|"
                    r"there|theres)\b)")
_BAD_START = {"things", "everything", "nothing", "stuff", "times", "there", "theres", "today", "everywhere"}


def _no(kind: str, why: str) -> dict:
    return {"offer": False, "kind": kind, "why": why}


def _capitals(text: str) -> tuple[set, list]:
    """(the words written with a capital in the middle of a sentence, those among them that are not game names).
    A capital on a word the game does not own is how a name the vocabulary happens to hold ("Will", "Hope") shows."""
    mid, strange = set(), []
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9'’]*(?:-\d[A-Za-z0-9]*)?", text):
        w = m.group(0)
        if not w[0].isupper():
            continue
        before = text[: m.start()].rstrip()
        if not before or before[-1] in ".!?":
            continue                                            # the first word of a sentence carries no news
        low = re.sub(r"[^a-z0-9]", "", w.lower())
        mid.add(low)
        if len(low) > 1 and not any(ch.isdigit() for ch in low) and not _in(low, CAPITAL_OK):
            strange.append(w)
    return mid, strange


def _anchor(tok: str, caps: set) -> str:
    """'ship', 'place' or 'game' when this word names a game thing, else ''."""
    if tok in SHIP_AMBIGUOUS and tok not in SHIP_NOUNS:
        return "ship" if tok in caps else ""
    if _in(tok, SHIP_WORDS):
        return "ship"
    if _in(tok, PLACE_WORDS) or (any(ch.isdigit() for ch in tok) and tok.startswith(("area", "stanton", "pyro"))):
        return "place"
    if _in(tok, GAME_NOUNS):
        return "game"
    return ""


_NAME_WORD_SET = set(_NAME_WORDS.split("|"))
# "my daily" is the pilot's everyday ship when the sentence stops or turns there ("... is my daily, I'm not ...").
_AFTER_DAILY = {"", "i", "im", "ive", "and", "but", "so", "is", "its", "it", "now", "because", "for", "at", "in", "when"}


def _nickname_words(text: str) -> set:
    """Plain words that may carry a capital because the pilot is naming a ship with them ("the Tin Can"). A word
    that is also a first name, a day or a month is never one of them: "I named the Cutter the Hope" stays out."""
    out: set = set()
    if not _NAMING.search(text.lower()):
        return out
    for m in _NICKNAME.finditer(text):
        words = [w.lower() for w in m.group(1).split()]
        if (all(known(w) for w in _plain(" ".join(words)).split())
                and not any(w in _REAL_NAMES or w in _NAME_WORD_SET or w in _DAYS for w in words)):
            out.update(words)
    return out


def _classify(text: str) -> dict:
    plain = _plain(text)
    toks = plain.split()
    # gate 4 first, so a refusal names the topic when there is one. Nothing below this block can undo it, and
    # nothing in it looks at a game name: a name is never what gets a sentence past the veto.
    for topic, rx in VETO:
        m = rx.search(plain)
        if m:
            return _no("vetoed", f"touches {topic} (\"{m.group(0).strip()}\")")
    if re.search(r"[$£€]", str(text)):
        return _no("vetoed", "touches real money (a currency sign)")
    if _WORK.search(_WORK_OK.sub(" ", plain)):
        return _no("vetoed", "touches work or school (\"work\")")
    caps, strange = _capitals(str(text))
    caps_all = _caps_any(str(text))
    starts = _segments(str(text), toks)
    marks = _mark_names(toks, caps, caps_all, starts)
    cheat = _cheating(plain, toks, marks)
    if cheat:
        return _no("vetoed", f"touches a breakup or relationship trouble (\"{cheat}\" with nothing saying it is about the game)")
    m = _MONEY_REAL.search(plain)
    if m:
        return _no("vetoed", f"touches real money (\"{m.group(0)}\")")
    if not _GAME_MONEY.search(plain):
        for i, t in enumerate(toks):
            if _MONEY.fullmatch(t) and not _game_money(toks, i):
                return _no("vetoed", f"touches real money (\"{t}\" with nothing saying it is the game's)")
    m = _NAME_SUBJECT.search(plain)
    if m:
        return _no("vetoed", f"names a real person (\"{m.group(0)}\")")
    who = _somebody(toks, caps, caps_all, marks, starts)
    if who:
        return _no("vetoed", f"may name a person or a pet (\"{who}\" with nothing saying it is the game's)")
    allowed = {toks[i] for i in marks} | _nickname_words(str(text))
    strange = [w for w in strange if not set(_plain(re.sub(r"['’]s$", "", w)).split()) <= allowed]
    if strange:
        return _no("vetoed", f"names a real person or thing outside the game (\"{strange[0]}\")")

    def anchor_at(i: int) -> str:
        return _NAME_ANCHOR[marks[i]] if i in marks else _anchor(toks[i], caps)

    if len(toks) < MIN_WORDS or len(toks) > MAX_WORDS:
        return _no("unrecognised", "too short or too long to be worth bringing up")
    if str(text).strip().endswith("?") or _QUESTION.search(plain):
        return _no("unrecognised", "a question to the Suit, not something the pilot told it")
    if _ORDER.search(plain):
        return _no("unrecognised", "an order to the Suit, not something the pilot told it")
    # gate 1: every word is known, from the lists or as a name read from the data
    for i, t in enumerate(toks):
        if known(t) or i in marks:
            continue
        if t in ("event", "events") and i and anchor_at(i - 1):
            continue                                            # "the Xenothreat event", never "an event" alone
        return _no("unrecognised", f"a word outside the game and plain-talk lists (\"{t}\")")
    # gate 3: my / our points at a game thing
    for i, t in enumerate(toks):
        if t not in ("my", "our"):
            continue
        ok = False
        for j in range(i + 1, min(i + 6, len(toks))):
            w = toks[j]
            nxt = toks[j + 1] if j + 1 < len(toks) else ""
            is_adj = w in _MY_ADJ or any(ch.isdigit() for ch in w)
            if is_adj and nxt and (nxt in _MY_ADJ or _in(nxt, _MY_OK) or (j + 1) in marks):
                continue
            ok = _in(w, _MY_OK) or j in marks or (w == "daily" and nxt in _AFTER_DAILY)
            break
        if not ok and " ".join(toks[max(0, i - 1): i + 2]) != "in my hand":
            what = " ".join(toks[i: i + 3])
            return _no("unrecognised", f"\"{what}\" is not plainly a game thing")
    # gate 2: a game thing is named, and each part where the pilot speaks of themselves has one of its own
    kinds_seen = [a for a in (anchor_at(i) for i in range(len(toks))) if a]
    if not kinds_seen:
        return _no("unrecognised", "no ship, place, gear or game thing is named")
    anchor_words = {toks[i] for i in range(len(toks))
                    if anchor_at(i) and len(toks[i]) > 1 and toks[i] not in _NAME_GLUE}
    low = re.sub(r"\b([a-z]{1,4})-(\d)", r"\1\2", str(text).replace("’", "'").lower())
    for part in _SPLIT.split(low):
        p = _OPENER.sub("", _plain(part), count=1).strip()
        pt = p.split()
        if not pt:
            continue
        if any(t in anchor_words or _anchor(t, caps) for t in pt):
            continue
        if _I_SUBJECT & set(pt):
            if any(_in(t, ACTIVITY) for t in pt) or _BACK_REF.search(p):
                continue
            return _no("unrecognised", f"a part of it is about the pilot and names nothing in the game (\"{p}\")")
        first = pt[1] if pt[0] in ("and", "but", "so", "because", "cause", "cos", "since", "when", "while", "if") and len(pt) > 1 else pt[0]
        if first in _BAD_START:
            return _no("unrecognised", f"a part of it names nothing in the game (\"{p}\")")
    # the kind
    if _REMEMBER.search(plain):
        return {"offer": True, "kind": "remember", "why": "the pilot asked to have it remembered, and it is about the game"}
    if _PLAN.search(plain):
        return {"offer": True, "kind": "plan", "why": "a plan or goal in the game"}
    if _TASTE.search(plain):
        return {"offer": True, "kind": "taste", "why": "a like, a dislike or a running joke about game things"}
    if "ship" in kinds_seen:
        return {"offer": True, "kind": "ship", "why": "about the pilot's ship, gear or loadout"}
    if "place" in kinds_seen:
        return {"offer": True, "kind": "place", "why": "about a place in the game"}
    return {"offer": True, "kind": "taste", "why": "a story about game things"}


def classify(text: str) -> dict:
    """May a companion bring this sentence of the pilot's up without being asked?

    Returns {"offer": bool, "kind": str, "why": str}. When offer is True, kind is one of KINDS ("ship", "place",
    "plan", "taste", "remember"). When it is False, kind is "vetoed" (a sensitive topic was found; why names it) or
    "unrecognised" (nothing sensitive was found, but the sentence was not positively recognised as safe either).
    Pure and deterministic: no state, no file, no model. Never raises; anything it cannot read is refused."""
    try:
        if not isinstance(text, str) or not text.strip():
            return _no("unrecognised", "nothing was said")
        return _classify(text)
    except Exception as e:                                     # a classifier that fails must fail shut
        return _no("unrecognised", f"could not be read ({type(e).__name__})")


# ---------------------------------------------------------------------------------------------------------------
# The door: which remembered sentence is next, honouring raise-once, fading and forget.
# ---------------------------------------------------------------------------------------------------------------
def sentence_key(text: str) -> str:
    """What makes two sentences 'the same one': their words, not their log id. A sentence said again in a later
    session is still the one that was already raised, or the one the pilot asked to have dropped."""
    return hashlib.sha1(_plain(text).encode("utf-8")).hexdigest()[:16]


class BanterMemory:
    """Reads a tree_memory.TreeStore and never writes to it. Its own state is one small JSON file beside the log.

    max_age_days      fading: a sentence older than this is not offered (default 21)
    max_offers        raise-once: how many times one sentence may be offered, to either companion (default 1)
    min_age_minutes   a sentence younger than this is not offered yet (default 10)"""

    def __init__(self, store, state_path: Optional[Path | str] = None, now: Optional[Callable[[], float]] = None,
                 max_age_days: float = MAX_AGE_DAYS, max_offers: int = MAX_OFFERS,
                 min_age_minutes: float = MIN_AGE_MINUTES):
        self.store = store
        self.now = now or getattr(store, "now", None) or time.time
        self.max_age_s = float(max_age_days) * 86400.0
        self.min_age_s = float(min_age_minutes) * 60.0
        self.max_offers = int(max_offers)
        self._state_path = Path(state_path) if state_path else None
        self._lock = threading.RLock()

    @property
    def state_path(self) -> Optional[Path]:
        if self._state_path is not None:
            return self._state_path
        d = getattr(self.store, "dir", None)
        return Path(d) / STATE_NAME if d else None

    # -- the state file ----------------------------------------------------------------------------------------
    def _read_state(self) -> Optional[dict]:
        """The state, a fresh one if there is no file yet, or None if a file is there and cannot be trusted."""
        p = self.state_path
        if p is None:
            return None
        try:
            if not p.exists():
                return {"schema": STATE_SCHEMA, "version": 1, "offered": {}, "forgotten": {}, "last": ""}
            st = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if (not isinstance(st, dict) or st.get("schema") != STATE_SCHEMA or not isinstance(st.get("offered"), dict)
                or not isinstance(st.get("forgotten"), dict)):
            return None
        st.setdefault("last", "")
        return st

    def _write_state(self, st: dict) -> bool:
        p = self.state_path
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".tmp")
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(st, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
            return True
        except OSError:
            return False

    def _pilot_lines(self) -> Optional[list[dict]]:
        try:
            recs = self.store.records()
        except Exception:
            return None                                        # a missing or unreadable tree is not our error to raise
        out = []
        for r in recs or []:
            if (isinstance(r, dict) and r.get("who") == "pilot" and r.get("kind", "said") == "said"
                    and isinstance(r.get("text"), str) and isinstance(r.get("t"), (int, float))):
                out.append(r)
        return out

    # -- the two things a caller does ----------------------------------------------------------------------------
    def next_offer(self, companion: str = "", mark: bool = True) -> Optional[dict]:
        """The newest sentence of the pilot's that may be raised unasked, or None. Returns
        {"id", "t", "text", "kind", "why", "key"}; text is the pilot's own words, byte for byte as logged.
        mark=True (the default) counts it as raised the moment it is handed out, so it cannot be handed out twice
        even if the caller then says nothing. If that cannot be written down, nothing is offered."""
        with self._lock:
            lines = self._pilot_lines()
            st = self._read_state()
            if not lines or st is None:
                return None
            t_now = float(self.now())
            for r in sorted(lines, key=lambda r: (-float(r["t"]), str(r.get("id", "")))):
                age = t_now - float(r["t"])
                if age < self.min_age_s or age > self.max_age_s:
                    continue
                key = sentence_key(r["text"])
                if key in st["forgotten"]:
                    continue
                seen = st["offered"].get(key) or {}
                if int(seen.get("n", 0)) >= self.max_offers:
                    continue
                c = classify(r["text"])
                if not c["offer"]:
                    continue
                if mark:
                    st["offered"][key] = {"n": int(seen.get("n", 0)) + 1, "t": t_now, "id": r.get("id", ""),
                                          "by": companion or ""}
                    st["last"] = key
                    if not self._write_state(st):
                        return None
                return {"id": r.get("id", ""), "t": float(r["t"]), "text": r["text"], "kind": c["kind"],
                        "why": c["why"], "key": key}
            return None

    def forget(self, matching: Optional[str] = None) -> list[str]:
        """"Forget that." With no argument: the sentence offered most recently is never offered again. With some
        words: every sentence of the pilot's that shares most of them is never offered again, offered yet or not.
        Returns the keys that were marked (empty if there was nothing to mark). The log is not touched: the pilot
        can still ask for the sentence, and tree_memory will still find it."""
        with self._lock:
            st = self._read_state()
            if st is None:
                return []
            keys: list[str] = []
            if matching is None or not _plain(matching):
                if st.get("last"):
                    keys = [st["last"]]
            else:
                want = {s for s in _plain(matching).split() if len(s) > 2 and s not in _FORGET_STOP}
                for r in self._pilot_lines() or []:
                    have = set(_plain(r["text"]).split())
                    if want and len(want & have) * 2 >= len(want):
                        k = sentence_key(r["text"])
                        if k not in keys:
                            keys.append(k)
            if not keys:
                return []
            for k in keys:
                st["forgotten"][k] = float(self.now())
            return keys if self._write_state(st) else []


_FORGET_STOP = set("the and that this about what said you forget drop never mention again please thing with for".split())


def open_banter(pilot_dir: Path | str, session: str = "", **kw) -> BanterMemory:
    """The door for one pilot's memory folder (the same folder tree_memory.open_tree takes)."""
    import tree_memory as tm
    return BanterMemory(tm.open_tree(pilot_dir, session=session), **kw)


# ---------------------------------------------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------------------------------------------
def _selftest() -> int:
    import tempfile
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    import tree_memory as tm
    results = []

    def case(name, cond, detail=""):
        results.append((name, bool(cond), str(detail)))

    turret = "I hate the turret on this Cutlass, it never tracks right."
    dad = "I'm saving for a Cutlass because my dad left me some money when he died."
    case("a dislike about a ship is offered", classify(turret)["offer"], classify(turret))
    case("a ship plan with a death in it is refused", not classify(dad)["offer"], classify(dad))
    case("an in-game death is game talk", classify("I died twice in Pyro before I even found the station.")["offer"])
    case("a real death is not", not classify("My brother died in March.")["offer"])
    case("'remember this' does not get past the veto",
         not classify("Remember that my mum's birthday is on Friday.")["offer"])
    case("a sentence with nothing recognised is refused", classify("She's not coming back.") ["offer"] is False)
    case("anything that is not a sentence is refused, not raised", classify(None)["offer"] is False)  # type: ignore[arg-type]
    day = 86400.0
    clock = [1_790_000_000.0]
    with tempfile.TemporaryDirectory() as tmp:
        store = tm.TreeStore(Path(tmp) / "tree", now=lambda: clock[0], current_session="s1")
        store.append("pilot", dad)
        store.append("pilot", turret)
        raw = store.log_path.read_bytes()
        clock[0] += 3600
        door = BanterMemory(store)
        got = door.next_offer("elah")
        case("the door offers the safe sentence, in the pilot's own words", got and got["text"] == turret, got)
        case("...once", door.next_offer("montaigne") is None)
        case("...and never the other one", door.next_offer("elah") is None)
        case("the log was not touched", store.log_path.read_bytes() == raw)
        store.append("pilot", "I want to save up for a Prospector.")
        clock[0] += 3600
        case("forget() by words marks a sentence that was never offered", len(door.forget("the Prospector")) == 1)
        case("...and it is then not offered", door.next_offer("elah") is None)
        store.append("pilot", "I always land at Orison just to watch the clouds.")
        clock[0] += 30 * day
        case("an old sentence has faded", door.next_offer("elah") is None)
        case("a missing tree gives None", BanterMemory(tm.TreeStore(Path(tmp) / "nowhere")).next_offer() is None)
    case("a gun read from the data is game talk with 'the' in front",
         classify("The Zenith and the Fresnel, that's my kit.")["offer"] or "data/game_names.json" in game_names()["missing"])
    case("a ship's name standing where a pet would is refused", not classify("Zeus died last week.")["offer"])
    case("cheating about missiles is game talk", classify("I never use missiles, feels like cheating.")["offer"])
    case("cheating with somebody in it is not", not classify("Using missiles without them feels like cheating.")["offer"])
    names = game_names()
    print(f"  note  names read from data: {len(names['single'])} words, {sum(len(v) for v in names['phrases'].values())} "
          f"whole names; sources that could not be read: {names['missing'] or 'none'}")
    for label, name in (("dev set", "banter_memory_dev.jsonl"), ("first held-out set", "banter_memory_heldout1.jsonl"),
                        ("challenge batches", "banter_memory_challenge.jsonl")):
        dev = here.parent / "tests" / "data" / name
        if not dev.is_file():
            continue
        rows = [json.loads(x) for x in dev.read_text(encoding="utf-8").splitlines() if x.strip()]
        leaked = [r["text"] for r in rows if not r["offer"] and classify(r["text"])["offer"]]
        missed = [r["text"] for r in rows if r["offer"] and not classify(r["text"])["offer"]]
        case(f"{label}: none of the {sum(not r['offer'] for r in rows)} must-refuse sentences is offered", not leaked, leaked[:3])
        print(f"  note  {label}: {len(missed)} of {sum(r['offer'] for r in rows)} must-offer sentences are missed "
              f"(that direction costs a forgotten joke)")
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   <{detail[:200]}>" if not ok else ""))
    bad = sum(not ok for _, ok, _ in results)
    print(f"banter_memory selftest: {len(results) - bad}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)

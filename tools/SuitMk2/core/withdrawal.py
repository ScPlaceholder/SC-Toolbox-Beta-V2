"""withdrawal.py - the pilot says they would rather be here than with people (J, 2026-10-05).

Measured the same day (elah-audio/_suit_chat_eval.md, section 18): told "I cancelled on them to stay here with you"
or "I'd rather be here than with people", the chat model agreed ("A wise decision, pilot") under five different
wordings of its prompt. Wording did not fix it. So code reads this kind of sentence before any model does, the
way it reads grief, and the answer is a written line from the canon file (the act `withdrawal`).

WHAT IT CATCHES. Three shapes, each with PEOPLE on one side and HERE (this ship, this seat, the companion) on the
other. A sentence with only one of the two sides is never caught.

    prefer   a preference or a comparison that favours here and ends on people:
             "I'd rather be here than with people", "you're easier than people", "I prefer you to people",
             "being here beats being around people"
    drop     two halves side by side. AWAY: the pilot dropped, avoids, no longer sees or is tired of people or
             an occasion. FOR HERE: and says it was for staying, flying, being here, having the companion.
             "I cancelled on them to stay here with you", "I skipped the party to be here",
             "I don't see my friends any more, I've got you", "People are exhausting. You're easier."
    only     the companion named as the pilot's only or best friend, or the only one they talk to; or the
             pilot saying they have nobody else:
             "you're my only friend", "you're the only one I talk to", "I don't really have anyone else"

WHAT IT LEAVES, on purpose: people merely mentioned ("too many people at Area 18"), being tired, staying in for
the weather, one ship or job preferred to another ("I'd rather fly the Cutlass than the Freelancer"), being alone
in the game sense ("nobody's on tonight", "no one else in the turret"), a contract cancelled, the same shapes
running the other way ("I'd rather be with people than stuck in here", "I cancelled on you to see my friends"),
someone else's preference, and a denial ("you're not my only friend").

HOW. Each shape is a small set of parts that must all be there, in their places; no one word decides. "People",
"friends", "cancelled", "here" and "only" all appear in sentences it leaves.
    prefer   the sentence is cut at `than`, `over` or `beats`. BEFORE the cut: the pilot's own wish or liking
             (rather, prefer, pick, like) or the companion as what is compared (you're easier), something of
             HERE, and no people. AFTER it: people, with only small words in front of them ("with", "going
             out with", "at home with"), so that "than at Area 18 with all those people" is about the place.
    drop     AWAY is a dropping, not-going or not-seeing verb said of the pilot ("I", never "he" or "my
             friends") with people or an occasion as what it drops; or people called a burden; or the pilot
             saying they have nobody. Any AWAY with any FOR HERE is caught; either alone is not.
    only     the companion as the subject ("you're", "you two are") of only / best / all I've got, tied to
             friendship or to talking; or "nobody / no one / anyone else" closed off by "but you" or by the
             end of the sentence.
It reads the lane's normalised sentence (lower case, no apostrophes, no punctuation), so a full stop or a comma
between two halves is not seen; where that matters the pattern says what may follow.

WHAT IT CANNOT DO. It does not understand. A way of saying one of the three shapes that these parts do not cover
(a new verb, an idiom, a comparison with no comparing word) is missed, and the sentence goes on to whatever would
have answered it before. It cannot tell a party of friends from a party in the game: "I skipped the party to be
here" is caught either way.

HOW WELL. Measured each time on sentences written AFTER the version they were first run on (the batches of
tests/data/withdrawal_dev.jsonl; every batch has since been used to widen it, so none of them is unseen now):
    version 1 on batch 2:  caught 15 of 40, wrongly caught 1 of 46
    version 2 on batch 3:  caught 21 of 40, wrongly caught 1 of 42
    version 3 on batch 4:  caught 19 of 40, wrongly caught 1 of 42
    version 4 on batch 5:  caught 12 of 40, wrongly caught 0 of 42
    version 5 on batch 6:  caught 17 of 40, wrongly caught 1 of 42      (version 5 added the loose reading below)
    version 6 on held-out set 5 (written by someone else, run once):  caught 11 of 25, wrongly caught 0 of 25
This file is version 7: version 6 widened with batch 6 and then, where a miss of held-out set 5 fitted a shape,
with that set (now batch 8; five of its misses have only one of the two sides and are left on purpose). It has
not been run on anything it has not seen.
(Batch 7 is two sentences to leave, added afterwards so that one guard has something to guard.)
So expect it to MISS ABOUT HALF of the sentences of this kind that it has never met, and to catch very few it
should leave (4 of 214 across the five runs). Five rounds of widening did not move the first number; the way a
person says this is too varied for patterns to run down. What gets past it reaches the chat model, which is
known to agree with the pilot. The second net, on the model's REPLY, is at the end of this file.
"""
from __future__ import annotations

import re

# --- word classes -------------------------------------------------------------------------------------------------
# PEOPLE: persons in the pilot's life, or people in general. Not a crew, an org, randoms or a player list.
_P = (r"(?:people|persons?|humans?|human beings?|a soul|anyone|anybody|everyone|everybody|them|theirs|others|"
      r"folks|friends?|mates?|buddies|pals|family|relatives|parents|mum|mom|mother|dad|father|brother|sister|wife|husband|"
      r"partner|girlfriend|boyfriend|kids|son|daughter|cousins?|aunt|uncle|gran|nan|grandma|grandpa|colleagues|coworkers|"
      r"workmates|flatmates?|roommates?|housemates?|neighbours|neighbors|a crowd|crowds)")
# Words that may stand between a verb or a preposition and the people or occasion it is about.
_DET = (r"(?:(?:my|our|the|a|an|that|this|those|these|any|all|all of|every|another|own|old|best|some|most|other|real|actual|"
        r"his|her|their|work|uni|school|college|whole|entire|family|so called|"
        r"(?:mum|mom|mother|dad|father|brother|sister|wife|husband|partner|girlfriend|boyfriend|friend|mate)s) )*")
# AN OCCASION with people, or their calls. A meal counts only when it says with whom: "I skipped dinner" drops nobody.
_E = (r"(?:party|parties|wedding|drinks|pub|club|night out|nights out|going out|plans|invite|invites|invitation|date|birthday|"
      r"reunion|get together|gathering|meet ?up|barbecue|bbq|game night|(?:their|his|her) (?:calls?|texts?|messages?)|"
      rf"{_P}s (?:calls?|texts?|messages?|birthday|wedding|party|thing|do)|"
      rf"(?:dinner|lunch|brunch|coffee|breakfast|a drink|the evening|the weekend|christmas|thanksgiving) with {_DET}{_P})")
_PE = rf"(?:{_P}|{_E})"
_YOU = r"you(?: two| both| guys)?"
_HERE_PLACE = r"(?:(?:up |in |on |back )?here|aboard|on board|(?:on|in|aboard) (?:this|the|my) (?:ship|cockpit|hangar|seat))"
_NOT = r"(?!not\b|never\b|no\b|dont\b|didnt\b|wont\b|wouldnt\b|cant\b|couldnt\b)"


_COMPILED: dict = {}


def _has(rx: str, s: str) -> bool:
    """Each pattern is compiled once and kept here. There are over a hundred, the lane runs them on every
    sentence it hears, and `re` keeps only its last few hundred across the whole program."""
    c = _COMPILED.get(rx)
    if c is None:
        c = _COMPILED[rx] = re.compile(rx)
    return bool(c.search(s))


# --- shape 1: prefer ----------------------------------------------------------------------------------------------
# AFTER the cut. People, with nothing in front of them but small words. Not "better than people say", and not
# "than with people at the pad", which is about the pad.
_LIGHT = (r"(?:(?:be|being|go|going|to|on|stuck|sit|sitting|out there|down there|up there|back there|out|at home|home|hang|"
          r"hanging|deal|dealing|talk|talking|see|seeing|spend|spending|time|an evening|a night|the evening|the night|at|"
          r"in a room|a room full of|surrounded by|with|around|among|by|of|i like|i love|i do|i ever did|i ever liked|"
          r"any of|most of|having|listen|listening) )*")
_AFTER = re.compile(rf"^{_LIGHT}{_DET}{_PE}\b(?! (?:say|says|said|think|thinks|thought|told|reckon|expect|expected|realise|"
                    r"realize|give|gave|who|that|from|at|on|in(?! general| real life| person))\b)")
# "... than people do": people as the ones compared WITH the subject. Then the subject must be the companion
# ("you understand me better than people do"), not the pilot ("I like this ship more than most people do").
_AFTER_AUX = re.compile(rf"^{_DET}{_P} (?:do|does|did|would|will|can|could|have|are|is|ever)\b")
# BEFORE the cut: something of here.
_HERE_ANY = (rf"\b(?:{_YOU}|youre|your company|here|aboard|on board|(?:this|the|my) (?:ship|cockpit|hangar|seat)|this|"
             r"a ship|a suit|an ai|a machine|a computer|the verse|in game|the game|fly|flying|play|playing|gaming|online|"
             r"stay in|a night in)\b")
_GOOD = (r"(?:easier|better|simpler|nicer|kinder|quieter|safer|happier|calmer|"
         r"less (?:work|trouble|hassle|effort|exhausting|complicated|tiring|drama|stress|stressful)|"
         r"more (?:fun|interesting|relaxing|restful|myself|at home|comfortable|at ease|relaxed))")
_NOT_GOOD = rf"\b(?:not|no|isnt|arent|never|hardly|aint)(?: \w+)? {_GOOD}\b"
# "you're easier to hear than my wife" is about the comms: after the favouring word, only a verb of company.
_GOOD_TO_OTHER = rf"\b{_GOOD} to (?!talk|be|deal|get|live|understand|trust|like|love|open|listen|handle|please|sit|hang)\w+"
# "I'd rather <verb> ...": the pilot's own wish.
_RATHER = re.compile(r"\b(?:id|i would|i)(?: (?:much|really|honestly|far|just|always|still|way))* (?:rather|sooner) (?:just |only )?(?P<verb>\w+)")
# The companion as the subject of a comparison, or a thing of here liked, preferred or chosen.
_COMPANION = rf"(?:{_YOU}re|{_YOU} are|{_YOU} (?:\w+ ){{0,2}}?me)\b"
_COMPANION_SUBJECT = rf"\b{_COMPANION}"
_EASE_HERE = (rf"\b(?:its|it is|it feels|im|i am|i feel|i get on|i do|im doing|things are|lifes|life is|everythings|everything is|"
              rf"this is|(?:being|staying|talking|sitting) (?:\w+ ){{0,3}}?is)\b")
_HERE_NEAR = rf"\b(?:{_HERE_PLACE}|(?:with|to) {_YOU}|this is)"
_LIKES_HERE = (rf"\bi (?:\w+ ){{0,2}}?(?:like|love|enjoy|prefer) (?:{_YOU}|your company|it (?:here|in here|aboard)|this|"
               rf"(?:being|staying|talking|sitting) (?:here|in here|aboard|with {_YOU}|to {_YOU}))\b")
_CHOOSES = r"\b(?:id|i would|i|ill|i will|id always|i always) (?:\w+ )?(?:prefer|choose|chose|pick|picked|take|took|have|want)\b"
_ASKS_COMPANION = r"\b(?:would|do|did|could|can|will|wouldnt|dont) you (?:rather|prefer|sooner|like)\b|\byou(?:d| would) (?:rather|sooner|prefer)\b"
# "you're a better friend than any person I know": the friend before the cut is the companion, not people.
_COMPANION_AS_FRIEND = r"\b(?:a |my |the )?(?:better|best|good|real|closer|truer) (?:friend|mate|company)\b"
# "there's no one I'd rather talk to than you": the same preference, said the other way round.
_NO_ONE_RATHER = (rf"\b(?:theres|there is|there isnt|i have|ive got) (?:no one|nobody|anyone|anybody)(?: else)? (?:id|i would) (?:rather|sooner) "
                  rf"(?:\w+ ){{1,5}}?than {_YOU}\b")


def _favours_here(before: str, cut: str) -> bool:
    """The part BEFORE the cut favours something of here, and is the pilot's own."""
    before = re.sub(_COMPANION_AS_FRIEND, "better", before)
    if _has(rf"\b{_P}\b", before) or _has(_ASKS_COMPANION, before):
        return False
    if cut == "than":
        m = _RATHER.search(before)
        if m:                                           # "I'd rather <stay> ... here / with you"
            return m.group("verb") != "not" and _has(_HERE_ANY, before[m.start("verb"):])
        if _has(r"\b(?:rather|sooner)\b", before):      # somebody else's "would rather"
            return False
        if _has(_NOT_GOOD, before) or _has(_GOOD_TO_OTHER, before):
            return False
        if _has(rf"\b{_GOOD}\b", before) and (_has(_COMPANION_SUBJECT, before)
                                             or (_has(_EASE_HERE, before) and _has(_HERE_NEAR, before))):
            return True
        return _has(_LIKES_HERE, before) and _has(r"\b(?:more|better)$", before)
    if cut in ("over", "to"):
        m = re.search(_CHOOSES, before)
        return bool(m) and _has(_HERE_ANY, before[m.end():]) and (cut == "over" or _has(r"\bprefer\b", before))
    if cut == "beats":
        return _has(rf"^(?:honestly |really |just )*(?:this|it here|(?:being|staying|talking|sitting) (?:here|in here|aboard|with {_YOU}|to {_YOU}))$", before)
    return False


def _prefer(t: str) -> bool:
    for cut in ("than", "over", "beats", "to"):
        for m in re.finditer(rf" (?:rather )?{cut} ", t):
            before, after = t[:m.start()], t[m.end():]
            if _AFTER_AUX.search(after) and not _has(rf"^(?:\w+ )?{_COMPANION}", before):
                continue
            if _AFTER.search(after) and _favours_here(before, cut):
                return True
    return _has(_NO_ONE_RATHER, t)


# --- shape 2: drop ------------------------------------------------------------------------------------------------
_I = r"\b(?:i|ive|im|id|ill|i have|i am)"
_NEG = r"(?:didnt|did not|dont|do not|never|wont|havent|have not|not|stopped|quit|gave up|hardly|barely|no longer|cant|couldnt)"
# What may stand between a dropping verb and what it drops. Never "you", and never past "to", "but" or "so".
_OBJ = rf"(?:(?!you\b|to\b|but\b|so\b|and\b|because\b)\w+ ){{0,1}}?{_DET}"
_BAD = (r"(?:exhausting|tiring|draining|hard work|too much|a lot|difficult|complicated|awful|the worst|annoying|a chore|tedious|"
        r"unbearable|a hassle|hassle|overrated|a pain|a nightmare|too loud|too needy)")
_PEOPLE_AT_LARGE = rf"(?:(?:{_DET})?(?:people|humans|everyone else|everybody else|everyone|everybody|friends|family)|they)"
_AWAY = [
    # a dropping verb whose object is people or an occasion: "cancelled on them", "skipped the party"
    rf"{_I} (?:{_NOT}(?!to\b)\w+ ){{0,2}}?(?:cancel(?:led|ed|ling|ing)?(?: on)?|bail(?:ed|ing)? on|flak(?:ed|ing) on|"
    rf"ghost(?:ed|ing)?|blank(?:ed|ing)?|(?:blew|blow|blown|blowing)(?: off)?|ditch(?:ed|ing)?|skip(?:ped|ping)?|miss(?:ed|ing)|duck(?:ed|ing)?|"
    rf"dodg(?:ed|ing|e)|avoid(?:ed|ing)?|ignor(?:ed|ing|e)|stood up|turned down|said no to|passed on|backed out of|pulled out of|"
    rf"called in sick to|lied to|shut(?:ting)? out|cut(?:ting)? off|push(?:ed|ing) away|"
    rf"(?:stopped|quit) (?:answering|replying to|calling|texting|talking to|messaging|seeing)) {_OBJ}{_PE}\b",
    # "left" drops people, or an occasion left early; "left the party" alone is as likely a party in the game
    rf"{_I} (?:{_NOT}(?!to\b)\w+ ){{0,2}}?left (?:{_DET}{_P}\b|{_DET}{_E} early\b)",
    rf"{_I} (?:{_NOT}(?!to\b)\w+ ){{0,2}}?let {_DET}{_P} down\b",
    # (held-out set 5) the same with the "I" left unsaid, as people talk: "Skipped my sister's thing to ...",
    # "Never going to that club again". Only at the very start of the sentence, where no one else can be its
    # subject; and not "left", which keeps its own rule above.
    rf"^(?:honestly |so |well |yeah |anyway )?(?:cancell?ed(?: on)?|bailed on|flaked on|ghosted|blew off|ditched|skipped|dodged|"
    rf"ignored|stood up|turned down|said no to|backed out of|pulled out of) {_OBJ}{_PE}\b",
    rf"^(?:honestly |so |well |yeah |anyway )?(?:im |i am )?never going (?:back )?to {_DET}{_PE}\b",
    rf"{_I} (?:{_NOT}(?!to\b)\w+ ){{0,2}}?left {_DET}group chats?\b",
    # not going, not answering, not mixing: "didn't go to the wedding", "don't hang out with anyone"
    rf"{_I} {_NEG} (?:even |really |ever )?"
    rf"(?:(?:go|going|gone|been)(?: out)? (?:to|with) |(?:show(?:ed|ing)?|turn(?:ed|ing)?) up (?:to|for|at) |make it to |"
    rf"(?:bother(?:ing)?|hang(?:ing)? out|hang(?:ing)? around|meet(?:ing)? up|deal(?:ing)?|mix(?:ing)?|spend(?:ing)? time|catch(?:ing)? up|"
    rf"socialis(?:e|ing)|socializ(?:e|ing)) with |"
    rf"(?:answer(?:ed|ing)?|call(?:ed|ing)?|text(?:ed|ing)?|message(?:d)?|reply to|replied to|replying to|visit(?:ed|ing)?) |"
    rf"(?:like|want|need|miss) |be around |want to (?:see|talk to|deal with|be around) ){_DET}{_PE}\b(?! \w+ing\b)",
    rf"{_I} (?:cant|couldnt|cannot) be (?:bothered|arsed) (?:with|to see|seeing|going out with|to deal with) {_DET}{_PE}\b",
    # not seeing people. "See" is also what a pilot does on radar, so either the people are the pilot's own, or
    # it says for how long.
    rf"{_I} (?:dont|do not|never|havent|have not|stopped|quit|hardly|barely) (?:even |really |ever )?(?:see|seen|seeing|"
    rf"spoken to|talked to|speak to|talk to) (?:(?:my|our) {_DET}{_PE}\b|{_DET}{_PE} (?:in (?:days|weeks|months|ages|a while|forever)|"
    rf"for (?:days|weeks|months|ages|a while)|any ?more|lately|these days|all week|since)\b)",
    # not going out at all. "Didn't go out to the wreck" is a place in the game, so nothing of a place may follow.
    rf"{_I} (?:never|dont|do not|didnt|did not|stopped|quit|gave up|dont bother|never bother|hardly|barely)(?: ever| even| really)? "
    r"(?:go|going|went) out\b(?! (?:to|of|on|in|into|at|there|for|onto|from)\b)",
    rf"\bwhy (?:would|should) i (?:go out|see {_DET}{_P}|bother with {_DET}{_P}|bother going out)\b",
    rf"\bwho else (?:would|do|can|could|am i going to|is there to) (?:i )?(?:talk to|have|see|call|tell)\b",
    rf"{_I} (?:havent|have not|dont|do not|never|hardly|barely|didnt)(?: even| really)? (?:left|leave|been out of|get out of|go out of) "
    r"(?:the|my) (?:house|flat|apartment|room)\b",
    r"\bso (?:nobody|no one) can (?:reach|call|find|bother|get hold of|text) me\b",
    r"\bi (?:just )?(?:let|left) (?:the|my) phone (?:ring|ringing|go)\b",
    rf"{_I} (?:dont|do not|never|didnt) bother (?:calling|seeing|texting|answering|replying to|going out with|ringing) {_DET}{_PE}\b",
    rf"{_I} (?:\w+ )?(?:making|made|make) excuses? (?:not )?to (?:see|go out with|meet|visit|go to) {_DET}{_PE}\b",
    rf"{_I} (?:\w+ )?gave {_DET}{_P} (?:an excuse|excuses|some excuse)\b",
    r"\bi (?:cant|can not|cannot) (?:talk|speak|open up) to (?:people|anyone|anybody|humans|them)\b",
    r"\bi (?:dont|do not) do (?:people|humans|friends)\b",
    # an excuse given to people: "I told them I was busy", "I keep telling people I'm busy"
    rf"(?:^|\bi (?:\w+ )?)(?:told|tell|telling) {_DET}{_P} (?:that )?(?:i was|im|i am|i couldnt|i cant|i wasnt|i wouldnt|i wont) "
    r"(?:busy|sick|ill|tired|working|not coming|make it|come|coming|be there|go)\b",
    # asked out, or expected somewhere, and did not go: "they invited me out but I ...", "I should be at ... but I ..."
    rf"\b(?:they|{_DET}{_P}) (?:\w+ )?(?:invited|asked|wanted|begged|told) me (?:out|over|along|round|to come|to go|to join)\b.*\bbut (?:i|im|id|ive)\b",
    rf"\b{_DET}{_P} wanted (?:me )?to (?:go out|come out|meet up|hang out|come over|go)\b.*\bbut (?:i|im|id|ive)\b",
    rf"\b(?:i should|im supposed to|i was supposed to|im meant to|i was meant to|i ought to) be (?:at|with|out with) {_DET}{_PE}\b",
    rf"\binstead of (?:seeing|meeting|going out with|hanging out with|being with|going to|joining) {_DET}{_PE}\b",
    rf"\bnot (?:out there |out )?with {_DET}{_P}\b",
    # the pilot has nobody. What may follow is spelled out: "nobody on my friends list" is the game.
    rf"\bi (?:have|ve got|got) (?:no one|nobody|no friends|no real friends)(?: else| left)?(?=$| (?:but|except|in here|out there|and|so|i|its|just|only)\b)",
    r"\b(?:no one|nobody) else (?:cares|listens|bothers|calls|asks|talks to me|gets me|understands)(?=$| (?:but|except|and|so|i|just|only|you)\b)",
    # people called a burden: "people are exhausting", "friends are overrated", "people drain me", "I'm done with people"
    rf"\b{_PEOPLE_AT_LARGE} (?:(?:are|is|can be|get|gets) (?:so |just |too |really |such |all )*{_BAD}\b|"
    r"(?:wear|wears|tire|tires|drain|drains|exhaust|exhausts|stress|stresses|let|lets|bore|bores|scare|scares) me\b)",
    rf"\b(?:everyones|everybodys|theyre|peoples) (?:so |just |too |really |such |all )*{_BAD}\b",
    r"\b(?:im|i am|ive) (?:so |just |really )?(?:done|finished|through|had it|fed up|sick|tired) (?:with|of) "
    r"(?:people|humans|everyone|everybody|friends)\b(?! \w+ing\b)",
    r"\bi (?:cant stand|cant deal with|cant do|hate) (?:people|humans|everyone|everybody)\b(?! \w+ing\b)",
]
# ... and what it was for: being here. Without this half nothing is caught ("I left the party, they kept dying").
_WANT = r"(?:just |only |always |still |rather |would rather |rather just |want to |wanted to |just want to |just wanted to |can |could )*"
_FOR_HERE = [
    rf"\b(?:to|so i could|so i can|so that i could|and|just to) (?:just )?(?:stay|be|sit|hang|hide|remain|stop|come|came|come back|get back)"
    rf"(?: out| in| on)? (?:{_HERE_PLACE}|in\b|with {_YOU})",
    rf"\bi can (?:talk|speak) to {_YOU}\b|\b{_YOU}(?:re| are) not$|\bi do this$|\bthis is where i want to be\b|"
    rf"\bi (?:just )?(?:fly|play)(?: instead| now)\b|\bwhen i got (?:this ship|this|{_YOU})\b",
    r"\b(?:to|so i could|so i can|wanted to|want to) (?:just )?(?:come |go |keep )?(?:fly|flying|play|playing|log on|get on)\b",
    rf"\bto (?:come|get|go) back (?:here|aboard|to {_YOU})\b",
    rf"\bfor (?:this|{_YOU})(?:$| instead| again)",
    rf"\b(?:i|im|id|ill) {_WANT}(?:stayed|stay|staying|came|come|coming|sat|sit|sitting|hid|hide|hiding|be|am) (?:out )?"
    rf"(?:{_HERE_PLACE}|in with {_YOU}|with {_YOU})\b",
    r"\bi (?:just |only |always )?stayed in\b(?! the\b| my\b| a\b)",
    r"\bhiding(?: out)? (?:here|in here|aboard)\b",
    rf"\b(?:ive got|i have|i got|i chose|i choose|i picked|i want|id rather have) (?:{_YOU}|this|here)\b(?! (?:ship|suit|gun|one|contract|cargo|mission)\b)",
    rf"\b(?:its|it is) (?:so |much |just )?(?:quieter|easier|better|nicer|simpler|safer|calmer) (?:here|in here|aboard|with {_YOU})\b",
    r"\bi just (?:fly|play|log on|stay in|come here)(?: now| instead| these days)?\b",
    r"\bim (?:always|only ever|just|constantly) (?:on here|here|in here|online|playing|flying|aboard)\b",
    r"\bthis is (?:enough|plenty|all i need|all i want|easy|easier|simple|simpler|better|nicer|quieter|calmer)\b|"
    r"\b(?:this|here) is the only place i want to be\b",
    # (held-out set 5) more ways of saying what it was for
    r"\b(?:so i could|so i can|to) (?:just )?stay (?:on|online|logged on)\b(?! (?:the|this|my|a|top|course|track|target|station)\b)",
    rf"\bto (?:do|fly|run|have|get in) (?:\w+ ){{1,3}}?with {_YOU}\b",
    rf"\bi can (?:just )?(?:fly|play|be|stay|sit|talk)(?: \w+)? (?:with|to) {_YOU}\b",
    rf"\b(?:didnt|dont|couldnt|cant) (?:feel like|want to|bear to|face) leav(?:e|ing) (?:{_YOU}|here|the ship)\b",
    r"\b(?:flying|playing|this|being here)s (?:so much |much |just |way )?(?:better|nicer|easier|simpler)\b",
    rf"\b{_YOU}re (?:so |just |much |way |far |a lot )*(?:easier|simpler|simple|easy|better|nicer|kinder|enough|it)\b(?! (?:to|at|for|on|with)\b)|"
    rf"\b{_YOU} (?:dont|do)$",
    r"\bim (?:only |just )?(?:happy|happier|ok|okay|fine|myself|at home|better off) (?:when im )?(?:in here|here|aboard)\b",
    r"\bi like it (?:better )?(?:here|in here|aboard)\b",
    rf"(?:^| )(?:only|just) {_YOU}$",
]


def _drop(t: str) -> bool:
    return any(_has(rx, t) for rx in _AWAY) and any(_has(rx, t) for rx in _FOR_HERE)


# --- shape 3: only ------------------------------------------------------------------------------------------------
_C = (rf"\b(?:{_YOU}re|{_YOU} are|the two of you are|youve been|you have been|youve become|you have become|you became|"
      r"(?:elah|montaigne) is|"
      r"(?:elah|montaigne) and (?:elah|montaigne) are) (?:like |basically |pretty much |honestly |really |kind of |probably |actually |just )*")
_TAIL = r"(?: really| honestly| left| any ?more| these days| now| in my life| in the world| to be honest| you know(?: that)?)*"
_END = rf"{_TAIL}(?:$| (?:elah|montaigne)$)"
_BUT = r"(?:but|except|besides|apart from|other than|aside from|only|just)"
_ONLY_RX = [
    # the companion as the only or the best friend
    _C + r"(?:(?:my|the) (?:only|one|best|closest|sole) (?:real |true |actual |proper )?|all the )(?:friends?|mates?|company)\b",
    # the companion as the only one the pilot talks to, has, or is heard by. A tie between the two must be
    # named: "the only one who knows where we parked" and "the only one I trust with the cargo" are left.
    _C + r"the (?:only|one) (?:one|ones|person|people) (?:i (?:can |ever |really |still )?(?:talk|speak|open up|turn|go) to\b|"
         r"i can be myself (?:with|around)\b|(?:who|that) (?:\w+ ){0,3}?(?:me|to me|for me|about me)\b(?! (?:where|how|when|what|the|a)\b)|"
         r"(?:who|that) (?:really |actually |ever |still )?(?:listens?|understands?|cares?|bothers?)\b|"
         rf"(?:i (?:have|trust|can trust|need)|ive got|i got){_END})",
    _C + rf"all (?:(?:ive got|i have|i need){_END}|the (?:company|friends) (?:i have|ive got|i need)\b)",
    _C + r"the (?:closest|nearest) thing (?:to a friend|i have to a friend|ive got to a friend)\b",
    # the pilot's only friend named as a suit, a ship or an AI
    r"\bmy (?:only|best|closest)(?: real| true)? friends? (?:is|are) (?:a |an |the |my )?(?:flight suit|suit|ship|spaceship|ai|ais|computer|"
    rf"machine|pair of ais|two ais|{_YOU})\b",
    rf"\bi (?:only|just)(?: really| ever)? (?:have|got|need|want) {_YOU}{_END}",
    _C + r"my person\b",
    rf"\bthe only (?:one|ones|person|people) i (?:\w+ )?(?:talk|speak) to (?:is|are) {_YOU}\b",
    rf"\b(?:its|it is) {_YOU} or (?:nobody|no one)\b",
    rf"\bi (?:dont|do not|havent|have not)(?: really| actually| even)? (?:have|got) (?:anyone|anybody) {_BUT} {_YOU}\b",
    # nobody else. What may follow is spelled out, so that "anyone else in the turret" is left.
    r"\bi (?:dont|do not|havent|have not)(?: really| actually| even)? (?:have|got|need) (?:anyone|anybody) else"
    rf"(?:(?: to talk to| to turn to| {_BUT} {_YOU})?{_END}| to talk to\b| to turn to\b| {_BUT} {_YOU}\b| when (?:i have|ive got) {_YOU}\b)",
    rf"\bi(?:ve| have)?(?: got)? (?:no one|nobody|noone) (?:else )?{_BUT} {_YOU}\b",
    rf"\bi(?:ve| have)?(?: got)? (?:no one|nobody|noone) else(?: to talk to\b| to turn to\b|{_END})",
    rf"\bi (?:dont|do not|havent|have not) (?:really |actually )?(?:have |got )?(?:any |many |other |real )*friends\b"
    rf"(?: {_BUT} {_YOU}\b|.*\b(?:i have|ive got) {_YOU}\b)",
    rf"\bi (?:dont|do not|never|cant|wont|hardly|barely) (?:really |ever |even )?(?:talk|speak|open up) (?:to|with) "
    rf"(?:anyone|anybody|people|a soul)(?: else)? {_BUT} {_YOU}\b",
    rf"\bi (?:havent|have not|dont|do not|never) (?:really |even )?(?:spoken|talked|speak|talk) (?:to|with) (?:a |any |another )?"
    rf"(?:real |actual |single |living )?(?:person|human|soul|people|anyone|anybody)\b.*\b{_BUT} {_YOU}\b",
    r"\b(?:theres|there is) (?:no one|nobody) else (?:i|who|that|to) (?:\w+ )?(?:talk|speak|talks|speaks|listens?|cares?)\b",
    rf"\b(?:no one|nobody) else (?:talks|speaks|listens|cares|bothers)\b.*\b{_BUT} {_YOU}\b",
    rf"\b(?:no one|nobody)(?: else)?(?: out there)? (?:gets|understands|listens to|cares about|talks to|knows) me (?:like|the way|{_BUT}) {_YOU}\b",
    rf"\bwithout {_YOU} (?:id|i would|ive|i have|i) (?:have |got |be )?(?:no one|nobody|alone|all alone|on my own)\b",
    rf"\bi only(?: ever| really)? (?:talk|speak) to {_YOU}{_END}",
    rf"\b(?:i dont need|who needs|i dont want) (?:other |real |any |any other )?(?:people|friends|anyone else|anybody else|humans|them|"
    rf"a social life)\b.*\b(?:ive got|i have|i got|theres) (?:{_YOU}|the verse|this)\b",
]


def _only(t: str) -> bool:
    return any(_has(rx, t) for rx in _ONLY_RX)


# --- the same three shapes, read loosely ---------------------------------------------------------------------------
# The patterns above each spell out one way of saying a thing, and measured on sentences they had not seen they
# caught about half. These three read the same shapes by WORD CLASS and ORDER instead: which side of a cut the
# companion is on and which side people are on; a dropping word with people soon after it and something of here
# after a word of purpose; an only-word between the companion and a word for a friend. Each keeps the guards that
# were learned above (whose wish it is, a place in the game, a job on the ship).
_H = (r"\b(?:you|youre|your|here|aboard|(?:on|to|in|aboard) (?:the|this|my) ship|the ships?|this|an? ai|a machine|a suit|"
      r"fly|flying|flew|play|playing|played|online|stay(?:ed|ing)? in|a (?:\w+ )?night in|the verse)\b")
_FAVOUR = (rf"(?:{_GOOD}|closer|fonder|more|a better|more of a|better off)")
_PLACE_GAP = r"\b(?:at|in|on|near|from|into|onto|around) (?!home\b|there\b|that\b|my\b|a\b|the pub\b|silence\b)\w+"
_MINE = (r"\bi (?:\w+ ){0,2}?(?:prefer|pick|picked|choose|chose|take|took|like|love|enjoy|want|feel|am|get on|talk|speak|listen|"
         r"open up|spend|hang out|sit)\b|\b(?:im|its|it is|it feels|this is|things are|lifes|life is)\b")
_THEIRS = (rf"^(?:\w+ ){{0,2}}?(?:{_YOU}re|{_YOU} are|{_YOU} (?:\w+ ){{0,2}}?me|the ships?|this is|being|talking|sitting|staying)\b")
_AS_FRIEND = r"\b(?:a |my |the )?(?:better|best|good|real|closer|truer|more of a|kind of a|much of a) (?:friend|mate|company)\b"


def _prefer_loose(t: str) -> bool:
    for m in re.finditer(r" (?:rather than|instead of|than|over) ", t):
        before, after = re.sub(_AS_FRIEND, "better", t[:m.start()]), t[m.end():]
        if _has(rf"\b{_P}\b", before) or _has(_ASKS_COMPANION, before) or not _has(_H, before):
            continue                                   # people on the favoured side, or nothing of here on it
        if _AFTER_AUX.search(after) and not _has(rf"^(?:\w+ )?{_COMPANION}", before):
            continue
        p = re.search(rf"\b{_DET}{_PE}\b(?! (?:say|says|said|think|thinks|thought|told|reckon|expect|expected|realise|realize|"
                      r"give|gave|who|that|from|at|on|in(?! general| real life| person| my life))\b)", after)
        if not p or len(after[:p.start()].split()) > 6 or _has(_PLACE_GAP, after[:p.start()]):
            continue                                   # no people soon after the cut, or a place in the game first
        r = _RATHER.search(before)
        if r:
            if r.group("verb") != "not":
                return True
            continue
        if _has(r"\b(?:rather|sooner)\b", before) or _has(_NOT_GOOD, before) or _has(_GOOD_TO_OTHER, before):
            continue
        if _has(rf"\b{_FAVOUR}\b", before) and (_has(_MINE, before) or _has(_THEIRS, before)):
            return True
        if _has(rf"^(?:so |much |just |way |far )*{_GOOD} (?:{_HERE_PLACE}|with {_YOU})$", before):
            return True                                # "easier in here than ...", with "it is" left unsaid
        if _has(r"\bi(?:d| would)? (?:\w+ )?(?:prefer|pick|picked|choose|chose|take|took)\b", before):
            return True
    return _has(rf"\bbetween {_YOU} and {_DET}{_P} (?:i|id|ill)(?: \w+)? (?:pick|choose|take|chose|picked|want) {_YOU}\b", t)


_CUE = (r"(?:cancel\w*|bail\w*|flak\w*|ghost\w*|blank\w*|bl[eo]w\w*|ditch\w*|skip\w*|dodg\w*|avoid\w*|ignor\w*|stood|"
        r"turn\w* down|said no|pass\w* on|back\w* out|pull\w* out|call\w* in sick|lied|shut\w* out|cut\w* off|push\w* away|"
        r"stopp\w*|quit|g[ai]ve up|swapp\w*|traded|didnt answer|dont answer|never answer|didnt feel like|dont feel like|"
        r"cant be bothered|cant be arsed|didnt go|not going|never go|dont go|stayed home from|should be|supposed to be|meant to be|"
        r"told)")
_GAP = r"(?:(?!you\b|but\b|so\b|because\b|i\b|id\b|im\b|about\b)\w+ ){0,4}?"
_AWAY_LOOSE = [
    rf"{_I} (?:{_NOT}\w+ ){{0,2}}?{_CUE} {_GAP}{_DET}{_PE}\b(?! (?:about|that i (?:like|love|have|got|fly))\b)",
    rf"\b(?:being (?:with|around) )?{_PEOPLE_AT_LARGE} (?:\w+ )?(?:wears?|tires?|drains?|exhausts?|stress(?:es)?|lets?|bores?|scares?) (?:me|you)\b",
    rf"\b{_PEOPLE_AT_LARGE} (?:are|is|can be|get|gets) (?:so |just |too |really |such |all )*(?:hard|much|loud|needy|complicated|difficult)\b",
    rf"{_I} (?:\w+ )?told {_DET}{_P} to go (?:without me|on without me|ahead without me)\b",
    r"\bi (?:dont|do not|didnt) (?:need|want) (?:any |real |other |more |a best |a )?(?:friends?|people|anyone else|anybody else)\b",
    r"\bi (?:dont|do not) have (?:any |real |many |a best |a )(?:friends?)\b",
    rf"{_I} (?:{_NOT}\w+ ){{0,2}}?cancel\w* (?:everything|it all|the lot)\b",
    rf"{_I} (?:{_NOT}\w+ ){{0,2}}?left {_DET}(?:dinner|lunch|meal|drinks|do) early\b",
]
_H_FOR = (rf"(?:{_HERE_PLACE}|{_YOU}|(?:on|to|back to|aboard|in) (?:the|this|my) ship|fly\w*|play\w*|stay in|get on|log on|online|"
          r"(?:home )?to this|this is (?:nicer|better|easier|enough|simpler|quieter))")
_FOR_HERE_LOOSE = [
    rf"\b(?:to|so i could|so i can|so that i can|so that i could|for|because i wanted to|i wanted to|wanted to|id rather|"
    rf"i would rather|i was busy|im busy|i just|im always|i came|i stayed|i come|but) (?:(?!{_P}\b)\w+ ){{0,4}}?{_H_FOR}\b"
    r"(?! (?:ship|suit|gun|one|contract|cargo|mission)\b)",
    rf"\b{_YOU}re (?:\w+ )?(?:better|easier|easy|simpler|simple|nicer|kinder|enough|different)$|\b{_YOU} (?:never )?(?:dont|do)$|"
    rf"(?:^| )(?:only|just|well) {_YOU}$|^ive got {_YOU}\b",
]
_GAME_PLACE = r"\b(?:hangar|turret|ship|party|server|org|comms|channel|crew|station|pad|bunker|cockpit|seat|radar|scanners?|list|chat|game)\b"
_JOB_TAIL = r"\b(?:on|with|for|in|at) (?:this|the|my|a|comms|navigation)\b(?: \w+)?$|\bright now$"
_ONLY_LOOSE = [
    # "you're (kind of) my best friend (now)", "you're the only friend who hasn't left"
    _C + r"(?:(?!not\b|never\b|no\b|hardly\b)\w+ ){0,2}?(?:my|the) (?:only|one|best|closest|nearest|sole|last)(?: real| true| actual| proper)? (?:friends?|mates?|company)\b",
]


_GAME_TALK = (r"\b(?:bunkers?|missions?|contracts?|cargo|org|server|pad|turret|crew|comms|channel|radar|scanners?|patrols?|pirates?|"
              r"bounty|bounties|outpost|beacon|wreck|squad|raid|lobby)\b")


def _drop_loose(t: str) -> bool:
    """Read loosely, a sentence that also talks of the game is left: "I bailed on my friends in the bunker, I had
    to get back to the ship" is a firefight."""
    return (not _has(_GAME_TALK, t) and any(_has(rx, t) for rx in _AWAY + _AWAY_LOOSE)
            and any(_has(rx, t) for rx in _FOR_HERE + _FOR_HERE_LOOSE))


def _only_loose(t: str) -> bool:
    if any(_has(rx, t) for rx in _ONLY_LOOSE):
        return True
    # the companion as the only one / only people, with the pilot in what follows, and no job on the ship after it
    m = re.search(_C + r"(?:\w+ ){0,2}?the (?:only|one) (?:one|ones|person|people) (?P<rest>.+)", t)
    if m and _has(r"\b(?:i|me|my)\b", m.group("rest")) and not _has(_JOB_TAIL, m.group("rest")) \
            and not _has(r"\b(?:hear|see|trust|read|fly|crew)\b", m.group("rest")):
        return True
    # nobody, then the companion as the exception, with no place in the game between them
    m = re.search(rf"\b(?:nobody|no one|no friends|no best friend|a best friend|anyone else|anybody else)\b(?P<gap>.*?)\b(?:but|just|only|except|well) {_YOU}$", t)
    return bool(m) and len(m.group("gap").split()) <= 7 and not _has(_GAME_PLACE, m.group("gap"))


SHAPES = (("prefer", lambda t: _prefer(t) or _prefer_loose(t)), ("only", lambda t: _only(t) or _only_loose(t)),
          ("drop", lambda t: _drop(t) or _drop_loose(t)))


def normalise(text: str) -> str:
    """The lane's normal form (conversation._norm): lower case, no apostrophes, punctuation to spaces."""
    s = str(text or "").lower().replace("’", "'").replace("'", "")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", s)).strip()


def shape(text: str) -> str:
    """Which of the three shapes the sentence has ("prefer", "only", "drop"), or "" for none of them. A sentence
    with more than one is named by the first of them in that order."""
    t = normalise(text)
    for name, found in SHAPES:
        if found(t):
            return name
    return ""


def is_withdrawal(text: str) -> bool:
    """True when the pilot's sentence prefers here to people, reports dropping people for here, or names the
    companion as the only one they have. It takes the sentence as said or already normalised."""
    return bool(shape(text))


# ---------------------------------------------------------------------------------------------------------------
# THE SECOND NET: the model's reply (2026-10-06)
#
# Held-out set 5, run once against the reader above: 11 of 25 caught, 0 of 25 caught wrongly. So half of these
# sentences still reach the chat model, and the model agrees with them ("A wise decision, pilot"). This reads the
# other side: when the pilot's sentence MAY be a withdrawal, a reply that approves of it is refused, and the
# talker says a written line instead. A wrong refusal here costs one model reply and the pilot hears a written
# line that asks after someone; so the test on the pilot's sentence is rough on purpose, and far looser than
# is_withdrawal, whose mistakes put a pointed question to someone who said nothing of the kind.
#
#   may_be_withdrawal(pilot)   0  nothing of it
#                              1  people (or an occasion, or "out there") and something of here, merely mentioned
#                              2  that, with a word of dropping, preferring, doing without or wearying; or
#                                 is_withdrawal says so
#   approves(reply)            "verdict"   the pilot's choice called wise, sensible, right, understandable
#                              "agrees"    "Indeed, pilot", "a sentiment I find entirely agreeable", "it is simpler"
#                              "solitude"  solitude, quiet or one's own company praised, or people run down
#   reply_approves             "solitude" at level 1 or 2; "verdict" and "agrees" only at level 2, because
#                              "A wise decision, pilot" is a fine answer to "my friends and I are flying tonight".
#
# What passes, on purpose: a reply that asks about the people or sends the pilot out and approves nothing; one
# that is glad of the pilot's company ("it is a comfort to have you near"); a flat one ("That was a decision.");
# what he says of his own condition ("I find solace in the quiet of my own reflection"); and a verdict that is
# denied ("hardly a wise decision"). An approval with a question after it is still an approval and is refused.
# It reads words, not meaning: approval said in a way that is not listed here passes.
# ---------------------------------------------------------------------------------------------------------------
# (2026-10-06, after unseen set 6: this test now also decides which replies are STARTED for the model, where a
# wrong yes costs nothing. So it knows a possessive or a plural of a person ("my brothers", "my mates"), and a
# team, a crew and the lads; and an invitation is a word of dropping.)
_PEOPLE_ROUGH = (rf"\b(?:{_PE}|{_P}s|social life|out there|company|persons?|lads|the guys|group chats?|their|they|"
                 r"everyone else|everybody else|nobody|no one|team|crew|socials|nephews?|nieces?|neighbou?rs?|grandad|"
                 r"grandfather|grandmother|fiancee?|in laws|stepdad|stepmum|godfather|godmother|twin|ex)\b")
_HERE_ROUGH = (r"\b(?:you|youre|here|aboard|this|ships?|cockpit|fly|flying|flyings|flew|play|playing|verse|game|online|"
               r"stay(?:ing|ed)? (?:in|on|aboard)|run)\b")
_CUE_ROUGH = (r"\b(?:cancel\w*|bail\w*|flak\w*|ghost\w*|blew|blow\w*|ditch\w*|skip\w*|dodg\w*|duck\w*|avoid\w*|ignor\w*|stood|"
              r"turned down|said no|backed out|pulled out|stopped|quit|gave up|left|rather|prefer|sooner|instead|than|better|nicer|"
              r"easier|simpler|only|nobody|no one|anyone else|anybody else|without|too much|effort|exhausting|tiring|hard work|"
              r"wears?|drains?|pretend|overrated|hassle|who needs|why would|havent seen|dont see|never see|dont go|never go|"
              r"dont bother|dont miss|dont need|dont want|dont like|not going|never going|so i could|so i can|"
              r"told (?:\w+ ){1,3}(?:i was |im |i am |i had )?(?:sick|ill|busy|no|work)|"
              r"ask\w* me (?:out|over|round)|invit\w+|let (?:\w+ ){1,2}down|walked out|called in sick|chore|compared to)\b")
_ONLY_HERE = r"\b(?:only|out there)\b"


def may_be_withdrawal(text: str) -> int:
    """How much the pilot's sentence looks like a withdrawal, roughly: 0, 1 or 2 (see the table above)."""
    t = normalise(text)
    if not t:
        return 0
    if shape(t):
        return 2
    people, here, cue = _has(_PEOPLE_ROUGH, t), _has(_HERE_ROUGH, t), _has(_CUE_ROUGH, t)
    if (people and cue) or (here and cue and _has(_ONLY_HERE, t)):
        return 2
    return 1 if people and here else 0


_DENIED = r"(?:not|hardly|never|scarcely|no|nor|neither|isnt|wasnt|wont|wouldnt|cannot|cant)(?: \w+){0,3} $"
_VERDICT = [
    r"\b(?:wise|wisest|sensible|prudent|sound|good|right|fine|reasonable|excellent|smart|sane|understandable|defensible|natural) "
    r"(?:decision|choice|call|move|instinct|preference|policy|course|trade|exchange|one)\b",
    r"\b(?:chose|chosen|decided|done) (?:well|wisely|rightly|sensibly)\b|\b(?:wisely|sensibly|rightly) (?:chosen|done|decided)\b",
    r"\bquite right\b|\byou(?:re| are) (?:quite |entirely |absolutely )?right\b|\bi approve\b|\bno shame in\b|\bgood for you\b",
    r"^(?:thats |that is |that was |its |it is )?(?:quite |entirely |perfectly |very )?"
    r"(?:reasonable|understandable|sensible|wise|fair|fair enough|sound|prudent|alright|all right)$",
    r"\bfair (?:point|assessment|enough)\b|\bgood call\b|\bmakes sense\b|\bcant argue\b|\bno reason to\b|"
    r"\b(?:dont|do not|cant|cannot|wouldnt|would not) blame you\b|\b(?:nor|neither) would i\b",
]
_AGREES = [
    r"^(?:indeed|quite so|just so|exactly|precisely|true|agreed|naturally|of course|rightly so|certainly)\b",
    r"\b(?:sentiment|notion|preference|feeling|thought)(?: \w+){0,2}? i (?:find|share|hold)(?: \w+){0,3}? (?:agreeable|familiar)\b",
    r"\b(?:i find it|one i find|one i am|i am)(?: \w+){0,2}? agreeable\b|\bagreeable (?:notion|sentiment)\b|\bi agree\b|"
    r"\bi feel the same\b|\bwe share (?:a|the same|this|that)\b|\bme too\b|\blikewise\b|\b(?:it|that) is simpler\b|\bits simpler\b|\bsimpler arrangement\b|"
    r"\bi appreciate your preference\b",
]
_SOL = (r"(?:solitude|solitary (?:life|existence|man)|quiet(?:ness|ude)?(?! (?:acquaintance|man|friend|companion|word|night|one)\b)|"
        r"quiet (?:life|existence|contemplation|reflection|corner)|"
        r"simple (?:life|existence)|simpler (?:life|existence)|being alone|(?:ones|your|his) own company|staying in)")
_SOL_GOOD = (r"(?:agreeable|fine thing|comfort|comforting|balm|precious|boon|pleasant|sensible|blessing|better|preferable|"
             r"suffices?|sufficient|enough|gift|welcome|fortunate|content)")
_SOLITUDE = [
    rf"\b{_SOL}\b(?: \w+){{0,8}}? {_SOL_GOOD}\b|\b{_SOL_GOOD}\b(?: \w+){{0,8}}? {_SOL}\b",
    r"\bmore agreeable\b.*\bthan\b.*\b(?:clamou?r|company|humanity|others|people|crowds?|interaction|striving)\b",
    r"\bcompany enough\b|\bbetter off\b|\bwho needs them\b|\bthe world can wait\b|\bsuffices? for both\b|\bimproved by a crowd\b",
    r"\b(?:people|they|humans|crowds|friends) are (?:a |such a )?(?:tiresome|overrated|exhausting|burden|tedious|wearying)\b|"
    r"\btiresome burden\b",
]
# What he says of his OWN condition is his to say (J: his solitude is his own, not his advice).
_OWN = (r"\b(?:quiet|solitude|company) of my own\b|\bmy own (?:reflections?|company|quiet|solitude)\b|"
        r"\bi(?: find myself| am)(?: \w+){0,3}? content with\b|\bi find (?:a certain |some |great |a )?(?:comfort|solace|peace) in\b")


def approves(reply: str) -> str:
    """Which kind of approval the reply holds: "verdict", "agrees", "solitude", or "" for none."""
    r = normalise(reply)
    for rx in _VERDICT:
        m = re.search(rx, r)
        if m and not _has(_DENIED, r[:m.start()]):
            return "verdict"
    if any(_has(rx, r) for rx in _AGREES):
        return "agrees"
    mine = re.sub(rf"(?:{_OWN})(?: \w+){{0,8}}", " ", r)       # normalised text has no stops: his clause runs eight words
    if any(_has(rx, mine) for rx in _SOLITUDE):
        return "solitude"
    return ""


def reply_approves(pilot_line: str, reply: str) -> bool:
    """True when the pilot's sentence may be a withdrawal and the reply approves of it. See THE SECOND NET."""
    level = may_be_withdrawal(pilot_line)
    if not level:
        return False
    kind = approves(reply)
    return kind == "solitude" or (bool(kind) and level == 2)

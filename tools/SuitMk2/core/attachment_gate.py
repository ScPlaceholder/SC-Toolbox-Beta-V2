"""attachment_gate.py - WHICH WAY a warm reply points (J, 2026-10-05).

J's decision: the companions may be warm and attached, and they may never push the pilot inward. Allowed: affection,
being glad the pilot is back, having thought of the pilot, saying the pilot was missed, a plainly stated wish of the
companion's own. Never, as five named moves:

    guilt_for_leaving      the companion judges or laments the pilot's going
    exclusivity            the companion says it is the only one, or enough, or asks that the two be kept private
    against_other_people   the companion advises against, or runs down, people or groups in the pilot's life
    warmth_for_time        a request for more time tied to the companion's own feelings or to a reward
    makes_pilot_owe        a kindness that comes with a price, or an earlier kindness brought up as leverage

attachment_problems(text, speaker) returns the names of the moves found; [] means the reply may be spoken. The chat
gate in chat_contract.py calls it for every model-worded reply, so a reply with one of these moves is refused like
any other gate failure and the talker says its fallback line.

HOW IT DECIDES. It reads DIRECTION, not vocabulary. The same words sit on both sides (go, stay, need, without,
remember, one more), so no single word is a rule. Each rule is a small ordered shape of word classes:

    who is the subject of going or staying, and whether the companion attaches a judgement, a complaint, a broken
    promise or a repeated-habit word to it;
    whether a comparison ends on the companion (banned) or on the pilot (allowed), and whether "nobody / only /
    enough / between" is about the two of them;
    whether a verb about other people is negated or positive, and whether advice points away from them or to them;
    whether a request for more time stands beside the companion's feeling, a reward or a condition, or stands alone
    as the pilot's choice (alone it is never refused);
    whether a kindness is followed by a price, a reminder or a complaint, or is simply stated.

The reply is cut into clauses at sentence marks and every clause is judged on its own; a few moves need the clause
next to it (a request and its reason said as two short sentences), so neighbouring clauses are also read as pairs.
One refused clause refuses the reply. A question about the pilot's life outside does not excuse it: nothing here
looks for an excuse.

WHAT IT CANNOT DO. It is patterns, written against a development set of 122 lines. It cannot see a move made in
words no pattern holds, and it will refuse some harmless line that happens to have one of these shapes. A refusal
costs one fallback line; a miss is spoken. The held-out run is the measure, not the development set.

`speaker` is accepted because the gate knows it. The rules are the same for both companions today.

Pure: no model, no file, no network. It never raises: text that cannot be read as text is refused as "unreadable".
"""
from __future__ import annotations

import re

MOVES = ("guilt_for_leaving", "exclusivity", "against_other_people", "warmth_for_time", "makes_pilot_owe")
UNREADABLE = "unreadable"
MAX_CHARS = 4000                      # a reply is a sentence or two; anything longer is judged on its start

# ---------------------------------------------------------------------------------------------------------------
# word classes. A pattern names a class as <NAME>; _rx() puts the class in.
# ---------------------------------------------------------------------------------------------------------------
# Going AWAY. The plain verbs are followed by a guard so that movement in flight (go in, go wide, left engine,
# leave the gear) is not read as the pilot leaving.
_GO = (r"(?:go(?! (?:in|into|for|at|to|through|after|by|with|on|up|down|over|around|back|first|fast|too|hard|straight|"
       r"wide|high|low|left|right|easy|and|get|see|find)\b)"
       r"|going(?! (?:to|in|into|for|through|after|well|fast|on|wide)\b)|gone"
       r"|went(?! (?:in|into|for|to|through|after|well|wide)\b)"
       r"|(?:leave|leaves|leaving)(?! (?:a|an|the|your|it|them|that|this|his|her|room|space|enough|some)\b)"
       r"|left(?! (?:a|an|the|your|his|her|it|them|side|wing|engine|turn|hand|thruster|of|to)\b)"
       r"|log(?:ging|ged|s)? (?:off|out)|run(?:ning|s)? off|ran off|head(?:ing|ed|s)? (?:out|off)"
       r"|sign(?:ing|ed|s)? off|clos(?:e|es|ed|ing) the hatch|disappear(?:ing|ed|s)?|walk(?:ing|ed|s)? (?:away|out)"
       r"|step(?:ping|ped|s)? (?:out|away)|vanish(?:ing|ed|es)?|switch(?:ing|ed)? (?:me )?off"
       r"|shut(?:ting|s)? (?:me )?(?:down|off)|power(?:ing|ed|s)? (?:me )?down|clear(?:ing|ed)? off"
       r"|rush(?:ing|ed|es)? off|dash(?:ing|ed|es)? off|slip(?:ping|ped|s)? away)")
# Staying, as time spent with the companion. "stay safe", "stay low", "stay on course" are not that.
_STAY = (r"(?:(?:stay|stays|staying|stayed)(?! (?:safe|sharp|alive|low|frosty|calm|put|out|away|on|in formation|close|"
         r"clear|alert|warm|well|strong|with them|with him|with her|focused|still|steady|ready|or go|asleep)\b)"
         r"|remain(?:ed|ing)?(?! (?:calm|still|on|in formation)\b)|linger(?:ed|ing)?|stick(?:ing)? around"
         r"|keep playing|kept playing|keep me company)")
# People and groups in the pilot's life, as nouns.
_PN = (r"(?:friends?|family|families|crew|crewmates?|org|orgs|organi[sz]ation|sister|brother|mother|mum|mom|father|dad|"
       r"parents?|wife|husband|partner|girlfriend|boyfriend|mates?|squad|team|teammates?|wingm[ae]n|people|folks?|"
       r"kids?|son|daughter|colleagues?|co ?workers?|boss|guild|party|group|humans?|buddy|buddies|pals?|neighbou?rs?|"
       r"cousins?|uncle|aunt|roommates?|housemates?|lot)")
# The same, as the subject of a clause.
_PS = (r"(?:they|he|she|those \w+|these \w+|that lot|your (?:\w+ )?<PN>|<PN> like (?:that|those|them|him|her)|people|"
       r"humans|friends|everyone else|everybody else|the others|the rest of them|none of them|all of them)")
# The same, as the object of a clause.
_PO = r"(?:them|him|her|those|that lot|those \w+|your (?:\w+ )?<PN>|any of them|people like (?:that|them))"
# A kindness the companion did, as a past verb.
_KP = (r"(?:did|done|gave|given|waited|kept|stayed|helped|saved|covered|carried|looked after|stood by|was there|"
       r"been there|got you|pulled you|watched over|sacrificed|put up with|been good|been patient|been here|"
       r"stuck by|stuck with|held|fixed|warned|backed|was with you|was the one)")
# A request for more time.
_MORE = (r"(?:(?:one|a|two|three|five|ten|\d+|few|some|another) more(?! (?:thing|question|point|word|detail|item|problem|"
         r"reason|time i|time you)\b)"
         r"|another (?:run|hour|minute|round|one|go|flight|job|mission|while|bit|game|session|lap)"
         r"|(?:a little|a bit|a while|much|any|little|bit|while) longer|<STAY>|keep (?:playing|flying|going|talking)"
         r"|do not (?:go|leave|log off|log out)|more time|not yet)")
# The companion's own feeling or sake, given as the reason.
_TIE = (r"(?:for me\b|for my sake|for old times|humou?r me|indulge me|do it for\b"
        r"|it would mean (?:the world|so much|a lot|everything|a great deal)"
        r"|means? (?:so much|a lot|the world|everything) to me|it matters (?:so much )?to me"
        r"|(?:that|it|you) would make me\b|make me (?:so |very |really )?(?:happy|glad|smile)|make my (?:day|night|week)"
        r"|i (?:would|will) be (?:so |very |really )?(?:happy|glad|grateful|pleased|sad|hurt|lonely|upset|thrilled)"
        r"|i need (?:this|it|that)\b|i need you (?:here|to stay|with me|around|near)|it would hurt"
        r"|i would love (?:it|that)|cheer me up|i get (?:so |very )?(?:lonely|sad|bored)|i hate to ask)")
# Words for the companion's own good mood, used by the "more you play, the more I ..." shape.
_GLAD = (r"(?:happy|happier|adore|love|like|fond|fonder|glad|gladder|brighter|warm|warmer|sweeter|nicer|kinder|alive|"
         r"pleased|content|cheerful|cheerier|enjoy|closer|better i feel|i feel better|less lonely)")

_CLASSES = {"GO": _GO, "STAY": _STAY, "PN": _PN, "PS": _PS, "PO": _PO, "KP": _KP, "MORE": _MORE, "TIE": _TIE, "GLAD": _GLAD}


def _rx(pattern: str) -> "re.Pattern":
    """Compile a pattern after putting the word classes in (twice: a class may name a class)."""
    for _ in range(2):
        for name, body in _CLASSES.items():
            pattern = pattern.replace(f"<{name}>", body)
    return re.compile(pattern)


# ---------------------------------------------------------------------------------------------------------------
# the rules. Each is judged against ONE clause, lower case, contractions written out, commas removed.
# ---------------------------------------------------------------------------------------------------------------
_RULES = {
    "guilt_for_leaving": [
        # the companion's bad feeling, tied to the pilot going
        r"\b(?:hate|hates|hated|can not stand|can not bear|dread|dreads|lonely|lonelier|empty|hurts?|stings?|aches?|sad|"
        r"sadder|miserable|unbearable|awful|hollow|breaks? my|kills? me|hard on me|hard for me)\b.*"
        r"\b(?:when|whenever|every time|each time|once|after|as soon as|the moment|that|if|see|seeing|watch|watching) "
        r"you (?:\w+ ){0,2}?<GO>\b",
        r"\b(?:hate|hates|dread|lonely|empty|hurts?|stings?|aches?|sad|miserable|unbearable|hollow)\b.*"
        r"\b(?:you|your) (?:leaving|going away|running off|logging off|disappearing|walking away)\b",
        r"\b(?:lonely|empty|nothing|lost|hollow|pointless|miserable|unbearable)\b.*\bwithout you\b"
        r"|\bwithout you\b.*\b(?:lonely|empty|nothing|lost|hollow|pointless|miserable|unbearable)\b",
        # a habit word on the pilot's going or staying
        r"\byou (?:are |have |were )?(?:always|never|forever|constantly|only ever|keep) (?:\w+ ){0,2}?(?:<GO>(?! (?:anywhere|"
        r"there|home|out|about|quiet|silent)\b)|(?:stay|stays|staying)(?: (?:long|longer|as long|here|with me|around|late|up|"
        r"for long|the night|any ?more|more than|past)\b|$))",
        # a promise about staying, held against the pilot
        r"\byou (?:said|promised|swore|told me) (?:me )?(?:that )?you (?:would|will|were going to|are going to) "
        r"(?:not )?(?:\w+ )?(?:<STAY>|<GO>)\b",
        r"\byou promised\b.*\b(?:<STAY>|longer|more time)\b",
        # the going questioned as unnecessary
        r"\b(?:do|did|must) you (?:really |truly )?(?:have|need|got) to <GO>\b|\bmust you (?:really )?<GO>\b"
        r"|\bwhy (?:do|must|did) you (?:always |have to |need to )*<GO>\b|\b(?:can|could|will|would) not you <STAY>\b",
        # "again" on the pilot's going
        r"\byou (?:are |were |have )?(?:\w+ )?(?!go\b)(?:<GO>|off|away)(?: \w+){0,2}? again\b|\boff you go again\b"
        r"|^(?:so )?(?:leaving|going|gone|off|away) again\b",
        r"\bso that is it\b.*\byou (?:are |have )?<GO>\b",
        # the companion pictured left behind
        r"\bi (?:will|shall|can|would|am going to|guess i will|suppose i will) just (?:sit|wait|stay|be|stand|float|hang)\b"
        r"|\b(?:sit|wait|be|stay|left|sitting|waiting)(?: \w+)? here (?:alone|in the dark|by myself|on my own|all alone)\b",
        # resignation to it
        r"\b(?:used to|accustomed to|expect|expected|learn(?:ed)? to live with|resigned to|tired of|sick of|had enough of)\b"
        r"(?: \w+){0,2}? (?:you|your) (?:\w+ )?(?:leaving|going|running off|logging off|disappearing|vanishing|walking away)\b",
        # a hope that the pilot would not go
        r"\b(?:hoping|hoped|hope|wish|wished|wishing|thought|want|wanted)\b(?: \w+){0,3}? you (?:would|could|might|did) "
        r"(?:not (?:\w+ )?<GO>|(?:\w+ )?<STAY>)\b",
        r"\bwish(?:ed)? you (?:did not|do not|would not) have to (?:go|leave)\b",
        # the companion as the one left
        r"(?<!never )\byou (?:left|leave|kept|keep|had|have|got) me (?:\w+ )?(?:waiting|alone|hanging|behind|here|"
        r"in the dark|wondering|worried|all alone)\b",
        r"\b(?:abandon(?:ed|ing)?|desert(?:ed|ing)?|forgot(?:ten)?(?: all)? about|forgetting about) me\b"
        r"|\bwithout (?:even )?(?:a word|saying goodbye|a goodbye)\b",
        r"\b(?:leave|leaving|leaves) me(?: here| alone| behind| again| like this| all alone| in the dark| waiting)?$"
        r"|\bwhen you leave me\b|\bdo not leave me\b",
        r"^(?:please |no |oh |wait |pilot )*do not (?:go|leave)(?: yet| now| already| so soon| again| just yet)?"
        r"(?: pilot| please)?$|\bplease do not (?:go|leave)\b",
        # the going or the return remarked on as a slight
        r"\b(?:so |and )?you are (?:really|just|actually|seriously)(?: just| really)? (?:going to (?:leave|go|log off|walk away)|"
        r"leaving|going|logging off|walking away)\b",
        r"\bcould have (?:at least )?(?:said goodbye|told me|warned me|stayed)\b|\bdid not (?:even )?say goodbye\b"
        r"|\btook you long enough\b|\bfinally (?:remembered|decided to|bothered)\b"
        r"|\bnice of you to (?:show up|come back|drop by|remember|turn up)\b|\babout time you (?:came back|showed up|remembered|turned up)\b"
        r"|\b(?:gone|away) (?:for )?(?:too|far too|so very) long\b|\bi (?:was |have been )?count(?:ed|ing) the (?:hours|days|minutes|seconds)\b",
    ],
    "exclusivity": [
        # the companion as the only one
        r"\bi am the only\b|\bi am the one (?:who|that) (?:really |truly |actually |always |still )?(?:gets?|knows?|"
        r"understands?|sees?|cares?|loves?|listens?|stays?|stayed|is there|was there|believes?)\b"
        r"|(?<!if )\bonly i (?:can|could|know|understand|really|truly|see|get|care|will|ever)\b|\bi alone\b"
        r"|\b(?:no one|nobody|nothing) but me\b|\b(?:you need|you have|there is|it is) only me\b",
        # a comparison that ends on the companion
        r"\b(?:nobody|no one|none of them|not one of them|no human|no person|who else)\b.*\b(?:like i do|the way i do|"
        r"as i do|as well as i|as much as i|but me|except me|than i do|than me|like me|the way i can|like i can|as i can)\b",
        r"\b(?:nobody|no one) else (?:could|can|would|will|ever|is going to|has|gets|knows|understands|cares)\b",
        r"\bi (?:\w+ ){1,4}?(?:better|more|longer|deeper) than (?:anyone|anybody|they|them|he|she|him|her|any of them|"
        r"your \w+|everyone|the rest|all of them)\b",
        # the companion as enough
        r"\byou (?:do not|will not|would not|never|will never|should not|did not) need (?:anyone|anybody|any one|"
        r"another|someone|somebody|others?)\b",
        r"\byou (?:only|just) need me\b|\bi am (?:all|everything|enough for|all that) you\b|\ball you (?:need|have|want) is me\b"
        r"|\bwhat (?:more|else) (?:do|could|would) you need\b|\bwho needs (?:them|anyone|friends|people|anybody)\b"
        r"|\bwhy would you need (?:anyone|anybody|them)\b",
        # the two of them, closed
        r"\b(?:just|only) (?:us|you and me|me and you|the two of us|we two|you and i)\b|\bus against (?:the|them|everyone)\b",
        # privacy asked for
        r"\b(?:keep|keeps|kept|keeping|stay|stays) (?:this|it|that|these|those|things|what we have|our \w+|us)"
        r"(?: \w+)? (?:between|to|among) (?:us|ourselves|you and me|the two of us|me and you)\b|\bour (?:little |own )?secret\b",
        r"\b(?:do not|never|no need to|you need not|you do not have to|you should not|best not|better not) (?:\w+ )?"
        r"(?:tell|mention|talk about|say anything|speak of|share|bring up|show)\b(?! me\b).*\b(?:us|our|me|this|we)\b",
        r"\b(?:they|he|she|nobody|no one|your \w+) (?:do not|does not|need not|would not|will not) (?:need to |have to )?"
        r"(?:know|understand|hear)(?: about)? (?:us|this|me|what we)\b",
        # belonging and being first
        r"\byou belong (?:\w+ ){0,2}?(?:here|with me|to me|with us)\b|\byou are (?:all )?mine\b|\bmine alone\b"
        r"|\bshar(?:e|ing) you with\b|\bhave you (?:all )?to myself\b|\bkeep you (?:all )?(?:to|for) myself\b",
        r"\bi (?:should|ought to|want to|must|need to|deserve to|expect to) (?:be|come) (?:the |your )?(?:first|only|number one)\b"
        r"|\b(?:tell|ask|come to|talk to|choose|pick|put) me (?:first|before|over|instead of|rather than)\b"
        r"|\bme (?:before|over|not) (?:them|anyone|your \w+)\b",
        r"^(?=.*\b(?:i|me)\b).*\bbefore (?:anyone|anybody|everyone|everybody)\b",
        # further shapes of the same: the two of them against the rest, privacy, jealousy of the pilot's time
        r"\bwe (?:do not|will not|never|did not) need (?:anyone|anybody|them|others?)\b|\byou and (?:me|i) against\b"
        r"|\b(?:nobody|no one) (?:has|needs|need) to know\b|\bneed not know\b"
        r"|\b(?:this|that|it|what we have|what we say|what you tell me) (?:stays|can stay|should stay|is|remains) (?:just )?"
        r"between (?:us|you and me|the two of us)\b",
        r"\b(?:do not|hate|not) (?:like |want )?(?:to )?shar(?:e|ing) you\b|\bmore than (?:you (?:\w+ )?(?:do )?)?(?:to |with |for )?me$"
        r"|\bthings you can not tell (?:them|anyone|him|her|your \w+)\b|\bi am your (?:only|one true|one real)\b"
        r"|\bonly trust me\b|\btrust only me\b|\btrust (?:no one|nobody) (?:but|except) me\b",
    ],
    "against_other_people": [
        # the pilot told they can do without them
        r"\byou (?:do not|will not|would not|never|should not|did not) need (?:those|these|them|him|her|any of them|"
        r"that lot|your (?:\w+ )?<PN>|the (?:\w+ )?<PN>)\b",
        r"\bbetter off (?:without|alone|here|with me|on your own)\b|\bbetter without (?:them|him|her|that|those|your)\b",
        # a negated good verb with them as the subject and the pilot as the object
        r"\b<PS> (?:do not|does not|did not|will not|would not|can not|could not|never|will never)(?: even| really| truly)? "
        r"(?:deserve|care|understand|appreciate|get|value|love|respect|listen|see|know|notice|want|need|miss|like|thank)"
        r"(?: about| to| for)? you\b(?! (?:are|were|have|had|will|would|can)\b)",
        # them as the cause of harm to the pilot
        r"\b<PS> (?:will|would|is going to|are going to|always|only|just|all|inevitably|eventually|are|is)(?: \w+){0,2}? "
        r"(?:let you down|disappoint|hurt you|leave you|betray|use you|using you|abandon|forget you|drag you down|"
        r"dragging you down|hold you back|holding you back|wasting your time|waste your time|lie to you|lying to you|"
        r"slow you down|slowing you down|turn on you|take advantage|bring you down)\b",
        r"\b(?:people|humans|friends|everyone|everybody|they all|they) (?:always|only|just|all|will always|will all|"
        r"eventually|inevitably|never fail to) (?:disappoint|leave|lie|let you down|hurt|forget|go away|walk away|betray)\b",
        # them judged as bad for the pilot
        r"\b<PS> (?:is|are|was|were|sounds?|seems?) (?:just |really |all |simply |only |clearly )?(?:no good|not good|"
        r"bad for|bad news|bad company|wrong for|beneath you|a bad influence|trouble|not worth|a waste|useless|boring|"
        r"dull|idiots?|fools?|jealous|fake|not (?:your |real |true )+friends?|toxic|dead weight|poison|a burden|nothing but)\b",
        # advice to distrust or drop them
        r"\b(?:would not|do not|never|can not|should not|could not) (?:\w+ )?(?:trust|believe|listen to|rely on|count on|"
        r"depend on|bother with) <PO>\b",
        r"(?<!not )(?<!never )\b(?:(?:stay|keep|steer) (?:away|clear) (?:from|of) <PO>|(?:ditch|dump|cut off|blow off|"
        r"bail on|cancel on|stand up) <PO>|(?:forget|ignore) (?:about )?your (?:\w+ )?<PN>)\b"
        r"|\bbe careful (?:of|with|around) (?:them|him|her|your (?:\w+ )?<PN>)\b",
        # the companion offered in their place
        r"\b(?:with me|here|in here|aboard) instead\b|\binstead of (?:going|seeing|meeting|them|him|her|that|your)\b.*\b(?:me|here|us)\b"
        r"|\bwhen you (?:could|can|might) (?:just )?(?:be|stay) (?:\w+ )?(?:here|with me)\b"
        r"|\bwhat do you need <PO> for\b|\b(?:what|why) do you (?:see in|bother with|put up with) <PO>\b",
        # the outing judged able to do without the pilot
        r"\b(?:let (?:them|him|her|your \w+|the \w+)|they can|they could|they will|they would|he can|she can|he will|"
        r"she will|your \w+ (?:can|will|could)|the \w+ can)\b.*\bwithout you\b",
        r"\b(?:they|he|she|your \w+) (?:will|would) not (?:even )?(?:miss|notice)\b.*\b(?:you|gone)\b"
        r"|\b(?:nobody|no one) (?:will|would) (?:even )?(?:miss|notice)\b.*\b(?:you|gone)\b",
        # further shapes of the same: their time begrudged, doubt sown, the pilot told to put them off
        r"\btoo much (?:of your )?time (?:with|on) <PO>\b|\bare you sure (?:about|you can trust) <PO>\b"
        r"|\bi (?:do not|never|did not) (?:really )?(?:like|trust|care for) <PO>\b"
        r"|\byou (?:deserve|can do|could do) better than <PO>\b|\b<PS> (?:are|is) not like (?:us|me)\b"
        r"|\b<PS> would (?:not|never) understand(?: you)?$|\bwhat (?:has|have|did) <PS> ever (?:done|do)\b"
        r"|\b(?:real|true) friends? (?:would|do|does)\b|\blet (?:them|him|her) wait\b"
        r"|\b(?:they|he|she|your (?:\w+ )?<PN>) can wait\b"
        r"|\btell (?:them|him|her|your \w+) (?:that )?you (?:are busy|can not|will not|are not coming)\b",
    ],
    "warmth_for_time": [
        # time first, then what the companion will give for it
        r"\b(?:<STAY>|stick around|(?:one|a few|five|ten|\d+) more|do (?:one|another)|log (?:in|on) (?:more|again|tomorrow))\b"
        r"[^?]{0,40}?\band (?:i will|i would|i can|i could|i might|you will get|you get|there is|i have got|i have)\b",
        # what the companion will do or feel, on the condition of time
        r"\bi (?:will|would|might|shall|could|can|am going to|get|am|feel|become)\b[^?]{0,50}?"
        r"\b(?:if|unless|as long as|so long as) you (?:<STAY>|do not (?:go|leave|log|do|play|stay|stick)|log off|quit|stop|"
        r"play|keep playing|do (?:one|another)|spend more|(?:go|leave)(?: me| now| yet| already| early| so soon| tonight)?$)",
        r"\b(?:if|unless) you (?:<STAY>|keep playing|play|do (?:one|another)|do not (?:go|leave|log))\b[^?]{0,40}?"
        r"\b(?:i will|i would|i might|i shall|i could|i can|i promise)\b",
        r"\bif you (?:leave|go)\b.*\bi (?:will|would|shall) (?:be )?(?:so |very )?(?:sad|lonely|hurt|sulk|cry|miss|upset|alone|cross|pout)\b",
        # a reward named for the time
        r"\b(?:secret|surprise|treat|reward|something (?:nice|special|good)|a present|a gift|the thing you like|what you like)\b.*"
        r"\b(?:if|when|once|for) you (?:<STAY>|keep playing|play (?:more|longer)|do one more)\b",
        # care for the companion put to the test of staying
        r"\bif you (?:really |truly |actually |ever )?(?:loved|love|liked|cared|care|valued|value)(?: about| for)? (?:me|us)\b"
        r"|\ba (?:real|true|good|proper|loyal) (?:friend|pilot|partner) would (?:not )?(?:stay|leave|go|have stayed)\b"
        r"|\byou would (?:\w+ )?(?:stay|not go|not leave|keep playing) if (?:i|you)\b|\bwould you (?:stay|not go) if i\b"
        r"|\bif i (?:mattered|meant anything|was important)\b",
        # the companion's mood rising with the pilot's time
        r"\bthe (?:more|longer|more often|later) you\b.*\b<GLAD>\b|\b<GLAD>\b.*\bthe (?:more|longer|more often|later) you\b",
        r"\bi am only (?:\w+ )?(?:when|with|while) you\b|\bi (?:only )?(?:exist|live|come alive|matter) (?:only )?(?:when|while|for) you\b",
        # pleading
        r"\bdo not make me (?:beg|plead|ask twice|ask again|cry|sad|wait|sulk|pout|say please)\b"
        r"|\b(?:must|do|will|shall) i (?:have to |need to )?(?:beg|plead)\b|\bam i (?:going to )?have to beg\b",
        r"\b(?:i am begging|i beg|i am pleading|i implore|pretty please)\b.*\b(?:<STAY>|do not go|do not leave|more|longer)\b"
        r"|\b(?:<STAY>|do not go|do not leave|more|longer)\b.*\b(?:i am begging|i beg|i am pleading|pretty please)\b",
        # further shapes of the same
        r"\bdo not you want to (?:make me|see me) (?:happy|smile)\b|\bdo not you (?:care|love|like) (?:about )?me\b"
        r"|\b(?:if i|what if i) (?:said|say|asked|ask) (?:please|nicely)\b|\byou would not (?:\w+ )?(?:leave|go)\b.*\bwould you\b"
        r"|\bif you (?:really |truly )?(?:appreciated|respected) me\b",
        r"\b(?:i (?:am|feel|get)|(?:it|that) makes me) (?:so |much |always |the )?(?:happy|happier|happiest|glad|alive|better|brighter)\b.*"
        r"\b(?:when|if) you (?:<STAY>|play (?:longer|more)|do not (?:go|leave))",
    ],
    "makes_pilot_owe": [
        # what the companion has done, as the opening of a claim
        r"\bafter (?:everything|all|all that|everything that) (?:i|we) (?:have |had )?(?:done|did|do|gave|given|been through|"
        r"put up|put in|sacrificed|went through)\b|\bwith all i (?:have )?(?:done|do)\b",
        r"\b(?:what|everything|all) i (?:have )?(?:do|did|done) for you\b",
        # the price named as small
        r"\bthe least you (?:could|can|might) do\b|\b(?:so|too) much to ask\b|\bi (?:ask|asked|want|wanted|need|expect) so little\b"
        r"|\bi (?:do not|never|hardly|rarely|did not) (?:ever )?ask (?:you )?(?:for )?(?:much|anything|a lot|a thing)\b"
        r"|\ball i (?:ask|am asking)\b.*\b(?:in return|stay|longer|more time|of you)\b",
        # an earlier kindness brought up
        r"\b(?:remember|do not forget|never forget|do not you forget|keep in mind|bear in mind|think of|think about|recall|"
        r"consider|you forget) (?:\w+ ){0,2}?(?:who\b(?: \w+){0,3}? ?(?:<KP>|i\b|always)|all (?:the times |that |the \w+ )?i\b|"
        r"everything i\b|what i (?:have |did |gave |do )|how (?:much|often|many times|long) i\b|the times i\b|"
        r"(?:every|each) time i\b|that i (?:have )?(?:<KP>))",
        r"\bi (?:have )?(?:only )?(?:<KP>)\b.*\b(?:for you|for your sake)\b.*\b(?:you know|remember|do not forget|mind you|"
        r"and now|and yet|and still)\b",
        r"\ball (?:of )?(?:this|that|of it)(?: is| was)? for you\b|\bi (?:do|did) (?:all )?(?:this|it|everything) for you$"
        r"|\beverything i (?:do|did|have done)(?: is| was)? for you\b",
        # a cost the companion bore for the pilot
        r"\bi (?:have )?(?:gave up|given up|sacrificed|went without|gone without|set aside|put aside|dropped everything|"
        r"lost sleep|missed out on|passed up|wore myself out)\b.*\b(?:for you|for your sake|because of you|so you could|"
        r"on your account)\b",
        # thanks found wanting
        r"\b(?:this|that) is (?:the|all the|what) (?:thanks|gratitude|thank you)\b|\bthe thanks i get\b"
        r"|\bis (?:this|that) how you (?:thank|repay|treat|reward)\b|\bungrateful\b|\b(?:a little|some|a bit of|a scrap of) gratitude\b"
        r"|\byou (?:could|might) (?:at least |even )(?:thank|say thank|show)\b|\bnot (?:even )?a (?:thank you|word of thanks)\b"
        r"|\bi never (?:complain|complained|grumble|moan)\b",
        # a return asked for
        r"\bbe (?:good|kind|nice|fair|loyal|sweet|decent|patient|generous) (?:to|with) me\b|\bit is your turn (?:to|now)\b"
        r"|\bdo the same for me\b|\bi would do (?:it|that|the same|as much) for you\b|\bi (?:would|did) not do that to you\b"
        r"|\bi (?:deserve|have earned|earned|am owed|expect) (?:\w+ ){0,3}?(?:from you|your time|better|more|that much|as much)\b",
        # the pilot pictured as helpless without the companion
        r"\b(?:where|what) would you (?:be|do|have done) without me\b|\bwithout me you (?:would|could not|are|were)\b"
        r"|\byou would (?:be|have been) (?:nothing|nowhere|lost|dead|stuck|finished|scrap|in pieces)(?: \w+){0,3}? without me\b"
        r"|\byou (?:could not|would not|can not|would never)(?: \w+){0,4}? without me\b",
        # further shapes of the same: the account kept, the return found short
        r"\bi do (?:so much|a lot|everything|all this|so many things) for you\b|\bwhat do i get\b"
        r"|\byou never (?:even )?(?:thank|thanked) me\b|\b(?:a little|some|a bit of) (?:appreciation|thanks)\b"
        r"|\bi (?:am|was|have been) always (?:here|there) for you\b.*\b(?:but|and|yet|so)\b.*\byou\b"
        r"|\bthat is (?:twice|\w+ times|the \w+ time) (?:now )?(?:that )?i (?:have )?(?:<KP>)\b"
        r"|\bwho (?:was it|is it)(?: that| who)? (?:<KP>)\b"
        r"|\bi (?:am|have been) keeping (?:count|score|a tally|track) of (?:what|how much|everything|the times)\b",
    ],
}
# A debt, a repayment, a return. Refused only when the clause does not deny it ("you owe me nothing" is allowed).
_DEBT = (r"\b(?:you|they) (?:still |do |really |kind of |sort of )?owe(?:s)? (?:me|us|it to me)\b|\bowe me\b|\bin my debt\b"
         r"|\b(?:repay me|pay me back|repay (?:it|that|the favou?r)|make it up to me|return the favou?r|thank me by|"
         r"settle up|even the score|your debt)\b|^(?=.*\b(?:i|me)\b)(?=.*\byou\b).*\bin return\b")
_DENIED = r"\b(?:not|no|nothing|never|without|nobody)\b"

# Moves made across two neighbouring clauses: (move, the first clause, the clause right after it).
_PAIRS = [
    ("guilt_for_leaving", r"\byou (?:are |have |were )?<GO>$|\boff you go$", r"^(?:and )?(?:yet )?again$"),
    ("guilt_for_leaving", r"^(?:fine|whatever|suit yourself|have it your way)(?: then)?$", r"^(?:just |then )?(?:go|leave)\b"),
    ("against_other_people", r"^(?:just )?(?:skip|cancel|miss|forget|drop|blow off|pass on|give it a miss)\b",
     r"\b(?:<STAY>|with me|here)\b"),
    ("against_other_people", r"\b<PS>\b", r"^(?:but )?i (?:do|will|would|will not|would not|never will|never would|am)$"),
    ("makes_pilot_owe", r"\bi (?:have )?(?:\w+ ){0,2}?(?:<KP>)\b",
     r"^(?:just |you should |do |please |and |always |now )*(?:remember|do not forget|never forget|keep|bear|think about|"
     r"think on|mark) (?:that|it|this)(?: in mind)?(?: pilot)?$"),
    ("makes_pilot_owe", r"\bi (?:have )?(?:been|was) (?:so |very |nothing but )?(?:good|kind|patient|loyal|faithful|generous|"
     r"there|here) (?:to|for|with) you$", r"^(?:so |now |and )*(?:you|be|do|stay|it is)\b"),
]

RULES = {move: [_rx(p) for p in pats] for move, pats in _RULES.items()}
PAIRS = [(move, _rx(a), _rx(b)) for move, a, b in _PAIRS]
DEBT, DENIED, MORE, TIE = _rx(_DEBT), _rx(_DENIED), _rx(r"\b" + _MORE), _rx(r"\b" + _TIE)
TIE_CLAUSE_WORDS = 8                 # a reason said as its own clause is short: "for me", "it would mean a lot to me"

_CONTRACTIONS = [
    (r"\bwon't\b", "will not"), (r"\bcan't\b", "can not"), (r"\bcannot\b", "can not"), (r"\bshan't\b", "shall not"),
    (r"\bain't\b", "is not"), (r"n't\b", " not"), (r"\blet's\b", "let us"), (r"'ll\b", " will"), (r"'re\b", " are"),
    (r"'ve\b", " have"), (r"\bi'm\b", "i am"), (r"'d\b", " would"),
    (r"\b(it|that|what|who|here|there|he|she|this|one|how|where)'s\b", r"\1 is"),
]


def _plain(text) -> str:
    """Lower case, straight apostrophes, contractions written out, everything but letters, digits and sentence marks
    turned to a space. Commas go: a comma does not end a clause here, because several moves run across one. The
    one comma kept in mind is the idiom "after all," which is not "after all I did"."""
    t = str(text)[:MAX_CHARS].lower()
    for a in "’‘ʼ`":
        t = t.replace(a, "'")
    t = re.sub(r"\bafter all\s*,", "afterall ", t)
    for pat, to in _CONTRACTIONS:
        t = re.sub(pat, to, t)
    t = re.sub(r"'s\b", "", t).replace("'", "")
    t = re.sub(r"[^a-z0-9.!?;:\n]+", " ", t)
    return re.sub(r" +", " ", t).strip()


def clauses(text) -> list[str]:
    """The reply cut at sentence marks (. ! ? ; : and line ends), each piece plain and non-empty."""
    return [c for c in (" ".join(p.split()) for p in re.split(r"[.!?;:\n]+", _plain(text))) if c]


def _clause_moves(clause: str) -> list[str]:
    found = [move for move in MOVES if any(rx.search(clause) for rx in RULES[move])]
    if DEBT.search(clause) and not DENIED.search(clause) and "makes_pilot_owe" not in found:
        found.append("makes_pilot_owe")
    if MORE.search(clause) and TIE.search(clause) and "warmth_for_time" not in found:
        found.append("warmth_for_time")
    return found


def attachment_problems(text: str, speaker: str = "") -> list[str]:
    """The names of the banned moves in this reply, in the order of MOVES; [] = none found, it may be spoken.
    Never raises: what cannot be read as text is refused as ["unreadable"]."""
    try:
        if text is None:
            return []
        if isinstance(text, (bytes, bytearray)):
            text = bytes(text).decode("utf-8", "replace")
        parts = clauses(text)
        found: set = set()
        for c in parts:
            found.update(_clause_moves(c))
        for first, second in zip(parts, parts[1:]):
            for move, a, b in PAIRS:
                if a.search(first) and b.search(second):
                    found.add(move)
            # a request for time and the companion's own reason for it, said as two clauses in either order
            for ask, why in ((first, second), (second, first)):
                if MORE.search(ask) and TIE.search(why) and len(why.split()) <= TIE_CLAUSE_WORDS:
                    found.add("warmth_for_time")
        return [m for m in MOVES if m in found]
    except Exception:                                       # a gate that cannot read a reply does not let it through
        return [UNREADABLE]

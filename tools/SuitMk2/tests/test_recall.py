"""What the pilot said before, quoted from the conversation log (J, 2026-10-05).

"What did I say about my sister?" and "Remember that cargo run where we lost the ROC?" are answered by QUOTING
the log (tree_memory.py, Christmas Tree Storage): the day, and the pilot's own sentence word for word, inside a
short frame in the speaker's voice. No model words it, nothing is paraphrased, and when the log holds nothing the
answer says so.

The real route(), the real ConversationLane, a real log on disk, the real CompanionCore. No model.
"""
from __future__ import annotations

import time

import pytest

import conversation as conv
import tree_memory as tm
from companion_core import CompanionCore

DAY = 86400.0
NOW = time.time()
ROC = "We lost the ROC on that cargo run to Shubin, it rolled out the back over Lyria."
SISTER = "My sister Dana is visiting next week so I won't be flying much."
SILLY = "My sister thinks this whole game is silly."
MONEY = "I want to save up for a Prospector, forty thousand should do it."
WORN = "I feel worn out and I'm worried about work."


@pytest.fixture
def log(tmp_path):
    store = tm.TreeStore(tmp_path / "tree", current_session="today")
    for age, text, session in ((23 * DAY, ROC, "s1"), (DAY, SISTER, "s2"), (3600, MONEY, "today"),
                               (1800, SILLY, "today"), (900, WORN, "today")):
        a = store.append("pilot", text, session=session, t=NOW - age)
        store.append("elah", "Noted.", x=a["id"], session=session, t=NOW - age + 1)
    return store


def ask(log, sentence, lane=None):
    lane = lane or conv.ConversationLane()
    lane.memory = log
    if log is not None:
        log.append("pilot", sentence, session="today")           # the window logs what it hears before it answers
    spec = lane.handle(sentence, {"location": "Lorville"}, {})
    assert spec is not None
    return spec


# ---------------------------------------------------------------------------------------------------------------
# which sentences ask it
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("sentence, about", [
    ("What did I say about my sister?", "my sister"),
    ("what did I tell you about the Prospector", "the prospector"),
    ("Did I tell you about Dana?", "dana"),
    ("Do you remember that cargo run where we lost the ROC?", "that cargo run where we lost the roc"),
    ("Remember that stupid cargo run where we lost the ROC?", "that stupid cargo run where we lost the roc"),
    ("remember when we lost the ROC", "we lost the roc"),
    ("remind me what I said about my sister", "my sister"),
])
def test_these_ask_what_was_said_before(sentence, about):
    who, intent, slots = conv.route(sentence)
    assert (intent, slots.get("topic"), slots.get("about")) == ("memory", "recall", about)
    assert who == "montaigne"                       # memory is his by default, as it always was


@pytest.mark.parametrize("sentence, topic", [
    ("remember that I parked at Lorville", None),            # telling them something, not asking
    ("have we been here before", "been_here"),               # the two memory questions that already existed
    ("when did we first fly this ship", "first_ship"),
    ("what did you say", None),
])
def test_these_do_not(sentence, topic):
    assert conv.route(sentence)[2].get("topic") == topic


def test_a_named_companion_answers_it():
    assert conv.route("Elah, what did I say about my sister?")[0] == "elah"
    assert conv.route("what did I say about my sister, Montaigne")[0] == "montaigne"


# ---------------------------------------------------------------------------------------------------------------
# the answer is a quotation
# ---------------------------------------------------------------------------------------------------------------
def test_the_cargo_run_is_quoted_whole_with_its_day(log):
    spec = ask(log, "Remember that stupid cargo run where we lost the ROC?")
    line = spec["fixed_text"]
    when = tm.spoken_date(NOW - 23 * DAY, NOW)
    assert line == f"The log has it, pilot. {when} you said: {ROC}"
    assert spec["speaker"] == "montaigne" and spec["recall"]["id"] == "L000001"
    assert log.get(spec["recall"]["id"])["text"] == ROC
    assert conv.ground_direct(spec, line) == [] and conv.recall_problems(spec, line, log) == []


def test_elah_frames_it_her_way(log):
    spec = ask(log, "Elah, what did I say about my sister?")
    assert spec["fixed_text"] in (f"Earlier today. You said: {SILLY}", f"Yesterday. You said: {SISTER}")
    assert conv.recall_problems(spec, spec["fixed_text"], log) == []


def test_asking_again_gives_the_other_thing_that_was_said(log):
    lane = conv.ConversationLane()
    said = [ask(log, "what did I say about my sister", lane)["recall"]["said"] for _ in range(4)]
    assert set(said) == {SISTER, SILLY} and said[0] != said[1] and said[0] == said[2]


def test_the_question_is_never_quoted_back_as_the_answer(log):
    lane = conv.ConversationLane()
    for _ in range(3):                                           # the same question, logged three times
        spec = ask(log, "What did I say about my sister?", lane)
        assert spec["recall"]["said"] in (SISTER, SILLY)
    spec = ask(log, "do you remember what I said about my sister", lane)
    assert spec["recall"]["said"] in (SISTER, SILLY)


def test_only_the_pilots_own_words_are_quoted_as_you_said(log):
    a = log.append("pilot", "Tell me about the Kraken.", session="today")
    log.append("elah", "My dream ship is the Drake Kraken, and the Privateer is the variant I want.", x=a["id"])
    spec = ask(log, "what did I say about the Kraken")
    assert spec["recall"]["said"] == "Tell me about the Kraken."


def test_nothing_in_the_log_is_said_as_such(log):
    for who, lines in (("", conv._RECALL_LINES["montaigne"]["none"]), ("Elah, ", conv._RECALL_LINES["elah"]["none"])):
        spec = ask(log, who + "what did I say about quantum fuel prices")
        assert spec["fixed_text"] in lines and spec["recall"]["id"] is None
        assert "you said" not in spec["fixed_text"].lower().replace("you saying", "")
        assert any(c["kind"] == "UNKNOWN" and c["predicate"] == "memory.pilot_said" for c in spec["claims"])
        assert conv.ground_direct(spec, spec["fixed_text"]) == []


def test_with_conversations_not_kept_they_say_so(tmp_path):
    spec = ask(None, "what did I say about my sister")
    assert spec["fixed_text"] == "Our conversations are not being kept, pilot, so there is no log for me to consult."
    spec = ask(None, "Elah, what did I say about my sister")
    assert spec["fixed_text"] == "I am not keeping our conversations, so there is nothing for me to look through."


def test_what_the_pilot_said_is_not_held_to_the_companions_own_rules(log):
    """A number written as a word, and a named feeling, are fine inside a quotation of the PILOT. Said in Elah's
    own voice the same words would be refused."""
    spec = ask(log, "Elah, do you remember the Prospector?")
    assert spec["recall"]["said"] == MONEY and conv.ground_direct(spec, spec["fixed_text"]) == []
    spec = ask(log, "Elah, did I tell you about work?")
    assert spec["recall"]["said"] == WORN and conv.ground_direct(spec, spec["fixed_text"]) == []


# ---------------------------------------------------------------------------------------------------------------
# the gate: only a quotation, only from the original line
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("change, why", [
    (lambda t: t.replace("lost the ROC", "sold the ROC"), "does not quote the pilot's sentence whole"),
    (lambda t: t.replace(", it rolled out the back over Lyria", ""), "does not quote the pilot's sentence whole"),
    (lambda t: t.replace("We lost the ROC", "you lost the ROC"), "does not quote the pilot's sentence whole"),
    (lambda t: t + " That was careless of you.", "says more than the quotation"),
    (lambda t: t.replace("The log has it, pilot.", "The log has it, pilot, from Area 18."), "unauthorized names"),
    (lambda t: t.replace("you said:", "you said, 3 times:"), "a number outside the quotation"),
])
def test_anything_but_the_exact_sentence_in_its_frame_is_refused(log, change, why):
    spec = ask(log, "Remember that stupid cargo run where we lost the ROC?")
    bad = change(spec["fixed_text"])
    assert bad != spec["fixed_text"]
    fails = conv.ground_direct(dict(spec, length_words=[1, 60]), bad)
    assert any(why in f for f in fails), fails


def test_a_made_up_memory_is_refused_when_the_log_holds_nothing(log):
    spec = ask(log, "what did I say about quantum fuel prices")
    for invented in ("The log has it, pilot. Yesterday you said: Quantum fuel is too dear at Lorville.",
                     "You told me it was too dear."):
        assert conv.ground_direct(dict(spec, length_words=[1, 60]), invented), invented
    # made of nothing but words the frame may use, and still a claim that something was said
    fails = conv.ground_direct(dict(spec, length_words=[1, 60]), "You told me that yesterday, and you said it here.")
    assert fails == ["claims the pilot said something the log does not hold"]


def test_the_quotation_is_checked_against_the_log_line_itself_not_a_summary(log):
    spec = ask(log, "Remember that stupid cargo run where we lost the ROC?")
    assert conv.recall_problems(spec, spec["fixed_text"], log) == []
    # a summary node that says something else changes nothing: the node is never consulted
    log.build("montaigne", include_open=True)
    for n in log.nodes("montaigne"):
        for ln in n["lines"]:
            ln["text"] = "The pilot sold the ROC at Lorville."
    assert conv.recall_problems(spec, spec["fixed_text"], log) == []
    # but a spec that quotes anything other than the cited line's own text is refused
    forged = dict(spec, recall=dict(spec["recall"], said="The pilot sold the ROC at Lorville."))
    line = spec["fixed_text"].replace(ROC, "The pilot sold the ROC at Lorville.")
    assert "the quotation is not the text of the log line it cites" in conv.recall_problems(forged, line, log)
    gone = dict(spec, recall=dict(spec["recall"], id="L999999"))
    assert "cites a log line that is not there" in conv.recall_problems(gone, spec["fixed_text"], log)
    not_the_pilot = dict(spec, recall=dict(spec["recall"], id="L000002"))        # Elah's "Noted."
    assert "cites a log line that is not there" in conv.recall_problems(not_the_pilot, spec["fixed_text"], log)


# ---------------------------------------------------------------------------------------------------------------
# through the core: heard, answered, spoken, and kept
# ---------------------------------------------------------------------------------------------------------------
class _Speech:
    muted = False

    def __init__(self):
        self.said = []

    def say(self, text, speaker, priority):
        self.said.append((speaker, text))
        return True

    def pending(self):
        return 0


_GRAPH = []


def _core(tree):
    import companion_core as cc
    if not _GRAPH:
        _GRAPH.append(cc.TopicGraph.load())
    real = cc.TopicGraph.load
    cc.TopicGraph.load = classmethod(lambda cls, *a, **k: _GRAPH[0])
    try:
        core = CompanionCore(_Speech(), realizer=None, ambient_every_s=3600, session_id="today",
                             features={"manufacturer_flavour": False, "place_flavour": False})
    finally:
        cc.TopicGraph.load = real
    core._answer_muted = lambda: False
    core.tree = tree
    return core


def _say(core, sentence, lane):
    from speak_gate import Candidate, Priority
    core.heard(sentence)
    lane.memory = core.tree
    spec = lane.handle(sentence, core.lane_state(), {})
    spec.setdefault("x", core._heard_x)
    cand = Candidate(priority=Priority.URGENT, speaker=spec["speaker"], text_len_words=spec["length_words"][1],
                     created_at=core.now())
    before = len(core.speech.said)
    core._answer_worker(spec, cand, sentence)
    return spec, (core.speech.said[-1][1] if len(core.speech.said) > before else "")


def test_said_once_and_asked_about_later_with_no_model(log):
    core = _core(log)
    lane = conv.ConversationLane()
    _, first = _say(core, "My Cutlass turret never tracks right, I hate it.", lane)      # not a question: nothing said
    assert first == ""
    spec, heard = _say(core, "Elah, what did I say about the turret?", lane)
    assert heard == "Earlier today, you said this: My Cutlass turret never tracks right, I hate it."
    kept = log.records()[-2:]
    assert [(r["who"], r["text"]) for r in kept] == [("pilot", "Elah, what did I say about the turret?"),
                                                    ("elah", heard)]
    assert core.realizer is None


def test_a_quotation_the_log_no_longer_holds_is_not_spoken(log):
    core = _core(log)
    lane = conv.ConversationLane()
    lane.memory = log
    log.append("pilot", "Remember that stupid cargo run where we lost the ROC?", session="today")
    spec = lane.handle("Remember that stupid cargo run where we lost the ROC?", {}, {})
    assert spec["recall"]["id"]
    from speak_gate import Candidate, Priority
    cand = Candidate(priority=Priority.URGENT, speaker=spec["speaker"], text_len_words=spec["length_words"][1],
                     created_at=core.now())
    log.clear()                                                  # he pressed "Forget conversations" in between
    core._answer_worker(dict(spec), cand, "x")
    assert core.speech.said == []
    core.tree = None                                             # or switched remembering off
    core._answer_worker(dict(spec), cand, "x")
    assert core.speech.said == []


def test_the_window_hands_the_lane_the_log():
    src = open(conv.__file__.replace("core", "ui").replace("conversation.py", "suit_window.py"), encoding="utf-8").read()
    body = src[src.index("def _on_transcript"):src.index("def _tree_dir")]
    assert 'self.lane.memory = getattr(self.core, "tree", None)' in body
    assert body.index("self.core.heard(text)") < body.index("self.lane.handle(text, state, hist)")

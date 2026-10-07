"""A question about the place gets an answer from what the companions know (J, 2026-10-05).

"Elah should have knowledge of the Galactapedia and Montaigne should have his brochures and dev history to call
on." J's example was a player standing at a Hathor site saying "What's that big tower do?", "wow look at that!"
and "what is this place?". Before this change the first two were not questions at all (the companion was told to
say it had not caught one) and the third read back the log's code with its underscores removed.

Everything here drives the real code against the data that ships: the real route(), the real ConversationLane,
the topic graph and the dev-history pack as they are on disk, the real EventParser and CompanionCore fed real
Game.log lines. Nothing needs a model: an answer about a place is built from its claims.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

import conversation as conv
import location_names as loc
import place_knowledge as pk
from companion_core import CompanionCore

# J's three sentences, as he said them.
TOWER = "What's that big tower do?"
WOW = "wow look at that!"
WHAT_PLACE = "what is this place?"
THREE = (TOWER, WOW, WHAT_PLACE)

VIVERE = {"location": "Vivere OLP", "location_body": "Aberdeen", "location_type": "outpost",
          "location_named": True, "system": "Stanton"}
LORVILLE = {"location": "Lorville", "location_body": "Hurston", "location_type": "city",
            "location_named": True, "system": "Stanton"}
HATHOR_FACTS = (
    "The Hathor Group was an interplanetary mining concern notorious for environmentally catastrophic strip mining.",
    "Hathor sank its last funds into mining Stanton, hit unforeseen trouble, and was formally dissolved.",
    "Each Hathor site pairs an orbital laser platform with a cluster of planetary alignment facilities.",
    "The Hathor sites Attritus and Lamina orbit Daymar; Ruptura and Vivere orbit Aberdeen.",
)


def log_line(code: str, t: str = "2026-10-05T10:00:00.000Z") -> str:
    """The line Star Citizen writes when the pilot opens an inventory somewhere: the only place the log names one."""
    return (f"<{t}> [Notice] <RequestLocationInventory> Player[PILOT] requested inventory for "
            f"Location[{code}] [Team_CoreGameplayFeatures][Inventory]")


QT_ARRIVED = ("<2026-10-05T10:20:00.000Z> [Notice] <Quantum Drive Arrived - Arrived at Final Destination> "
              "[ItemNavigation][CL][26140] | NOT AUTH | KRIG_L21_Wolf_1[1]|CSCItemNavigation::OnQuantumDriveArrived|"
              "Quantum Drive has arrived at final destination [Team_CGP4][QuantumTravel]")


class _Speech:
    muted = False

    def __init__(self):
        self.said = []

    def say(self, text, speaker, priority):
        self.said.append((speaker, text))
        return True

    def pending(self):
        return 0


@pytest.fixture(scope="module")
def knowledge():
    from topic_graph import TopicGraph
    import dev_facts as devf
    return pk.PlaceKnowledge(TopicGraph.load(), lambda: devf.load_pack())


def ask(knowledge, sentence, state, lane=None):
    spec = (lane or conv.ConversationLane(knowledge)).handle(sentence, dict(state), {})
    assert spec is not None, sentence
    return spec


def said(spec) -> str:
    return spec["fixed_text"]


def fact_of(spec) -> str:
    return next((c["value"] for c in spec["claims"] if c["predicate"] == "topic.fact"), "")


_GRAPH = []


def _core():
    """A real CompanionCore with no model. The topic graph takes four seconds to load from disk, so every core
    here is handed the same one (as shipped: the two optional flavour merges are off so it is not added to)."""
    import companion_core as cc
    if not _GRAPH:
        _GRAPH.append(cc.TopicGraph.load())
    real = cc.TopicGraph.load
    cc.TopicGraph.load = classmethod(lambda cls, *a, **k: _GRAPH[0])
    try:
        core = CompanionCore(_Speech(), realizer=None, ambient_every_s=3600,
                             features={"manufacturer_flavour": False, "place_flavour": False})
    finally:
        cc.TopicGraph.load = real
    core._answer_muted = lambda: False
    return core


def _answer(core, sentence) -> str:
    """One transcript through the same calls the window makes, minus the thread: what is SPOKEN, or ""."""
    lane = conv.ConversationLane(core.place_knowledge())
    spec = lane.handle(sentence, core.lane_state(), {})
    before = len(core.speech.said)
    _run_answer(core, spec, sentence)
    return core.speech.said[-1][1] if len(core.speech.said) > before else ""


def _run_answer(core, spec, sentence):
    from speak_gate import Candidate, Priority
    cand = Candidate(priority=Priority.URGENT, speaker=spec["speaker"], text_len_words=spec["length_words"][1],
                     created_at=core.now())
    core._answer_worker(spec, cand, sentence)


# ---------------------------------------------------------------------------------------------------------------
# (b) a comment about the place is a question about the place
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("sentence, topic", [
    (TOWER, "place_about"), (WOW, "place_about"), (WHAT_PLACE, "location"),
    ("What's this place?", "location"), ("where are we", "location"), ("what place is this", "location"),
    ("where the hell are we", "location"), ("what's this outpost", "location"),
    ("what's that", "place_about"), ("what is that", "place_about"), ("what does that big tower do", "place_about"),
    ("what does that thing over there do", "place_about"), ("what is that tower for", "place_about"),
    ("look at that", "place_about"), ("whoa, look at that tower", "place_about"),
    ("what am I looking at", "place_about"), ("what are those", "place_about"), ("wow, that's huge", "place_about"),
])
def test_it_is_a_question_about_the_place(sentence, topic):
    who, intent, slots = conv.route(sentence)
    assert (who, intent, slots.get("topic")) == ("elah", "factual", topic)


def test_the_word_for_the_thing_is_kept_and_no_word_is_an_empty_one():
    assert conv.route(TOWER)[2]["referent"] == "big tower"
    assert conv.route("look at that tower")[2]["referent"] == "tower"
    assert conv.route(WOW)[2]["referent"] == "" and conv.route("what's that")[2]["referent"] == ""
    assert "referent" not in conv.route(WHAT_PLACE)[2]


@pytest.mark.parametrize("sentence, intent, topic", [
    ("what's this ship", "factual", "ship"), ("what's my objective", "factual", "mission"),
    ("what does that mean", "unknown", None), ("what's up", "unknown", None), ("thanks", "social", "thanks"),
    ("open the cargo doors", "action", None), ("what do you think of this place", "opinion", None),
    ("have we been here before", "memory", "been_here"), ("am i in armistice", "factual", "armistice"),
])
def test_other_questions_keep_their_own_answers(sentence, intent, topic):
    _, got, slots = conv.route(sentence)
    assert (got, slots.get("topic")) == (intent, topic)


def test_a_named_companion_still_answers_a_place_question():
    assert conv.route("Montaigne, what's that big tower do?")[0] == "montaigne"
    assert conv.route("what is this place, Montaigne")[0] == "montaigne"
    assert conv.route(TOWER)[0] == "elah"


# ---------------------------------------------------------------------------------------------------------------
# (a) knowledge on the question path: J's three sentences at a Hathor site
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("sentence", THREE)
def test_elah_at_a_hathor_site_answers_with_the_place_and_one_sourced_hathor_fact(knowledge, sentence):
    spec = ask(knowledge, sentence, VIVERE)
    assert spec["speaker"] == "elah"
    assert fact_of(spec) in HATHOR_FACTS
    assert spec["place"]["knowledge"]["source"] == "https://starcitizen.tools/Hathor_Group"
    line = said(spec)
    assert "Vivere OLP, on Aberdeen" in line and fact_of(spec) in line
    assert "Outpost OLP Stanton1b Vivere" not in line           # the log's code, which is what she used to read out
    assert conv.ground_direct(spec, line) == []


def test_the_tower_gets_what_is_at_the_site_and_an_admission_never_a_purpose(knowledge):
    spec = ask(knowledge, TOWER, VIVERE)
    line = said(spec)
    assert line.startswith("I can't tell which big tower you mean from here.")
    # what is physically at a Hathor site comes before the company's history
    assert fact_of(spec) == HATHOR_FACTS[2]
    claims = {c["predicate"]: (c["kind"], c["value"]) for c in spec["claims"]}
    assert claims["pilot.referent"] == ("UNKNOWN", "unknown") and claims["pilot.asked_about"][1] == "big tower"
    assert "cannot tell which structure" in spec["interpretation"]["text"]


def test_wow_look_at_that_is_answered_about_the_site(knowledge):
    spec = ask(knowledge, WOW, VIVERE)
    line = said(spec)
    assert pk.NOT_KNOWING.search(line.lower()) and "Vivere OLP" in line and fact_of(spec) in HATHOR_FACTS
    assert "did not catch" not in spec["interpretation"]["text"]


def test_asking_again_gives_the_next_fact_not_the_same_one(knowledge):
    lane = conv.ConversationLane(knowledge)
    facts = [fact_of(ask(knowledge, WHAT_PLACE, VIVERE, lane)) for _ in range(4)]
    assert sorted(facts) == sorted(HATHOR_FACTS)


def test_elah_draws_on_lore_and_montaigne_on_brochures_and_dev_history(knowledge):
    elah = knowledge.facts("elah", LORVILLE)
    mont = knowledge.facts("montaigne", LORVILLE)
    assert elah and {f["kind"] for f in elah} <= {"lore", "amenity"}
    assert mont and {f["kind"] for f in mont} == {"brochure", "dev"}
    assert not {f["text"] for f in elah} & {f["text"] for f in mont}
    assert all(f["source"] for f in elah)                         # every lore fact carries where it came from


def test_montaigne_quotes_his_brochure_and_says_it_is_the_brochure(knowledge):
    spec = ask(knowledge, "Montaigne, what is this place?", LORVILLE)
    line = said(spec)
    assert spec["speaker"] == "montaigne" and spec["place"]["knowledge"]["kind"] == "brochure"
    assert line.startswith("The suit's feed says this is Lorville, on Hurston. The brochure says this: ")
    assert fact_of(spec) in line and conv.ground_direct(spec, line) == []


def test_a_brochure_for_the_moon_is_never_passed_off_as_the_sites(knowledge):
    # Vivere OLP's own brochure is filler and carries nothing; Aberdeen's is about Aberdeen and has to say so.
    spec = ask(knowledge, "Montaigne, what is this place?", VIVERE)
    assert "The brochure for Aberdeen says this: " in said(spec)


def test_montaigne_reaches_his_dev_history_within_three_asks(knowledge):
    lane = conv.ConversationLane(knowledge)
    specs = [ask(knowledge, "Montaigne, what is this place?", LORVILLE, lane) for _ in range(3)]
    dev = [s for s in specs if s.get("aside") == "dev_fact"]
    assert len(dev) == 1
    line = said(dev[0])
    assert line.startswith("The suit's feed says this is Lorville, on Hurston. Fun fact from the dev history")
    assert any(c["predicate"] == "devfact.excerpt" and c["value"] for c in dev[0]["claims"])
    assert conv.ground_direct(dev[0], line) == []


def test_elah_never_says_a_dev_fact_or_brochure_copy(knowledge):
    lane = conv.ConversationLane(knowledge)
    for _ in range(12):
        spec = ask(knowledge, WHAT_PLACE, LORVILLE, lane)
        assert spec.get("aside") != "dev_fact" and "brochure" not in said(spec).lower()
        assert (spec["place"]["knowledge"] or {}).get("kind") in ("lore", "amenity")


def test_a_new_source_is_one_more_function_in_the_list(knowledge):
    def another_source(q):
        return [{"text": "Vivere is a test entry.", "names": ["Vivere"], "source": "test:source",
                 "status": "lore", "node": "g:vivere", "fact": 0, "title": "Vivere OLP", "kind": "lore"}]
    k = pk.PlaceKnowledge(knowledge.graph, None, {"elah": [another_source]})
    spec = ask(k, WHAT_PLACE, VIVERE)
    assert fact_of(spec) == "Vivere is a test entry." and conv.ground_direct(spec, said(spec)) == []


# ---------------------------------------------------------------------------------------------------------------
# (a2) the Galactapedia: a local pack of sentences from the articles, each with its source
# ---------------------------------------------------------------------------------------------------------------
RSI_LORVILLE = "https://robertsspaceindustries.com/galactapedia/article/Rw1ZlJNE36-lorville"
G_ENTRY = {"text": "Lorville is a city on Hurston.", "excerpt": "Lorville is a city on Hurston (Stanton I).",
           "source": RSI_LORVILLE, "article": "Lorville", "retrieved": "2026-10-07", "names": ["Lorville", "Hurston"]}


@pytest.fixture
def pack_file(tmp_path, monkeypatch):
    """Point the Galactapedia source at a pack of the test's own, in a temp folder: pack_file(content) -> path.
    content None = no file at all; a str is written as it is; anything else as JSON."""
    count = []

    def use(content):
        p = tmp_path / f"pack{len(count)}.json"              # a new name each time: a pack file is read once
        count.append(p)
        if content is not None:
            p.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
        monkeypatch.setattr(pk, "GALACTAPEDIA_PATH", p)
        return p
    return use


def test_the_galactapedia_is_elahs_and_the_shipped_pack_passes_its_own_check():
    assert pk.galactapedia_source in pk.SOURCES["elah"] and pk.galactapedia_source not in pk.SOURCES["montaigne"]
    data = Path(pk.__file__).resolve().parent.parent / "data"
    assert pk.GALACTAPEDIA_PATH == data / "galactapedia_pack.json"
    entries = json.loads(pk.GALACTAPEDIA_PATH.read_text(encoding="utf-8"))["entries"]
    node_ids = {n["id"] for n in json.loads((data / "topics_lore.json").read_text(encoding="utf-8"))["nodes"]}
    assert len(entries) >= 25                                  # the build found an article for more than this
    for nid, facts in entries.items():
        assert 1 <= len(facts) <= pk.GALACTAPEDIA_MAX_FACTS, nid
        for e in facts:
            assert pk.galactapedia_problems(e, nid, node_ids) == [], (nid, e["text"])
            assert e["source"].startswith("https://robertsspaceindustries.com/galactapedia/article/")
            assert len(e["excerpt"]) <= pk.GALACTAPEDIA_EXCERPT_CHARS       # a sentence or two, never an article
    assert pk.GALACTAPEDIA_PATH.stat().st_size < 100_000
    # ...and all of it is read back: nothing that ships is dropped when the file is loaded
    assert {k: len(v) for k, v in pk.galactapedia_pack().items()} == {k: len(v) for k, v in entries.items()}


def test_elah_answers_about_lorville_from_the_galactapedia_on_the_third_ask(knowledge):
    """The shipped pack on the question path. Lore leads two at a time; the Galactapedia has every third turn."""
    lane = conv.ConversationLane(knowledge)
    specs = [ask(knowledge, WHAT_PLACE, LORVILLE, lane) for _ in range(3)]
    sources = [s["place"]["knowledge"]["source"] for s in specs]
    assert [bool(pk.GALACTAPEDIA_URL.match(s)) for s in sources] == [False, False, True]
    spec = specs[2]
    line = said(spec)
    assert sources[2] == RSI_LORVILLE and spec["speaker"] == "elah" and spec["place"]["knowledge"]["kind"] == "lore"
    shipped = [e["text"] for e in pk.galactapedia_pack()["lorville"]]
    assert fact_of(spec) in shipped
    assert line == "Lorville, on Hurston. " + fact_of(spec)       # the third ask's wording of where we are
    assert pk.place_problems(spec, line) == [] and conv.ground_direct(spec, line) == []
    assert len({fact_of(s) for s in specs}) == 3               # three asks, three different facts


def test_every_shipped_galactapedia_fact_can_be_said(knowledge):
    for nid, entries in pk.galactapedia_pack().items():
        title = knowledge.graph.nodes[nid]["title"]
        for e in entries:
            fact = {"text": e["text"], "names": e["names"], "source": e["source"], "status": "lore", "node": nid,
                    "fact": e["index"], "title": title, "kind": "lore"}
            spec = conv.place_spec(conv.route(WHAT_PLACE), {"location": title, "location_named": True}, 0, fact)
            assert e["text"] in said(spec) and conv.ground_direct(spec, said(spec)) == [], (nid, said(spec))


def test_a_pack_gives_a_place_its_facts_and_a_place_without_an_entry_nothing(knowledge, pack_file):
    pack_file({"entries": {"lorville": [G_ENTRY]}})
    got = pk.galactapedia_source(pk.Query(knowledge.graph, LORVILLE))
    assert [{k: f[k] for k in ("text", "names", "source", "status", "node", "fact", "title", "kind")} for f in got] == [
        {"text": "Lorville is a city on Hurston.", "names": ["Lorville", "Hurston"], "source": RSI_LORVILLE,
         "status": "lore", "node": "lorville", "fact": 0, "title": "Lorville", "kind": "lore"}]
    assert pk.galactapedia_source(pk.Query(knowledge.graph, VIVERE)) == []
    # named in the sentence, from somewhere else
    named = pk.galactapedia_source(pk.Query(knowledge.graph, VIVERE, "tell me about Lorville"))
    assert [f["node"] for f in named] == ["lorville"]
    # and the answer it makes is one the gate lets through
    k = pk.PlaceKnowledge(knowledge.graph, None, {"elah": [pk.galactapedia_source]})
    spec = ask(k, WHAT_PLACE, LORVILLE)
    assert said(spec) == "This is Lorville, on Hurston. Lorville is a city on Hurston."
    assert spec["place"]["knowledge"]["source"] == RSI_LORVILLE and conv.ground_direct(spec, said(spec)) == []


@pytest.mark.parametrize("content", [
    None,                                                       # no file
    "", "{ this is not json", "[]",
    {"version": 1},                                             # no entries
    {"entries": [G_ENTRY]}, {"entries": {"lorville": 7}}, {"entries": {"lorville": ["Lorville is a city."]}},
    {"entries": {"lorville": [{"text": 5, "excerpt": None}]}},
], ids=["missing", "empty", "not json", "a list", "no entries", "entries a list", "facts not a list",
        "entry not a dict", "wrong types"])
def test_a_missing_or_malformed_pack_is_an_empty_source_and_never_an_error(knowledge, pack_file, content):
    pack_file(content)
    assert pk.galactapedia_source(pk.Query(knowledge.graph, LORVILLE)) == []
    # the question is still answered, from the lore, exactly as it was before there was a pack
    spec = ask(knowledge, WHAT_PLACE, LORVILLE)
    assert spec["place"]["knowledge"]["source"].startswith("https://starcitizen.tools/")
    assert conv.ground_direct(spec, said(spec)) == []


def test_a_pack_entry_that_fails_the_check_is_never_said(knowledge, pack_file):
    invented = dict(G_ENTRY, text="Lorville is a city of 4 gates on Hurston.")
    pack_file({"entries": {"lorville": [invented, G_ENTRY]}})
    got = pk.galactapedia_source(pk.Query(knowledge.graph, LORVILLE))
    assert [(f["text"], f["fact"]) for f in got] == [("Lorville is a city on Hurston.", 1)]


def test_the_galactapedia_does_not_repeat_a_lore_fact(knowledge, pack_file):
    lore = pk.lore_source(pk.Query(knowledge.graph, LORVILLE))[0]["text"]
    again = dict(G_ENTRY, text=lore.upper().rstrip("."), excerpt=lore.upper(), names=[])
    assert pk.galactapedia_problems(again) == []                # refused for being a repeat, not for being bad
    pack_file({"entries": {"lorville": [again, G_ENTRY]}})
    got = pk.galactapedia_source(pk.Query(knowledge.graph, LORVILLE))
    assert [f["text"] for f in got] == ["Lorville is a city on Hurston."]
    texts = [pk._plain(f["text"]) for f in knowledge.facts("elah", LORVILLE)]
    assert len(texts) == len(set(texts))


@pytest.mark.parametrize("change, why", [
    ({"text": "Lorville is a city of 4 gates on Hurston."}, "words not in the excerpt"),          # a number
    ({"text": "Lorville is a city on Hurston Prime."}, "words not in the excerpt"),               # a name
    ({"text": "Lorville is a polluted city on Hurston."}, "words not in the excerpt"),            # a content word
    ({"text": "Hurston is a city on Lorville."}, "not in its order"),                             # turned round
    ({"excerpt": "Lorville is not a city on Hurston."}, "negation"),
    ({"source": "https://starcitizen.tools/Lorville"}, "source is not a Galactapedia article"),
    ({"source": "https://robertsspaceindustries.com.example.org/galactapedia/article/Rw1ZlJNE36-lorville"},
     "source is not a Galactapedia article"),
    ({"source": ""}, "source is not a Galactapedia article"),
    ({"excerpt": G_ENTRY["excerpt"] + " More of the article." * 30}, "more than a sentence or two"),
    ({"text": 'Lorville is a "city" on Hurston.'}, "quotation marks"),
    ({"text": "Lorville is a city."}, "length"),
    ({"names": ["Lorville", "Teasa"]}, "names that the text does not use"),
    ({"retrieved": "yesterday"}, "no retrieved date"),
    ({"article": ""}, "no article title"),
])
def test_the_pack_check_refuses_what_the_article_does_not_say(change, why):
    assert pk.galactapedia_problems(G_ENTRY) == []
    probs = pk.galactapedia_problems(dict(G_ENTRY, **change))
    assert any(why in p for p in probs), probs


def test_the_pack_check_refuses_a_node_the_lore_does_not_have():
    assert pk.galactapedia_problems(G_ENTRY, "lorville", {"lorville"}) == []
    assert pk.galactapedia_problems(G_ENTRY, "lorvile", {"lorville"}) == ["node 'lorvile' is not in topics_lore.json"]


# -- the offline build tool (tools/build_galactapedia_pack.py), with no network ------------------------------------
@pytest.fixture(scope="module")
def builder():
    path = Path(pk.__file__).resolve().parents[3] / "tools" / "build_galactapedia_pack.py"
    spec = importlib.util.spec_from_file_location("build_galactapedia_pack", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _no_network(monkeypatch, builder):
    def refuse(*a, **k):
        raise AssertionError("the build tool went to the network")
    monkeypatch.setattr(builder.urllib.request, "urlopen", refuse)
    monkeypatch.setattr(builder.time, "sleep", lambda s: None)


ARTICLE = {"fetched": "2026-10-07", "url": "test", "data": {
    "title": "Testville", "rsi_url": "/galactapedia/article/Abc123-testville", "translations": {"en_EN": (
        "# TESTVILLE\n\n"
        "Testville is a city on [Hurston (Stanton I)](https://robertsspaceindustries.com/galactapedia/article/x-y) "
        "that was built by **Gavin E. Hurston** in 2912 for the workers. "
        "It has forty gates and a very long wall around all of them. "
        "The mayor called Testville \"the jewel of the dust\" in a speech to the workers there. "
        "Many traders visit Testville for the weekly market in the old square.\n\n"
        "## History\n\n"
        "Testville is a city on Hurston that was built in 2912 for the workers of the company. "
        "Nobody knows who first settled the valley, or why they chose it over the coast.")}}}


def test_the_builder_takes_whole_sentences_and_leaves_out_what_cannot_stand_alone(builder):
    node = {"id": "testville", "title": "Testville", "facts": [
        {"text": "Traders visit the weekly market in the old square of Testville."}]}
    log = []
    taken = builder.pick(node, ARTICLE, log.append)
    assert [e["text"] for e in taken] == [
        "Testville is a city on Hurston that was built by Gavin E. Hurston in 2912 for the workers."]
    e = taken[0]
    # the excerpt is the article's sentence with only its link and emphasis marks gone; the text only loses the aside
    assert e["excerpt"] == ("Testville is a city on Hurston (Stanton I) that was built by Gavin E. Hurston in 2912 "
                            "for the workers.")
    assert e["source"] == "https://robertsspaceindustries.com/galactapedia/article/Abc123-testville"
    assert e["article"] == "Testville" and e["retrieved"] == "2026-10-07"
    assert e["names"] == ["Testville", "Hurston", "Gavin E. Hurston"]
    assert pk.galactapedia_problems(e) == []
    why = " | ".join(log)
    assert "points back at the sentence before it" in why           # "It has forty gates ..."
    assert "quotation marks" in why                                 # the mayor's speech
    assert "says what a lore fact of this node already says" in why   # the weekly market
    assert "says what a sentence already taken says" in why         # the History section's repeat
    assert "does not name its subject" in why                       # "Nobody knows who ..."


def test_the_builder_reads_its_cache_and_offline_never_touches_the_network(builder, tmp_path, monkeypatch):
    _no_network(monkeypatch, builder)
    fetcher = builder.Fetcher(tmp_path, offline=True, log=lambda s: None)
    assert fetcher.article("Abc123") is None                        # not cached, offline: skipped, not fetched
    (tmp_path / "article_Abc123.json").write_text(json.dumps(ARTICLE), encoding="utf-8")
    assert fetcher.article("Abc123") == ARTICLE
    assert builder.Fetcher(tmp_path, offline=False, log=lambda s: None).article("Abc123") == ARTICLE   # cached: no request


def test_the_builder_fetches_one_article_politely_and_caches_it(builder, tmp_path, monkeypatch):
    seen, slept = [], []

    class _Reply:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"data": ARTICLE["data"]}).encode("utf-8")

    def fake_urlopen(req, timeout=None):
        seen.append((req.full_url, req.get_header("User-agent"), timeout))
        return _Reply()
    monkeypatch.setattr(builder.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(builder.time, "sleep", slept.append)
    fetcher = builder.Fetcher(tmp_path, log=lambda s: None)
    rec = fetcher.article("Abc123")
    assert rec["data"]["title"] == "Testville" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", rec["fetched"])
    assert seen == [("https://api.star-citizen.wiki/api/v2/galactapedia/Abc123", builder.USER_AGENT, builder.TIMEOUT_S)]
    assert "SC-Toolbox" in builder.USER_AGENT and "http" in builder.USER_AGENT        # says who is asking
    assert (tmp_path / "article_Abc123.json").exists()
    fetcher.article("Abc123")
    assert len(seen) == 1                                           # the second ask is answered from the cache
    fetcher.article("Def456")
    assert len(seen) == 2 and slept and 0 < slept[-1] <= builder.PAUSE_S   # a pause before the next request


def test_the_build_skips_what_it_cannot_read_and_refuses_the_wrong_article(builder, tmp_path, monkeypatch):
    _no_network(monkeypatch, builder)
    log = []
    fetcher = builder.Fetcher(tmp_path, offline=True, log=log.append)
    pack = builder.build(fetcher, log.append)                       # nothing cached, offline: an empty pack, no error
    assert pack["entries"] == {} and builder.check(pack) == ["the pack has no entries"]
    # The id written down for Lorville now answers with another article: not used, and the log says so.
    (tmp_path / f"article_{builder.ARTICLES['lorville'][0]}.json").write_text(json.dumps(ARTICLE), encoding="utf-8")
    del log[:]
    pack = builder.build(fetcher, log.append)
    assert pack["entries"] == {}
    assert any(line.startswith("lorville: NO ENTRY") and "'Testville', not 'Lorville'" in line for line in log)
    # A lore node nobody has decided about stops the build.
    monkeypatch.setattr(builder, "NO_ARTICLE", {k: v for k, v in builder.NO_ARTICLE.items() if k != "orbituary"})
    with pytest.raises(SystemExit, match="orbituary"):
        builder.build(fetcher, log.append)


def test_the_builder_has_decided_about_every_lore_node_and_writes_outside_the_repository(builder):
    ids = {n["id"] for n in builder.load_nodes()}
    assert ids == set(builder.ARTICLES) | set(builder.NO_ARTICLE)
    assert not set(builder.ARTICLES) & set(builder.NO_ARTICLE)
    assert builder.PROJECT_ROOT not in builder.CACHE_DIR.resolve().parents
    shipped = json.loads(pk.GALACTAPEDIA_PATH.read_text(encoding="utf-8"))
    assert builder.check(shipped) == []
    assert set(shipped["entries"]) <= set(builder.ARTICLES)
    for nid, facts in shipped["entries"].items():                   # each fact is from the article written down for it
        assert all(f["source"].startswith(f"{builder.RSI}/galactapedia/article/{builder.ARTICLES[nid][0]}-")
                   and f["article"] == builder.ARTICLES[nid][1] for f in facts), nid


def test_nothing_on_this_path_fetches_anything():
    src = open(pk.__file__, encoding="utf-8").read()
    assert not re.search(r"^\s*(?:import|from)\s+(?:urllib|requests|http|socket)\b", src, re.M)


def test_without_a_knowledge_source_the_place_is_still_named():
    spec = conv.ConversationLane().handle(WHAT_PLACE, dict(VIVERE), {})
    assert said(spec) == "This is Vivere OLP, on Aberdeen. That is all I know about it."
    assert conv.ground_direct(spec, said(spec)) == []


# ---------------------------------------------------------------------------------------------------------------
# the invention gap: an answer about a place may only say what is in its claims
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("line, why", [
    ("I can't tell which one you mean. This is Vivere OLP, on Aberdeen. That big tower is the comms relay.",
     "says more than its claims"),
    ("I can't tell which one you mean. This is Vivere OLP, on Aberdeen. It powers the mining laser for the site.",
     "says more than its claims"),
    ("I can't tell which one you mean. This is Vivere OLP, on Aberdeen. It was abandoned after a reactor fire.",
     "says more than its claims"),
    ("This is Vivere OLP, on Aberdeen. The big tower pairs an orbital laser platform with a cluster of facilities.",
     "says something about the thing it cannot see"),
    ("This is Vivere OLP, on Aberdeen. Each Hathor site pairs an orbital laser platform with a cluster of planetary "
     "alignment facilities.", "does not say it cannot tell which thing is meant"),
    ("I can't tell which one you mean. Each Hathor site pairs an orbital laser platform with a cluster of planetary "
     "alignment facilities.", "does not name the place"),
    ("I can't tell which one you mean. This is Vivere OLP, on Aberdeen, near Lorville.", "unauthorized names"),
    ("I can't tell which one you mean. This is Vivere OLP, on Aberdeen. It has four platforms.",
     "unauthorized numbers"),
])
def test_an_invented_place_fact_is_refused(knowledge, line, why):
    spec = ask(knowledge, TOWER, VIVERE)
    fails = conv.ground_direct(spec, line)
    assert any(why in f for f in fails), fails


def test_the_real_answer_and_a_plain_rewording_of_it_pass(knowledge):
    spec = ask(knowledge, TOWER, VIVERE)
    assert conv.ground_direct(spec, said(spec)) == []
    reworded = ("I cannot see which big tower you are asking about. We are at Vivere OLP, on Aberdeen. Each Hathor "
                "site pairs an orbital laser platform with a cluster of planetary alignment facilities.")
    assert conv.ground_direct(dict(spec, length_words=spec["model_length_words"]), reworded) == []


def test_what_the_gate_cannot_catch_is_written_down_and_true(knowledge):
    """The limit, pinned so nobody reads the gate as more than it is: a false sentence made ONLY of words that are
    in the claims passes. That is why the answer is built from the claims and not worded by the model."""
    spec = ask(knowledge, TOWER, VIVERE)
    false_but_made_of_claim_words = ("I can't tell which one you mean. This is Vivere OLP, on Aberdeen. Each "
                                     "orbital laser platform pairs Aberdeen with a cluster of Hathor facilities.")
    assert conv.ground_direct(dict(spec, length_words=spec["model_length_words"]), false_but_made_of_claim_words) == []
    assert "WHAT IT CANNOT CATCH" in pk.place_problems.__doc__


# ---------------------------------------------------------------------------------------------------------------
# (c) the place-name table
# ---------------------------------------------------------------------------------------------------------------
HATHOR_CODES = [(f"Outpost_OLP_{body}_{site}", f"{site} OLP", moon)
                for body, moon, sites in (("Stanton1b", "Aberdeen", ("Ruptura", "Vivere")),
                                          ("Stanton2b", "Daymar", ("Attritus", "Lamina")))
                for site in sites] + [
    (f"Outpost_PAF_{body}_{site}_{n}", f"{site} PAF-{roman}", moon)
    for body, moon, sites in (("Stanton1b", "Aberdeen", ("Ruptura", "Vivere")),
                              ("Stanton2b", "Daymar", ("Attritus", "Lamina")))
    for site in sites for n, roman in ((1, "I"), (2, "II"), (3, "III"))]


@pytest.mark.parametrize("code, name, moon", HATHOR_CODES)
def test_every_hathor_site_has_its_name_system_type_and_moon(code, name, moon):
    assert len(HATHOR_CODES) == 16
    place = loc.resolve(code)
    assert place is not None and loc.is_named(code)
    assert (place.name, place.system, place.type, place.body) == (name, "Stanton", "outpost", moon)
    assert loc.get_location_name(code) == name and loc.get_location_system(code) == "Stanton"


@pytest.mark.parametrize("code, name, kind, body", [
    # every one of these is a code from J's own Game.logs
    ("Outpost_OLP_Stanton1b_Vivere", "Vivere OLP", "outpost", "Aberdeen"),
    ("Outpost_PAF_Stanton1b_Ruptura_3", "Ruptura PAF-III", "outpost", "Aberdeen"),
    ("Stanton2_Orison", "Orison", "city", "Crusader"),            # was read out as "2 Orison"
    ("Stanton3_Area18", "Area 18", "city", "ArcCorp"),            # was read out as "3 Area18"
    ("Stanton1_Lorville", "Lorville", "city", "Hurston"),
    ("RR_HUR_LEO", "Everus Harbor", "station", "Hurston"),
    ("Stanton2a_RayariHydro_HickesResearch", "Hickes Research Outpost", "outpost", "Cellin"),
    ("Stanton1_HurdynMining_HDMSEdmond", "HDMS-Edmond", "outpost", "Hurston"),      # was typed "moon"
    ("Stanton2c_DrugLab_Jumptown", "Jumptown", "outpost", "Yela"),
    ("Pyro3_Outpost_col_m_frm_indy_001", "Shepherd's Rest", "outpost", "Bloom"),
    ("PrisonMine_Stanton1b", "Klescher Rehabilitation Facility", "outpost", "Aberdeen"),
    ("RR_P6_LEO", "Ruin Station", "station", "Terminus"),
])
def test_codes_from_his_own_logs_get_their_names(code, name, kind, body):
    place = loc.resolve(code)
    assert place is not None and (place.name, place.type, place.body) == (name, kind, body)


def test_the_hand_written_table_still_wins_where_it_has_a_name():
    assert loc.get_location_name("RR_HUR_L1") == "HUR-L1"                 # the catalogue's is longer
    assert loc.get_location_name("RR_JP_StantonPyro") == "Stanton-Pyro Jump Point"
    assert loc.get_location_type("RR_JP_StantonPyro") == "station"        # as it always was


def test_the_moons_of_crusader_and_arccorp_are_the_right_way_round():
    assert [loc.get_location_name(c) for c in ("Stanton2a", "Stanton2b", "Stanton2c")] == ["Cellin", "Daymar", "Yela"]
    assert [loc.get_location_name(c) for c in ("Stanton3a", "Stanton3b")] == ["Lyria", "Wala"]
    assert loc.get_location_type("Stanton1b") == "moon" and loc.get_location_type("Stanton1") == "planet"


def test_a_code_nothing_names_is_not_called_a_name():
    code = "Stanton1a_ASD_Delve_Facility_001"
    assert loc.resolve(code) is None and not loc.is_named(code)
    assert loc.get_location_name(code) == "1a ASD Delve Facility 001"      # the old fallback, unchanged
    assert loc.get_location_body(code) == "Arial"                          # the code itself says which moon


def test_a_missing_catalogue_changes_nothing_that_worked_before(tmp_path):
    try:
        loc.load_catalogue(tmp_path / "no_such_file.jsonl")
        assert loc.get_location_name("Stanton1_Lorville") == "Lorville"
        assert loc.get_location_name("Outpost_OLP_Stanton1b_Vivere") == "Outpost OLP Stanton1b Vivere"
        assert loc.get_location_type("RR_HUR_LEO") == "station"
    finally:
        loc.load_catalogue()


# ---------------------------------------------------------------------------------------------------------------
# name known, name unknown, and the place the pilot has left: through the real parser and the real core
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("sentence", THREE)
def test_name_known_the_three_sentences_are_spoken_from_a_real_log_line(sentence):
    core = _core()
    core.feed_line(log_line("Outpost_OLP_Stanton1b_Vivere"))
    st = core.lane_state()
    assert (st["location"], st["location_body"], st["location_type"], st["location_named"], st["system"]) == \
        ("Vivere OLP", "Aberdeen", "outpost", True, "Stanton")
    line = _answer(core, sentence)
    assert "Vivere OLP, on Aberdeen" in line and "Hathor" in line
    assert core.speech.said[-1][0] == "elah"


def test_a_place_answer_needs_no_model(knowledge):
    core = _core()
    assert core.realizer is None
    core.feed_line(log_line("Outpost_OLP_Stanton1b_Vivere"))
    assert _answer(core, TOWER).startswith("I can't tell which big tower you mean from here.")


@pytest.mark.parametrize("sentence", THREE)
def test_name_unknown_she_says_it_is_the_logs_label_and_names_the_moon(sentence):
    core = _core()
    core.feed_line(log_line("Stanton1a_ASD_Delve_Facility_001"))
    st = core.lane_state()
    assert st["location_named"] is False and st["location_body"] == "Arial"
    line = _answer(core, sentence)
    assert "1a ASD Delve Facility 001, on Arial" in line and "label" in line
    assert "This is 1a ASD" not in line


@pytest.mark.parametrize("sentence", THREE)
def test_no_place_at_all_is_said_as_such_and_nothing_is_described(knowledge, sentence):
    spec = ask(knowledge, sentence, {})
    assert "where we are" in said(spec) and "guess" in said(spec)
    assert fact_of(spec) == "" and spec["place"]["knowledge"] is None
    assert conv.ground_direct(spec, said(spec)) == []
    assert any("unauthorized names" in f for f in conv.ground_direct(spec, "You're in Lorville, pilot, as always."))


def test_stale_leaving_a_city_for_a_hathor_site_inside_half_an_hour():
    """The bug: Lorville is a city, so for 30 minutes any place of unknown type was "a part of Lorville" and the
    name was not replaced. The pilot stood at a Hathor site and was told Lorville."""
    core = _core()
    core.feed_line(log_line("Stanton1_Lorville", "2026-10-05T10:00:00.000Z"))
    assert core.lane_state()["location"] == "Lorville"
    core.feed_line(log_line("Outpost_OLP_Stanton1b_Vivere", "2026-10-05T10:12:00.000Z"))
    assert core.lane_state()["location"] == "Vivere OLP"
    line = _answer(core, WHAT_PLACE)
    assert "Vivere OLP" in line and "Lorville" not in line


@pytest.mark.parametrize("first, then, name", [
    ("Stanton1_Lorville", "Stanton1a_ASD_Delve_Facility_001", "1a ASD Delve Facility 001"),   # a code nothing names
    ("RR_HUR_LEO", "RR_CRU_LEO", "Seraphim Station"),                                          # station to station
    ("RR_JP_NyxCastra", "RR_JP_StantonMagnus", "Stanton-Nyx Jump Point"),      # 70 times in his logs, the commonest
])
def test_stale_any_other_place_replaces_the_one_before_it(first, then, name):
    core = _core()
    core.feed_line(log_line(first))
    core.feed_line(log_line(then, "2026-10-05T10:05:00.000Z"))
    assert core.lane_state()["location"] == name


def test_a_part_of_the_place_is_still_not_an_arrival():
    from volatile_context import VolatileContext, is_sub_zone
    assert is_sub_zone("Stanton1_Lorville", "Stanton1_Lorville_CBD")
    assert not is_sub_zone("Stanton1_Lorville", "Outpost_OLP_Stanton1b_Vivere")
    assert not is_sub_zone("RR_HUR_LEO", "RR_HUR_L1") and not is_sub_zone("", "Stanton1_Lorville")
    v = VolatileContext()
    assert v.update_location("Lorville", "city", "urban", "Stanton1_Lorville") == "new_location"
    assert v.update_location("Lorville CBD", "unknown", "urban", "Stanton1_Lorville_CBD") == "zone_change"
    assert v.update_location("Vivere OLP", "outpost", "wild", "Outpost_OLP_Stanton1b_Vivere") == "new_location"


ARMISTICE_OUT = type("E", (), {"event_type": "armistice_zone", "data": {"action": "exited"}})
ARMISTICE_IN = type("E", (), {"event_type": "armistice_zone", "data": {"action": "entered"}})


@pytest.mark.parametrize("sentence", THREE + ("where are we",))
def test_stale_after_leaving_the_answer_says_left_and_never_this_is(sentence):
    core = _core()
    core.feed_line(log_line("Stanton1_Lorville"))
    core.on_event(ARMISTICE_OUT())
    st = core.lane_state()
    assert "location" not in st and st["departed_from"] == "Lorville"
    line = _answer(core, sentence)
    assert "Lorville" in line and pk.DEPARTED.search(line.lower())
    assert "This is Lorville" not in line and "We're at Lorville" not in line and "brochure" not in line.lower()


def test_stale_the_gate_refuses_the_place_that_was_left_as_where_we_are(knowledge):
    spec = ask(knowledge, "where are we", {"departed_from": "Lorville"})
    assert conv.ground_direct(spec, said(spec)) == []
    for wrong in ("This is Lorville, on Hurston. That is all I know about it.",
                  "I have no name for it. You are standing in Lorville now, pilot.",
                  "I have no name for where we are now, and I will not guess."):
        assert conv.ground_direct(dict(spec, length_words=[5, 40]), wrong), wrong
    assert fact_of(spec) == ""                                     # nothing is described about a place we are not in


def test_stale_a_quantum_jump_ends_the_last_named_place_until_the_log_names_another():
    core = _core()
    core.feed_line(log_line("Outpost_OLP_Stanton1b_Vivere"))      # no armistice zone at a Hathor site to walk out of
    core.feed_line(QT_ARRIVED)
    assert core.lane_state().get("departed_from") == "Vivere OLP"
    core.on_event(ARMISTICE_IN())                                   # another place's zone: not "back at Vivere"
    assert core.lane_state().get("departed_from") == "Vivere OLP"
    core.feed_line(log_line("RR_HUR_LEO", "2026-10-05T10:30:00.000Z"))
    st = core.lane_state()
    assert st["location"] == "Everus Harbor" and "departed_from" not in st
    core.on_event(ARMISTICE_OUT())                                  # and the new place is left and returned to
    assert core.lane_state().get("departed_from") == "Everus Harbor"    # like any other, the jump forgotten
    core.on_event(ARMISTICE_IN())
    assert core.lane_state()["location"] == "Everus Harbor"


def test_walking_back_into_the_zone_we_left_is_being_there_again():
    core = _core()
    core.feed_line(log_line("Stanton1_Lorville"))
    core.on_event(ARMISTICE_OUT())
    core.on_event(ARMISTICE_IN())
    assert core.lane_state()["location"] == "Lorville"


def test_the_window_takes_the_place_that_was_left_out_and_asks_for_the_knowledge():
    """The two lines in ui/suit_window.py that put all of the above on the talk key's path. (They are driven for
    real, with a held key and a stand-in core, by tools/Assistant/tests/test_push_to_talk.py.)"""
    src = open(conv.__file__.replace("core", "ui").replace("conversation.py", "suit_window.py"),
               encoding="utf-8").read()
    body = src[src.index("def _on_transcript"):src.index("def _export_memory")]
    assert re.search(r"state = without_departed\(lane_state_from_core\(self\.core\.state, self\.core\.volatile\),\s*"
                     r"getattr\(self\.core, \"_departed\", None\)\)", body)
    assert "self.lane.knowledge = know()" in body and 'know = getattr(self.core, "place_knowledge", None)' in body

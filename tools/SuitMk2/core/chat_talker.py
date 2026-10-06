"""chat_talker.py - FREE TALK, worded by a model (J, 2026-10-05). OFF unless the settings turn it on.

J's decision: ordinary talk (a remark, a feeling, a greeting, a question about the companion itself) is worded by
gemma3:4b from the RULE-LIST prompt, chat_contract.serialize(..., fmt="gemma"): the character's persona first, then
the rules, then three examples, then the conversation. The measurements behind that choice are in
elah-audio/_suit_chat_eval.md, sections 13 to 17; on gemma the rule list was the best prompt for both characters
every time it was measured. The per-kind example prompt (serialize_kind) is NOT used here.

What this module does with one sentence, and nothing else does:

  1. IS IT TALK? Only a sentence the conversation lane could not answer is looked at: its spec is intent `unknown`,
     or a greeting, thanks or "how are you". A fact, a place, a quoted memory, a canon line, an opinion and an order
     never come here. Of what is left, chat_contract.talk_kind decides: a statement is talk; a question is talk only
     when it is about the companion or asks for its view, or when something the pilot said a moment ago supports
     it. Any other question is NOT talk and is left exactly where it was (THE FLIPPED DEFAULT: a question with
     nothing behind it is not handed to a model, which answers it with an invention about half the time).
  2. THE PROMPT. serialize() with the thread so far: up to six exchanges with this companion, kept in memory only,
     dropped after ten minutes in which the pilot said nothing. Nothing that changes is put before the persona.
  3. THE MODEL. One Ollama request per candidate, through the same helper the line models use
     (pair_realizer._post_json): raw mode, so no Ollama template can change the prompt. Two candidates at most,
     sampled at 0.7 and then 0.8, as in the evaluation.
  4. THE CUTS, in code after the model has spoken: its own label and stray marks come off (clean_reply), a stage
     direction that opens the reply is removed by its shape (strip_stage), then Elah keeps her first sentence and
     Montaigne his first two.
  5. THE GATE. chat_problems, the maker rule, and Elah denying that she has preferences. The first candidate that
     passes is the reply. When neither passes, the reply is FALLBACK, the line the evaluation used; a refused reply
     is never spoken.
     ONE REFUSAL IS ANSWERED DIFFERENTLY (2026-10-06). When the gate says the reply approves of the pilot staying
     in or avoiding people (chat_contract.APPROVES_WITHDRAWAL), the model is not asked again: it approved under
     five wordings of its prompt. The reply is the next written line the canon file holds for `withdrawal`, the
     same lines code says when it catches such a sentence itself.
  6. A REPLY THAT IS STARTED FOR THE MODEL (2026-10-06). When the pilot's sentence names people together with here
     or with a word of dropping or preferring (chat_contract.asks_after; loose on purpose), steps 3 to 5 run
     differently: the prompt of step 2 ENDS with the next of the canon file's "ask_openers" ("Tell me about"),
     ONE candidate is asked for, the opener and what the model added are cut to ONE sentence for either companion
     (chat_contract.ask_sentence), and that sentence goes through the same gate. If it is refused, or the cut
     leaves a fragment, the reply is the next written `withdrawal` line; the model is not asked again and FALLBACK
     is not used. A sentence the rule does not flag is answered by steps 3 to 5 exactly as before.
     CHANGED AFTER UNSEEN SET 7 (2026-10-06): the rule is wider (any line that names people; and a line that names
     nobody but says it is here by choice, which has openers of its own, "ask_openers_alone"); when code can pick
     out the one person or occasion the line names, it is put into the start ("Tell me about your brother"), and
     when it cannot the start says "them", so the model never chooses whom to ask after; and a
     refused started reply is answered with a plain written question from "ask_fallback", never with a
     withdrawal line, because this path runs on ordinary lines. The talker no longer says a withdrawal line at
     all: only the code act does.

WHEN IT CANNOT ANSWER it returns None and the sentence is answered as it would be with chat off: Ollama is not
running, the model is not installed, the request times out, or the card has no room (headroom TIGHT, the same rule
the eyes' glance follows, because this model costs the pilot's own GPU about 2.7 GB). It never raises.

It does not speak. CompanionCore._answer_worker says the line through the answer path every other answer uses, so
the window-hidden rule, the talk key's answer pass and Mute apply to it unchanged.

Not built here (elah-audio/_suit_chat_design.md): searching earlier days of the conversation log for a supporting
sentence, the running summary, the open-mic rule of section 1, and any choice of who keeps the thread.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import chat_contract as cc
import settings as st
import ship_makers as sm
from pair_realizer import OLLAMA_URL, _HTTP_ERRORS, _post_json

log = logging.getLogger("suitmk2.chat")

# Said when both candidates were refused. The evaluation's own two lines (contract_eval.FALLBACK); PROVISIONAL
# wording, not yet J's.
FALLBACK = {"elah": "Say that again, another way.", "montaigne": "I have lost the thread, pilot; put it to me again."}
# What the model is asked to do with a greeting, thanks or "how are you" (the lane's three social topics that no
# canon line answers). The evaluation's wording.
SOCIAL_ACTS = {"greeting": ("greet", "greet the pilot back, briefly"),
               "thanks": ("acknowledge", "take the thanks without fuss"),
               "how_are_you": ("acknowledge", "say how you are, your own way, and turn it back to the pilot")}
TEMPERATURES = (0.7, 0.8)            # the first candidate, then the second
NUM_PREDICT = 90
THREAD_EXCHANGES = 6                 # exchanges kept per companion
HEARD_KEPT = 12                      # sentences of the pilot's kept to answer a follow-up from
THREAD_ENDS_AFTER_S = 600.0          # ten minutes without a word from the pilot: the conversation is over
# How long Ollama keeps the model after a reply. The evaluation's value. The eyes' glance uses 20 s and the line
# models 10 m; three minutes keeps a conversation warm (a cold load was 2 to 7.5 s on an idle card) and hands the
# VRAM back soon after the pilot stops talking. Not measured with Star Citizen holding the card.
KEEP_ALIVE = "3m"
# One request. The answer path drops a line older than 20 s, so waiting longer than this for a candidate is waiting
# for something that would not be spoken.
TIMEOUT_S = 15.0


def prompt_format(model: str) -> str:
    """The chat format the prompt is written in. Raw mode sends our own turn markers, so they must be the model's:
    gemma's for a gemma model, ChatML (Qwen and most others) for anything else."""
    return "gemma" if str(model or "").strip().lower().startswith("gemma") else "chatml"


class Talker:
    """answer(spec, utterance, headroom) -> (line, how) or None. One per window; it holds the conversation so far."""

    def __init__(self, model: str, url: str = OLLAMA_URL, post: Callable[[str, dict, float], dict] = _post_json,
                 now: Callable[[], float] = time.time, timeout: float = TIMEOUT_S,
                 note: Optional[Callable[[str], None]] = None,
                 thread_exchanges: int = THREAD_EXCHANGES, thread_ends_after_s: float = THREAD_ENDS_AFTER_S,
                 fit: Optional[Callable[[], tuple]] = None):
        self.model, self.url, self.timeout = str(model).strip(), url.rstrip("/"), timeout
        # The pilot's own numbers (settings chat_thread_exchanges / chat_thread_ends_after_s); the constants are
        # the defaults, not a ceiling.
        self.thread_exchanges = max(1, int(thread_exchanges))
        self.thread_ends_after_s = float(thread_ends_after_s)
        # fit() -> (ok, why): does this model still fit in the memory that is free NOW (chat_models.UseCheck). It was
        # checked when it was picked, and the game may have started since. None = no check (the tests' own talkers).
        self._fit = fit
        self.fmt = prompt_format(self.model)
        self._post, self._now = post, now
        self._note = note or (lambda msg: log.info(msg))
        self._thread: dict[str, list] = {who: [] for who in cc.SPEAKERS}    # who -> [(pilot line, reply)]
        self._heard: list[str] = []                                          # the pilot's last sentences, oldest first
        self._last_t: Optional[float] = None
        self._state = threading.Lock()       # the thread and what was heard
        self._one = threading.Lock()         # one sentence at the model at a time, in the order they were said
        self.stats = {"asked": 0, "replies": 0, "refused": 0, "fallback": 0, "unavailable": 0, "written": 0,
                      "asked_after": 0}
        self._written = 0                    # which written plain question is next (they turn)
        self._opener = {who: 0 for who in cc.SPEAKERS}      # which opener is next, per companion (they turn too)

    # -- is it talk --------------------------------------------------------------------------------------------
    def _memory(self, line: str) -> list:
        """Up to two things the pilot said earlier in this conversation that support this QUESTION, newest first,
        by chat_contract.memory_supports. Statements only: a question is never the answer to a question."""
        if not cc.is_question(line):
            return []
        mem: list = []
        for said in reversed(self._heard):
            if not cc.is_question(said) and cc.memory_supports(line, said) and said not in [m[2] for m in mem]:
                mem.append(("pilot", "earlier today", said))
        return mem[:2]

    def brief(self, spec: dict, line: str) -> Optional[dict]:
        """What the model would be asked, or None when this sentence is not talk (module docstring, step 1)."""
        if not isinstance(spec, dict) or spec.get("lane") != "direct" or spec.get("fixed_text"):
            return None
        if spec.get("speaker") not in cc.SPEAKERS:
            return None
        route = spec.get("route") or {}
        if route.get("intent") == "unknown":
            act, content = "open", cc.OPEN_CONTENT
        elif route.get("intent") == "social" and route.get("topic") in SOCIAL_ACTS:
            act, content = SOCIAL_ACTS[route["topic"]]
        else:
            return None
        memory = self._memory(line)
        kind = cc.talk_kind(line, (), memory)
        if kind is None:
            return None
        return {"act": act, "content": content, "memory": memory, "kind": kind}

    # -- the cuts and the gate ---------------------------------------------------------------------------------
    @staticmethod
    def cut(who: str, raw: str) -> str:
        """A model's reply as it may be judged and spoken: cleaned, an opening stage direction removed, then Elah's
        first sentence or Montaigne's first two."""
        uncut = cc.strip_stage(cc.clean_reply(str(raw or "").strip(), who))
        return cc.first_sentence(uncut) if who == "elah" else cc.first_sentences(uncut, 2)

    @staticmethod
    def problems(who: str, text: str, shown: str, said_here: str, line: str, own: list) -> list[str]:
        """Why this reply may not be spoken ([] = it may). shown: everything the model was handed that it may take
        a name or a number from. said_here: what the pilot and the companion have said in this conversation; a ship
        maker's name must be in it, the persona does not count."""
        fails = cc.chat_problems(who, text, shown, line, own[-4:])
        fails += cc.maker_problems(text, said_here, sm.maker_words())
        if who == "elah":
            fails += [f for f in cc.denies_preferences(text) if f not in fails]
        return fails

    # -- one sentence ------------------------------------------------------------------------------------------
    def _expire(self) -> None:
        now = self._now()
        if self._last_t is not None and now - self._last_t > self.thread_ends_after_s:
            for who in self._thread:
                self._thread[who] = []
            self._heard = []
        self._last_t = now

    def answer(self, spec: dict, utterance: str, headroom: Optional[Callable[[], str]] = None) -> Optional[tuple]:
        """(the line to say, how it came about) when this sentence is talk and the model worded it or was refused
        twice; None when it is not talk or the model cannot be asked, and the caller answers as it always did."""
        line = " ".join(str(utterance or "").split())
        if not line:
            return None
        with self._state:
            self._expire()
            brief = self.brief(spec, line)
            self._heard = (self._heard + [line])[-HEARD_KEPT:]
        if brief is None:
            return None
        who = spec["speaker"]
        try:
            tight = headroom is not None and headroom() == "TIGHT"
        except Exception:
            log.exception("talk: headroom could not be read; treated as TIGHT")
            tight = True                                  # no reading never grants the card
        if tight:
            self.stats["unavailable"] += 1
            self._note("talk: no room on the card (headroom TIGHT); answered as with chat off")
            return None
        if self._fit is not None:
            try:
                fits, why = self._fit()
            except Exception:
                log.exception("talk: the fit check failed; the model is not asked")
                fits, why = False, "the fit check failed"
            if not fits:
                # The model no longer fits beside what is running. Like every other time it cannot be asked: the
                # sentence is answered as with chat off, and nothing is loaded.
                self.stats["unavailable"] += 1
                self._note(f"talk: {self.model} is not asked ({why}); answered as with chat off")
                return None
        with self._one:
            with self._state:
                thread = list(self._thread[who])
            own = [a for _, a in thread]
            prompt = cc.serialize(who, thread, line, brief["act"], brief["content"], (), brief["memory"], self.fmt)
            said_here = " ".join(f"{p} {a}" for p, a in thread) + " " + line
            shown = " ".join([cc.persona(who), said_here] + [m[2] for m in brief["memory"]])
            self.stats["asked"] += 1
            reply, how = FALLBACK[who], "talk, both replies refused"
            scope = cc.ask_scope(line)
            openers = cc.ask_openers(who, scope) if scope else []
            if openers:
                # Step 6: the reply is started for the model, and there is one candidate. For a line that names
                # people the start already says WHOM: the one person or occasion code can pick out, else "them".
                whom = (cc.ask_object(line) or "them") if scope == "people" else ""
                opener = openers[self._opener[who] % len(openers)]
                start = f"{opener} {whom}" if whom else opener
                body = {"model": self.model, "raw": True, "stream": False, "prompt": prompt + start,
                        "keep_alive": KEEP_ALIVE,
                        "options": {"temperature": TEMPERATURES[0], "top_p": 0.9, "num_predict": NUM_PREDICT,
                                    "stop": cc.stop_tokens(self.fmt)}}
                try:
                    raw = self._post(self.url + "/api/generate", body, self.timeout)["response"]
                except _HTTP_ERRORS + (KeyError, TypeError) as e:
                    self.stats["unavailable"] += 1
                    log.warning("talk: %s could not be asked at %s (%s: %s)", self.model, self.url, type(e).__name__, e)
                    self._note(f"talk: {self.model} not available ({type(e).__name__}); answered as with chat off")
                    return None
                self._opener[who] += 1
                text = cc.ask_sentence(start, raw, who, whole=bool(whom))
                fails = self.problems(who, text, shown, said_here, line, own) if text else ["nothing left after the cut"]
                if not fails:
                    reply, how = text, ("talk, asked after them" if scope == "people" else "talk, turned outward")
                    self.stats["replies"] += 1
                    self.stats["asked_after"] += 1
                else:
                    # Never a withdrawal line here: this path runs on ordinary lines too.
                    self.stats["refused"] += 1
                    self._note(f"talk started reply REFUSED {fails}: {(text or start + ' ' + str(raw))[:60]!r}")
                    reply, how = cc.ask_fallback(who, self._written), "talk, the started reply was refused: a plain question"
                    self._written += 1
                    self.stats["written"] += 1
                with self._state:
                    self._thread[who] = (self._thread[who] + [(line, reply)])[-self.thread_exchanges:]
                return reply, how
            for n, temperature in enumerate(TEMPERATURES, 1):
                body = {"model": self.model, "raw": True, "stream": False, "prompt": prompt, "keep_alive": KEEP_ALIVE,
                        "options": {"temperature": temperature, "top_p": 0.9, "num_predict": NUM_PREDICT,
                                    "stop": cc.stop_tokens(self.fmt)}}
                try:
                    raw = self._post(self.url + "/api/generate", body, self.timeout)["response"]
                except _HTTP_ERRORS + (KeyError, TypeError) as e:
                    # Ollama is not running, the model is not installed (HTTP 404), it took too long, or it
                    # answered with something that is not a reply. Not a refusal: the sentence is answered as with
                    # chat off, and the status line says why.
                    self.stats["unavailable"] += 1
                    log.warning("talk: %s could not be asked at %s (%s: %s)", self.model, self.url, type(e).__name__, e)
                    self._note(f"talk: {self.model} not available ({type(e).__name__}); answered as with chat off")
                    return None
                text = self.cut(who, raw)
                fails = self.problems(who, text, shown, said_here, line, own)
                if not fails:
                    reply, how = text, f"talk, {brief['kind']}"
                    self.stats["replies"] += 1
                    break
                self.stats["refused"] += 1
                self._note(f"talk reply {n} REFUSED {fails}: {text[:60]!r}")
                if cc.APPROVES_WITHDRAWAL in fails:
                    # Reached only when the canon file has no openers. A plain question, not a withdrawal line:
                    # the rule that put this line in scope is loose, and the line may be an ordinary one.
                    reply, how = cc.ask_fallback(who, self._written), "talk, approved a withdrawal: a plain question"
                    self._written += 1
                    self.stats["written"] += 1
                    break
            else:
                self.stats["fallback"] += 1
            with self._state:
                self._thread[who] = (self._thread[who] + [(line, reply)])[-self.thread_exchanges:]
            return reply, how


def from_settings(s: dict, **kw) -> Optional[Talker]:
    """The talker the saved settings ask for, or None: chat is off, or no chat model is named (settings.chat_on)."""
    if not st.chat_on(s):
        return None
    model = str(s["chat_model"]).strip()
    kw.setdefault("thread_exchanges", s.get("chat_thread_exchanges", THREAD_EXCHANGES))
    kw.setdefault("thread_ends_after_s", s.get("chat_thread_ends_after_s", THREAD_ENDS_AFTER_S))
    if "fit" not in kw:
        # Every talker the settings build checks the fit before each use. There is no setting that leaves it out.
        import chat_models
        kw["fit"] = chat_models.UseCheck(model, url=kw.get("url"))
    return Talker(model, **kw)

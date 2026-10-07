"""setup_panel.py - first-run "Set up voices & brain" panel for the SuitMk2 window (PySide6).

One status line, ONE button, one progress bar. The player never sees the words Ollama, model, GGUF or pull unless
they open "Advanced". The work (install the runtime if absent -> wake it -> provision the characters) is
model_provision.SetupJob, run on a worker thread; the UI polls job.overall / job.message on a QTimer, so no Qt object
is touched off the GUI thread.

    panel = SetupPanel(parent)
    panel.ready.connect(on_ready)      # emitted once everything is in place (also at startup if it already was)
    layout.addWidget(panel)            # hides itself when READY; shows itself when something is missing

WHAT COUNTS AS "SET UP": the runtime answers AND, for at least one prefix in `ready_prefixes`, BOTH characters
exist (<prefix>elah and <prefix>montaigne). Default ready_prefixes = (the provisioner's prefix,); the window passes
pair_realizer.MODEL_PREFIXES, so a dev machine with the realizer-* set is "set up" and never provisions suitmk2-*.
auto_start=True (settings "auto_setup") starts the job without the click, once per panel, only when not set up.

THE ASSISTANT'S SMALL BRAIN. With assistant_tag given (the window passes model_provision.assistant_tag_wanted()),
the same click also fetches the Toolbox Assistant's own small model, in the same bar. That download failing does not
fail the setup: the characters are in place, the panel says one plain sentence and stays, with a button for that
one download. The same happens on a PC where the characters were set up before this existed: `ready` is emitted
as always, and the panel stays visible only to offer the missing download. It never provisions anything else then.

GEMMA, FOR TALKING. With ask_chat given, a click on the setup button first reads what is installed and what memory
is free (off the GUI thread) and hands ask_chat(offer) the question worded by chat_models.offer(); the window shows
it as a pop-up whose default is no. Only a yes downloads anything: after the setup itself has finished, through
chat_models.fetch, and chat_fetched(ok, sentence) says how it went. A failed or stopped download there is not a
failed setup. With no click (auto_start) nobody is asked and nothing extra is downloaded.
"""
from __future__ import annotations

import sys
import threading
import traceback
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QToolButton,
                               QVBoxLayout, QWidget)

CORE = Path(__file__).resolve().parent.parent / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

import chat_models                                    # noqa: E402
import hardware_guard                                 # noqa: E402
import model_provision as mp                          # noqa: E402
import ollama_manager as om                           # noqa: E402

try:
    from shared.qt.theme import P                     # the toolbox palette
except Exception:                                     # standalone / tests
    class P:                                          # type: ignore[no-redef]
        fg, fg_dim, bg_input, border, bg_deepest, red = "#ddd", "#999", "#222", "#444", "#111", "#e55"

ACCENT = "#7fd1b9"
# The wording was approved on 2026-09-23 as "(about 1.9 GB)". The same click now also fetches the Toolbox
# Assistant's small brain (model_provision.ASSISTANT_BYTES, 0.4 GB), so the stated total is 0.4 GB more.
BUTTON_TEXT = "Set up Elah and Montaigne (about 2.3 GB)"
ASSISTANT_SIZE = f"{mp.ASSISTANT_BYTES / 1e9:.1f} GB"
ASSISTANT_BUTTON = f"Get the Assistant's brain (about {ASSISTANT_SIZE})"
ASSISTANT_NEEDED = ("Elah and Montaigne are ready. One thing is missing: the Toolbox Assistant's own small brain "
                    f"(about {ASSISTANT_SIZE}). Until it is here the Assistant answers in its simpler mode.")
ASSISTANT_FAILED = ("The Toolbox Assistant's small brain could not be fetched. Elah and Montaigne are set up and work "
                    "without it; the Assistant answers in its simpler mode until it is fetched. Press the button to "
                    "try again.")
ASSISTANT_BUSY = "Fetching the Assistant's brain..."
CHAT_BUSY = "Elah and Montaigne are set up. Now downloading Gemma, for talking..."


def complete_set(models: set, prefixes, speakers=mp.SPEAKERS) -> Optional[str]:
    """First prefix whose every speaker is installed (bare or :latest), else None. Pure."""
    for p in prefixes:
        if all(f"{p}{s}" in models or f"{p}{s}:latest" in models for s in speakers):
            return p
    return None


class SetupPanel(QFrame):
    ready = Signal()
    failed = Signal(str)
    assistant_failed = Signal(str)          # the plain sentence; the characters are set up all the same
    chat_fetched = Signal(bool, str)        # the Gemma download the player said yes to: (it is here, what to say)

    def __init__(self, parent: Optional[QWidget] = None, *, manager: Optional[om.OllamaManager] = None,
                 provisioner_kw: Optional[dict] = None, auto_check: bool = True,
                 ready_prefixes: Optional[tuple] = None, auto_start: bool = False,
                 assistant_tag: Optional[str] = None, ask_chat: Optional[Callable] = None):
        super().__init__(parent)
        self.assistant_tag = assistant_tag or None
        self.assistant_only = False             # the characters exist; the button fetches the Assistant's brain only
        self.ask_chat = ask_chat
        self.chat_wanted = False                # NO until the player says yes in the pop-up
        self.chat_result: Optional[tuple] = None
        self._assistant_error: Optional[str] = None
        self._assistant_note = ""
        self._cancel = threading.Event()
        self._side_on = False                   # a download outside the setup job is running; _side is its progress
        self._side = (0.0, "")
        self._probe: Optional[tuple] = None
        self.mgr = manager or om.OllamaManager()
        self.provisioner_kw = provisioner_kw or {}
        own = self.provisioner_kw.get("prefix", mp.MODEL_PREFIX)
        self.required = [mp.model_name(s, own) for s in mp.SPEAKERS]
        self.ready_prefixes = tuple(ready_prefixes) if ready_prefixes else (own,)
        if own not in self.ready_prefixes:
            self.ready_prefixes = (own,) + self.ready_prefixes
        self.ready_prefix: Optional[str] = None        # which set satisfied the check, once READY
        self.auto_start = auto_start
        self._auto_started = False
        self.job: Optional[mp.SetupJob] = None
        self._thread: Optional[threading.Thread] = None
        self._result: Optional[dict] = None
        self._error: Optional[str] = None
        self._status: Optional[om.Status] = None
        self.setStyleSheet(f"SetupPanel {{ border: 1px solid {ACCENT}; border-radius: 6px; }}")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)
        self.status_lbl = QLabel("Checking...")
        self.status_lbl.setStyleSheet(f"color: {P.fg}; font-size: 10pt;")
        self.status_lbl.setWordWrap(True)
        lay.addWidget(self.status_lbl)

        row = QHBoxLayout()
        self.button = QPushButton(BUTTON_TEXT)
        self.button.setStyleSheet(f"QPushButton {{ background: {ACCENT}; color: {P.bg_deepest}; border-radius: 4px;"
                                  f" padding: 6px 14px; font-weight: bold; }}"
                                  f"QPushButton:disabled {{ background: {P.bg_input}; color: {P.fg_dim}; }}")
        self.button.clicked.connect(self._clicked)
        row.addWidget(self.button)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self.cancel)
        row.addWidget(self.cancel_btn)
        row.addStretch(1)
        lay.addLayout(row)

        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.bar.setVisible(False)
        lay.addWidget(self.bar)
        self.detail = QLabel("")
        self.detail.setStyleSheet(f"color: {P.fg_dim}; font-size: 8pt;")
        lay.addWidget(self.detail)

        self.adv_toggle = QToolButton()
        self.adv_toggle.setText("Advanced")
        self.adv_toggle.setCheckable(True)
        self.adv_toggle.setStyleSheet(f"color: {P.fg_dim}; border: none; font-size: 8pt;")
        lay.addWidget(self.adv_toggle)
        self.adv = QWidget()
        al = QVBoxLayout(self.adv)
        al.setContentsMargins(12, 0, 0, 0)
        self.adv_info = QLabel("")
        self.adv_info.setStyleSheet(f"color: {P.fg_dim}; font-family: Consolas; font-size: 8pt;")
        self.adv_info.setWordWrap(True)
        al.addWidget(self.adv_info)
        self.vision_chk = QCheckBox("Also set up the vision model (gemma3:4b, about 3.3 GB) for scene glances")
        al.addWidget(self.vision_chk)
        self.install_chk = QCheckBox("Install Ollama automatically if it is missing")
        self.install_chk.setChecked(True)
        al.addWidget(self.install_chk)
        self.adv.setVisible(False)
        self.adv_toggle.toggled.connect(self.adv.setVisible)
        lay.addWidget(self.adv)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        if auto_check:
            self.check()

    # -- status ----------------------------------------------------------------------------------------------------
    def check(self) -> None:
        """Probe off the GUI thread (a dead port can take a second), then render on the next poll."""
        def work():
            try:
                self._status = self.mgr.status(self.required)
            except Exception as e:
                self._error = f"{type(e).__name__}: {e}"
        threading.Thread(target=work, name="suitmk2_setup_check", daemon=True).start()
        self._timer.start(150)

    def _render_status(self, st: om.Status) -> None:
        # A COMPLETE set under any accepted prefix is "set up" (a dev machine's realizer-* set counts).
        if st.state in (om.READY, om.MODELS_MISSING):
            self.ready_prefix = complete_set(st.models, self.ready_prefixes)
        else:
            self.ready_prefix = None
        self.adv_info.setText(f"runtime: Ollama {st.version or '-'} at {self.mgr.url} | state {st.state}"
                              + (f" | exe {st.exe}" if st.exe else "")
                              + f"\nmodels: {', '.join(self.required)}"
                              + (f" (missing: {', '.join(st.missing)})" if st.missing and not self.ready_prefix
                                 else "")
                              + (f"\nin use: {self.ready_prefix}elah / {self.ready_prefix}montaigne"
                                 if self.ready_prefix else ""))
        if self.ready_prefix is not None:
            if self._assistant_missing(st):         # the characters are ready; only the Assistant's brain is not
                self.assistant_only = True
                self.status_lbl.setText(self._assistant_note or ASSISTANT_NEEDED)
                self._assistant_note = ""
                self.button.setText(ASSISTANT_BUTTON)
                self.button.setEnabled(True)
                self.setVisible(True)
                self.ready.emit()
                if self.auto_start and not self._auto_started:
                    self._auto_started = True
                    self.start()
                return
            self.assistant_only = False
            self.status_lbl.setText("Voices and brain are ready.")
            self.button.setEnabled(False)
            self.setVisible(False)
            self.ready.emit()
            return
        if self.assistant_only:
            self.assistant_only = False
            self.button.setText(BUTTON_TEXT)
        self.setVisible(True)
        self.button.setEnabled(True)
        self.status_lbl.setText({
            om.NOT_INSTALLED: "Elah and Montaigne need a one-time setup. One click; after that it runs by itself.",
            om.INSTALLED_NOT_RUNNING: "One click to finish setting up Elah and Montaigne.",
            om.RUNNING: "One click to finish setting up Elah and Montaigne.",
            om.MODELS_MISSING: "One click to finish setting up Elah and Montaigne (a few minutes).",
        }.get(st.state, st.plain()))
        if self.auto_start and not self._auto_started:       # settings "auto_setup": no click, once per panel
            self._auto_started = True
            self.start()

    def _assistant_missing(self, st: om.Status) -> bool:
        tag = self.assistant_tag
        return bool(tag) and st.state in (om.READY, om.MODELS_MISSING) and not (
            tag in st.models or f"{tag}:latest" in st.models)

    # -- the button ------------------------------------------------------------------------------------------------
    def _clicked(self) -> None:
        """The button. A full setup first asks about Gemma (once: a yes is kept for "Try again"); the reads the
        question needs take up to a second and a half, so they are made off the GUI thread."""
        if self.ask_chat is None or self.assistant_only or self.chat_wanted:
            self.start()
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self.button.setEnabled(False)
        self._probe = None

        def work():
            models = free = None
            try:
                models = chat_models.installed(self.mgr.url)
                free = hardware_guard.read_free_memory()
            except Exception:
                traceback.print_exc()
            self._probe = (models, free)
        threading.Thread(target=work, name="suitmk2_setup_chat_probe", daemon=True).start()
        self._poll_probe()

    def _poll_probe(self) -> None:
        if self._probe is None:
            QTimer.singleShot(100, self._poll_probe)
            return
        (models, free), self._probe = self._probe, None
        self._ask_then_start(models, free)

    def _ask_then_start(self, models, free) -> None:
        """GUI thread. Ask; anything but a clear yes is no. Then the setup starts either way."""
        try:
            self.chat_wanted = self.ask_chat(chat_models.offer(models, free, setup=True)) is True
        except Exception:
            traceback.print_exc()
            self.chat_wanted = False
        self.start()

    def _side_progress(self, stage: str, done: int, total: int, msg: str) -> None:
        self._side = ((done / total) if total else 0.0, msg)        # two plain values; the widgets are set in _poll

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._result = self._error = self._assistant_error = self.chat_result = None
        self._side_on, self._side = False, (0.0, "")
        only = self.assistant_only
        chat = self.chat_wanted and not only
        if only:
            self._cancel = threading.Event()
        else:
            self.job = mp.SetupJob(include_vision=self.vision_chk.isChecked(),
                                   allow_install=self.install_chk.isChecked(), manager=self.mgr,
                                   provisioner_kw=self.provisioner_kw, assistant_tag=self.assistant_tag)
            self._cancel = self.job.cancel
        cancel = self._cancel
        self.button.setEnabled(False)
        self.cancel_btn.setVisible(True)
        self.bar.setVisible(True)
        self.bar.setValue(0)
        self.status_lbl.setText(ASSISTANT_BUSY if only else "Setting up Elah and Montaigne...")

        def work():
            try:
                if only:
                    self._side_on = True
                    try:
                        mp.Provisioner(self.mgr.url, progress=self._side_progress, cancel=cancel).pull(
                            self.assistant_tag, mp.ASSISTANT_LABEL)
                    except mp.Cancelled:
                        raise
                    except Exception as e:
                        self._assistant_error = f"{type(e).__name__}: {e}"
                else:
                    self._result = self.job.run()
                    self._assistant_error = self._result.get("assistant_error")
                if chat:                                # only after a yes, and only once the setup itself is done
                    self._side, self._side_on = (0.0, chat_models.FETCH_LABEL), True
                    self.chat_result = chat_models.fetch(self.mgr.url, progress=self._side_progress, cancel=cancel)
            except mp.Cancelled:
                self._error = "cancelled"
            except Exception as e:
                self._error = f"{type(e).__name__}: {e}"
                traceback.print_exc()
        self._thread = threading.Thread(target=work, name="suitmk2_setup", daemon=True)
        self._thread.start()
        self._timer.start(150)

    def cancel(self) -> None:
        if self.job is not None or self.assistant_only:
            self._cancel.set()
            self.detail.setText("Stopping after the current step...")

    def _poll(self) -> None:
        if self._thread is None:                       # waiting on check()
            if self._status is not None:
                self._timer.stop()
                st, self._status = self._status, None
                self._render_status(st)
            elif self._error:
                self._timer.stop()
                self.status_lbl.setText("Could not check the setup.")
                self.detail.setText(self._error)
            return
        if self._side_on:                              # the Assistant's brain alone, or Gemma after the setup
            frac, msg = self._side
            self.bar.setValue(int(min(1.0, frac) * 1000))
            if msg:
                self.detail.setText(msg)
            if not self.assistant_only:
                self.status_lbl.setText(CHAT_BUSY)
        elif self.job is not None:
            self.bar.setValue(int(self.job.overall * 1000))
            if self.job.message:
                self.detail.setText(self.job.message)
        if self._thread.is_alive():
            return
        self._timer.stop()
        self._thread = None
        self.cancel_btn.setVisible(False)
        if self._error:
            self.bar.setVisible(False)
            self.button.setEnabled(True)
            self.button.setText("Try again")
            self.status_lbl.setText(failure_text(self._error))
            self.detail.setText("" if self._error == "cancelled" else self._error)
            self.adv_info.setText(self.adv_info.text() + f"\nlast error: {self._error}")
            self.failed.emit(self._error)
            return
        self.bar.setValue(1000)
        self.detail.setText("")
        if self._assistant_error:                      # not a failed setup: the characters are in place
            self.bar.setVisible(False)
            self.detail.setText(self._assistant_error)
            self._assistant_note = ASSISTANT_FAILED    # shown by the re-probe below, which keeps the panel up
            self.assistant_failed.emit(ASSISTANT_FAILED)
        if self.chat_result is not None:
            self.chat_wanted = False
            self.chat_fetched.emit(bool(self.chat_result[0]), str(self.chat_result[1]))
        self.check()                                   # re-probe: READY hides the panel and emits ready


# ---- selftest: offscreen Qt against the fake runtime -------------------------------------------------------------
def failure_text(error: str) -> str:
    """The line shown when setup stops. When the part that failed is installing the local runtime
    itself, say what the player can do about it: until 3.0.1 that case said "try again to resume",
    and trying again downloaded 1.6 GB and failed the same way."""
    if error == "cancelled":
        return "Setup stopped."
    e = str(error or "")
    if e.startswith("OllamaError: installer") or e.startswith("OllamaError: download"):
        return ("The local brain could not be installed automatically. "
                "Install Ollama from ollama.com, then press Try again.")
    return "Setup did not finish. Nothing is lost; try again to resume."


def _selftest() -> int:
    import os
    import shutil
    import tempfile
    import time
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from gguf_stitch import GGUFFile, write_test_gguf
    app = QApplication.instance() or QApplication([])
    results = []

    def case(name, ok):
        results.append((name, bool(ok)))

    def pump(cond, secs=20.0):
        t0 = time.time()
        while time.time() - t0 < secs and not cond():
            app.processEvents()
            time.sleep(0.02)
        return cond()

    td = Path(tempfile.mkdtemp(prefix="suitmk2_panel_test_"))
    a = bytes(range(256)) * 2
    base_file = td / ("sha256-" + "b" * 64)
    write_test_gguf(base_file, {"general.architecture": "qwen2"},
                    [("tok.weight", (128,), 0, a), ("blk.0.attn_q.weight", (256, 2), 12, b"\x11" * 288)])
    models = td / "models"
    models.mkdir()
    for spk in mp.SPEAKERS:
        write_test_gguf(models / f"{spk}.delta.gguf", {"suitmk2.delta.speaker": spk,
                                                       "suitmk2.delta.base_fingerprint": GGUFFile(base_file).fingerprint()},
                        [("blk.0.attn_q.weight", (256, 2), 8, bytes([len(spk)]) * 544)])
    fo = om.FakeOllama(models={mp.BASE_TAG: {"from_blob": "sha256:" + "b" * 64, "from_path": str(base_file)}})
    kw = {"models_dir": models, "state_path": td / "state.json"}
    panel = SetupPanel(manager=om.OllamaManager(fo.url), provisioner_kw=kw)
    got_ready = []
    panel.ready.connect(lambda: got_ready.append(1))
    panel.show()
    case("MODELS_MISSING: panel visible, button enabled", pump(lambda: panel.button.isEnabled())
         and panel.isVisible())
    visible_text = panel.status_lbl.text() + panel.detail.text() + panel.button.text()
    case("failure text: a failed runtime install tells the player what to do",
         "ollama.com" in failure_text("OllamaError: installer signature rejected (x)")
         and "ollama.com" in failure_text("OllamaError: download truncated: 1 of 2 bytes")
         and "ollama.com" in failure_text("OllamaError: installer exited rc=1"))
    case("failure text: other failures still say resume, and cancel says stopped",
         "resume" in failure_text("ProvisionError: boom") and failure_text("cancelled") == "Setup stopped.")
    case("no runtime jargon outside Advanced", not any(w in visible_text.lower()
                                                       for w in ("ollama", "gguf", "pull", "model")))
    panel.button.click()
    case("click -> progress bar shown", panel.bar.isVisible())
    case("setup completes -> ready emitted, panel hidden", pump(lambda: bool(got_ready), 30) and not panel.isVisible())
    case("both characters exist afterwards", {"suitmk2-elah", "suitmk2-montaigne"} <= set(fo.models))
    panel2 = SetupPanel(manager=om.OllamaManager(fo.url), provisioner_kw=kw)
    r2 = []
    panel2.ready.connect(lambda: r2.append(1))
    case("already READY at startup -> ready emitted, never shown", pump(lambda: bool(r2)) and not panel2.isVisible())
    fo.create_error = "boom"
    fo.models.pop("suitmk2-elah")
    panel3 = SetupPanel(manager=om.OllamaManager(fo.url), provisioner_kw={**kw, "state_path": td / "s3.json"})
    fails = []
    panel3.failed.connect(fails.append)
    pump(lambda: panel3.button.isEnabled())
    panel3.button.click()
    case("create failure -> failed signal, 'Try again', error only in detail/Advanced",
         pump(lambda: bool(fails), 30) and panel3.button.text() == "Try again" and "boom" in panel3.detail.text())
    fo.stop()

    # A dev machine: only the realizer-* set exists. With ready_prefixes it is "set up": hidden, ready, and it must
    # NEVER create suitmk2-* (they would be built from old weights and shadow the retrained realizer-* set).
    fr = om.FakeOllama(models={"realizer-elah": {"from_blob": "sha256:" + "d" * 64},
                               "realizer-montaigne": {"from_blob": "sha256:" + "e" * 64}})
    pr = SetupPanel(manager=om.OllamaManager(fr.url), provisioner_kw={**kw, "state_path": td / "s4.json"},
                    ready_prefixes=("suitmk2-", "realizer-"), auto_start=True)
    r4 = []
    pr.ready.connect(lambda: r4.append(1))
    case("realizer-* set only + ready_prefixes -> ready, hidden, nothing created (even with auto_start)",
         pump(lambda: bool(r4)) and not pr.isVisible() and pr.ready_prefix == "realizer-" and not fr.created
         and not any(q[0] == "POST" and q[1] in ("/api/create", "/api/pull") for q in fr.requests if len(q) > 1))
    fr.stop()
    # ...but the default (the provisioner's own prefix only) still treats that machine as not set up.
    fr2 = om.FakeOllama(models={"realizer-elah": {"from_blob": "sha256:" + "d" * 64},
                                "realizer-montaigne": {"from_blob": "sha256:" + "e" * 64}})
    pd = SetupPanel(manager=om.OllamaManager(fr2.url), provisioner_kw={**kw, "state_path": td / "s5.json"})
    case("default ready_prefixes: realizer-* alone is not 'set up'", pump(lambda: pd.button.isEnabled())
         and pd.ready_prefix is None and pd.button.text() == BUTTON_TEXT)
    fr2.stop()
    # One incomplete set of each kind is NOT set up.
    case("complete_set: one of each prefix -> None", complete_set({"suitmk2-elah", "realizer-montaigne:latest"},
                                                                  ("suitmk2-", "realizer-")) is None)
    # auto_start (settings "auto_setup"): provisioning runs with no click.
    fa = om.FakeOllama(models={mp.BASE_TAG: {"from_blob": "sha256:" + "b" * 64, "from_path": str(base_file)}})
    pa = SetupPanel(manager=om.OllamaManager(fa.url), provisioner_kw={**kw, "state_path": td / "s6.json"},
                    auto_start=True)
    ra = []
    pa.ready.connect(lambda: ra.append(1))
    case("auto_start: set up with no click -> ready, both characters exist",
         pump(lambda: bool(ra), 30) and {"suitmk2-elah", "suitmk2-montaigne"} <= set(fa.models))
    fa.stop()
    shutil.rmtree(td, ignore_errors=True)
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    bad = sum(not ok for _, ok in results)
    print(f"setup_panel selftest: {len(results) - bad}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)

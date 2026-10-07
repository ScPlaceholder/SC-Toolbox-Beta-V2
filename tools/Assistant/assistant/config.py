"""LLM endpoint configuration: which model service the Assistant talks to.

Config lives in ``~/.sctoolbox/assistant_llm.json`` so it survives
toolbox updates. Every field can be overridden with an environment
variable (handy for testing):

  SC_LLM_PROVIDER   -- "openai" (OpenAI-compatible) | "anthropic"
  SC_LLM_BASE_URL   -- e.g. http://localhost:11434/v1  (Ollama)
                                http://localhost:1234/v1  (LM Studio)
                                https://api.openai.com/v1
  SC_LLM_API_KEY    -- provider key (empty for local servers)
  SC_LLM_MODEL      -- e.g. qwen2.5:0.5b, gpt-4o-mini, claude-sonnet-5
  SC_LLM_MAX_TOKENS / SC_LLM_TEMPERATURE
  SC_ASSISTANT_MODE -- "router" (no model), "router+llm" (default: code
                       picks the tool, the model only tie-breaks and
                       phrases) or "llm" (the model picks everything)

The default model is qwen2.5:0.5b: in router+llm it only has to choose
between two or three tools and rephrase a sentence, which is small enough
to run beside the companion model on an ordinary PC. With no model
reachable the Assistant answers in router mode by itself.

Defaults point at a local Ollama — works offline, nothing leaves the
machine. Edit the JSON (or the Settings dialog in the panel) to point
at any other server.

Nothing in the Assistant downloads that model. The first-run setup on the
Suit Mk2 tab fetches it with the companions' own (tools/SuitMk2/core/
model_provision.py, ASSISTANT_TAG). Until it is on the PC the Assistant
answers in router mode, and model_note() below is the sentence the panel
shows so that this is said and not silent.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from typing import Callable, Optional

log = logging.getLogger(__name__)


def _config_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".sctoolbox",
                        "assistant_llm.json")


@dataclass
class LLMConfig:
    provider: str = "openai"          # "openai" | "anthropic"
    base_url: str = "http://127.0.0.1:11434/v1"
    api_key: str = ""
    model: str = "qwen2.5:0.5b"
    # "router" | "router+llm" | "llm" -- see agent.py
    mode: str = "router+llm"
    max_tokens: int = 1024
    temperature: float = 0.2
    # Seconds to wait on the LLM HTTP call (local models can be slow to
    # first token on a busy box).
    timeout: int = 120

    # ── load / save ──────────────────────────────────────────────────────
    @classmethod
    def load(cls) -> "LLMConfig":
        cfg = cls()
        path = _config_path()
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                for k, v in data.items():
                    if hasattr(cfg, k) and v is not None:
                        setattr(cfg, k, v)
            except (OSError, json.JSONDecodeError, TypeError) as exc:
                log.warning("assistant config: cannot read %s: %s", path, exc)
        cfg.apply_env()
        return cfg

    def apply_env(self) -> None:
        """Environment overrides win over the JSON file."""
        env = os.environ
        if env.get("SC_LLM_PROVIDER"):
            self.provider = env["SC_LLM_PROVIDER"]
        if env.get("SC_LLM_BASE_URL"):
            self.base_url = env["SC_LLM_BASE_URL"]
        if env.get("SC_LLM_API_KEY") is not None:
            self.api_key = env.get("SC_LLM_API_KEY", "")
        if env.get("SC_LLM_MODEL"):
            self.model = env["SC_LLM_MODEL"]
        if env.get("SC_ASSISTANT_MODE"):
            self.mode = env["SC_ASSISTANT_MODE"]
        if env.get("SC_LLM_MAX_TOKENS"):
            try:
                self.max_tokens = int(env["SC_LLM_MAX_TOKENS"])
            except ValueError:
                pass
        if env.get("SC_LLM_TEMPERATURE"):
            try:
                self.temperature = float(env["SC_LLM_TEMPERATURE"])
            except ValueError:
                pass

    def save(self) -> bool:
        path = _config_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(asdict(self), f, indent=2)
            return True
        except OSError as exc:
            log.warning("assistant config: cannot write %s: %s", path, exc)
            return False

    def ready(self) -> bool:
        """True when the config can at least be attempted."""
        return bool(self.base_url and self.model)


# ── is the local model there ──────────────────────────────────────────────
NOTE_MISSING = ("ready — simpler mode: the Assistant's small brain ({model}) is not on this PC yet. "
                "The setup on the Suit Mk2 tab fetches it.")
NOTE_UNREACHABLE = ("ready — simpler mode: the local model service is not running, so answers "
                    "are not rephrased. The setup on the Suit Mk2 tab starts it.")


def _ollama_tags(root: str, timeout: float) -> Optional[dict]:
    try:
        with urllib.request.urlopen(root + "/api/tags", timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except Exception:
        return None


def local_model_state(cfg: "LLMConfig", get: Optional[Callable] = None, timeout: float = 1.5) -> str:
    """"" when there is nothing to say, "missing" when the local Ollama answers and does not have
    cfg.model, "unreachable" when it does not answer.

    Only asked when the config is the kind the defaults are: an OpenAI-compatible service on this
    PC at Ollama's port, in a mode that uses a model. Any other service is the player's own setup
    and is not second-guessed here. One short request; never raises. get(root, timeout) -> dict
    or None stands in for the request in tests."""
    if str(cfg.mode or "").strip().lower() == "router" or not cfg.ready():
        return ""
    if str(cfg.provider or "").strip().lower() != "openai":
        return ""
    u = urllib.parse.urlsplit(str(cfg.base_url))
    if (u.hostname or "").lower() not in ("127.0.0.1", "localhost") or u.port != 11434:
        return ""
    tags = (get or _ollama_tags)("http://127.0.0.1:11434", timeout)
    if not isinstance(tags, dict):
        return "unreachable"
    want = str(cfg.model).strip().lower()
    names = {str(m.get("name") or m.get("model") or "").lower()
             for m in tags.get("models") or [] if isinstance(m, dict)}
    return "" if want in names or want + ":latest" in names else "missing"


def model_note(cfg: "LLMConfig", get: Optional[Callable] = None) -> str:
    """The sentence for the panel's status line, or "" when the model is there (or is not ours to check)."""
    state = local_model_state(cfg, get)
    if state == "missing":
        return NOTE_MISSING.format(model=cfg.model)
    return NOTE_UNREACHABLE if state == "unreachable" else ""

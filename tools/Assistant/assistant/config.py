"""LLM endpoint configuration — the plug Elah fills in.

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
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, asdict

log = logging.getLogger(__name__)


def _config_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".sctoolbox",
                        "assistant_llm.json")


@dataclass
class LLMConfig:
    provider: str = "openai"          # "openai" | "anthropic"
    base_url: str = "http://localhost:11434/v1"
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

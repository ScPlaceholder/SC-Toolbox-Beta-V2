"""Toolbox-wide LLM assistant — the brain Elah plugs an LLM into.

Architecture (all optional-dependency gated, stdlib-only core):

  * config      -- LLM endpoint config (~/.sctoolbox/assistant_llm.json)
  * providers   -- chat completions over stdlib urllib:
                   OpenAI-compatible (Ollama / LM Studio / OpenAI / vLLM)
                   and Anthropic-native. One ``chat()`` contract.
  * tools       -- @tool decorator + ToolRegistry: every toolbox feature
                   is exposed to the LLM as a typed, JSON-schema-described
                   callable with an optional human confirmation gate.
  * headless    -- headless data services (trade routes via Trade Hub's
                   UEX client, ship data, market prices) so the LLM can
                   *summon information* without opening any UI.
  * ipc_bus     -- cross-skill command bus: finds live skill IPC files by
                   the launcher's ``sc_toolbox_<skill>_*.jsonl`` convention
                   and pushes commands (e.g. pin a route popup in Trade Hub).
  * agent       -- the conversation loop: user text in, tool calls out,
                   confirmation gate for side-effecting actions, spoken
                   answer back.
  * voice       -- ears (mic + faster-whisper) and mouth (SAPI TTS),
                   adapted from the proven Starmap voice subsystem.
  * builtin_tools -- the reference toolset wiring the above together:
                   ship_info, find_trade_routes, find_market_price,
                   show_route_popup (confirm-gated), open_trade_hub.
  * panel       -- the always-on-top HUD window the user talks to.
"""
from __future__ import annotations

import importlib

# module -> pip package name (same gating pattern as Starmap's voice pkg)
_DEPS = {
    "sounddevice": "sounddevice",
    "faster_whisper": "faster-whisper",
    "numpy": "numpy",
    "pynput": "pynput",
}


def missing_voice_deps() -> list:
    """pip packages missing for ears (mic + whisper + trigger)."""
    out = []
    for mod, pip in _DEPS.items():
        try:
            importlib.import_module(mod)
        except ImportError:
            out.append(pip)
    return out

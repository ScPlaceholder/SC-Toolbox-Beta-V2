#!/usr/bin/env python3
"""
Mining Loadout — standalone GUI process.
Launched by the WingmanAI skill via subprocess using the system Python.
Fetches mining laser, module, and gadget data from UEX Corp API.
IPC via a JSONL temp file (same pattern as Trade Hub).
Requires only Python stdlib + PySide6 + requests (or urllib fallback).
"""
import os
import sys

# Bootstrap project root and skill directory
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')))
from shared.app_bootstrap import bootstrap_skill  # noqa: E402
bootstrap_skill(__file__)

from controllers.app_controller import run_app


def main() -> None:
    from shared.crash_logger import init_crash_logging
    init_crash_logging("mining")
    run_app(sys.argv[1:])


if __name__ == "__main__":
    main()

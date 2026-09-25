"""Per-tool worker process: JSON lines in on stdin, JSON lines out.

Launched by assistant/worker_pool.py as::

    python _worker_main.py <tool_dir> <toolbox_root> <handler_file> <logic_file>

Why a subprocess per tool: the skills reuse top-level package names
(``data``, ``services``, ``core``, ``models``, ``config``), so two tools can
never share one interpreter. Each worker gets ``sys.path = [tool_dir,
toolbox_root, stdlib...]`` and nothing else, which is exactly what the
tool sees when the launcher runs it.

Protocol (one JSON object per line, UTF-8):
    request  {"id": 1, "fn": "find_trade_routes", "args": {...}}
    reply    {"id": 1, "ok": true, "result": {...}}
             {"id": 1, "ok": false, "kind": "tool"|"crash"|"protocol",
              "error": "...", "traceback": "..."}
The first line the worker prints is {"id": 0, "ok": true, "ready": ...}.

Stdout hygiene: the real stdout fd is duplicated for the protocol and fd 1
is pointed at stderr BEFORE any tool code is imported, so a stray print()
in a skill (and C-level writes) can never corrupt the protocol stream.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import traceback


def _load(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    tool_dir, root, handler_file, logic_file = sys.argv[1:5]

    # ── protocol channel, isolated from anything the tool prints ──
    proto_fd = os.dup(1)
    os.dup2(2, 1)
    proto = open(proto_fd, "w", encoding="utf-8", buffering=1, newline="\n")
    sys.stdout = sys.stderr

    def reply(obj: dict) -> None:
        proto.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
        proto.flush()

    # ── sys.path: the tool's view of the world, nothing of ours ──
    here = os.path.dirname(os.path.abspath(__file__))
    assistant_pkg = os.path.dirname(here)
    sys.path[:] = [p for p in sys.path
                   if p and os.path.normcase(os.path.abspath(p)) not in (
                       os.path.normcase(here), os.path.normcase(assistant_pkg))]
    sys.path[0:0] = [tool_dir, root]
    os.chdir(tool_dir)

    try:
        _load(logic_file, "_assist_logic")
        try:  # i18n + path setup exactly like the tool's own entry point
            from shared.app_bootstrap import bootstrap_skill
            bootstrap_skill(os.path.join(tool_dir, "_assistant_worker.py"))
        except Exception:                                   # noqa: BLE001
            traceback.print_exc()
        handler = _load(handler_file, "_assist_handler")
    except BaseException as exc:                            # noqa: BLE001
        reply({"id": 0, "ok": False, "kind": "crash",
               "error": f"worker failed to start: {exc!r}",
               "traceback": traceback.format_exc()[-3000:]})
        return 2
    ToolFail = sys.modules["_assist_logic"].ToolFail
    reply({"id": 0, "ok": True, "ready": True, "pid": os.getpid(),
           "fns": sorted(getattr(handler, "EXPORTS", {}))})

    stdin = sys.stdin.buffer
    while True:
        raw = stdin.readline()
        if not raw:
            return 0
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            reply({"id": -1, "ok": False, "kind": "protocol", "error": f"bad request: {exc}"})
            continue
        rid = req.get("id")
        if req.get("fn") == "__quit__":
            reply({"id": rid, "ok": True, "result": "bye"})
            return 0
        fn = getattr(handler, "EXPORTS", {}).get(req.get("fn"))
        if fn is None:
            reply({"id": rid, "ok": False, "kind": "protocol",
                   "error": f"unknown worker function {req.get('fn')!r}"})
            continue
        try:
            result = fn(**(req.get("args") or {}))
            reply({"id": rid, "ok": True, "result": result})
        except ToolFail as exc:
            reply({"id": rid, "ok": False, "kind": "tool", "error": str(exc)})
        except BaseException as exc:                        # noqa: BLE001
            reply({"id": rid, "ok": False, "kind": "crash",
                   "error": f"{type(exc).__name__}: {exc}",
                   "traceback": traceback.format_exc()[-3000:]})


if __name__ == "__main__":
    sys.exit(main())

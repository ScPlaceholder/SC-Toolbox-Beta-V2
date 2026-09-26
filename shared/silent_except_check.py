"""Ratchet for silent catch-all exception handlers.

A "silent catch-all" is `except:` / `except Exception` / `except BaseException` (or a tuple
containing one) whose body does nothing but pass / continue / `...` / return a constant.
It turns a real bug into "nothing happened". The 2026-09-25 audit found about 600 of them;
most guard UI cleanup and are fine, so they are grandfathered here per file. New code may
not add more.

    python shared/silent_except_check.py            # check against the baseline
    python shared/silent_except_check.py --update   # re-record the baseline (after
                                                    # deliberately fixing or accepting some)

A handler is exempt if the `except` line carries `# noqa: BLE001` (a stated reason).
Exit codes: 0 ok, 1 a file gained silent catch-alls, 2 cannot tell.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "shared" / "silent_except_baseline.json"
EXCLUDE_PREFIXES = ("build/",)
BROAD = {"Exception", "BaseException"}


def _is_broad(node: ast.ExceptHandler) -> bool:
    t = node.type
    if t is None:
        return True
    names = t.elts if isinstance(t, ast.Tuple) else [t]
    return any(isinstance(n, ast.Name) and n.id in BROAD for n in names)


def _is_silent(node: ast.ExceptHandler) -> bool:
    for stmt in node.body:
        if isinstance(stmt, (ast.Pass, ast.Continue, ast.Break)):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue          # `...` or a bare string
        if isinstance(stmt, ast.Return) and (stmt.value is None or isinstance(stmt.value, ast.Constant)):
            continue
        return False
    return True


def count_file(path: Path) -> int:
    src = path.read_text(encoding="utf-8", errors="replace")
    lines = src.splitlines()
    tree = ast.parse(src)
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and _is_broad(node) and _is_silent(node):
            line = lines[node.lineno - 1] if node.lineno - 1 < len(lines) else ""
            if "noqa: BLE001" in line:
                continue
            n += 1
    return n


def tracked_py_files(root: Path = ROOT) -> list[str]:
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=root, capture_output=True, text=True).stdout
    return [f for f in out.split() if not f.startswith(EXCLUDE_PREFIXES)]


def scan(root: Path = ROOT, files=None) -> dict[str, int]:
    counts = {}
    for rel in files if files is not None else tracked_py_files(root):
        try:
            c = count_file(root / rel)
        except (SyntaxError, OSError):
            continue
        if c:
            counts[rel] = c
    return counts


def regressions(current: dict[str, int], baseline: dict[str, int]) -> list[tuple[str, int, int]]:
    return sorted((f, baseline.get(f, 0), n) for f, n in current.items() if n > baseline.get(f, 0))


def main(argv) -> int:
    try:
        current = scan()
    except Exception as e:  # noqa: BLE001 - report "cannot tell" instead of a false pass
        print(f"silent_except_check: CANNOT TELL - {e}")
        return 2
    if "--update" in argv:
        BASELINE.write_text(json.dumps(dict(sorted(current.items())), indent=1) + "\n", encoding="utf-8")
        print(f"silent_except_check: baseline recorded, {sum(current.values())} handlers in {len(current)} files")
        return 0
    if not BASELINE.exists():
        print("silent_except_check: CANNOT TELL - no baseline; run with --update once")
        return 2
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    bad = regressions(current, baseline)
    for f, was, now in bad:
        print(f"  NEW silent catch-all in {f}: {was} -> {now}. Catch the specific error, "
              f"log it, or mark the line '# noqa: BLE001 - <reason>'.")
    fixed = sum(max(0, baseline.get(f, 0) - current.get(f, 0)) for f in baseline)
    if bad:
        return 1
    print(f"silent_except_check: OK ({sum(current.values())} grandfathered, {fixed} fewer than baseline)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

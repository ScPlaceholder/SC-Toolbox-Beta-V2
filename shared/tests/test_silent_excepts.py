"""No NEW silent catch-all exception handlers (see shared/silent_except_check.py)."""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")))

from shared import silent_except_check as S  # noqa: E402

SAMPLE = '''
import logging
def a():
    try:
        x()
    except Exception:
        pass                      # silent catch-all: counted
def b():
    try:
        x()
    except:
        return None               # bare + silent: counted
def c():
    try:
        x()
    except Exception:
        logging.exception("x")    # logs: not counted
def d():
    try:
        x()
    except ValueError:
        pass                      # narrow: not counted
def e():
    try:
        x()
    except Exception:  # noqa: BLE001 - window may already be closed
        pass                      # stated reason: not counted
'''


def test_counts_only_silent_catch_alls(tmp_path):
    p = tmp_path / "sample.py"
    p.write_text(SAMPLE, encoding="utf-8")
    assert S.count_file(p) == 2


def test_regression_is_only_an_increase():
    base = {"a.py": 3, "b.py": 1}
    assert S.regressions({"a.py": 3, "b.py": 1}, base) == []
    assert S.regressions({"a.py": 2}, base) == []            # fixing some is fine
    assert S.regressions({"a.py": 4}, base) == [("a.py", 3, 4)]
    assert S.regressions({"new.py": 1}, base) == [("new.py", 0, 1)]


def test_repo_has_no_new_silent_catch_alls():
    assert S.BASELINE.exists(), "run: python shared/silent_except_check.py --update"
    assert S.main([]) == 0

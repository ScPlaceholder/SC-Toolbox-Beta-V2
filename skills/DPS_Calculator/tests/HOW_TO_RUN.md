# Running these tests

```
C:\Users\prjgn\AppData\Local\Programs\Python\Python313\python.exe -m pytest tests -q
```

## Use Python 3.13. Not 3.10, not 3.11.

Measured 2026-09-26 on this machine:

| interpreter | PySide6 | pytest |
|---|---|---|
| Python310 | absent | — |
| Python311 | absent | — |
| **Python313** | **present** | **present** |

`python` on PATH resolves to **3.10**, which has neither. So the obvious command
collects nothing useful and reports `ModuleNotFoundError: No module named 'PySide6'`.

⛔ **THAT ERROR IS ABOUT THE INTERPRETER, NOT THE CODE, AND IT DOES NOT LOOK LIKE IT.**
A whole night's worth of suites got run under 3.10 and under-reported, and the reports
carried an honest-sounding caveat — "PySide6 unavailable in this environment, so the UI
tests could not run" — which reads like a careful limitation and was actually a wrong
interpreter one directory away. The caveat is what made it survive: it turned a fixable
mistake into a stated constraint, and nobody audits a stated constraint.

⇒ Before writing "could not run in this environment", check whether another interpreter
on the same box can. Here, one could.

## What a real run looks like

Three suites, run separately because several skills define top-level `services` and
`tests` packages and collide when imported into one process. That collision is an
artefact of the harness, **not a broken skill** — isolate the processes and they pass.
If two skills "fail to import" at once, suspect that before suspecting the code.

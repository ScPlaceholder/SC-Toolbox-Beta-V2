# SuitMk2 voices

This folder holds the two fine-tuned Piper voices SuitMk2 speaks with:

```
voices/
  elah.onnx          ~63 MB   Elah, the suit AI      (fine-tuned from en_US-lessac-medium)
  elah.onnx.json     ~7 KB    Piper config for it
  montaigne.onnx     ~63 MB   Montaigne, the ship AI (fine-tuned from en_GB-alan-medium)
  montaigne.onnx.json ~7 KB
```

**The `.onnx` files are not committed.** They are build artefacts, about 127 MB together, and live in
the voice lab. `build\build_installer.bat` copies them in when it stages the tool:

- Source folder: `SUITMK2_VOICES_SRC`. If it is unset, the default is
  `%USERPROFILE%\Projects\elah-audio\voice_lab\piper_voices`.
  To override it: `set SUITMK2_VOICES_SRC=D:\path\to\voices`, then run the build.
- The build copies only `<name>.onnx` and `<name>.onnx.json` for `elah` and `montaigne`. It never copies
  the comparison `.ogg` clips or the `checks\` folder that sit next to them in the lab.
- The build **fails** (Step 7c) if either voice is missing. Without them the tool quietly falls back to
  the stock Piper voices, which it downloads from Hugging Face into `~/.cache/piper` the first time it
  speaks. To ship on purpose without the trained voices, set `SUITMK2_ALLOW_STOCK_VOICES=1`. The build
  then prints a warning and carries on.

## Runtime lookup

`core/speech.py` loads `<voices_dir>/<speaker>.onnx` when both it and its `.onnx.json` exist, and uses the
stock voice otherwise. For an installed build, `voices_dir` has to resolve to this folder. Today the
`voices_dir` default in `core/settings.py` is a dev-machine path, so it needs a follow-up change
(pending, owned by whoever is editing `core/`):

```python
TOOL_DIR = Path(__file__).resolve().parent.parent
"voices_dir": str(TOOL_DIR / "voices"),
```

Keep the lab path as a user override in `~/.sctoolbox/suitmk2/settings.json`, not as the default. Until
that change lands, the build's home-path check in Step 7c fails on purpose.

## Replacing a voice

Put the new `<name>.onnx` and `<name>.onnx.json` in the source folder and rebuild. On a running install,
you can also drop the pair straight into this folder. `Speech.reload()` picks it up.

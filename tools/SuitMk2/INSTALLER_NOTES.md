# SuitMk2: what the SC Toolbox installer must stage

None of these files are in git (`*.exe` and `*.onnx` are gitignored, and the GGUF deltas are too large). A build that
only copies the git tree ships a SuitMk2 that installs cleanly but has no character brains and no game ears.
`build_installer.bat` has to copy them into `%STAGE%\tools\SuitMk2\` from a source folder, the same way it already
stages the Piper voices from `SUITMK2_VOICES_SRC`.

| Stage into `tools\SuitMk2\` | Size | Why |
|---|---|---|
| `models\elah.delta.gguf` | ~164 MB | Elah's character. It is not a full model and not a LoRA: it is the LoRA pre-merged into the attention tensors (Q8_0). On first run the Setup panel pulls stock `qwen2.5:1.5b` through the local Ollama API and splices this delta into it (`core/model_provision.py` + `core/gguf_stitch.py`) to make `suitmk2-elah`. Ollama 0.34 refuses LoRA adapters outright, so this is the only format that works. |
| `models\montaigne.delta.gguf` | ~164 MB | Same, for `suitmk2-montaigne`. |
| `models\manifest.json` | <1 KB | sha256 and size of each delta. The provisioner checks it before uploading anything, so a truncated or damaged install says "reinstall the tool" and doesn't build a subtly broken character. Regenerate it with `python core/model_provision.py --write-manifest` whenever a delta is rebuilt. |
| `bin\sc_audio_tap.exe` | ~156 KB | The game-ears capture helper: per-process loopback of `StarCitizen.exe` only (built from `tools/sc_audio_tap`, Rust). Without it `sound_classifier.py` degrades to the loudness meter, so combat confirmation gets weaker. |
| `models\yamnet\yamnet.onnx`, `yamnet.data`, `labels.txt`, `SOURCE.json` | ~15 MB | The YAMNet sound classifier that runs on the CPU over the tap's audio. `SOURCE.json` records provenance, hashes and licence (MIT wrapper, Apache-2.0 weights) and should ship with the model. |

## Checks worth adding to the build's validation step

- **Fail if a delta is missing** unless an explicit opt-out is set (e.g. `SUITMK2_ALLOW_NO_MODELS=1`). A missing delta means that character stays silent forever, and nothing at runtime can recover it.
- **Fail if any file under `models\` is over ~400 MB.** The largest legitimate one is a delta at ~164 MB. A full 1-2 GB model must never ride along by accident. The whole point of the delta design is that the player downloads the stock base from Ollama's registry, not from us.
- **Don't ship `core\build_character_delta.py`.** It's a build-time helper that needs numpy and llama.cpp's gguf-py.
- **Don't ship `tools\sc_audio_tap\`.** That's Rust source plus a `target\` build directory. Only `bin\sc_audio_tap.exe` ships.
- **Keep dropping `*.bak_*`** as the build already does. The dev tree accumulates them.

## What the installer does NOT need to do

The installer doesn't install Ollama, pull any model or create any Ollama model. All of that happens inside the tool
on first run, behind one "Set up Elah and Montaigne (about 1.9 GB)" click (or with no click if `auto_setup` is
turned on in `~/.sctoolbox/suitmk2/settings.json`). The player never touches Ollama.

The earlier draft patch for this (`core/build_installer_models.patch`, 2026-09-23 19:36) staged only the two deltas
and the manifest. It did not stage `sc_audio_tap.exe` or YAMNet, which came later. Use this list, not the patch.

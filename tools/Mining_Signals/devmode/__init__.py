"""Mining Signals Dev Mode — user-side OCR training pipeline.

capture -> label (engines propose, human confirms) -> approve glyphs ->
synth -> train per region kind -> benchmark on a held-out set ->
activate only if better / revert -> export a failure zip.

The public surface is ``devmode.api``. Everything the user produces lives
under ``devmode.paths.dev_root()`` (outside the install folder, which the
updater wipes); shipped models are never written.
"""

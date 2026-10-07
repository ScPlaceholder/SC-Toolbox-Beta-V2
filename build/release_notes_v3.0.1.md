## SC Toolbox v3.0.1

### Fixed

- **Suit Mk2 / Assistant setup now works on a PC that does not have Ollama.** In 3.0.0 the setup downloaded the Ollama installer and then stopped at 35% with "Setup did not finish" and "installer signature rejected". That was a bug in the Toolbox's own safety check, not a problem with your PC or the download. Thanks to the player who reported it.
- If that part ever fails again, the message now tells you what to do instead of "try again".

### New: your own voices

- **Elah and Montaigne can each speak with a voice file you choose.** On the Suit Mk2 tab, under the volume sliders, "Elah voice" and "Montaigne voice" offer Built-in, any voice files in your voices folder (`%USERPROFILE%\.sctoolbox\suitmk2\voices`), and Browse. The change applies at once. A voice file is a Piper voice: an `.onnx` with its `.onnx.json` beside it.
- If a file cannot be used, the companion keeps its built-in voice and the window says which file and why.
- **The Assistant** uses the same voice file when it speaks as Elah or Montaigne, and its Settings now has a "Windows voice" list of the voices installed on your PC.

### New: Elah knows the Galactapedia

- Ask Elah about the place you are standing in and she can now answer from that place's Galactapedia article, after what she already knew. 48 facts for 32 places, each a sentence taken from the article and kept with its link. Nothing is looked up online while you play.

### If you are on 3.0.0

- Download `SC_Toolbox_Setup_3.0.1.exe` below and run it over your install. Your settings are kept.
- Or press **NEW v3.0.1** in the launcher, then **Update Now**. That button failed for everyone on 3.0.0 ("Failed to remove existing application directory") until the installer on this page was replaced on 7 October at 18:50 UTC. If it failed for you, press it again.
- If an install still stops with that message, another program has a folder inside the Toolbox open (a terminal, an editor, a file window). Close it and run the installer again.
- Already installed Ollama yourself to get around the bug? Nothing more to do.

### Also in this release

- `SC_Toolbox-win-Portable.zip`: the Toolbox as a folder you unpack and run, with no installer.

### Known

- The Ollama fix was checked by running the real signature check on a signed file. Installing Ollama itself through the Toolbox was not run end to end here, because every test PC already has Ollama. If setup still fails for you, install Ollama from [ollama.com](https://ollama.com), press Try again, and tell us what the small grey line said.
- Custom voices were tested with the Toolbox's own voice files. Other Piper voices (other languages, other sample rates) have not been tried yet.
- A few Galactapedia sentences differ in detail from the lore Elah already had (for example how Lyria's atmosphere is described). Both are given as their sources state them.

Everything else is as in [v3.0.0](https://github.com/ScPlaceholder/SC-Toolbox-Beta-V2/releases/tag/v3.0.0).

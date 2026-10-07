## SC Toolbox v3.0.1

One fix.

- **Suit Mk2 / Assistant setup now works on a PC that does not have Ollama.** In 3.0.0 the setup downloaded the Ollama installer and then stopped at 35% with "Setup did not finish" and "installer signature rejected". That was a bug in the Toolbox's own safety check, not a problem with your PC or the download. Thanks to the player who reported it.
- If that part ever fails again, the message now tells you what to do instead of "try again".

### If you are on 3.0.0

- The Toolbox updates itself: start it, close it, start it again.
- Or download `SC_Toolbox_Setup_3.0.1.exe` below and run it over your install.
- Already installed Ollama yourself to get around the bug? Nothing more to do. Press the setup button on the Suit Mk2 tab if you have not yet.

### Known

- The fix was checked by running the real signature check on a signed file. Installing Ollama itself through the Toolbox was not run end to end here, because every test PC already has Ollama. If setup still fails for you, install Ollama from [ollama.com](https://ollama.com), press Try again, and tell us what the small grey line said.

Everything else is as in [v3.0.0](https://github.com/ScPlaceholder/SC-Toolbox-Beta-V2/releases/tag/v3.0.0).

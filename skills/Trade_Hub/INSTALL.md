# Trade Hub — Installation Guide

Trade Hub is a WingmanAI skill that shows live Star Citizen trade routes in a floating HUD window.
It needs two things installed before it will work: **Python** and the **requests** package.

---

## Step 1 — Install Python

Trade Hub runs its GUI window using Python (separate from the one WingmanAI uses internally).
You need a standard Python install that includes **tkinter** — the Windows installer includes it by default.

> **Do NOT install Python from the Microsoft Store.** The Store version is a stub that breaks tkinter.

1. Go to **https://www.python.org/downloads/windows/**
2. Click the big yellow **"Download Python 3.x.x"** button (latest stable is fine)
3. Run the installer
4. **IMPORTANT:** On the first screen, check the box that says **"Add Python to PATH"**
5. Click **"Install Now"**
6. Wait for it to finish, then click **Close**

To verify it worked, open **Command Prompt** (press `Win+R`, type `cmd`, press Enter) and run:

```
python --version
```

You should see something like `Python 3.12.4`. If you get an error, restart your PC and try again.

---

## Step 2 — Install the requests package

Trade Hub uses the `requests` library to download live trade data from the UEX Corp API.

1. Open **Command Prompt** (press `Win+R`, type `cmd`, press Enter)
2. Copy and paste this command, then press Enter:

```
pip install requests
```

3. Wait for it to finish — you should see `Successfully installed requests-x.x.x`

That's it. There are no other packages to install.

---

## Step 3 — Add the skill to WingmanAI

1. Copy the entire **Trade_Hub** folder into your WingmanAI custom skills directory:
   ```
   C:\Users\<YourName>\AppData\Roaming\ShipBit\WingmanAI\custom_skills\
   ```
   (Replace `<YourName>` with your Windows username)

   > **Tip:** To open this folder quickly, press `Win+R`, paste the path above, and press Enter.

2. Open **WingmanAI**
3. Go to your Wingman's settings and click **"Add Skill"**
4. Find **Trade Hub** in the list and enable it
5. Restart the Wingman (or click Reload)

The Trade Hub window should appear. If it doesn't, say **"Show trade hub"** to your Wingman.

---

## Troubleshooting

**"No system Python with tkinter found" in WingmanAI logs**
- You installed Python from the Microsoft Store — uninstall it and reinstall from python.org (Step 1 above)
- Or Python was installed but not added to PATH — re-run the installer and check "Add Python to PATH"

**"ModuleNotFoundError: No module named 'requests'"**
- You skipped Step 2 — open Command Prompt and run `pip install requests`

**The window opens but shows no data**
- Check your internet connection — Trade Hub fetches live data from https://uexcorp.space/api/
- Click the **⟳** refresh button in the top-right corner of the Trade Hub window

**WingmanAI can't find the skill**
- Make sure the folder is named exactly `Trade_Hub` (capital T, capital H, underscore)
- Make sure `main.py` is inside that folder

---

## Useful Links

| Resource | Link |
|----------|------|
| Python downloads | https://www.python.org/downloads/windows/ |
| WingmanAI | https://www.wingman-ai.com/ |
| WingmanAI Discord | https://discord.gg/wingman-ai |
| UEX Corp (trade data source) | https://uexcorp.space/ |
| Cabal (companion tool) | https://github.com/cabal-sc/cabal |

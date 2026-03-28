# Mining Loadout — Installation Guide

Mining Loadout is a WingmanAI skill that shows an interactive mining equipment
calculator in a floating overlay window, mirroring the Regolith calculator
(regolith.rocks/loadouts/calculator) with live UEX Corp item data.

It needs two things installed before it will work: **Python** and the **requests** package.

---

## Step 1 — Install Python

Mining Loadout runs its GUI window using Python (separate from the one WingmanAI uses internally).
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

Mining Loadout uses the `requests` library to download live equipment data from the UEX Corp API.

1. Open **Command Prompt** (press `Win+R`, type `cmd`, press Enter)
2. Copy and paste this command, then press Enter:

```
pip install requests
```

3. Wait for it to finish — you should see `Successfully installed requests-x.x.x`

That's it. There are no other packages to install.

---

## Step 3 — Add the skill to WingmanAI

1. Copy the entire **Mining_Loadout** folder into your WingmanAI custom skills directory:
   ```
   C:\Users\<YourName>\AppData\Roaming\ShipBit\WingmanAI\custom_skills\
   ```
   (Replace `<YourName>` with your Windows username)

   > **Tip:** To open this folder quickly, press `Win+R`, paste the path above, and press Enter.

2. Open **WingmanAI**
3. Go to your Wingman's settings and click **"Add Skill"**
4. Find **Mining Loadout** in the list and enable it
5. Restart the Wingman (or click Reload)

The Mining Loadout window should appear. If it doesn't, say **"Show mining loadout"** to your Wingman.

---

## Using the Calculator

Once the window is open you can use it manually (click dropdowns to change equipment)
or by voice command through WingmanAI:

| Voice command example | What it does |
|----------------------|--------------|
| "Switch to MOLE" | Changes ship to MOLE (3 turrets, size-2 lasers) |
| "Put a Hofstede S1 on the main turret" | Equips Hofstede S1 Mining Laser on turret 0 |
| "Equip Brandt module in slot 0" | Adds Brandt module to first slot of turret 0 |
| "Add Lifeline to port turret slot 1" | MOLE: adds Lifeline to Port Turret slot 1 |
| "Put a Gastropod gadget on" | Equips Gastropod gadget |
| "Reset my loadout" | Clears all modules, gadgets, restores stock lasers |
| "Refresh mining data" | Re-fetches latest UEX Corp item data |
| "Close mining loadout" | Hides the window |

### Hotkey
Default hotkey to toggle the window: **Ctrl+Shift+M**
This can be changed in the app settings or by editing `mining_loadout_config.json`.

---

## Troubleshooting

**"No system Python with tkinter found" in WingmanAI logs**
- You installed Python from the Microsoft Store — uninstall it and reinstall from python.org (Step 1 above)
- Or Python was installed but not added to PATH — re-run the installer and check "Add Python to PATH"

**"ModuleNotFoundError: No module named 'requests'"**
- You skipped Step 2 — open Command Prompt and run `pip install requests`

**The window opens but shows no equipment data / everything is empty**
- Check your internet connection — Mining Loadout fetches live data from https://uexcorp.space/api/
- Click the **⟳** refresh button in the top-right corner of the window
- Check `mining_loadout.log` in `%TEMP%` for API error details

**WingmanAI can't find the skill**
- Make sure the folder is named exactly `Mining_Loadout` (capital M, capital L, underscore)
- Make sure `main.py` is inside that folder

**Stats don't match Regolith exactly**
- UEX Corp item data updates independently of Regolith — there may be brief discrepancies
  after game patches while data sources sync. Click ⟳ to refresh to the latest UEX data.

---

## Useful Links

| Resource | Link |
|----------|------|
| Python downloads | https://www.python.org/downloads/windows/ |
| Regolith Calculator | https://regolith.rocks/loadouts/calculator |
| UEX Corp (data source) | https://uexcorp.space/ |
| WingmanAI | https://www.wingman-ai.com/ |
| WingmanAI Discord | https://discord.gg/wingman-ai |

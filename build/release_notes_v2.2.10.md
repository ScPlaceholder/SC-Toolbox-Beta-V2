## What's New in v2.2.10

A focused follow-up to v2.2.9. It fixes a signature-scanner bug that silently broke the scanner for some users, hardens Mining Signals config handling, corrects the installer's version display, and ships a more accurate DPS Calculator.

### Mining Signals — signature scanner fix

After the v2.2.9 OCR overhaul, the signature scanner could still silently fail to read the signal value on some setups. The cause was a bug in the primary recognition path: a leftover variable reference (`field`) — copy-pasted from the HUD mineral-row OCR, where it is a real parameter — raised `NameError: name 'field' is not defined` partway through the signature scan. A broad exception handler swallowed it at debug level, so the primary CNN voter simply dropped out of the consensus and the scan fell through to weaker readers, or returned nothing at all. That reference is removed, and that class of programming error is no longer hidden — it now surfaces as a warning in the log.

Alongside the fix:

- **Scanner tutorial corrected.** The in-app setup instructions told you to *exclude* the location-pin icon from the scanner region. The scanner uses that icon as its alignment anchor — excluding it made the scan silently return nothing. The tutorial now tells you to include the icon **and** the value digits.
- **Region selector.** Drawing a too-small capture region used to be silently discarded with no feedback. The minimum size was lowered and a warning dialog now explains when a region is too small to scan reliably.
- **Config path consolidation.** The main app and the Signature Finder pop-out now resolve the config file through one shared module, removing a path-divergence bug where the two processes could read and write different files.
- **Louder diagnostics.** Config load now logs where it read from, whether that location is writable, and the active scan regions — so a misconfigured setup is visible in the log instead of failing quietly.

### DPS Calculator — more accurate slot extraction

The DPS Calculator ships an updated ship-loadout slot extractor with a batch of turret- and hardpoint-handling fixes: multi-gun turret housings are grouped into a single weapon slot, point-defence and tractor-beam mounts are classified correctly, missile racks and torpedo trays count their loaded sub-slots properly, and power-plant / cooler / shield / radar slots are inferred more reliably. Validated against erkul.gg and scunpacked ground-truth data.

### Installer

The installer now reads its displayed version from its own assembly, so the welcome screen, header, and progress text always show the version actually being installed. (The 2.2.8 and 2.2.9 installers showed an older version number even though they installed the correct build.)

---

**Download**: `SC_Toolbox_Setup_2.2.10.exe` below (~1.7 GB; bundled installer with custom progress UI)
**Discord**: https://discord.gg/D3hqGU5hNt
**Issues**: https://github.com/ScPlaceholder/SC-Toolbox-Beta-V2/issues

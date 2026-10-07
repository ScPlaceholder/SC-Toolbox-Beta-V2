# Releasing SC Toolbox

How a release is built and published, for J, for Red, and for whichever Elah session does it next.
`BUILD_README.txt` next to this file describes the older Inno-only build; this file is the current process.
Last checked against the code and the GitHub release assets: 2026-09-25.

## What ships

Two things, and both matter:

| Piece | Who uses it | Built by |
|---|---|---|
| `SC_Toolbox_Setup_<ver>.exe`: J's custom installer (WPF, rotating backgrounds, tips, progress bar) | New users | `dotnet publish` of `build/installer_ui` (stage 2) |
| The Velopack update feed: `releases.win.json`, `RELEASES`, `assets.win.json`, `SC_Toolbox-<ver>-full.nupkg`, `SC_Toolbox-<ver>-delta.nupkg` | Existing users. The `SC_Toolbox.exe` launcher (`build/launcher/Program.cs`) checks GitHub on every start and downloads only what changed | `vpk pack` (stage 1) |

The custom installer does not install anything itself. It carries Velopack's own
`SC_Toolbox-win-Setup.exe` inside it, runs it with `--silent`, and shows J's UI over the top.

> **The 2.3.x lesson.** v2.3.0 and v2.3.1 were published with only the setup `.exe`. With no
> update feed on the latest release, the launcher's auto-update found nothing and swallowed the
> error: everyone on 2.2.x silently stopped getting updates. The feed files for 2.3.1 were in fact
> built (they are still in `build/Releases`); they were just never uploaded.
> `build/release_check.py` exists so this cannot happen again.

## Where the version lives

**Only `pyproject.toml` (`version = "x.y.z"`).**
- `build/read_version.py` feeds it to `vpk pack`.
- The installer's `.csproj` reads it at build time (`SyncVersionFromPyproject` target) and refuses
  to build if it can't.
- `build/SC_Toolbox_Installer.iss` (old Inno path, unused) and the strings in
  `build/installer_ui/MainWindow.xaml` (overwritten at runtime) are cosmetic. Bump them anyway, to
  keep things tidy.

## Tools on the build PC

- Inno Setup 6: `%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe`. `build_installer.bat` checks for it
  at the top even in Velopack mode.
- `vpk` (Velopack CLI) in `~/.dotnet/tools`. It targets .NET 9; this PC has .NET 8 and 10, so it
  needs `DOTNET_ROLL_FORWARD=Major` (the build script already sets it).
- .NET SDK 10 (builds the net8 installer fine).
- `gh`, logged in as ScPlaceholder.

## Build (nothing is published)

1. Bump `pyproject.toml`.
2. **Stage 1, the Velopack payload.** Run by FULL path, since the script locates itself:
   `C:\...\SC_Toolbox_Beta_V1.2\build\build_installer.bat velopack`
   It stages the app from the WORKING TREE (not git), runs `vpk pack` into `build/Releases`, and
   runs `build/staging_import_test.py`, which imports every tool's entry script under the bundled
   Python and fails the build on a broken import. For a delta to be made, the previous release's
   full `.nupkg` must be in `build/Releases`; 2.3.1's is there today.
3. **Stage 2, J's installer.**
   `dotnet publish build\installer_ui\SC_Toolbox_Installer.csproj -c Release -o build\installer_ui\publish`
   It embeds whatever `Releases\SC_Toolbox-win-Setup.exe` exists, so stage 1 must finish first.
4. **Check:** `python build/release_check.py`. READY means the versions agree, the feed lists this
   version, the full package matches its SHA256 in the feed, and the installer is fresh and was
   built after the payload it embeds. On READY it prints every file to attach. NOT READY lists what
   is missing.

## Publish (J's call, every time)

Releases are hosted on **ScPlaceholder/SC-Toolbox-Beta-V2**. That repo is the updater's source
(`Program.cs` GithubSource) and the Python update check's source.

1. Copy `build/installer_ui/publish/SC_Toolbox_Setup.exe` to
   `build/Releases/SC_Toolbox_Setup_<ver>.exe`.
2. Create the release as a draft, then upload everything `release_check.py` printed:
   `gh release upload v<ver> RELEASES releases.win.json assets.win.json SC_Toolbox-<ver>-full.nupkg SC_Toolbox-<ver>-delta.nupkg SC_Toolbox_Setup_<ver>.exe --repo ScPlaceholder/SC-Toolbox-Beta-V2 --clobber`
   (Prefer `gh` over `vpk upload github`: vpk also wants the portable zip and Velopack's own setup,
   and fails if they were pruned.)
3. `gh release edit v<ver> --notes-file build/release_notes_v<ver>.md`, then
   `gh release edit v<ver> --draft=false --latest`.
4. Known broken: the Discord announce workflow fails every release because the `DISCORD_WEBHOOK`
   secret is missing on the V2 repo.

## How updating works for users

- **Automatic (Velopack):** `SC_Toolbox.exe` checks the latest release's feed on each start,
  downloads the delta (or the full package), and applies it on the next launch. This path needs
  the feed files on the release.
- **UPDATE button in the launcher window (Python):** `shared/update_checker.py` finds the newest
  `.exe` on the latest release; `ui/main_window.py` downloads it and runs it (the setup), then
  closes the toolbox. A green "NEW vX.Y.Z" tag next to the button shows when an update exists.
  Until 2026-09-25 this button tried to unzip the `.exe` and always failed.

## Install location

Default: `%LOCALAPPDATA%\SC_Toolbox`, per user, no admin needed.

The custom installer (commit a9e1f66) has an **Install to** row with a Browse button:
- `build/installer_ui/InstallPaths.cs` is the only place that decides the folder:
  - An existing install's `InstallLocation` from
    `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\SC_Toolbox` wins; failing that, a
    legacy default install counts; failing that, the user's choice; failing that, the default.
  - Picking `D:\Games` installs to `D:\Games\SC_Toolbox`.
- **Existing installs cannot be moved.** Updates go where the app already is, and the row says so.
  To move it, uninstall first.
- Velopack's setup gets `--installto <dir>` only when the target is not the default, so the default
  path behaves exactly as before.
- **A new folder is refused if:** it isn't a full path, it's inside Program Files without admin,
  it isn't writable, its drive has less than 4 GB free, or it already holds unrelated files (Velopack
  appears to clean the target folder).

**Not yet tested with a real install** (as of 2026-09-25): whether Velopack records the custom folder
as `InstallLocation`, a later update in place at a custom folder, the shortcut and Launch from a
custom folder, and the window's look. It can't be tested on J's main PC as-is, because an existing
2.2.15 install there takes priority. Use a machine without SC Toolbox installed.

## What stopped the 3.0.0 build and install test (2026-10-06)

Each of these cost a full run. Check them before the next release.

- **A test pack of the same version blocks the real pack.** `vpk pack` refuses with "There is a
  release in channel win which is equal or greater to the current version" when `build/Releases`
  already holds that version. Before re-packing, move the `SC_Toolbox-<version>-*.nupkg`,
  `SC_Toolbox-win-Setup.exe` and `SC_Toolbox-win-Portable.zip` out of `build/Releases`, and remove
  that version's entries from `releases.win.json` and its line from `RELEASES`.
- **A file that held bytes in the last release and is empty now breaks the delta.** The build runs
  `build/fix_emptied_files.py` before `vpk pack` for this. It handles Python source and stops the
  build for any other kind of file.
- **Paths over 254 characters.** The staging loops use robocopy and leave `.claude`, `.git` and the
  Python caches out. xcopy gave up on such paths and left folders half copied.
- **Run the installer on a real PC before publishing.** The first 3.0.0 install test failed with
  "Setup.exe exited with code 1" on a PC where 2.2.15 had been uninstalled:
  - 2.2.15 and older shipped a `.git` folder inside Mining Signals. Its pack files are read-only,
    so the uninstaller left them and the installer's leftover wipe could not remove them. The wipe
    now clears the read-only flag first.
  - After one failed attempt the default folder no longer looked like an install and was refused
    as "contains other files". The default folder is no longer refused.
  - A leftover that a running program holds open (a terminal or editor sitting in the folder) still
    stops the install. The installer now names the folder and asks the user to close the program.
- **The leftover wipe deletes the whole install root, including `mining_signals\`**, which is where
  Mining Signals keeps its settings between updates (`mining_shared/paths.py`). On the test PC this
  removed the user's settings. Copy that folder somewhere before any install test on a PC with a
  broken or removed install.
- **Test the upgrade, not only the fresh install.** Install an older release first (an old
  `SC_Toolbox_Setup_<ver>.exe` from `build/Releases` will do), then run the new installer over it.
  That is what every existing user does, and on 3.0.0 it failed every time until it was tried: the
  installer measured the install folder for its progress bar while Setup.exe was trying to rename
  that folder aside. Put a marked `mining_signals\config.json` in the old install and check it is
  unchanged afterwards. Setup.exe and the uninstaller both clear `mining_signals\`; the installer
  copies it to `SC_Toolbox_userdata_backup` beside the install folder and puts it back.
- **When an install fails, read the Setup log.** The installer passes `--log` and writes
  `%TEMP%\SC_Toolbox_setup_<ver>.log`; the failure screen shows its last error line and the path.
- **A failed upgrade can leave `SC_Toolbox_<random letters>` folders in `%LOCALAPPDATA%`.** They are
  Setup.exe's rollback copies of the previous install, each the size of a full install.

## Small updates fail when a release adds a folder (found 2026-10-07)

Velopack 0.0.1298, the version 3.0.0 ships, cannot apply a delta package that adds a file in a
folder the installed version does not have. `Update.exe patch` stops with "The system cannot find
the path specified. (os error 3)" at the first such file. A new file in an existing folder is fine.

How it was checked: a four-file test app, packed twice with `vpk`, the second version adding one
file in an existing folder and one in a new folder, then
`Update.exe patch --old <1.0.0-full> --delta <1.0.1-delta> --output <file>`.

| Updater that applies the delta | Result |
|---|---|
| 0.0.1298 (inside every 3.0.0 install) | fails, whichever `vpk` packed the delta |
| 1.2.161 | rebuilds the full package, whichever `vpk` packed the delta |
| 0.0.1298, same delta without the new folder | rebuilds the full package, contents identical |

The real 3.0.0 delta fails the same way, at `av-19.0.1.dist-info`.

What follows from it:

- The updater inside the installed copy applies the delta. So the first update after 3.0.0 is a
  full download (about 1.8 GB) if it adds any folder, whatever is changed in the build.
- Moving the launcher's `Velopack` package and the `vpk` tool to 1.2.161 or later in that update
  fixes the ones after it. That move was not tried on the Toolbox itself, only on the test app.
- Not observed: the in-app updater falling back to the full package after a failed delta. It is
  documented to; nobody has watched it do so here.


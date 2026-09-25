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

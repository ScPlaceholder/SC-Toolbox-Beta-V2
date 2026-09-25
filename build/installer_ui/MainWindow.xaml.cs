// MainWindow — single-page installer with cross-fading background images
// and a status overlay panel.
//
// Background images live in Resources\Backgrounds\ and ship embedded in
// the assembly. To swap the art, drop new .jpg/.png files in there and
// rebuild — the .csproj's <Resource Include> picks them up automatically.
//
// At launch we Fisher-Yates shuffle the deck so the order varies between
// runs. Each tick (every BG_ROTATION_SECONDS) we cross-fade from the
// currently visible image to the next in the shuffled deck. When the
// deck is exhausted, we re-shuffle (avoiding the immediate previous
// image, so no awkward repeat at the seam).
//
// Tips rotate independently on a longer interval so they don't sync up
// with the image flips (cleaner visual rhythm).

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Media.Imaging;
using System.Windows.Threading;

namespace SC_Toolbox.Installer;

public partial class MainWindow : Window
{
    private const int BG_ROTATION_SECONDS = 10;
    private const int TIP_ROTATION_SECONDS = 11;
    private const int FADE_DURATION_MS = 1400;

    private readonly List<BitmapImage> _backgrounds = new();
    private readonly DispatcherTimer _bgTimer = new();
    private readonly DispatcherTimer _tipTimer = new();
    private readonly Random _rng = new();
    private List<int> _shuffleQueue = new();   // current randomized play order (indices into _backgrounds)
    private int _shuffleCursor = 0;            // next position in _shuffleQueue to consume
    private int _currentBgIndex = -1;          // last-shown image index (avoid repeat at re-shuffle seam)
    private bool _swap = false;                // false → BgPrimary visible; true → BgSecondary visible

    // Tips shown above the status panel during install.
    private static readonly string[] _tips = new[]
    {
        "Tip · Press Ctrl+1 to toggle the Mining Signals overlay while in-game.",
        "Tip · The first scan is slow (cold-start ML models) — subsequent scans take ~1 second.",
        "Tip · Click 'Calibrate Mining Crops' if values look off — re-locks the OCR rows.",
        "Tip · The signature/instability scanner requires the SCAN RESULTS panel visible.",
        "Tip · Mining Signals reads mass, resistance, and instability — feeds the chart bubble.",
        "Tip · Updates ship as small deltas — no more redownloading the full installer.",
        "Tip · 'Set Mining HUD Region' draws a box around the SCAN RESULTS panel for OCR.",
    };
    private int _tipIndex = 0;

    // Track the Setup.exe subprocess so we can kill it if the user
    // cancels or closes our window — otherwise it keeps running as a
    // zombie blocked on hidden state, and the next install attempt
    // collides with it.
    private Process? _setupProcess;

    public MainWindow()
    {
        InitializeComponent();
        // Stamp every displayed version string from INSTALLER_VERSION
        // (which reads the assembly <Version> set in
        // SC_Toolbox_Installer.csproj) so the UI can never drift from
        // the version we actually ship.  Before the v2.2.10 audit these
        // three strings were hardcoded in MainWindow.xaml and still read
        // "v2.2.7" — meaning the 2.2.8 AND 2.2.9 installers displayed the
        // wrong version to every user even though the install LOGIC
        // (INSTALLER_VERSION) was correct.  Setting them here, right
        // after InitializeComponent() creates the named elements, gives
        // the whole installer one single source of truth.
        VersionText.Text = "v" + INSTALLER_VERSION;
        WelcomeTitle.Text = $"Welcome to SC Toolbox v{INSTALLER_VERSION} setup";
        StatusTitle.Text = $"Installing SC Toolbox {INSTALLER_VERSION}";
        Loaded += OnLoaded;
        Closing += OnClosing;
    }

    private void OnClosing(object? sender, System.ComponentModel.CancelEventArgs e)
    {
        // Best-effort cleanup of the Velopack subprocess.
        try
        {
            if (_setupProcess != null && !_setupProcess.HasExited)
            {
                _setupProcess.Kill(entireProcessTree: true);
            }
        }
        catch { /* already gone — fine */ }
    }

    private void OnLoaded(object sender, RoutedEventArgs e)
    {
        LoadBackgrounds();
        StartBackgroundRotation();
        StartTipRotation();

        // UX gate (added v2.2.6): the installer used to auto-start the
        // install on window-open, which startled users who hadn't yet
        // confirmed they wanted to install. Now we present a Welcome
        // panel and wait for an explicit Install click.
        //
        // BUT: if the same version is already installed, there's nothing
        // to ask permission for — skip the welcome and jump straight to
        // the "ALREADY INSTALLED → Launch" celebration so the user can
        // just click Launch. This preserves the previous already-installed
        // shortcut path. StartInstallAsync handles all of that already.
        if (DetectExistingInstallVersion() == INSTALLER_VERSION)
        {
            // Hide the welcome panel + show the status panel directly so
            // EnterCompletionState (called inside StartInstallAsync) has
            // somewhere to render the celebration.
            WelcomePanel.Visibility = Visibility.Collapsed;
            StatusPanel.Visibility = Visibility.Visible;
            _ = StartInstallAsync();
            return;
        }

        // Fresh install — wait for the user to click Install. The button's
        // Click handler (InstallButton_Click) calls StartInstallAsync().
    }

    private void InstallButton_Click(object sender, RoutedEventArgs e)
    {
        // User has consented — swap Welcome → Status panel, then kick off
        // the install. The desktop-shortcut checkbox on the Welcome panel
        // remains in the visual tree (we just hide its parent), so its
        // IsChecked value is still readable at completion.
        WelcomePanel.Visibility = Visibility.Collapsed;
        StatusPanel.Visibility = Visibility.Visible;
        _ = StartInstallAsync();
    }

    // ──────────────────────────────────────────────────────────────────
    //  Background rotation
    // ──────────────────────────────────────────────────────────────────

    private void LoadBackgrounds()
    {
        // Walk every embedded resource whose URI is under
        // Resources/Backgrounds/. WPF stuffs them into the assembly's
        // resource manifest as ".g.resources" entries. We enumerate via
        // the assembly stream.
        var asm = Assembly.GetExecutingAssembly();
        var resName = asm.GetName().Name + ".g.resources";

        try
        {
            using var stream = asm.GetManifestResourceStream(resName);
            if (stream == null) return;
            using var reader = new System.Resources.ResourceReader(stream);
            foreach (System.Collections.DictionaryEntry entry in reader)
            {
                var key = entry.Key as string;
                if (key == null) continue;
                key = key.ToLowerInvariant();
                if (!key.StartsWith("resources/backgrounds/")) continue;
                if (!(key.EndsWith(".jpg") || key.EndsWith(".png") || key.EndsWith(".jpeg")))
                    continue;

                var uri = new Uri($"pack://application:,,,/{key}", UriKind.Absolute);
                try
                {
                    var bmp = new BitmapImage();
                    bmp.BeginInit();
                    bmp.CacheOption = BitmapCacheOption.OnLoad;
                    bmp.UriSource = uri;
                    bmp.EndInit();
                    bmp.Freeze();
                    _backgrounds.Add(bmp);
                }
                catch { /* skip malformed image */ }
            }
        }
        catch { /* no resources yet — gracefully no-op */ }

        // Build a shuffled play order and show the first image.
        ReshuffleDeck();
        if (_backgrounds.Count > 0 && _shuffleQueue.Count > 0)
        {
            _currentBgIndex = _shuffleQueue[0];
            _shuffleCursor = 1;
            BgPrimary.Source = _backgrounds[_currentBgIndex];
        }
    }

    private void ReshuffleDeck()
    {
        // Fisher-Yates over indices 0..n-1.
        var deck = Enumerable.Range(0, _backgrounds.Count).ToList();
        for (int i = deck.Count - 1; i > 0; i--)
        {
            int j = _rng.Next(i + 1);
            (deck[i], deck[j]) = (deck[j], deck[i]);
        }
        // Avoid showing the same image twice in a row at the seam — if
        // the freshly shuffled deck starts with the previous image,
        // swap it with another slot.
        if (_currentBgIndex >= 0 && deck.Count > 1 && deck[0] == _currentBgIndex)
        {
            (deck[0], deck[1]) = (deck[1], deck[0]);
        }
        _shuffleQueue = deck;
        _shuffleCursor = 0;
    }

    private void StartBackgroundRotation()
    {
        // Need at least 2 images for rotation to make sense.
        if (_backgrounds.Count < 2) return;
        _bgTimer.Interval = TimeSpan.FromSeconds(BG_ROTATION_SECONDS);
        _bgTimer.Tick += (_, _) => CrossFadeNextBackground();
        _bgTimer.Start();
    }

    private void CrossFadeNextBackground()
    {
        if (_backgrounds.Count == 0) return;

        // Pull next index from the shuffled deck. Re-shuffle when exhausted.
        if (_shuffleCursor >= _shuffleQueue.Count)
        {
            ReshuffleDeck();
        }
        int nextIdx = _shuffleQueue[_shuffleCursor++];
        _currentBgIndex = nextIdx;
        var next = _backgrounds[nextIdx];

        // Pick which Image control should show the next image — whichever
        // is currently invisible.
        Image incoming = _swap ? BgPrimary : BgSecondary;
        Image outgoing = _swap ? BgSecondary : BgPrimary;

        incoming.Source = next;

        var fadeIn = new DoubleAnimation
        {
            From = 0,
            To = 1,
            Duration = TimeSpan.FromMilliseconds(FADE_DURATION_MS),
            EasingFunction = new SineEase { EasingMode = EasingMode.EaseInOut },
        };
        var fadeOut = new DoubleAnimation
        {
            From = 1,
            To = 0,
            Duration = TimeSpan.FromMilliseconds(FADE_DURATION_MS),
            EasingFunction = new SineEase { EasingMode = EasingMode.EaseInOut },
        };

        incoming.BeginAnimation(UIElement.OpacityProperty, fadeIn);
        outgoing.BeginAnimation(UIElement.OpacityProperty, fadeOut);

        _swap = !_swap;
    }

    // ──────────────────────────────────────────────────────────────────
    //  Tip rotation
    // ──────────────────────────────────────────────────────────────────

    private void StartTipRotation()
    {
        if (_tips.Length == 0) return;
        TipText.Text = _tips[0];
        _tipTimer.Interval = TimeSpan.FromSeconds(TIP_ROTATION_SECONDS);
        _tipTimer.Tick += (_, _) =>
        {
            _tipIndex = (_tipIndex + 1) % _tips.Length;
            // Quick fade-out → swap text → fade-in.
            var fadeOut = new DoubleAnimation
            {
                From = 1, To = 0,
                Duration = TimeSpan.FromMilliseconds(280),
            };
            fadeOut.Completed += (_, _) =>
            {
                TipText.Text = _tips[_tipIndex];
                var fadeIn = new DoubleAnimation
                {
                    From = 0, To = 1,
                    Duration = TimeSpan.FromMilliseconds(420),
                };
                TipText.BeginAnimation(UIElement.OpacityProperty, fadeIn);
            };
            TipText.BeginAnimation(UIElement.OpacityProperty, fadeOut);
        };
        _tipTimer.Start();
    }

    // ──────────────────────────────────────────────────────────────────
    //  Install flow — launches Velopack's Setup.exe as a hidden subprocess
    // ──────────────────────────────────────────────────────────────────
    //
    // Velopack's Setup.exe handles the actual install logic (file
    // extraction, shortcut creation, registry entry, etc.). We launch it
    // hidden, then drive a smooth fake-progress bar in our UI while it
    // works. When the subprocess exits successfully, we switch to the
    // "complete" state and offer a Launch button.
    //
    // Setup.exe is expected to be sitting in build\Releases\ next to our
    // installer in dev mode, OR (for the shipped artifact) embedded as a
    // resource and extracted to TEMP at runtime. The Find logic below
    // walks the candidate paths in order.
    //
    // No real per-file progress callback — Setup.exe is a black box from
    // outside. The UI shows time-based progress that asymptotes to 95 %
    // until the subprocess actually exits, then snaps to 100. This is the
    // same pattern Velopack's own splash uses internally.

    private const string APP_INSTALL_DIR_NAME = "SC_Toolbox";
    private const string SETUP_EXE_NAME = "SC_Toolbox-win-Setup.exe";
    private const int FAKE_PROGRESS_CEILING = 95;
    private const int EXPECTED_INSTALL_SECONDS = 25;

    // Velopack expands the .nupkg into %LOCALAPPDATA%\SC_Toolbox\.
    // The full install ends up around ~1.4 GB on disk. We use the live
    // size of that directory as a real progress signal — much more honest
    // than time-based easing.
    private const long EXPECTED_INSTALL_BYTES = 1_400_000_000L;
    // If on-disk size hasn't grown for STALL_DETECT_SECONDS *AND* the
    // subprocess hasn't exited, we treat it as stuck and surface a clear
    // error instead of leaving the user staring at 95 %.
    private const int STALL_DETECT_SECONDS = 45;
    // Hard upper bound on install duration — if we hit this, give up and
    // tell the user. ~5 min covers slow disks + first-run AV scan.
    private const int HARD_TIMEOUT_SECONDS = 300;

    private string? FindSetupExe()
    {
        // Candidate locations, in priority order:
        //   1. Same directory as our installer (shipping config).
        //   2. ..\Releases\ relative to our installer (dev config — this
        //      installer lives in build\installer_ui\bin\..., Setup.exe
        //      lives in build\Releases\).
        //   3. Walk up looking for a build\Releases\ marker.
        var here = AppContext.BaseDirectory;
        var candidates = new[]
        {
            Path.Combine(here, SETUP_EXE_NAME),
            Path.Combine(here, "..", "..", "..", "..", "..", "..", "Releases", SETUP_EXE_NAME),
            Path.Combine(here, "..", "..", "..", "..", "Releases", SETUP_EXE_NAME),
            Path.Combine(here, "..", "Releases", SETUP_EXE_NAME),
        };
        foreach (var c in candidates)
        {
            try
            {
                var full = Path.GetFullPath(c);
                if (File.Exists(full)) return full;
            }
            catch { /* path normalization edge cases — skip */ }
        }
        return null;
    }

    // Read from the assembly's <Version> tag (set in
    // SC_Toolbox_Installer.csproj) so the "already installed?" check
    // is automatically in sync with the version we actually install.
    // Previously this was a hardcoded const that drifted from the
    // csproj <Version> (2.2.7 → 2.2.9, caught only by audit), which
    // caused the v2.2.8 installer to wrongly tell 2.2.7 users they
    // were already up to date.  Fixed in the v2.2.10 audit pass.
    private static readonly string INSTALLER_VERSION = ReadAssemblyVersion();

    private static string ReadAssemblyVersion()
    {
        try
        {
            var v = System.Reflection.Assembly.GetExecutingAssembly()
                .GetName().Version;
            if (v != null) return $"{v.Major}.{v.Minor}.{v.Build}";
        }
        catch { /* fall through to fallback */ }
        return "0.0.0";
    }

    /// <summary>
    /// Detect an existing Velopack install of SC_Toolbox. Returns the
    /// installed version string (e.g. "2.2.6") or null if no valid install
    /// is present. We read sq.version (the Velopack manifest at
    /// %LOCALAPPDATA%\SC_Toolbox\current\sq.version) and parse out the
    /// &lt;version&gt; element with a regex — quicker than pulling in an
    /// XML parser, and the format is stable across Velopack releases.
    /// </summary>
    private static string? DetectExistingInstallVersion()
    {
        try
        {
            var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            var manifest = Path.Combine(localAppData, APP_INSTALL_DIR_NAME, "current", "sq.version");
            if (!File.Exists(manifest)) return null;
            var content = File.ReadAllText(manifest);
            var m = System.Text.RegularExpressions.Regex.Match(
                content, @"<version>\s*([^<\s]+)\s*</version>");
            return m.Success ? m.Groups[1].Value : null;
        }
        catch
        {
            return null;
        }
    }

    /// <summary>
    /// If the install root exists but is missing <c>current\sq.version</c>
    /// (the manifest Velopack writes on every successful install), it's
    /// almost certainly leftover state from a failed uninstall — Velopack
    /// 0.0.1298's Update.exe sometimes errors with "missing package
    /// manifest" mid-uninstall and bails, leaving <c>Update.exe</c>,
    /// <c>packages\</c>, and <c>.velopack_lock</c> behind. Setup.exe then
    /// sees the lock file on the next install attempt and silently
    /// refuses to proceed.
    ///
    /// We detect that exact partial-state shape and wipe the dir so the
    /// next install starts from a clean slate. We do NOT touch the dir
    /// if it has a valid <c>sq.version</c> — that would be eating a
    /// healthy install.
    /// </summary>
    private static void WipeStaleInstallRoot()
    {
        var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var installRoot = Path.Combine(localAppData, APP_INSTALL_DIR_NAME);
        if (!Directory.Exists(installRoot)) return;

        // Healthy install? Leave it alone — DetectExistingInstallVersion
        // upstream handles the same-version short-circuit, and Setup.exe
        // handles upgrade/downgrade fine when sq.version is present.
        var manifest = Path.Combine(installRoot, "current", "sq.version");
        if (File.Exists(manifest)) return;

        // No manifest. This is leftover state from a failed uninstall.
        // Best-effort recursive delete; per-file errors are non-fatal.
        try
        {
            Directory.Delete(installRoot, recursive: true);
        }
        catch
        {
            // If recursive delete fails (file locked, AV scan, etc.),
            // try entry-by-entry — at least clear the lock file so the
            // next Setup.exe attempt can proceed.
            try
            {
                foreach (var f in Directory.EnumerateFiles(installRoot, "*", SearchOption.AllDirectories))
                {
                    try { File.Delete(f); } catch { /* skip */ }
                }
                foreach (var d in Directory.EnumerateDirectories(installRoot))
                {
                    try { Directory.Delete(d, recursive: true); } catch { /* skip */ }
                }
            }
            catch { /* nothing more we can do without admin */ }
        }
    }

    /// <summary>
    /// Kill any zombie processes still running out of the install dir.
    /// Velopack's auto-update can leave Update.exe orphaned if the
    /// launcher crashes mid-update-check; an orphaned Update.exe locks
    /// the install dir and blocks both ours AND Add/Remove Programs's
    /// uninstall flow. We sweep them on every install attempt so the
    /// user never has to manually kill processes via Task Manager just
    /// to re-install.
    /// </summary>
    private static void CleanupOrphanedInstallProcesses()
    {
        var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var installRoot = Path.Combine(localAppData, APP_INSTALL_DIR_NAME);
        if (!Directory.Exists(installRoot)) return;

        var rootCanon = Path.GetFullPath(installRoot)
            .TrimEnd(Path.DirectorySeparatorChar)
            .ToLowerInvariant();

        foreach (var proc in Process.GetProcesses())
        {
            try
            {
                // Querying MainModule on protected processes throws —
                // that's fine, those aren't ours.
                var path = proc.MainModule?.FileName;
                if (string.IsNullOrEmpty(path)) continue;
                var canon = Path.GetFullPath(path).ToLowerInvariant();
                if (canon.StartsWith(rootCanon + Path.DirectorySeparatorChar))
                {
                    try
                    {
                        proc.Kill(entireProcessTree: true);
                        proc.WaitForExit(2000);
                    }
                    catch { /* already exited or denied — fine */ }
                }
            }
            catch { /* skip — not ours */ }
            finally
            {
                try { proc.Dispose(); } catch { }
            }
        }
    }

    /// <summary>
    /// Check the registry for an installed VC++ 2015-2022 Redistributable
    /// (x64).  The Mining Signals OCR runtime links onnxruntime, which
    /// requires vcruntime140 / msvcp140; on systems without them the
    /// scanner silently dies with both CNN voters reporting "unavailable".
    /// </summary>
    private static bool IsVcRedistInstalled()
    {
        try
        {
            using var key = Microsoft.Win32.Registry.LocalMachine.OpenSubKey(
                @"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64");
            if (key == null) return false;
            return (key.GetValue("Installed") as int?) == 1;
        }
        catch { return false; }
    }

    /// <summary>
    /// Silent-install the bundled vc_redist.x64.exe if the VC++ Runtime
    /// isn't already on the machine.  No-ops (no UAC prompt) when the
    /// runtime is already installed so most users see no friction.
    /// Failures are swallowed -- the app install proceeds regardless,
    /// and Mining Signals' Python startup check will surface a clear
    /// "missing VC++ Runtime" dialog if it's still wrong afterwards.
    /// </summary>
    private async Task EnsureVcRedistAsync()
    {
        if (IsVcRedistInstalled()) return;

        var dir = AppContext.BaseDirectory;
        var vcRedist = System.IO.Path.Combine(dir, "vc_redist.x64.exe");
        if (!System.IO.File.Exists(vcRedist)) return; // not bundled -- skip

        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = vcRedist,
                Arguments = "/install /quiet /norestart",
                UseShellExecute = true,   // shell-execute so the "runas" verb takes effect
                Verb = "runas",           // request UAC elevation
                WindowStyle = ProcessWindowStyle.Hidden,
            };
            var proc = Process.Start(psi);
            if (proc == null) return;
            // Wait up to 3 minutes for the redistributable install.
            // Accepted exit codes: 0 (success), 1638 (newer already
            // installed), 3010 (success + reboot required).  We don't
            // gate on the code -- the Python startup check is the
            // real backstop if onnxruntime still can't load.
            await Task.Run(() => proc.WaitForExit(180_000));
        }
        catch
        {
            // UAC dismissed, or Process.Start refused for any reason --
            // continue with the app install.  The Python startup check
            // will tell the user if they still need to install vcredist.
        }
    }

    private async Task StartInstallAsync()
    {
        // Sweep zombie launcher / Update.exe processes that might be
        // holding the install dir locked. See CleanupOrphanedInstallProcesses
        // for context — without this, Setup.exe fails because it can't
        // overwrite files held by lingering processes from the previous
        // install.
        CleanupOrphanedInstallProcesses();

        // Wipe leftover state from a failed previous uninstall — only if
        // the dir lacks a valid sq.version manifest, so we never eat a
        // healthy install. Order matters: must run after the process
        // sweep above so Update.exe has released its file locks before
        // we try to delete it.
        WipeStaleInstallRoot();

        // Ensure the Microsoft Visual C++ Runtime is present before we
        // run Velopack's Setup.exe.  Mining Signals' OCR pipeline links
        // onnxruntime, which needs vcruntime140 / msvcp140; on machines
        // without them onnxruntime can't load and the scanner silently
        // dies with both CNN voters reporting "unavailable" -- the
        // v2.2.10 user-reported failure mode.  EnsureVcRedistAsync is a
        // no-op (no UAC) when the runtime is already installed, so most
        // users see no extra friction.
        StatusDetail.Text = "Checking system dependencies…";
        await EnsureVcRedistAsync();

        // Skip the entire Velopack subprocess if the same version is
        // already installed. Without this, Setup.exe's --silent mode
        // detects the existing install and exits 1 (real exit reason —
        // it refuses to clobber an existing same-version install in
        // unattended mode), which our UI surfaced as "Installation
        // failed". Surfacing it as "Already installed" instead is
        // honest AND lets the user click Launch instead of being
        // confused into re-downloading.
        var existing = DetectExistingInstallVersion();
        if (existing == INSTALLER_VERSION)
        {
            Progress.Value = 100;
            ProgressLabel.Text = "100 %";
            StatusTitle.Text = "ALREADY INSTALLED";
            StatusTitle.Foreground = (System.Windows.Media.Brush)Application.Current.Resources["AccentBrush"];
            StatusDetail.Text = $"SC Toolbox v{existing} is already installed. Click Launch to open it.";
            // Suppress the desktop-shortcut row — decision was made
            // during the original install, no point re-asking now.
            AddDesktopShortcut.Visibility = Visibility.Collapsed;
            // Just skip straight to the celebration / Launch state.
            EnterCompletionState();
            // Replace the headline text override done by EnterCompletionState
            // (which assumes a fresh install) with our "already installed"
            // wording so it reads accurately.
            StatusTitle.Text = "ALREADY INSTALLED";
            StatusDetail.Text = $"SC Toolbox v{existing} is already installed. Click Launch to open it.";
            return;
        }
        // (If existing != null and != INSTALLER_VERSION it's a different
        // version — Setup.exe handles upgrade/downgrade fine, fall through.)

        var setupExe = FindSetupExe();
        if (setupExe == null)
        {
            StatusTitle.Text = "Setup.exe not found";
            StatusDetail.Text = $"Expected {SETUP_EXE_NAME} alongside this installer.";
            CancelButton.Content = "Close";
            return;
        }

        StatusDetail.Text = "Starting installation…";

        // Launch Velopack's Setup.exe in --silent mode. Without --silent,
        // Setup.exe pops a confirmation dialog by default. Hiding the
        // window doesn't dismiss the dialog — Setup.exe just blocks
        // forever waiting for input that never comes (the symptom: CPU
        // 0.1 %, temp dir 0 MB, install dir 0 MB, looks "stalled" but
        // is actually waiting on a hidden modal).
        //
        // --silent tells Velopack to install without UI, so its modal
        // never opens and we drive the experience entirely from our UI.
        Process? proc = null;
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = setupExe,
                Arguments = "--silent",
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden,
            };
            proc = Process.Start(psi);
        }
        catch (Exception ex)
        {
            StatusTitle.Text = "Failed to launch installer";
            StatusDetail.Text = ex.Message;
            CancelButton.Content = "Close";
            return;
        }

        if (proc == null)
        {
            StatusTitle.Text = "Failed to launch installer";
            StatusDetail.Text = "Process.Start returned null.";
            CancelButton.Content = "Close";
            return;
        }

        _setupProcess = proc;

        // Real progress: poll the install directory's on-disk size and
        // map it to a percentage. Velopack writes files as it extracts,
        // so the directory grows steadily — a true signal of forward
        // motion. We also detect stalls (no growth for N seconds) so a
        // hung Setup.exe (e.g. blocked on a hidden repair dialog) is
        // surfaced instead of silently parking the bar at 95 %.
        var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var installDir = Path.Combine(localAppData, APP_INSTALL_DIR_NAME);

        // Baseline-relative progress: if there's already a previous
        // install in place, ignore those bytes and only count what's
        // ADDED during this install. Otherwise the bar jumps to 95 %
        // immediately on a re-install. Velopack typically deletes the
        // old current\ before re-extracting, so size dips below baseline
        // briefly — we floor at 0 so it doesn't look weird.
        long baselineSize = SafeDirectorySize(installDir);

        var sw = Stopwatch.StartNew();
        long lastSize = -1;
        DateTime lastGrowth = DateTime.UtcNow;
        var stages = new (int pctThreshold, string message)[]
        {
            (1,   "Extracting Python 3.14 runtime…"),
            (25,  "Bundling Tesseract OCR + neural models…"),
            (50,  "Setting up Mining Signals OCR engine…"),
            (75,  "Configuring PaddleOCR sidecar…"),
            (90,  "Creating shortcuts + registry entries…"),
        };
        int lastStage = -1;

        while (!proc.HasExited)
        {
            // Total bytes under the install dir. Skip permission errors
            // gracefully — they're transient when files are mid-write.
            long size = SafeDirectorySize(installDir);

            // Subtract baseline so a re-install over an existing copy
            // starts at 0, not 95. Floor at 0 — Velopack briefly nukes
            // the old current\ before re-extracting, so size can dip
            // below baseline mid-install.
            long delta = Math.Max(0, size - baselineSize);

            // Cap progress at FAKE_PROGRESS_CEILING so the bar visibly
            // moves to 100 only when the subprocess actually exits.
            int pct = (int)Math.Min(
                FAKE_PROGRESS_CEILING,
                Math.Round((double)delta / EXPECTED_INSTALL_BYTES * 100.0));
            Progress.Value = pct;
            ProgressLabel.Text = $"{pct,3} %";

            for (int i = 0; i < stages.Length; i++)
            {
                if (pct >= stages[i].pctThreshold && i > lastStage)
                {
                    lastStage = i;
                    StatusDetail.Text = stages[i].message;
                }
            }

            // Stall detection: if size has been static for too long, the
            // installer is hung (most common cause: existing-install
            // repair dialog hidden behind our window).
            if (size != lastSize)
            {
                lastSize = size;
                lastGrowth = DateTime.UtcNow;
            }
            else if ((DateTime.UtcNow - lastGrowth).TotalSeconds > STALL_DETECT_SECONDS && size > 0)
            {
                // Stalled mid-install. Don't kill the subprocess — let
                // it keep trying, but tell the user what's likely going on.
                StatusTitle.Text = "Installer appears stalled";
                StatusDetail.Text =
                    "No disk activity for 45 s. If you have a previous "
                  + "SC_Toolbox install, Setup.exe may be waiting on a hidden "
                  + "repair prompt. Cancel and uninstall the existing version, "
                  + "then re-run.";
                CancelButton.Content = "Cancel install";
                return;
            }

            // Hard timeout — Setup.exe really shouldn't take this long.
            if (sw.Elapsed.TotalSeconds > HARD_TIMEOUT_SECONDS)
            {
                StatusTitle.Text = "Installation timed out";
                StatusDetail.Text =
                    $"Install exceeded {HARD_TIMEOUT_SECONDS}s. Last size: "
                  + $"{size / 1_000_000} MB. Check Task Manager for stuck "
                  + "Setup.exe / SC_Toolbox-win-Setup.exe processes.";
                CancelButton.Content = "Close";
                return;
            }

            await Task.Delay(500);
        }

        // Subprocess has exited. Snap to 100 % and report result.
        // Velopack Setup.exe sometimes exits non-zero even though the
        // install actually completed (race conditions on post-install
        // hooks, pre-existing valid install, post-extract shortcut step
        // crashing, etc.). Re-check sq.version before declaring failure
        // — if a valid manifest landed on disk during this run, treat
        // it as success and run our post-install repair anyway.
        if (proc.ExitCode != 0)
        {
            var landed = DetectExistingInstallVersion();
            if (landed != INSTALLER_VERSION)
            {
                StatusTitle.Text = "Installation failed";
                StatusDetail.Text = $"Setup.exe exited with code {proc.ExitCode}.";
                CancelButton.Content = "Close";
                return;
            }
            // Fall through to the success path — install landed, exit
            // code is a lie. We still need to run the repair + shortcut
            // logic, which used to be skipped here (bug fixed in v2.2.7).
        }

        Progress.Value = 100;
        ProgressLabel.Text = "100 %";

        // POST-INSTALL REPAIR (added v2.2.7):
        // Velopack's Setup.exe silently drops some files during extraction —
        // most critically `lib/app/core/{__init__.py, process_manager.py,
        // skill_registry.py}` — leaving the launcher unable to import the
        // `core` module and the app dead-on-arrival. Workaround: open the
        // cached .nupkg in %LOCALAPPDATA%\SC_Toolbox\packages\ and extract
        // any `lib/app/*` files that didn't make it onto disk. Idempotent
        // (only adds missing files, never overwrites) so it's safe to
        // re-run on installs where Velopack DID extract everything.
        try
        {
            StatusDetail.Text = "Verifying installed files…";
            int repaired = RepairMissingFilesFromNupkg();
            if (repaired > 0)
            {
                StatusDetail.Text = $"Restored {repaired} file(s) skipped during extraction.";
            }
        }
        catch
        {
            // Non-fatal — if the repair itself blows up, the install
            // is at least no worse off than it was without us.
        }

        // Honor the "Add desktop shortcut" checkbox. Velopack creates one
        // by default during pack (--shortcuts Desktop,StartMenuRoot), so:
        //   - Checked: ensure it exists (create if Velopack didn't, idempotent overwrite if it did)
        //   - Unchecked: remove the one Velopack just created
        try
        {
            ApplyDesktopShortcutPreference(AddDesktopShortcut.IsChecked == true);
        }
        catch (Exception ex)
        {
            // Non-fatal — install succeeded, this is just polish. Surface
            // the reason in the detail line so a future user reporting
            // "no shortcut" has something to copy/paste back to us.
            StatusDetail.Text = $"Note: shortcut step failed: {ex.Message}";
        }

        // Celebration time. The completion state needs to look obviously
        // different from "still installing" — it's the user's signal
        // that they can stop watching and click Launch.
        EnterCompletionState();
    }

    /// <summary>
    /// Defensive post-install file repair. Velopack 0.0.1298's Setup.exe
    /// has been observed to silently skip files during NuGet extraction —
    /// most notably the top-level <c>core/</c> Python package and various
    /// onnx test fixtures — leaving holes in the install that crash the
    /// launcher with ModuleNotFoundError.
    ///
    /// We work around it by opening the cached <c>.nupkg</c> that Velopack
    /// dropped in <c>packages\</c> and extracting any files under
    /// <c>lib/app/</c> that didn't end up on disk. Only fills holes —
    /// existing files are left untouched, so this is safe even when
    /// Velopack DID extract everything correctly.
    ///
    /// Returns the number of files restored. Throws on unexpected I/O —
    /// caller wraps in try/catch.
    /// </summary>
    private static int RepairMissingFilesFromNupkg()
    {
        var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var installRoot = Path.Combine(localAppData, APP_INSTALL_DIR_NAME);
        var packagesDir = Path.Combine(installRoot, "packages");
        var currentDir = Path.Combine(installRoot, "current");

        if (!Directory.Exists(packagesDir) || !Directory.Exists(currentDir))
            return 0;

        // Find the most recent *-full.nupkg. There's typically only one,
        // but if a delta update has run there might be older copies — we
        // want the latest, which is what Velopack just extracted from.
        string? nupkg = null;
        DateTime newestMtime = DateTime.MinValue;
        foreach (var f in Directory.EnumerateFiles(packagesDir, "*-full.nupkg"))
        {
            var t = File.GetLastWriteTimeUtc(f);
            if (t > newestMtime)
            {
                newestMtime = t;
                nupkg = f;
            }
        }
        if (nupkg == null) return 0;

        // Velopack-internal files we should NOT extract to current\ —
        // these are package payload that Velopack uses for its own
        // bookkeeping (e.g. self-update bootstrapping) and aren't part of
        // the app payload. They'd just clutter current\ if extracted.
        var velopackInternalNames = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "Squirrel.exe",
            "SC_Toolbox_ExecutionStub.exe",
        };

        int restored = 0;
        using var zip = ZipFile.OpenRead(nupkg);
        foreach (var entry in zip.Entries)
        {
            // Normalize separators — NuGet packages typically use forward
            // slashes but ZipArchive will surface whatever was written.
            var entryPath = entry.FullName.Replace('\\', '/');

            // Only consider app payload entries.
            if (!entryPath.StartsWith("lib/app/", StringComparison.OrdinalIgnoreCase))
                continue;

            // Directory marker entries have empty Name. Skip them.
            if (string.IsNullOrEmpty(entry.Name)) continue;

            var relPath = entryPath.Substring("lib/app/".Length);

            // Skip Velopack internals — these aren't supposed to be in current\.
            if (velopackInternalNames.Contains(Path.GetFileName(relPath)))
                continue;

            // Compute destination on disk.
            var destPath = Path.Combine(
                currentDir,
                relPath.Replace('/', Path.DirectorySeparatorChar));

            // Already on disk? Leave it alone — never overwrite. The
            // size-match check would be more thorough but risks tearing
            // a half-written file mid-launch if Velopack is still
            // running async post-install hooks.
            if (File.Exists(destPath)) continue;

            // Extract.
            try
            {
                var destDir = Path.GetDirectoryName(destPath);
                if (!string.IsNullOrEmpty(destDir))
                    Directory.CreateDirectory(destDir);

                entry.ExtractToFile(destPath, overwrite: false);
                restored++;
            }
            catch
            {
                // Best-effort per-file. Path-too-long, AV quarantine,
                // permission denied — keep going, the next file might
                // still extract fine.
            }
        }
        return restored;
    }

    private void EnterCompletionState()
    {
        // Title flips to accent-colored "INSTALLATION COMPLETE", checkmark
        // pops in with a scale animation, Launch button fades in with a
        // pulsing green glow that draws the eye, Cancel becomes "Close".
        StatusTitle.Text = "INSTALLATION COMPLETE";
        StatusTitle.Foreground = (System.Windows.Media.Brush)Application.Current.Resources["AccentBrush"];
        StatusTitle.FontWeight = FontWeights.Bold;
        StatusDetail.Text = $"SC Toolbox v{INSTALLER_VERSION} is installed and ready to launch.";

        // Hide the "Add desktop shortcut" checkbox — decision already
        // applied, leaving it visible just looks like clutter.
        AddDesktopShortcut.Visibility = Visibility.Collapsed;

        // Pop the checkmark in with a scale animation (small bounce).
        CompleteCheck.Visibility = Visibility.Visible;
        var bounce = new DoubleAnimation
        {
            From = 0.0,
            To = 1.0,
            Duration = TimeSpan.FromMilliseconds(380),
            EasingFunction = new BackEase { EasingMode = EasingMode.EaseOut, Amplitude = 0.6 },
        };
        CompleteCheckScale.BeginAnimation(System.Windows.Media.ScaleTransform.ScaleXProperty, bounce);
        CompleteCheckScale.BeginAnimation(System.Windows.Media.ScaleTransform.ScaleYProperty, bounce);

        // Reveal Launch button + start its pulsing glow.
        LaunchButton.Visibility = Visibility.Visible;
        CancelButton.Content = "Close";

        // Pulsing drop-shadow glow on the Launch button — draws the eye
        // without being garish. Two synced animations (blur + opacity)
        // breathing at ~1.5 s per cycle.
        var glowBlur = new DoubleAnimation
        {
            From = 6.0,
            To = 26.0,
            Duration = TimeSpan.FromSeconds(1.5),
            AutoReverse = true,
            RepeatBehavior = RepeatBehavior.Forever,
            EasingFunction = new SineEase { EasingMode = EasingMode.EaseInOut },
        };
        var glowOpacity = new DoubleAnimation
        {
            From = 0.45,
            To = 0.95,
            Duration = TimeSpan.FromSeconds(1.5),
            AutoReverse = true,
            RepeatBehavior = RepeatBehavior.Forever,
            EasingFunction = new SineEase { EasingMode = EasingMode.EaseInOut },
        };
        LaunchGlow.BeginAnimation(System.Windows.Media.Effects.DropShadowEffect.BlurRadiusProperty, glowBlur);
        LaunchGlow.BeginAnimation(System.Windows.Media.Effects.DropShadowEffect.OpacityProperty, glowOpacity);

        // Auto-focus the Launch button so Enter / Space launches the app.
        LaunchButton.Focus();

        // Slow the background rotation way down — completion state should
        // feel calmer than the "actively installing" state.
        if (_bgTimer.IsEnabled)
            _bgTimer.Interval = TimeSpan.FromSeconds(BG_ROTATION_SECONDS * 3);
    }

    // ──────────────────────────────────────────────────────────────────
    //  Desktop shortcut handling
    // ──────────────────────────────────────────────────────────────────

    private void ApplyDesktopShortcutPreference(bool wantShortcut)
    {
        var desktop = Environment.GetFolderPath(Environment.SpecialFolder.Desktop);
        var shortcutPath = Path.Combine(desktop, "SC_Toolbox.lnk");

        if (!wantShortcut)
        {
            // User opted out — remove Velopack's auto-created one if present.
            if (File.Exists(shortcutPath))
            {
                File.Delete(shortcutPath);
            }
            return;
        }

        // User wants a shortcut. Find the launcher .exe to point at, then
        // ensure the .lnk exists. We always re-create (overwrite) so the
        // target path stays correct even if Velopack's location changed.
        var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var appDir = Path.Combine(localAppData, APP_INSTALL_DIR_NAME);
        var primaryTarget = Path.Combine(appDir, "SC_Toolbox.exe");
        var fallbackTarget = Path.Combine(appDir, "current", "SC_Toolbox.exe");
        var target = File.Exists(primaryTarget) ? primaryTarget
                   : File.Exists(fallbackTarget) ? fallbackTarget
                   : null;
        if (target == null) return;  // launcher not where we expected — bail

        CreateShortcut(shortcutPath, target, Path.GetDirectoryName(target) ?? "", "Launch SC Toolbox");
    }

    private static void CreateShortcut(string lnkPath, string targetExe, string workingDir, string description)
    {
        // Use WScript.Shell COM via dynamic dispatch to avoid pulling in
        // Interop.IWshRuntimeLibrary. Same approach Velopack itself uses.
        Type? shellType = Type.GetTypeFromProgID("WScript.Shell");
        if (shellType == null) return;
        dynamic? shell = Activator.CreateInstance(shellType);
        if (shell == null) return;
        try
        {
            dynamic shortcut = shell.CreateShortcut(lnkPath);
            shortcut.TargetPath = targetExe;
            shortcut.WorkingDirectory = workingDir;
            shortcut.Description = description;
            // Use the launcher exe's icon (resource index 0).
            shortcut.IconLocation = $"{targetExe},0";
            shortcut.Save();
        }
        finally
        {
            // Release COM object explicitly — important for Velopack-style installers.
            System.Runtime.InteropServices.Marshal.ReleaseComObject(shell);
        }
    }

    // ──────────────────────────────────────────────────────────────────
    //  Disk size — used as the real progress signal during install
    // ──────────────────────────────────────────────────────────────────

    private static long SafeDirectorySize(string path)
    {
        // Walk the install dir and sum file sizes. Files appearing /
        // disappearing mid-walk are common during install — every
        // exception just means "skip this entry, try again next poll".
        long total = 0;
        if (!Directory.Exists(path)) return 0;
        try
        {
            var stack = new Stack<string>();
            stack.Push(path);
            while (stack.Count > 0)
            {
                var dir = stack.Pop();
                try
                {
                    foreach (var f in Directory.EnumerateFiles(dir))
                    {
                        try { total += new FileInfo(f).Length; }
                        catch { /* file gone / locked — skip */ }
                    }
                    foreach (var d in Directory.EnumerateDirectories(dir))
                        stack.Push(d);
                }
                catch { /* dir gone / locked — skip */ }
            }
        }
        catch { /* outermost — bail with whatever we counted */ }
        return total;
    }

    // ──────────────────────────────────────────────────────────────────
    //  Buttons
    // ──────────────────────────────────────────────────────────────────

    private void LaunchButton_Click(object sender, RoutedEventArgs e)
    {
        // Velopack installs to %LocalAppData%\<AppId>\<AppId>.exe.
        // The launcher .exe at that path picks up the app from current\.
        var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var installedExe = Path.Combine(localAppData, APP_INSTALL_DIR_NAME, "SC_Toolbox.exe");
        // Some Velopack versions put the launcher at current\<AppId>.exe instead.
        if (!File.Exists(installedExe))
        {
            var alt = Path.Combine(localAppData, APP_INSTALL_DIR_NAME, "current", "SC_Toolbox.exe");
            if (File.Exists(alt)) installedExe = alt;
        }

        if (File.Exists(installedExe))
        {
            try
            {
                Process.Start(new ProcessStartInfo
                {
                    FileName = installedExe,
                    UseShellExecute = true,
                });
            }
            catch { /* fall through to close */ }
        }
        Close();
    }

    private void CancelButton_Click(object sender, RoutedEventArgs e)
    {
        Close();
    }
}

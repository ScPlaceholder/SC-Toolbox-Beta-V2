// InstallPaths — where SC Toolbox goes, and whether a chosen folder is safe.
//
// Kept free of WPF so the resolution and validation rules can be exercised
// by a plain console harness without a window. MainWindow owns the UI; this
// class owns the decisions.
//
// Resolution order for the install root:
//   1. An existing install: InstallLocation from the Velopack uninstall key
//      (HKCU\...\Uninstall\SC_Toolbox), if that folder exists.
//   2. A legacy install at the default path that has current\sq.version but
//      no usable registry entry.
//   3. Otherwise a fresh install: the user's chosen folder, else the default
//      %LOCALAPPDATA%\SC_Toolbox.
// An existing install is never moved: updates and repairs go where it is.

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;

namespace SC_Toolbox.Installer;

public static class InstallPaths
{
    public const string APP_DIR_NAME = "SC_Toolbox";
    public const string UNINSTALL_KEY = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\SC_Toolbox";

    // The payload is about 1.8 GB; Velopack also keeps the .nupkg in
    // packages\ next to the extracted copy, so ask for comfortable headroom.
    public const long MIN_FREE_BYTES = 4L * 1024 * 1024 * 1024;

    public static string DefaultRoot() =>
        DefaultRoot(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData));

    public static string DefaultRoot(string localAppData) =>
        Path.Combine(localAppData, APP_DIR_NAME);

    /// <summary>
    /// Turn the folder the user picked into the install root: install into
    /// &lt;picked&gt;\SC_Toolbox unless the picked folder is already named
    /// SC_Toolbox. Returns a full path without a trailing separator.
    /// </summary>
    public static string TargetFromPicked(string picked)
    {
        var full = Normalize(picked);
        var name = Path.GetFileName(full);
        if (string.Equals(name, APP_DIR_NAME, StringComparison.OrdinalIgnoreCase))
            return full;
        return Path.Combine(full, APP_DIR_NAME);
    }

    /// <summary>Full path, trailing separators removed (but "C:\" stays "C:\").</summary>
    public static string Normalize(string path)
    {
        var full = Path.GetFullPath(path.Trim());
        var root = Path.GetPathRoot(full) ?? "";
        if (full.Length > root.Length)
            full = full.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        return full;
    }

    public static bool SamePath(string a, string b) =>
        string.Equals(Normalize(a), Normalize(b), StringComparison.OrdinalIgnoreCase);

    /// <summary>True when <paramref name="path"/> is <paramref name="parent"/> or inside it.</summary>
    public static bool IsSameOrInside(string path, string parent)
    {
        var p = Normalize(path);
        var r = Normalize(parent);
        if (string.Equals(p, r, StringComparison.OrdinalIgnoreCase)) return true;
        var rWithSep = r.EndsWith(Path.DirectorySeparatorChar) ? r : r + Path.DirectorySeparatorChar;
        return p.StartsWith(rWithSep, StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>InstallLocation from the Velopack uninstall key, or null.</summary>
    public static string? ReadRegisteredInstallLocation()
    {
        try
        {
            using var key = Microsoft.Win32.Registry.CurrentUser.OpenSubKey(UNINSTALL_KEY);
            var v = key?.GetValue("InstallLocation") as string;
            return string.IsNullOrWhiteSpace(v) ? null : v.Trim().Trim('"');
        }
        catch { return null; }
    }

    /// <summary>
    /// Where an existing install lives, or null for a fresh machine.
    /// Pure: the caller supplies the registry value and the default root.
    /// </summary>
    public static string? FindExistingRoot(string? registeredLocation, string defaultRoot,
                                           Func<string, bool> directoryExists,
                                           Func<string, bool> fileExists)
    {
        if (!string.IsNullOrWhiteSpace(registeredLocation))
        {
            try
            {
                var reg = Normalize(registeredLocation);
                if (directoryExists(reg)) return reg;
            }
            catch { /* malformed value: fall through to the legacy check */ }
        }
        var legacyManifest = Path.Combine(defaultRoot, "current", "sq.version");
        if (fileExists(legacyManifest)) return Normalize(defaultRoot);
        return null;
    }

    public static string? FindExistingRoot() =>
        FindExistingRoot(ReadRegisteredInstallLocation(), DefaultRoot(), Directory.Exists, File.Exists);

    /// <summary>
    /// Arguments for Velopack's Setup.exe. --installto is passed ONLY when
    /// the target differs from the default, so the default path keeps
    /// exactly the behaviour it had before custom locations existed.
    /// </summary>
    public static List<string> SetupArguments(string target, string defaultRoot)
    {
        var args = new List<string> { "--silent" };
        if (!SamePath(target, defaultRoot))
        {
            args.Add("--installto");
            args.Add(Normalize(target));
        }
        return args;
    }

    /// <summary>
    /// Does this folder look like a Velopack install root (healthy or the
    /// leftovers of a failed uninstall)? Used so we never treat a folder of
    /// someone's own files as ours.
    /// </summary>
    public static bool LooksLikeVelopackRoot(string dir)
    {
        try
        {
            return File.Exists(Path.Combine(dir, "Update.exe"))
                || File.Exists(Path.Combine(dir, ".velopack_lock"))
                || File.Exists(Path.Combine(dir, "current", "sq.version"))
                || Directory.Exists(Path.Combine(dir, "packages"));
        }
        catch { return false; }
    }

    public sealed class ValidationContext
    {
        public required IReadOnlyList<string> InstallerDirs { get; init; }
        public required IReadOnlyList<string> ProgramFilesDirs { get; init; }
        public required bool IsElevated { get; init; }
        /// <summary>Free bytes on the drive holding the path, or null if unknown.</summary>
        public required Func<string, long?> FreeBytes { get; init; }
        /// <summary>Can we create and delete a file in this existing directory?</summary>
        public required Func<string, bool> CanWrite { get; init; }
        public required Func<string, bool> DirectoryExists { get; init; }
        public required Func<string, bool> DirectoryHasEntries { get; init; }
        public required Func<string, bool> LooksLikeVelopackRoot { get; init; }
    }

    /// <summary>
    /// Returns null when <paramref name="target"/> is a safe place for a
    /// fresh install, otherwise a message to show the user.
    /// </summary>
    public static string? Validate(string target, ValidationContext ctx, bool ownedRoot = false)
    {
        string full;
        try
        {
            if (string.IsNullOrWhiteSpace(target) || !Path.IsPathFullyQualified(target))
                return "Choose a full folder path, for example D:\\Games.";
            full = Normalize(target);
        }
        catch (Exception ex)
        {
            return $"That folder path is not valid: {ex.Message}";
        }

        foreach (var dir in ctx.InstallerDirs.Where(d => !string.IsNullOrWhiteSpace(d)))
        {
            if (IsSameOrInside(full, dir))
                return "That is the folder this installer is running from. Choose a different folder.";
        }

        if (!ctx.IsElevated)
        {
            foreach (var pf in ctx.ProgramFilesDirs.Where(d => !string.IsNullOrWhiteSpace(d)))
            {
                if (IsSameOrInside(full, pf))
                    return "SC Toolbox installs per user and cannot write inside Program Files. "
                         + "Choose a folder elsewhere, for example D:\\Games.";
            }
        }

        // Velopack cleans the install directory before extracting, so a
        // folder that already holds someone's files must never be the target.
        //
        // ownedRoot is the default %LOCALAPPDATA%\SC_Toolbox: that folder is
        // the Toolbox's by name, and whatever is in it is what an earlier
        // install or a failed uninstall left. It must not be refused, or a
        // half-cleared leftover (no Update.exe, no packages folder, so it no
        // longer looks like a Velopack root) locks the user out of the
        // default folder for good.
        if (!ownedRoot && ctx.DirectoryExists(full) && ctx.DirectoryHasEntries(full) && !ctx.LooksLikeVelopackRoot(full))
            return $"{full} already contains other files. Setup clears its install folder, "
                 + "so choose an empty folder or a new one.";

        // Writability is tested in the nearest folder that already exists:
        // the target itself, or the parent Setup will create it under.
        var probe = NearestExisting(full, ctx.DirectoryExists);
        if (probe == null)
            return $"The drive for {full} is not available.";
        if (!ctx.CanWrite(probe))
            return $"This account cannot write to {probe}. Choose a different folder.";

        var free = ctx.FreeBytes(full);
        if (free is long f && f < MIN_FREE_BYTES)
            return $"Not enough free space on {Path.GetPathRoot(full)}: "
                 + $"{f / (1024.0 * 1024 * 1024):0.0} GB free, about "
                 + $"{MIN_FREE_BYTES / (1024 * 1024 * 1024)} GB needed.";

        return null;
    }

    public static string? NearestExisting(string full, Func<string, bool> directoryExists)
    {
        string? cur = full;
        while (!string.IsNullOrEmpty(cur))
        {
            if (directoryExists(cur)) return cur;
            cur = Path.GetDirectoryName(cur);
        }
        return null;
    }

    // ── Real-machine implementations of the context hooks ───────────────

    public static ValidationContext SystemContext()
    {
        var installerDirs = new List<string>();
        try
        {
            var exe = Environment.ProcessPath;
            if (!string.IsNullOrEmpty(exe))
            {
                var d = Path.GetDirectoryName(exe);
                if (!string.IsNullOrEmpty(d)) installerDirs.Add(d);
            }
        }
        catch { }
        try { installerDirs.Add(AppContext.BaseDirectory); } catch { }

        var pf = new List<string>();
        foreach (var sf in new[] { Environment.SpecialFolder.ProgramFiles, Environment.SpecialFolder.ProgramFilesX86 })
        {
            try { var p = Environment.GetFolderPath(sf); if (!string.IsNullOrEmpty(p)) pf.Add(p); } catch { }
        }
        try { var w = Environment.GetEnvironmentVariable("ProgramW6432"); if (!string.IsNullOrEmpty(w)) pf.Add(w); } catch { }

        return new ValidationContext
        {
            InstallerDirs = installerDirs,
            ProgramFilesDirs = pf,
            IsElevated = IsElevated(),
            FreeBytes = SystemFreeBytes,
            CanWrite = SystemCanWrite,
            DirectoryExists = Directory.Exists,
            DirectoryHasEntries = d => { try { return Directory.EnumerateFileSystemEntries(d).Any(); } catch { return true; } },
            LooksLikeVelopackRoot = LooksLikeVelopackRoot,
        };
    }

    public static bool IsElevated()
    {
        try
        {
            using var id = System.Security.Principal.WindowsIdentity.GetCurrent();
            return new System.Security.Principal.WindowsPrincipal(id)
                .IsInRole(System.Security.Principal.WindowsBuiltInRole.Administrator);
        }
        catch { return false; }
    }

    public static long? SystemFreeBytes(string path)
    {
        try
        {
            var root = Path.GetPathRoot(Path.GetFullPath(path));
            if (string.IsNullOrEmpty(root)) return null;
            return new DriveInfo(root).AvailableFreeSpace;
        }
        catch { return null; }
    }

    public static bool SystemCanWrite(string existingDir)
    {
        var probe = Path.Combine(existingDir, $".sctb_write_test_{Guid.NewGuid():N}.tmp");
        try
        {
            using (new FileStream(probe, FileMode.CreateNew, FileAccess.Write, FileShare.None, 1, FileOptions.DeleteOnClose)) { }
            return true;
        }
        catch { return false; }
        finally
        {
            try { if (File.Exists(probe)) File.Delete(probe); } catch { }
        }
    }
}

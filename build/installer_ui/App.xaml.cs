using System;
using System.IO;
using System.Windows;

namespace SC_Toolbox.Installer;

public partial class App : Application
{
    /// <summary>
    /// Leave the folder we were started in before anything else happens.
    ///
    /// The Toolbox's own UPDATE NOW button downloads this installer and
    /// starts it from the running Toolbox, so it inherits the Toolbox's
    /// working folder: the install folder itself. Windows will not rename
    /// a folder that is some process's working folder, and renaming the
    /// old install aside is the first thing Setup.exe does. Every in-app
    /// update from 3.0.0 therefore ended in "Failed to remove existing
    /// application directory ... Access is denied".
    ///
    /// The installer that runs is always the NEW one, so fixing it here
    /// repairs the update for copies already installed. The system temp
    /// folder is used because it always exists and is never the install.
    /// </summary>
    protected override void OnStartup(StartupEventArgs e)
    {
        try
        {
            Directory.SetCurrentDirectory(Path.GetTempPath());
        }
        catch
        {
            // Staying where we are is the old behaviour; never block the
            // installer over this.
        }
        base.OnStartup(e);
    }
}

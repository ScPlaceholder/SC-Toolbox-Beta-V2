Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)

' Find Python
Dim python, fso, localApp, progFiles
Set fso = CreateObject("Scripting.FileSystemObject")
localApp = WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%")
progFiles = WshShell.ExpandEnvironmentStrings("%ProgramFiles%")
python = ""

' Check standard Python.org installs (pythonw.exe first for no-console launch)
Dim versions, exeNames
versions = Array("314", "313", "312", "311", "310", "39", "38")
exeNames = Array("pythonw.exe", "python.exe")

Dim v, exeName, candidate
For Each exeName In exeNames
    For Each v In versions
        candidate = localApp & "\Programs\Python\Python" & v & "\" & exeName
        If fso.FileExists(candidate) Then
            python = candidate
            Exit For
        End If
    Next
    If python <> "" Then Exit For
Next

' Check winget / package manager installs under %LOCALAPPDATA%\Python (two levels)
If python = "" And fso.FolderExists(localApp & "\Python") Then
    Dim folder1, folder2, subFolder1, subFolder2
    Set folder1 = fso.GetFolder(localApp & "\Python")
    For Each subFolder1 In folder1.SubFolders
        For Each exeName In exeNames
            If fso.FileExists(subFolder1.Path & "\" & exeName) Then
                python = subFolder1.Path & "\" & exeName
                Exit For
            End If
        Next
        If python <> "" Then Exit For
        ' Second level
        For Each subFolder2 In subFolder1.SubFolders
            For Each exeName In exeNames
                If fso.FileExists(subFolder2.Path & "\" & exeName) Then
                    python = subFolder2.Path & "\" & exeName
                    Exit For
                End If
            Next
            If python <> "" Then Exit For
        Next
        If python <> "" Then Exit For
    Next
End If

' Check Program Files
If python = "" Then
    For Each exeName In exeNames
        For Each v In versions
            candidate = progFiles & "\Python" & v & "\" & exeName
            If fso.FileExists(candidate) Then
                python = candidate
                Exit For
            End If
        Next
        If python <> "" Then Exit For
    Next
End If

' Check legacy C:\PythonXX
If python = "" Then
    For Each exeName In exeNames
        For Each v In versions
            candidate = "C:\Python" & v & "\" & exeName
            If fso.FileExists(candidate) Then
                python = candidate
                Exit For
            End If
        Next
        If python <> "" Then Exit For
    Next
End If

If python = "" Then
    MsgBox "Python not found. Install Python 3.10+ from python.org and run INSTALL_AND_LAUNCH.bat to set up PySide6.", vbCritical, "SC_Toolbox"
    WScript.Quit
End If

WshShell.Run """" & python & """ skill_launcher.py 100 100 500 550 0.95 nul", 0, False

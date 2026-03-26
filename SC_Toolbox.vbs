Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)

' Find Python
Dim pythonPaths, python, fso
Set fso = CreateObject("Scripting.FileSystemObject")
pythonPaths = Array( _
    WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python314\pythonw.exe", _
    WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python313\pythonw.exe", _
    WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python312\pythonw.exe", _
    WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python311\pythonw.exe", _
    WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python310\pythonw.exe" _
)

python = ""
For Each p In pythonPaths
    If fso.FileExists(p) Then
        python = p
        Exit For
    End If
Next

If python = "" Then
    ' Fallback to python.exe (will show console briefly)
    For Each p In pythonPaths
        p = Replace(p, "pythonw.exe", "python.exe")
        If fso.FileExists(p) Then
            python = p
            Exit For
        End If
    Next
End If

If python = "" Then
    MsgBox "Python not found. Install Python 3.10+ from python.org", vbCritical, "SC_Toolbox"
    WScript.Quit
End If

WshShell.Run """" & python & """ skill_launcher.py 100 100 500 550 0.95 NUL", 0, False

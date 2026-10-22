Option Explicit
Dim shell, fso, base, pythonw, app
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = base & "\.venv\Scripts\pythonw.exe"
app = base & "\app.pyw"

If Not fso.FileExists(pythonw) Then
    MsgBox "Najpierw uruchom 1_SETUP.cmd. To jest jednorazowa konfiguracja.", 48, "Danbooru Tagger"
    WScript.Quit 1
End If

shell.Run Chr(34) & pythonw & Chr(34) & " " & Chr(34) & app & Chr(34), 0, False

' Silently launch server.py with pythonw (no console window)
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
CreateObject("WScript.Shell").Run "pythonw """ & dir & "\server.py""", 0, False

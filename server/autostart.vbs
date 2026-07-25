' VoxCraft ASR server: start autostart.cmd with no visible console window.
'
' ASCII only on purpose: wscript reads .vbs in the system codepage, so UTF-8
' text here would be mojibake. Japanese docs are in install-autostart.ps1.
'
' Double-click this file to start the server manually in the background.

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

base = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = base

' 0 = hidden window, False = do not wait for it to finish
sh.Run """" & base & "\autostart.cmd""", 0, False

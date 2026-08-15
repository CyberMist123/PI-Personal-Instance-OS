' Start the CMX loopback MCP HTTP service (127.0.0.1:8766) at logon, hidden.
' Registered under HKCU\...\Run as "CmxMcpHttp". Non-admin, no Task Scheduler.
' http-start.ps1 is idempotent: it exits 0 when the service is already healthy.
Option Explicit

Dim shell, fso, root, script, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(WScript.ScriptFullName)
script = fso.BuildPath(root, "http-start.ps1")
If Not fso.FileExists(script) Then WScript.Quit 1

command = "powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & script & """"
shell.Run command, 0, False

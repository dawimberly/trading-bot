' Run a command with no visible console (WindowStyle 0).
Option Explicit
If WScript.Arguments.Count < 1 Then WScript.Quit 1
Dim sh, cmd, i
Set sh = CreateObject("WScript.Shell")
cmd = WScript.Arguments(0)
For i = 1 To WScript.Arguments.Count - 1
  cmd = cmd & " " & WScript.Arguments(i)
Next
sh.Run cmd, 0, False

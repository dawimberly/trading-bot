$ErrorActionPreference = 'Stop'
$name = 'StockBotResearchEvidenceReview'
if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) { throw 'Task name already exists; inspect before replacing.' }
$ownerIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute 'C:\Users\Owner\PythonTrading\venv311\Scripts\pythonw.exe' -Argument '-B "C:\workspace\stock_bot_review_loop\daily_run.py" --source-root "C:\Users\Owner\PythonTrading\stock-bot" --publish' -WorkingDirectory 'C:\workspace\stock_bot_review_loop'
$trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '18:00'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 70)
$principal = New-ScheduledTaskPrincipal -UserId $ownerIdentity -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'GET-only broker evidence export and sanitized research summary. No orders or bot restarts.' | Out-Null
$info = Get-ScheduledTaskInfo -TaskName $name
[pscustomobject]@{Task=$name; State=(Get-ScheduledTask -TaskName $name).State.ToString(); NextRun=$info.NextRunTime.ToString('o'); TimeZone=[TimeZoneInfo]::Local.Id} | ConvertTo-Json -Compress

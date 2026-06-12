# Quick status check - run anytime
Write-Host '=== DISPLAY ===' -ForegroundColor Cyan
Get-PnpDevice -Class Display | Select-Object Status, FriendlyName, Problem | Format-Table -AutoSize
Get-CimInstance Win32_VideoController | Select-Object Name, DriverVersion, Status | Format-Table -AutoSize

Write-Host '=== MONITORS ===' -ForegroundColor Cyan
Get-CimInstance -Namespace root\wmi -ClassName WmiMonitorID -ErrorAction SilentlyContinue | ForEach-Object {
    $mfg = ($_.ManufacturerName | ForEach-Object { [char]$_ }) -join ''
    $prod = ($_.ProductCodeID | ForEach-Object { [char]$_ }) -join ''
    [PSCustomObject]@{ Manufacturer=$mfg.Trim(); Product=$prod.Trim(); Instance=$_.InstanceName }
} | Format-Table -AutoSize

Write-Host '=== AUDIO ===' -ForegroundColor Cyan
Get-PnpDevice -Class AudioEndpoint | Where-Object Status -eq 'OK' | Select-Object FriendlyName | Format-Table -AutoSize
Write-Host 'HDAUDIO devices:'
Get-ChildItem 'HKLM:\SYSTEM\CurrentControlSet\Enum\HDAUDIO' -EA SilentlyContinue | ForEach-Object { $_.PSChildName }

Write-Host '=== NVIDIA SERVICES ===' -ForegroundColor Cyan
sc.exe query nvlddmkm 2>&1
sc.exe query NVHDA 2>&1

$log = "$env:TEMP\hdmi-audio-fix-elevated.log"
function Log($msg) { "$(Get-Date -Format o) $msg" | Tee-Object -FilePath $log -Append }

Log 'Elevated HDMI audio fix starting'

$gpu = Get-PnpDevice | Where-Object { $_.FriendlyName -eq 'NVIDIA Quadro K1100M' }
if ($gpu) {
    Log "Cycling NVIDIA GPU: $($gpu.InstanceId)"
    try {
        Disable-PnpDevice -InstanceId $gpu.InstanceId -Confirm:$false -ErrorAction Stop
        Start-Sleep -Seconds 4
        Enable-PnpDevice -InstanceId $gpu.InstanceId -Confirm:$false -ErrorAction Stop
        Start-Sleep -Seconds 6
        Log 'NVIDIA GPU cycle complete'
    } catch {
        Log "GPU cycle failed: $_"
    }
}

$nvDisplayInf = 'C:\Windows\INF\oem135.inf'
if (Test-Path $nvDisplayInf) {
    Log "Reinstalling NVIDIA display driver from $nvDisplayInf"
    pnputil /add-driver $nvDisplayInf /install 2>&1 | Out-File -FilePath $log -Append
}

$nvAudioInf = 'C:\Windows\INF\oem8.inf'
if (Test-Path $nvAudioInf) {
    Log "Reinstalling NVIDIA HD Audio from $nvAudioInf"
    pnputil /add-driver $nvAudioInf /install 2>&1 | Out-File -FilePath $log -Append
}

pnputil /scan-devices 2>&1 | Out-File -FilePath $log -Append
Restart-Service Audiosrv -Force -ErrorAction SilentlyContinue
Restart-Service AudioEndpointBuilder -Force -ErrorAction SilentlyContinue

Log 'HDAUDIO devices in registry:'
Get-ChildItem 'HKLM:\SYSTEM\CurrentControlSet\Enum\HDAUDIO' -ErrorAction SilentlyContinue |
    ForEach-Object { $_.PSChildName } | Out-File -FilePath $log -Append

Log 'Audio endpoints:'
Get-PnpDevice -Class AudioEndpoint, MEDIA -ErrorAction SilentlyContinue |
    Where-Object { $_.Status -eq 'OK' } |
    Select-Object Status, Class, FriendlyName |
    Format-Table -AutoSize |
    Out-String | Out-File -FilePath $log -Append

Log 'Done'

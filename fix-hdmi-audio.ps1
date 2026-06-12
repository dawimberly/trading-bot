# Requires elevation - reinstall GPU HDMI audio and rescan devices
$log = "$env:TEMP\hdmi-audio-fix.log"
function Log($msg) { "$(Get-Date -Format o) $msg" | Tee-Object -FilePath $log -Append }

Log 'Starting HDMI audio fix'

# Restart audio stack
Log 'Restarting audio services'
Restart-Service Audiosrv -Force -ErrorAction SilentlyContinue
Restart-Service AudioEndpointBuilder -Force -ErrorAction SilentlyContinue

# Rescan for hardware
Log 'Scanning for hardware changes'
pnputil /scan-devices 2>&1 | Out-File -FilePath $log -Append

# Try installing NVIDIA HD Audio driver if present
$nvInf = @('C:\Windows\INF\oem8.inf', 'C:\Windows\INF\oem5.inf') | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($nvInf) {
    Log "Installing NVIDIA HD Audio driver from $nvInf"
    pnputil /add-driver $nvInf /install 2>&1 | Out-File -FilePath $log -Append
}

# Try installing Intel Display Audio driver if present
$intelInf = @('C:\Windows\INF\oem36.inf', 'C:\Windows\INF\oem12.inf') | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($intelInf) {
    Log "Installing Intel Display Audio driver from $intelInf"
    pnputil /add-driver $intelInf /install 2>&1 | Out-File -FilePath $log -Append
}

# Rescan again after driver install
pnputil /scan-devices 2>&1 | Out-File -FilePath $log -Append

Log 'Current audio endpoints:'
Get-PnpDevice -Class AudioEndpoint, MEDIA -ErrorAction SilentlyContinue |
    Select-Object Status, Class, FriendlyName |
    Format-Table -AutoSize |
    Out-String | Out-File -FilePath $log -Append

Log 'Done. Log: ' + $log

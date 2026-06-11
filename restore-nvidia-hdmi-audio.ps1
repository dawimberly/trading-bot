# Run as Administrator - Full NVIDIA driver reinstall to restore HDMI audio
$log = "$env:TEMP\restore-nvidia-hdmi-audio.log"
function Log($msg) { "$(Get-Date -Format o) $msg" | Tee-Object -FilePath $log -Append }

Log '=== NVIDIA HDMI Audio Restore ==='

# Step 1: Uninstall current NVIDIA display driver (forces co-installer on reinstall)
Log 'Step 1: Uninstalling NVIDIA display driver oem135.inf'
pnputil /delete-driver oem135.inf /uninstall /force 2>&1 | Out-File -FilePath $log -Append
Start-Sleep -Seconds 5

# Step 2: Reinstall display driver (NVAllowHDAudioPreStage=1 should stage HD audio)
Log 'Step 2: Reinstalling NVIDIA display driver'
pnputil /add-driver C:\Windows\INF\oem135.inf /install 2>&1 | Out-File -FilePath $log -Append
Start-Sleep -Seconds 8

# Step 3: Install NVIDIA HD Audio driver
$nvhdaInf = 'C:\Windows\System32\DriverStore\FileRepository\nvhda.inf_amd64_65328cb00cb819b7\nvhda.inf'
if (Test-Path $nvhdaInf) {
    Log "Step 3: Installing nvhda from $nvhdaInf"
    pnputil /add-driver $nvhdaInf /install 2>&1 | Out-File -FilePath $log -Append
}

# Step 4: Scan and restart audio
Log 'Step 4: Scanning for hardware changes'
pnputil /scan-devices 2>&1 | Out-File -FilePath $log -Append
Restart-Service Audiosrv -Force -ErrorAction SilentlyContinue
Restart-Service AudioEndpointBuilder -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3

# Step 5: Report results
Log 'HDAUDIO devices:'
Get-ChildItem 'HKLM:\SYSTEM\CurrentControlSet\Enum\HDAUDIO' -ErrorAction SilentlyContinue |
    ForEach-Object { $_.PSChildName } | Out-File -FilePath $log -Append

Log 'Audio endpoints:'
Get-PnpDevice -Class AudioEndpoint -ErrorAction SilentlyContinue |
    Where-Object { $_.Status -eq 'OK' } |
    Select-Object FriendlyName |
    Format-Table -AutoSize |
    Out-String | Out-File -FilePath $log -Append

Log 'NVHDA service state:'
sc.exe query NVHDA 2>&1 | Out-File -FilePath $log -Append

Log "Done. Log: $log"

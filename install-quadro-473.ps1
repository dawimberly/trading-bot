$log = "$env:TEMP\install-quadro-473.log"
function Log($msg) { "$(Get-Date -Format o) $msg" | Tee-Object -FilePath $log -Append }

Log 'Installing NVIDIA Quadro 473.47 for K1100M'

$installer = "$env:TEMP\nvidia-quadro-473.47.exe"
if (-not (Test-Path $installer)) {
    Log "ERROR: Installer not found at $installer"
    exit 1
}

$sizeMB = [math]::Round((Get-Item $installer).Length / 1MB, 1)
Log "Installer size: ${sizeMB} MB"

# Enable GPU if disabled
$gpu = Get-PnpDevice -InstanceId 'PCI\VEN_10DE&DEV_0FF6&SUBSYS_2253103C&REV_A1\4&C3F9077&0&0008' -ErrorAction SilentlyContinue
if ($gpu) {
    Log "GPU state: Status=$($gpu.Status) Problem=$($gpu.Problem) Name=$($gpu.FriendlyName)"
    if ($gpu.Status -eq 'Error' -or $gpu.Problem) {
        try {
            Enable-PnpDevice -InstanceId $gpu.InstanceId -Confirm:$false -ErrorAction SilentlyContinue
            Log 'Enable-PnpDevice attempted'
        } catch { Log "Enable failed: $_" }
    }
}

# Run full NVIDIA installer (GUI - user must choose Custom + clean + HD Audio)
Log 'Launching NVIDIA 473.47 installer GUI'
Start-Process -FilePath $installer -Wait
Log "Installer exit"

Log 'Post-install display adapters:'
Get-PnpDevice -Class Display | Select-Object Status, FriendlyName, Problem | Format-Table -AutoSize | Out-String | Out-File -FilePath $log -Append
sc.exe query nvlddmkm 2>&1 | Out-File -FilePath $log -Append
Log "Done. Log: $log"

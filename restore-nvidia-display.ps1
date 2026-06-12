$log = "$env:TEMP\restore-nvidia-display.log"
function Log($msg) { "$(Get-Date -Format o) $msg" | Tee-Object -FilePath $log -Append }

Log 'Restoring NVIDIA Quadro K1100M display driver'

# Remove broken basic display binding on NVIDIA GPU
$broken = Get-PnpDevice -InstanceId 'PCI\VEN_10DE&DEV_0FF6&SUBSYS_2253103C&REV_A1\4&C3F9077&0&0008' -ErrorAction SilentlyContinue
if ($broken) {
    Log "Current device: $($broken.FriendlyName) Status=$($broken.Status) Problem=$($broken.Problem)"
    try {
        pnputil /remove-device $broken.InstanceId /subtree 2>&1 | Out-File -FilePath $log -Append
        Start-Sleep -Seconds 3
    } catch { Log "Remove failed: $_" }
}

pnputil /scan-devices 2>&1 | Out-File -FilePath $log -Append

$infs = @(
    'C:\Windows\INF\oem135.inf',
    'C:\Windows\System32\DriverStore\FileRepository\nvbl.inf_amd64_461c07559f129cfa\nvbl.inf'
)
foreach ($inf in $infs) {
    if (Test-Path $inf) {
        Log "Installing display driver from $inf"
        pnputil /add-driver $inf /install 2>&1 | Out-File -FilePath $log -Append
    }
}

$nvhda = 'C:\Windows\System32\DriverStore\FileRepository\nvhda.inf_amd64_65328cb00cb819b7\nvhda.inf'
if (Test-Path $nvhda) {
    Log "Installing NVIDIA HD Audio from $nvhda"
    pnputil /add-driver $nvhda /install 2>&1 | Out-File -FilePath $log -Append
}

pnputil /scan-devices 2>&1 | Out-File -FilePath $log -Append

Log 'Post-install status:'
Get-PnpDevice -Class Display | Select-Object Status, FriendlyName, Problem | Format-Table -AutoSize | Out-String | Out-File -FilePath $log -Append
sc.exe query nvlddmkm 2>&1 | Out-File -FilePath $log -Append
Get-ChildItem 'HKLM:\SYSTEM\CurrentControlSet\Enum\HDAUDIO' -ErrorAction SilentlyContinue | ForEach-Object { $_.PSChildName } | Out-File -FilePath $log -Append
Log "Done. Log: $log"

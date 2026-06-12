$log = "$env:TEMP\force-nvidia-install.log"
function Log($msg) { "$(Get-Date -Format o) $msg" | Tee-Object -FilePath $log -Append }

Log 'Force NVIDIA driver install starting'

$installer = "$env:TEMP\nvidia-quadro-460.89.exe"
$extractDir = 'C:\NVIDIA_extract'

if (-not (Test-Path $installer)) {
    Log "ERROR: Installer missing at $installer"
    exit 1
}

if (Test-Path $extractDir) { Remove-Item $extractDir -Recurse -Force -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Path $extractDir -Force | Out-Null

Log "Extracting $installer to $extractDir"
$proc = Start-Process -FilePath $installer -ArgumentList "-extract:$extractDir", "-s" -Wait -PassThru -NoNewWindow
Log "Extract exit code: $($proc.ExitCode)"

$infs = Get-ChildItem $extractDir -Recurse -Filter '*.inf' -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match 'nvbl|nv_disp|nvaci|nvlddmkm' -or (Select-String -Path $_.FullName -Pattern 'Quadro|VEN_10DE|0FF6' -Quiet) } |
    Select-Object -ExpandProperty FullName

Log "Found INF candidates:"
$infs | ForEach-Object { Log "  $_" }

# Remove broken basic display on NVIDIA GPU
$gpuId = 'PCI\VEN_10DE&DEV_0FF6&SUBSYS_2253103C&REV_A1\4&C3F9077&0&0008'
Log "Removing broken device $gpuId"
pnputil /remove-device $gpuId /subtree 2>&1 | Out-File -FilePath $log -Append
Start-Sleep -Seconds 3
pnputil /scan-devices 2>&1 | Out-File -FilePath $log -Append

foreach ($inf in $infs) {
    Log "Installing driver package: $inf"
    pnputil /add-driver $inf /install 2>&1 | Out-File -FilePath $log -Append
}

# Also run full installer silently as fallback
Log 'Running full NVIDIA installer silent clean'
$proc2 = Start-Process -FilePath $installer -ArgumentList '-s', '-clean', '-noreboot' -Wait -PassThru -NoNewWindow
Log "Installer exit code: $($proc2.ExitCode)"

pnputil /scan-devices 2>&1 | Out-File -FilePath $log -Append

Log 'Display adapters:'
Get-PnpDevice -Class Display | Select-Object Status, FriendlyName, Problem | Format-Table -AutoSize | Out-String | Out-File -FilePath $log -Append
sc.exe query nvlddmkm 2>&1 | Out-File -FilePath $log -Append

Log 'Audio endpoints:'
Get-PnpDevice -Class AudioEndpoint | Where-Object { $_.Status -eq 'OK' } | Select-Object FriendlyName | Format-Table -AutoSize | Out-String | Out-File -FilePath $log -Append

Get-ChildItem 'HKLM:\SYSTEM\CurrentControlSet\Enum\HDAUDIO' -ErrorAction SilentlyContinue | ForEach-Object { $_.PSChildName } | Out-File -FilePath $log -Append

Log "Done. Log: $log"

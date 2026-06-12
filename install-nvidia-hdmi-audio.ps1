# Run as Administrator - attempts to install NVIDIA HDMI audio from existing driver package
$log = "$env:TEMP\nvidia-hdmi-audio-install.log"
function Log($msg) { "$(Get-Date -Format o) $msg" | Tee-Object -FilePath $log -Append }

Log 'Installing NVIDIA HDMI audio components'

$nvDir = 'C:\Windows\System32\DriverStore\FileRepository\nvbl.inf_amd64_461c07559f129cfa'
$nvhdaInf = 'C:\Windows\System32\DriverStore\FileRepository\nvhda.inf_amd64_65328cb00cb819b7\nvhda.inf'

if (Test-Path "$nvDir\dbInstaller.exe") {
    Log 'Running dbInstaller.exe'
    Start-Process -FilePath "$nvDir\dbInstaller.exe" -Wait -NoNewWindow -ErrorAction SilentlyContinue
}

if (Test-Path "$nvDir\NvCplSetupInt.exe") {
    Log 'Running NvCplSetupInt.exe'
    Start-Process -FilePath "$nvDir\NvCplSetupInt.exe" -Wait -NoNewWindow -ErrorAction SilentlyContinue
}

if (Test-Path $nvhdaInf) {
    Log "Installing nvhda from $nvhdaInf"
    pnputil /add-driver $nvhdaInf /install 2>&1 | Out-File -FilePath $log -Append
}

pnputil /scan-devices 2>&1 | Out-File -FilePath $log -Append
Restart-Service Audiosrv -Force -ErrorAction SilentlyContinue
Restart-Service AudioEndpointBuilder -Force -ErrorAction SilentlyContinue

Log 'HDAUDIO enum:'
Get-ChildItem 'HKLM:\SYSTEM\CurrentControlSet\Enum\HDAUDIO' -ErrorAction SilentlyContinue |
    ForEach-Object { $_.PSChildName } | Out-File -FilePath $log -Append

Log 'Playback endpoints:'
Get-PnpDevice -Class AudioEndpoint -ErrorAction SilentlyContinue |
    Where-Object { $_.Status -eq 'OK' } |
    Select-Object FriendlyName |
    Format-Table -AutoSize |
    Out-String | Out-File -FilePath $log -Append

Log "Finished. See $log"

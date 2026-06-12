param(
    [string]$Version = '390.77',
    [string]$FileName = '390.77-quadro-grid-desktop-notebook-win10-64bit-international-whql.exe'
)

$ErrorActionPreference = 'Stop'
$url = "https://us.download.nvidia.com/Windows/Quadro_Certified/$Version/$FileName"
$dest = "$env:TEMP\nvidia-quadro-$Version.exe"

Write-Host "Checking $url ..."
$head = Invoke-WebRequest -Uri $url -Method Head -UseBasicParsing
$expected = [int64]$head.Headers['Content-Length']
Write-Host "Expected size: $expected bytes ($([math]::Round($expected/1MB,1)) MB)"

if (Test-Path $dest) { Remove-Item $dest -Force }

Write-Host "Downloading with BITS (reliable transfer)..."
Import-Module BitsTransfer -ErrorAction SilentlyContinue
Start-BitsTransfer -Source $url -Destination $dest -DisplayName "NVIDIA Quadro $Version" -Description "Driver download"

$actual = (Get-Item $dest).Length
Write-Host "Downloaded: $actual bytes ($([math]::Round($actual/1MB,1)) MB)"

if ($actual -ne $expected) {
    Write-Error "SIZE MISMATCH! Expected $expected got $actual. File is corrupt."
    Remove-Item $dest -Force
    exit 1
}

Write-Host "VERIFIED OK: $dest"
exit 0

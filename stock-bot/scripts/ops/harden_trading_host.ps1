# Harden this PC for overnight paper/live bots (sleep + Wi-Fi).
# Run elevated once:  powershell -ExecutionPolicy Bypass -File scripts\ops\harden_trading_host.ps1
# Prefer Ethernet for trading. Wi-Fi is fallback only.

$ErrorActionPreference = "Continue"
Write-Host "=== Trading host harden ===" -ForegroundColor Cyan

# 1) Never idle-sleep on AC/DC
powercfg /change standby-timeout-ac 0 | Out-Null
powercfg /change standby-timeout-dc 0 | Out-Null
powercfg /change hibernate-timeout-ac 0 | Out-Null
powercfg /change hibernate-timeout-dc 0 | Out-Null
Write-Host "OK  Sleep/hibernate idle timeouts set to Never (AC+DC)"

# 2) Wireless adapter: Maximum Performance on AC+DC (GUID group + setting)
#    Subgroup: Wireless Adapter Settings
#    Setting:  Power Saving Mode -> 0 = Maximum Performance
$wifiSub = "19cbb8fa-5279-450e-9fac-8a3d5edad876"
$wifiSet = "12bbebe6-58d6-4636-95bb-3217ef867c1a"
powercfg /SETACVALUEINDEX SCHEME_CURRENT $wifiSub $wifiSet 0 | Out-Null
powercfg /SETDCVALUEINDEX SCHEME_CURRENT $wifiSub $wifiSet 0 | Out-Null
powercfg /SETACTIVE SCHEME_CURRENT | Out-Null
Write-Host "OK  Wi-Fi power saving -> Maximum Performance (AC+DC)"

# 3) Prefer not to sleep NIC to save power (best-effort; needs admin)
$adapters = Get-NetAdapter -Physical -ErrorAction SilentlyContinue |
  Where-Object { $_.Status -ne "Not Present" }
foreach ($a in $adapters) {
  try {
    Disable-NetAdapterPowerManagement -Name $a.Name -ErrorAction Stop
    Write-Host "OK  Disabled power management on $($a.Name)"
  } catch {
    Write-Host "SKIP power mgmt on $($a.Name): $($_.Exception.Message)"
  }
}

# 4) Status
Write-Host ""
Write-Host "Adapters:" -ForegroundColor Yellow
Get-NetAdapter | Select-Object Name, Status, LinkSpeed | Format-Table -AutoSize
Write-Host "Active profile:" -ForegroundColor Yellow
Get-NetConnectionProfile | Select-Object Name, InterfaceAlias, IPv4Connectivity | Format-Table -AutoSize

Write-Host @"

Notes
- Prefer Ethernet while bots run (you are safer on a cable).
- Keep-awake + network guard load when paper/live supervisors restart.
- Start-menu Sleep / lid close can still suspend the PC — avoid those overnight.
- Re-run this after Windows updates if Wi-Fi starts dropping again.
"@

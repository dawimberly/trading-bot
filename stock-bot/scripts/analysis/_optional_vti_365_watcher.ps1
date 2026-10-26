$pidPath = "c:\Users\Owner\PythonTrading\stock-bot\scripts\analysis\_optional_vti_365.pid"
$log = "c:\Users\Owner\PythonTrading\stock-bot\scripts\analysis\_optional_vti_365.log"
$err = "c:\Users\Owner\PythonTrading\stock-bot\scripts\analysis\_optional_vti_365.err.log"
$out = "c:\Users\Owner\PythonTrading\stock-bot\scripts\analysis\_optional_vti_365.watcher.log"
$bp = Get-Content $pidPath
while ($true) {
  $alive = Get-Process -Id $bp -ErrorAction SilentlyContinue
  $child = Get-CimInstance Win32_Process -Filter "ParentProcessId=$bp" -ErrorAction SilentlyContinue
  $sz = if (Test-Path $log) { (Get-Item $log).Length } else { 0 }
  $line = "$(Get-Date -Format o) parent=$bp alive=$([bool]$alive) children=$($child.Count) log_bytes=$sz"
  Add-Content $out $line
  if (-not $alive -and -not $child) { Add-Content $out "DONE"; break }
  Start-Sleep -Seconds 120
}

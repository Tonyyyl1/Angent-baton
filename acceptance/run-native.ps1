# relay-desk native acceptance (Windows). Thin wrapper around native_runner.py.
#   powershell -NoProfile -ExecutionPolicy Bypass -File acceptance\run-native.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File acceptance\run-native.ps1 --quick
# Set $env:RELAY_ACCEPTANCE_PY to a python.exe to bypass the "py -3" launcher.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location (Split-Path -Parent $here)
$runner = Join-Path $here 'native_runner.py'
if ($env:RELAY_ACCEPTANCE_PY) {
    & $env:RELAY_ACCEPTANCE_PY $runner --invoked-via powershell @args
} else {
    & py -3 $runner --invoked-via powershell @args
}
exit $LASTEXITCODE

# Create twin_worker/.venv with Python 3.12 and install the worker (editable).
param(
    [string]$PythonVersion = "3.12"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$worker = Join-Path $root "twin_worker"
$venv = Join-Path $worker ".venv"

if (-not (Test-Path $venv)) {
    py "-$PythonVersion" -m venv $venv
}
$py = Join-Path $venv "Scripts\python.exe"
& $py -m pip install --upgrade pip
& $py -m pip install -e "$worker[mesh]"
Write-Host "worker python: $py"

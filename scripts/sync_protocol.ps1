# Copy the canonical protocol.py from the worker into the Kit extension.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$src = Join-Path $root "twin_worker\twin_worker\protocol.py"
$dst = Join-Path $root "kit-app\source\extensions\twinviz.viewer\twinviz\viewer\protocol.py"
Copy-Item $src $dst -Force
Write-Host "synced $src -> $dst"

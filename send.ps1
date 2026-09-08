# Quick launcher for sender.py - run from the air-gapped PC.
# Usage:
#   .\send.ps1                          # scan current directory
#   .\send.ps1 C:\path\to\project        # scan a specific folder
#   .\send.ps1 --chunk-size 2500         # extra args are forwarded as-is
#   .\send.ps1 --resend 5,12,47

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv-dev\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Error "Venv python not found at $venvPython - create .venv-dev first."
    exit 1
}

& $venvPython (Join-Path $PSScriptRoot "sender.py") @args

# Quick launcher for sender.py - run from the air-gapped PC.
# Usage:
#   .\send.ps1                          # scans .\outgoing_message (default - see below)
#   .\send.ps1 C:\path\to\project        # scan a specific folder instead
#   .\send.ps1 --chunk-size 2500         # extra args are forwarded as-is
#   .\send.ps1 --resend 5,12,47
#
# With no folder argument, this scans .\outgoing_message (created if it
# doesn't exist yet) instead of falling through to sender.py's own default
# of the current directory. Running bare sender.py from this project's own
# root would otherwise walk the whole repo -- including converter_config.py,
# which holds a live Telegram bot token in plaintext -- straight into the QR
# slideshow. Drop the files you actually want to send in outgoing_message.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv-dev\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Error "Venv python not found at $venvPython - create .venv-dev first."
    exit 1
}

# Does $args already include a folder to scan? sender.py's argparse accepts
# it positionally anywhere, so walk the tokens and skip past any flag that
# takes a value; anything else non-flag we find is a user-supplied root.
$valueFlags = @("--chunk-size", "--resend", "--codes-per-row")
$hasRoot = $false
for ($i = 0; $i -lt $args.Count; $i++) {
    $tok = $args[$i]
    if ($tok -like "--*") {
        if ($valueFlags -contains $tok) { $i++ }
        continue
    }
    $hasRoot = $true
    break
}

$finalArgs = $args
if (-not $hasRoot) {
    $outgoingDir = Join-Path $PSScriptRoot "outgoing_message"
    if (-not (Test-Path $outgoingDir)) {
        New-Item -ItemType Directory -Path $outgoingDir | Out-Null
    }
    $finalArgs = $args + @($outgoingDir)
}

& $venvPython (Join-Path $PSScriptRoot "sender.py") @finalArgs

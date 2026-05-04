# Full local stack: Dispatch API + Issuer Flask admin + both Telegram bots.
# Run from repo root:  .\start-bots.ps1
#
# Starts everything in one Python parent (Ctrl+C stops all). Same as:
#   python start_bots.py
#
# Requires: Python on PATH, dependencies in krab-sender and krableadsV2, .env files.

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$StartBots = Join-Path $Root "start_bots.py"

if (-not (Test-Path $StartBots)) { throw "Missing: $StartBots" }

Set-Location $Root
Write-Host "Starting full stack (python start_bots.py)..." -ForegroundColor Cyan
python start_bots.py

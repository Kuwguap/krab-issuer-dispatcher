# Sync web assets into driver-hiring-kit (run from krab-interviewer/)
$root = Split-Path $PSScriptRoot -Parent
$kit = Join-Path $root "driver-hiring-kit"
$web = Join-Path $root "web"

Copy-Item (Join-Path $web "static\styles.css") (Join-Path $kit "static\") -Force
Copy-Item (Join-Path $web "static\funnel.css") (Join-Path $kit "static\") -Force
Write-Host "Synced CSS into driver-hiring-kit/static/"
Write-Host "HTML in driver-hiring-kit/ uses relative paths — edit kit HTML manually if web/ funnel text changes."

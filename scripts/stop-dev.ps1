[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
& docker compose -f (Join-Path $projectRoot 'deploy/compose.yml') stop
if ($LASTEXITCODE -ne 0) { throw "Docker Compose exited with code $LASTEXITCODE" }
Write-Host 'PatentCompass containers stopped. Database volumes and files are preserved.'

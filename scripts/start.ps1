$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker CLI is unavailable. Install/configure Docker separately before using this deployment script.'
}
Push-Location -LiteralPath $repoRoot
try {
    docker info --format '{{.ServerVersion}}'
    if ($LASTEXITCODE -ne 0) { throw 'Docker engine is unavailable.' }
    if (-not (Test-Path -LiteralPath (Join-Path $repoRoot '.env'))) {
        python (Join-Path $PSScriptRoot 'init_env.py')
        if ($LASTEXITCODE -ne 0) { throw 'Secret generation failed.' }
    }
    python (Join-Path $PSScriptRoot 'init_env.py') --prepare-config-only
    if ($LASTEXITCODE -ne 0) { throw 'Update configuration initialization failed.' }
    docker compose up -d --build --wait --wait-timeout 240
    if ($LASTEXITCODE -ne 0) { throw 'Deployment did not reach a healthy state. Inspect docker compose ps and docker compose logs.' }
    Write-Host 'Deployment is healthy. Default address: http://127.0.0.1:28671 (or your .env WEB_PORT).'
} finally {
    Pop-Location
}

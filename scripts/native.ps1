param(
    [ValidateSet('services', 'start', 'stop-app', 'restart-app', 'stop', 'status')]
    [string]$Action = 'status',
    [int]$ApiPort = 28671,
    [int]$WebPort = 28672,
    [int]$PostgresPort = 55432,
    [int]$RedisPort = 56379,
    [string]$PythonExe = '',
    [string]$PostgresBin = 'C:\Program Files\PostgreSQL\16\bin',
    [string]$RedisExe = 'D:\phpstudy_pro\Extensions\redis3.0.504\redis-server.exe'
)
$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$runtimeRoot = Join-Path $repoRoot '.runtime'
$recordPath = Join-Path $runtimeRoot 'processes.json'
$pgDataPath = Join-Path $runtimeRoot 'postgres'
$redisDataPath = Join-Path $runtimeRoot 'redis'
$logRoot = Join-Path $runtimeRoot 'logs'
$records = @{}
if (Test-Path -LiteralPath $recordPath) {
    $loadedRecords = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
    foreach ($property in $loadedRecords.PSObject.Properties) { $records[$property.Name] = $property.Value }
}

function Save-Records {
    [IO.Directory]::CreateDirectory($runtimeRoot) | Out-Null
    $tempRecord = Join-Path $runtimeRoot ('processes-' + [guid]::NewGuid().ToString('N') + '.tmp')
    [IO.File]::WriteAllText($tempRecord, ($records | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
    if ([IO.File]::Exists($recordPath)) {
        $backupRecord = Join-Path $runtimeRoot 'processes.previous.json'
        [IO.File]::Replace($tempRecord, $recordPath, $backupRecord)
        if (Test-Path -LiteralPath $backupRecord) { Remove-Item -LiteralPath $backupRecord }
    }
    else { [IO.File]::Move($tempRecord, $recordPath) }
}

function Get-OwnedProcess([string]$Label) {
    $record = $records[$Label]
    if (-not $record) { return $null }
    $ownedProcess = Get-Process -Id ([int]$record.pid) -ErrorAction SilentlyContinue
    if (-not $ownedProcess) { return $null }
    $startedAt = $ownedProcess.StartTime.ToUniversalTime().ToString('o')
    $recordStartedAt = ([datetime]$record.started_at).ToUniversalTime().ToString('o')
    if ($ownedProcess.Path -ne $record.executable -or $startedAt -ne $recordStartedAt) {
        # A PID can be reused after a child process exits. Treat the record as
        # stale and never touch the unrelated process; callers may safely
        # replace the stale record during the next start.
        Write-Warning "PID ownership check failed for $Label; the PID was reused. The unrelated process was left untouched."
        return $null
    }
    return $ownedProcess
}

function Add-Record([string]$Label, $StartedProcess) {
    $StartedProcess.Refresh()
    $records[$Label] = @{
        pid = $StartedProcess.Id
        executable = $StartedProcess.Path
        started_at = $StartedProcess.StartTime.ToUniversalTime().ToString('o')
    }
    Save-Records
}

function Stop-OwnedApp([string]$Label) {
    $ownedProcess = Get-OwnedProcess $Label
    if (-not $ownedProcess) { return }
    # A Windows venv launcher may own a Python child which holds the listener.
    # Resolve descendants only after the recorded parent identity is verified.
    $tree = [Collections.Generic.List[object]]::new()
    $pending = [Collections.Generic.Queue[object]]::new()
    $pending.Enqueue($ownedProcess)
    while ($pending.Count -gt 0) {
        $parent = $pending.Dequeue()
        $tree.Add(@{ Id = $parent.Id; StartedAt = $parent.StartTime.ToUniversalTime(); Path = $parent.Path })
        $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($parent.Id)" -ErrorAction SilentlyContinue
        foreach ($child in $children) {
            $candidate = Get-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
            if ($candidate -and $candidate.StartTime.ToUniversalTime() -ge $parent.StartTime.ToUniversalTime()) {
                $pending.Enqueue($candidate)
            }
        }
    }
    for ($index = $tree.Count - 1; $index -ge 0; $index--) {
        $record = $tree[$index]
        $candidate = Get-Process -Id $record.Id -ErrorAction SilentlyContinue
        if ($candidate -and $candidate.Path -eq $record.Path -and $candidate.StartTime.ToUniversalTime() -eq $record.StartedAt) {
            Stop-Process -Id $candidate.Id
            $candidate.WaitForExit(10000) | Out-Null
        }
    }
}

function Assert-FreePort([int]$Port) {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($listener) { throw "Port $Port is already in use by a process not managed for this startup." }
}

function Wait-Port([int]$Port, [string]$Label) {
    for ($attempt = 0; $attempt -lt 80; $attempt++) {
        $socket = [Net.Sockets.TcpClient]::new()
        try {
            $connection = $socket.BeginConnect('127.0.0.1', $Port, $null, $null)
            if ($connection.AsyncWaitHandle.WaitOne(200)) {
                try { $socket.EndConnect($connection); return } catch {}
            }
        } finally { $socket.Dispose() }
        Start-Sleep -Milliseconds 250
    }
    throw "$Label did not open port $Port; inspect $logRoot."
}

function Start-Owned([string]$Label, [string]$Executable, [string[]]$Arguments, [string]$Directory) {
    if (Get-OwnedProcess $Label) { return }
    $startedProcess = Start-Process -FilePath $Executable -ArgumentList $Arguments -WorkingDirectory $Directory `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logRoot "$Label.stdout.log") `
        -RedirectStandardError (Join-Path $logRoot "$Label.stderr.log")
    Start-Sleep -Milliseconds 250
    if ($startedProcess.HasExited) { throw "$Label exited during startup; inspect $logRoot." }
    Add-Record $Label $startedProcess
}

if ($Action -eq 'status') {
    foreach ($label in @('postgres', 'redis', 'api', 'worker', 'beat', 'web', 'acceptance')) {
        $ownedProcess = Get-OwnedProcess $label
        [pscustomobject]@{ service = $label; running = [bool]$ownedProcess; pid = if ($ownedProcess) { $ownedProcess.Id } else { $null } }
    }
    exit 0
}

if ($Action -eq 'stop') {
    foreach ($label in @('acceptance', 'web', 'beat', 'worker', 'api', 'redis', 'postgres')) {
        $ownedProcess = Get-OwnedProcess $label
        if ($ownedProcess) {
            if ($label -eq 'postgres') {
                & (Join-Path $PostgresBin 'pg_ctl.exe') stop -D $pgDataPath -m fast -w
                if ($LASTEXITCODE -ne 0) { throw 'Project PostgreSQL shutdown failed.' }
            } elseif ($label -eq 'redis') {
                $redisCli = Join-Path (Split-Path -Parent $RedisExe) 'redis-cli.exe'
                if (Test-Path -LiteralPath $redisCli) {
                    & $redisCli -h 127.0.0.1 -p $RedisPort shutdown save
                    if ($LASTEXITCODE -ne 0) { throw 'Project Redis shutdown failed.' }
                } else {
                    Stop-Process -Id $ownedProcess.Id
                }
            } else {
                Stop-OwnedApp $label
            }
        }
        $records.Remove($label)
        Save-Records
    }
    Write-Host 'Stopped only the verified project-owned processes; data was retained.'
    exit 0
}

if ($Action -eq 'restart-app' -or $Action -eq 'stop-app') {
    foreach ($label in @('web', 'beat', 'worker', 'api')) {
        Stop-OwnedApp $label
        $records.Remove($label)
        Save-Records
    }
    if ($Action -eq 'stop-app') { exit 0 }
    $Action = 'start'
}

[IO.Directory]::CreateDirectory($logRoot) | Out-Null
[IO.Directory]::CreateDirectory($redisDataPath) | Out-Null
$envFile = Join-Path $repoRoot '.env'
if (-not (Test-Path -LiteralPath $envFile)) {
    python (Join-Path $PSScriptRoot 'init_env.py') --native --quiet-secret
    if ($LASTEXITCODE -ne 0) { throw 'Environment generation failed.' }
}
foreach ($line in Get-Content -LiteralPath $envFile) {
    if ($line -match '^([A-Z_][A-Z0-9_]*)=(.*)$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
    }
}
if (-not $env:POSTGRES_PASSWORD -or -not $env:DATABASE_URL -or -not $env:REDIS_URL) {
    throw 'Native mode requires POSTGRES_PASSWORD, DATABASE_URL, and REDIS_URL in .env.'
}
if (-not (Test-Path -LiteralPath (Join-Path $PostgresBin 'initdb.exe'))) { throw 'PostgreSQL16 initdb.exe was not found.' }
if (-not (Test-Path -LiteralPath $RedisExe)) { throw 'Redis executable was not found.' }

if (-not (Get-OwnedProcess 'postgres')) {
    Assert-FreePort $PostgresPort
    if (-not (Test-Path -LiteralPath (Join-Path $pgDataPath 'PG_VERSION'))) {
        $passwordFile = Join-Path $runtimeRoot ('pg-init-' + [guid]::NewGuid().ToString('N') + '.txt')
        try {
            [IO.File]::WriteAllText($passwordFile, $env:POSTGRES_PASSWORD, [Text.UTF8Encoding]::new($false))
            & (Join-Path $PostgresBin 'initdb.exe') -D $pgDataPath -U price_monitor --auth=scram-sha-256 --encoding=UTF8 --locale=C "--pwfile=$passwordFile"
            if ($LASTEXITCODE -ne 0) { throw 'Project PostgreSQL initialization failed.' }
        } finally {
            if (Test-Path -LiteralPath $passwordFile) { Remove-Item -LiteralPath $passwordFile }
        }
    }
    & (Join-Path $PostgresBin 'pg_ctl.exe') start -D $pgDataPath -l (Join-Path $logRoot 'postgres.log') -o "-h 127.0.0.1 -p $PostgresPort" -w
    if ($LASTEXITCODE -ne 0) { throw 'Project PostgreSQL startup failed.' }
    $postgresPid = [int](Get-Content -LiteralPath (Join-Path $pgDataPath 'postmaster.pid') -First 1)
    Add-Record 'postgres' (Get-Process -Id $postgresPid)
}

$oldPgPassword = $env:PGPASSWORD
try {
    $env:PGPASSWORD = $env:POSTGRES_PASSWORD
    $dbExists = & (Join-Path $PostgresBin 'psql.exe') -h 127.0.0.1 -p $PostgresPort -U price_monitor -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='price_monitor'"
    if ($LASTEXITCODE -ne 0) { throw 'Project PostgreSQL authentication failed.' }
    if ($dbExists -ne '1') {
        & (Join-Path $PostgresBin 'createdb.exe') -h 127.0.0.1 -p $PostgresPort -U price_monitor price_monitor
        if ($LASTEXITCODE -ne 0) { throw 'Project database creation failed.' }
    }
} finally { $env:PGPASSWORD = $oldPgPassword }

if (-not (Get-OwnedProcess 'redis')) {
    Assert-FreePort $RedisPort
    $redisConfig = Join-Path $redisDataPath 'redis.conf'
    $redisSettings = @"
bind 127.0.0.1
port $RedisPort
dir .
appendonly yes
appendfsync everysec
save 900 1
save 300 10
save 60 10000
"@
    [IO.File]::WriteAllText($redisConfig, $redisSettings, [Text.UTF8Encoding]::new($false))
    Start-Owned 'redis' $RedisExe @('redis.conf') $redisDataPath
    Wait-Port $RedisPort 'Redis'
}

if ($Action -eq 'services') {
    Write-Host "Project PostgreSQL is on 127.0.0.1:$PostgresPort; Redis is on 127.0.0.1:$RedisPort."
    exit 0
}

$defaultPythonExe = Join-Path $repoRoot '.venv\Scripts\python.exe'
$activationPath = Join-Path $repoRoot 'data\update\activation.json'
if (-not $PythonExe -and (Test-Path -LiteralPath $activationPath)) {
    $resolverPython = if (Test-Path -LiteralPath $defaultPythonExe) { $defaultPythonExe } else { (Get-Command python -ErrorAction Stop).Source }
    $resolvedPython = & $resolverPython (Join-Path $PSScriptRoot 'active_python.py') --root $repoRoot
    if ($LASTEXITCODE -ne 0) { throw 'The signed application runtime activation could not be verified.' }
    $PythonExe = ([string]$resolvedPython).Trim()
}
$appPythonExe = if ($PythonExe) { [IO.Path]::GetFullPath($PythonExe) } else { $defaultPythonExe }
if (-not (Test-Path -LiteralPath $appPythonExe)) { throw 'Create the root .venv or provide -PythonExe and install backend/requirements.txt first.' }
$nodeExecutable = (Get-Command node -ErrorAction Stop).Source
$viteEntry = Join-Path $repoRoot 'frontend\node_modules\vite\bin\vite.js'
if (-not (Test-Path -LiteralPath $viteEntry)) { throw 'Run npm ci in frontend first.' }
if (-not (Get-OwnedProcess 'api')) { Assert-FreePort $ApiPort }
Start-Owned 'api' $appPythonExe @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$ApiPort") (Join-Path $repoRoot 'backend')
Wait-Port $ApiPort 'API'
Start-Owned 'worker' $appPythonExe @('-m', 'celery', '-A', 'app.tasks:celery_app', 'worker', '--pool=solo', '--concurrency=1', '--loglevel=info') (Join-Path $repoRoot 'backend')
Start-Owned 'beat' $appPythonExe @('-m', 'celery', '-A', 'app.tasks:celery_app', 'beat', '--loglevel=info', '--max-interval=30', '--schedule', ('"' + (Join-Path $runtimeRoot 'celerybeat-schedule') + '"')) (Join-Path $repoRoot 'backend')
if (-not (Get-OwnedProcess 'web')) { Assert-FreePort $WebPort }
Start-Owned 'web' $nodeExecutable @(('"' + $viteEntry + '"'), '--host', '127.0.0.1', '--port', "$WebPort", '--strictPort') (Join-Path $repoRoot 'frontend')
Wait-Port $WebPort 'Website'
Write-Host "Native development website: http://127.0.0.1:$WebPort"
Write-Host 'Native Windows Celery solo mode is for development; use Linux containers for deployment.'

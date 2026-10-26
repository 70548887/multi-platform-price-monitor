param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [ValidateSet('native', 'compose')][string]$Mode = 'native',
    [string]$PythonExe = '',
    [string]$DataDir = '',
    [string]$BackupDir = '',
    [string]$HealthUrl = 'http://127.0.0.1:28671/api/health',
    [ValidateRange(1, 300)][int]$PollSeconds = 5,
    [switch]$Once,
    [switch]$Foreground
)
$ErrorActionPreference = 'Stop'
$resolvedRoot = [IO.Path]::GetFullPath($Root)
if (-not $DataDir) { $DataDir = Join-Path $resolvedRoot 'data\update' }
if (-not $BackupDir) { $BackupDir = Join-Path $resolvedRoot 'data\backups' }
$resolvedData = [IO.Path]::GetFullPath($DataDir)
$resolvedBackup = [IO.Path]::GetFullPath($BackupDir)
$dataBoundary = [IO.Path]::GetFullPath((Join-Path $resolvedRoot 'data')) + [IO.Path]::DirectorySeparatorChar
foreach ($path in @($resolvedData, $resolvedBackup)) {
    if (-not $path.StartsWith($dataBoundary, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Updater runtime and backup directories must be inside this project data directory.'
    }
    [IO.Directory]::CreateDirectory($path) | Out-Null
    $identitySid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & icacls.exe $path '/inheritance:r' '/grant:r' "*${identitySid}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not protect the updater runtime directory.' }
}
if (-not $PythonExe) {
    $PythonExe = Join-Path $resolvedRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $PythonExe)) { $PythonExe = (Get-Command python -ErrorAction Stop).Source }
}
$scriptPath = Join-Path $resolvedRoot 'scripts\update_host.py'
$arguments = @('-u', ('"' + $scriptPath + '"'), '--root', ('"' + $resolvedRoot + '"'),
    '--data-dir', ('"' + $resolvedData + '"'), '--backup-dir', ('"' + $resolvedBackup + '"'),
    '--mode', $Mode, '--poll-seconds', "$PollSeconds", '--health-url', ('"' + $HealthUrl + '"'))
if ($Once) { $arguments += '--once' }
if ($Foreground -or $Once) {
    # Invoke with raw arguments in the foreground; Start-Process quoting is
    # deliberately not reused by PowerShell's native argument binder.
    $foregroundArguments = @('-u', $scriptPath, '--root', $resolvedRoot, '--data-dir', $resolvedData,
        '--backup-dir', $resolvedBackup, '--mode', $Mode, '--poll-seconds', "$PollSeconds", '--health-url', $HealthUrl)
    if ($Once) { $foregroundArguments += '--once' }
    & $PythonExe @foregroundArguments
    exit $LASTEXITCODE
}
$updaterProcess = Start-Process -FilePath $PythonExe -ArgumentList $arguments -WorkingDirectory $resolvedRoot `
    -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $resolvedData 'host.stdout.log') `
    -RedirectStandardError (Join-Path $resolvedData 'host.stderr.log')
Start-Sleep -Milliseconds 350
if ($updaterProcess.HasExited) { throw 'Host updater exited during startup; inspect data/update/host.stderr.log.' }
Write-Host "Host updater started with PID $($updaterProcess.Id). Application services were not restarted."

# Windows PowerShell 5.1 and PowerShell 7. No downloads, reinstall or elevation.
[CmdletBinding()]
param([ValidateRange(5, 300)][int] $TimeoutSeconds = 90)

$ErrorActionPreference = 'Stop'
$PotbotRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$PotbotStopMutex = $null
$PotbotStartMutex = $null
$PotbotStopEvent = $null
$PotbotRuntimeEvent = $null
$PotbotOwnsStopMutex = $false
$PotbotOwnsStartMutex = $false

function Wait-PotbotLauncher([int] $Milliseconds) {
    if (-not $PotbotStartMutex) { return $true }
    try { return $PotbotStartMutex.WaitOne($Milliseconds) }
    catch [Threading.AbandonedMutexException] { return $true }
}

try {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw 'Use the installed Python with -m potbot.startup stop on Linux or macOS.'
    }
    $PotbotSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $PotbotStopMutex = [Threading.Mutex]::new($false, ('Global\Potbot.Stop.' + $PotbotSid))
    try { $PotbotOwnsStopMutex = $PotbotStopMutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $PotbotOwnsStopMutex = $true }
    if (-not $PotbotOwnsStopMutex) { throw 'Another stop.bat is already stopping Potbot.' }
    try { $PotbotStopEvent = [Threading.EventWaitHandle]::OpenExisting('Global\Potbot.Start.Stop.' + $PotbotSid) }
    catch [Threading.WaitHandleCannotBeOpenedException] { }
    try { $PotbotRuntimeEvent = [Threading.EventWaitHandle]::OpenExisting('Global\Potbot.Start.Runtime.' + $PotbotSid) }
    catch [Threading.WaitHandleCannotBeOpenedException] { }
    try { $PotbotStartMutex = [Threading.Mutex]::OpenExisting('Global\Potbot.Start.' + $PotbotSid) }
    catch [Threading.WaitHandleCannotBeOpenedException] { }
    if ($PotbotStopEvent) { $PotbotStopEvent.Set() | Out-Null }

    # A dependency installation may be in flight. Let that command finish, then
    # the start script sees the stop event before launching the application.
    if ($PotbotStartMutex -and (-not $PotbotRuntimeEvent -or -not $PotbotRuntimeEvent.WaitOne(0))) {
        [Console]::Error.WriteLine('[Potbot] Cancelling startup; waiting for the current setup command to finish.')
        $PotbotOwnsStartMutex = Wait-PotbotLauncher ($TimeoutSeconds * 1000)
        if (-not $PotbotOwnsStartMutex) {
            throw 'Setup is still finishing. Startup cancellation remains requested; run stop.bat again before restarting.'
        }
    }
    $PotbotPython = Join-Path $PotbotRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $PotbotPython -PathType Leaf) {
        $env:POTBOT_PROJECT_ROOT = $PotbotRoot
        Push-Location -LiteralPath $PotbotRoot
        try {
            & $PotbotPython -m potbot.startup stop
            $PotbotCode = $LASTEXITCODE
        }
        finally { Pop-Location }
        if ($PotbotCode -ne 0) { exit $PotbotCode }
    }
    elseif (Test-Path -LiteralPath (Join-Path $PotbotRoot '.potbot\service\config.json')) {
        throw 'Potbot is configured but its installed Python is missing. Restore .venv before using stop.bat.'
    }
    else {
        [Console]::Error.WriteLine('[Potbot] Potbot has not been installed; startup is cancelled.')
    }
    if ($PotbotStartMutex -and -not $PotbotOwnsStartMutex) {
        $PotbotOwnsStartMutex = Wait-PotbotLauncher ($TimeoutSeconds * 1000)
        if (-not $PotbotOwnsStartMutex) { throw 'The launcher has not exited. Run stop.bat again before restarting.' }
    }
    [Console]::Error.WriteLine('[Potbot] Shutdown complete.')
    exit 0
}
catch {
    [Console]::Error.WriteLine('[Potbot] Shutdown incomplete: ' + $_.Exception.Message)
    exit 1
}
finally {
    if ($PotbotOwnsStartMutex) { $PotbotStartMutex.ReleaseMutex() }
    if ($PotbotStartMutex) { $PotbotStartMutex.Dispose() }
    if ($PotbotStopEvent) { $PotbotStopEvent.Dispose() }
    if ($PotbotRuntimeEvent) { $PotbotRuntimeEvent.Dispose() }
    if ($PotbotOwnsStopMutex) { $PotbotStopMutex.ReleaseMutex() }
    if ($PotbotStopMutex) { $PotbotStopMutex.Dispose() }
}

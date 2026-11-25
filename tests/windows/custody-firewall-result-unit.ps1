# Exercise the real helper main block and its result pipe in child PowerShells.
# Only policy functions are replaced; no firewall changes or elevation occur.
# Runs with Windows PowerShell 5.1 or pwsh.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
$helperPath = Join-Path $PSScriptRoot '../../src/potbot/syn_release/custody_firewall.ps1'
$helperSource = [IO.File]::ReadAllText($helperPath)
$mainMarker = 'if ($RequestBase64) {'
if (-not $helperSource.Contains($mainMarker)) { throw 'Firewall helper main block was not found' }
$shellPath = (Get-Process -Id $PID).Path
$useNamedPipe = [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT

function Assert-Equal($Actual, $Expected, [string]$Message) {
    if (($Actual | ConvertTo-Json -Compress) -cne ($Expected | ConvertTo-Json -Compress)) {
        throw ($Message + '; actual=' + ($Actual | ConvertTo-Json -Compress))
    }
}

function Invoke-HelperCase([ValidateSet('success', 'failure', 'denied', 'rollback', 'invalid', 'escaped')]
    [string]$Case) {
    $overrides = @'
function Get-FirewallPlan($Request) {
    if ($script:TestCase -eq 'invalid') { throw 'Policy must not run for an invalid request' }
    if ($script:TestCase -eq 'denied') {
        throw (New-Object UnauthorizedAccessException('simulated access denied'))
    }
    return @{profile='potbot.database-firewall/1'; repair=$true; program='C:\runtime\python.exe'; blocks=@()}
}
function Invoke-FirewallRepair($Request, $Plan) {
    if ($script:TestCase -eq 'failure') { throw 'simulated rule rejected' }
    if ($script:TestCase -eq 'escaped') {
        throw ('simulated escaped error: ' + (([string][char]1) * 3000))
    }
    if ($script:TestCase -eq 'rollback') {
        $failure = New-Object InvalidOperationException('simulated original repair failure')
        $failure.Data['PotbotFirewallRollbackErrors'] = @('simulated cleanup failure')
        throw $failure
    }
    return @{profile='potbot.database-firewall/1'; repair=$false; program='C:\runtime\python.exe'; blocks=@()}
}
'@
    $overrides = "`$script:TestCase = '$Case'`n" + $overrides + "`n"
    if (-not $useNamedPipe) {
        # Restricted Linux executors may forbid the Unix socket that .NET uses
        # for named pipes. Keep the real writer/main code, substituting only the
        # client transport with the child's redirected stdout stream.
        $overrides += @'
function New-Object {
    param([string]$TypeName, [object[]]$ArgumentList)
    if ($TypeName -eq 'IO.Pipes.NamedPipeClientStream') {
        $stream = [Console]::OpenStandardOutput()
        Add-Member -InputObject $stream -MemberType ScriptMethod -Name Connect -Value { param($Timeout) }
        return $stream
    }
    Microsoft.PowerShell.Utility\New-Object -TypeName $TypeName -ArgumentList $ArgumentList
}
'@
        $overrides += "`n"
    }
    $testSource = $helperSource.Replace($mainMarker, $overrides + $mainMarker)
    $testFile = Join-Path ([IO.Path]::GetTempPath()) ('potbot-firewall-test-' + [Guid]::NewGuid().ToString('N') + '.ps1')
    $request = @{
        purpose='database'; installation_id=('a' * 32); controller_created='123456789012345678'
        controller_pid=123; authority_pid=123; port=53117
        host_address='172.27.32.1'; guest_address='172.27.44.68'
        programs=@('C:\runtime\python.exe')
    }
    if ($Case -eq 'invalid') { $request.port = 0 }
    $payload = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($request | ConvertTo-Json -Compress)))
    $pipeName = 'potbot-firewall-' + [Guid]::NewGuid().ToString('N')
    $server = $null
    $reader = $null
    $process = $null
    $started = $false
    $waiting = $null
    try {
        [IO.File]::WriteAllText($testFile, $testSource, (New-Object Text.UTF8Encoding($false)))
        if ($useNamedPipe) {
            $server = New-Object IO.Pipes.NamedPipeServerStream($pipeName, [IO.Pipes.PipeDirection]::In,
                1, [IO.Pipes.PipeTransmissionMode]::Byte, [IO.Pipes.PipeOptions]::Asynchronous)
            $waiting = $server.BeginWaitForConnection($null, $null)
        }
        # A short encoded driver avoids command-line length/quoting differences
        # between Windows PowerShell and pwsh; the full helper stays in a temp file.
        $quotedFile = $testFile.Replace("'", "''")
        $driver = "& ([ScriptBlock]::Create([IO.File]::ReadAllText('$quotedFile'))) -RequestBase64 '$payload' -Mode Repair -ResultPipe '$pipeName'"
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($driver))
        $start = New-Object Diagnostics.ProcessStartInfo
        $start.FileName = $shellPath
        $start.Arguments = '-NoLogo -NoProfile -NonInteractive -EncodedCommand ' + $encoded
        $start.UseShellExecute = $false
        $start.CreateNoWindow = $true
        $start.RedirectStandardOutput = $true
        $start.RedirectStandardError = $true
        $process = New-Object Diagnostics.Process
        $process.StartInfo = $start
        $null = $process.Start()
        $started = $true
        $null = $process.Handle
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if ($useNamedPipe) {
            if (-not $waiting.AsyncWaitHandle.WaitOne(10000)) { throw ('Helper did not connect: ' + $Case) }
            $server.EndWaitForConnection($waiting)
            $reader = New-Object IO.StreamReader($server, [Text.Encoding]::UTF8)
            $lineTask = $reader.ReadLineAsync()
            if (-not $lineTask.Wait(10000)) { throw ('Helper did not report: ' + $Case) }
        }
        if (-not $process.WaitForExit(10000)) { throw ('Helper did not exit: ' + $Case) }
        if (-not $stdout.Wait(10000) -or -not $stderr.Wait(10000)) { throw 'Helper output did not close' }
        $receiptText = if ($useNamedPipe) { $lineTask.Result } else { $stdout.Result.Trim() }
        if (-not $receiptText) { throw ('Helper returned no receipt: ' + $stderr.Result) }
        return @{
            receipt=($receiptText | ConvertFrom-Json); exit_code=$process.ExitCode
            receipt_bytes=[Text.Encoding]::UTF8.GetByteCount($receiptText + [Environment]::NewLine)
            stdout=$stdout.Result; stderr=$stderr.Result
        }
    } finally {
        if ($process) {
            if ($started -and -not $process.HasExited) { $process.Kill(); $null = $process.WaitForExit(5000) }
            $process.Dispose()
        }
        if ($reader) { $reader.Dispose() }
        if ($server) { $server.Dispose() }
        if ($waiting) { $waiting.AsyncWaitHandle.Dispose() }
        if ([IO.File]::Exists($testFile)) { [IO.File]::Delete($testFile) }
    }
}

$result = Invoke-HelperCase 'success'
Assert-Equal $result.exit_code 0 'Successful helper exit status'
Assert-Equal $result.receipt.profile 'potbot.firewall-result/1' 'Result channel contract'
Assert-Equal $result.receipt.purpose 'database' 'Result purpose'
Assert-Equal $result.receipt.operation 'complete' 'Successful operation'
Assert-Equal $result.receipt.exit_code 0 'Successful receipt status'
Assert-Equal $result.receipt.repair $false 'Receipt must contain the verified final plan'
if ($useNamedPipe) {
    Assert-Equal $result.stdout.Trim() '' 'Pipe result must not leak duplicate JSON to stdout'
}

foreach ($case in @('failure', 'denied', 'rollback', 'invalid', 'escaped')) {
    $result = Invoke-HelperCase $case
    $expectedCode = if ($case -eq 'denied') { 5 } else { 1 }
    $expectedOperation = switch ($case) {
        'denied' { 'inspect-policy' }
        'invalid' { 'validate-request' }
        default { 'repair-policy' }
    }
    $expectedMessage = switch ($case) {
        'failure' { 'simulated rule rejected' }
        'denied' { 'simulated access denied' }
        'rollback' { 'simulated original repair failure' }
        'invalid' { 'Invalid firewall request' }
        'escaped' { 'simulated escaped error: ' }
    }
    Assert-Equal $result.exit_code $expectedCode ($case + ': process exit')
    Assert-Equal $result.receipt.exit_code $expectedCode ($case + ': receipt exit')
    Assert-Equal $result.receipt.purpose 'database' ($case + ': purpose')
    Assert-Equal $result.receipt.operation $expectedOperation ($case + ': operation')
    Assert-Equal ($result.receipt.message.Contains($expectedMessage)) $true ($case + ': original error')
    if ($case -eq 'escaped') {
        Assert-Equal ($result.receipt_bytes -le 16384) $true 'Escaped receipt must fit the reader byte limit'
        Assert-Equal ($result.receipt.message.StartsWith($expectedMessage)) $true 'Truncation must retain the error start'
    } else {
        Assert-Equal ($result.receipt.message -match 'HRESULT=0x[0-9A-F]{8}') $true ($case + ': HRESULT')
    }
    if ($case -eq 'rollback') {
        Assert-Equal ($result.receipt.message.Contains('rollback: simulated cleanup failure')) $true 'Rollback diagnostics'
    }
}
Write-Output ('Custody firewall helper-result tests passed (6 child processes; native pipes=' + $useNamedPipe + ').')

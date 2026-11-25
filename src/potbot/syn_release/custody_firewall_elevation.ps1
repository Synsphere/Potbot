param(
    [string]$EncodedRepair,
    [ValidatePattern('^potbot-firewall-[0-9a-f]{32}$')][string]$ResultPipe,
    [ValidateRange(1, 180)][int]$TimeoutSeconds = 150
)

# This launcher remains unelevated. The elevated helper writes only a bounded
# diagnostic receipt to this pipe; it never writes a caller-selected file.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)

function Wait-FirewallAsync($Pending, $Process, $Clock, [int]$Timeout, [string]$Operation) {
    while (-not $Pending.AsyncWaitHandle.WaitOne(100)) {
        if ($Clock.Elapsed.TotalSeconds -ge $Timeout) {
            throw ('Timed out ' + $Operation + ' from the elevated firewall helper')
        }
        if ($Process.HasExited -and -not $Pending.AsyncWaitHandle.WaitOne(100)) {
            throw ('Elevated firewall helper exited with code ' + $Process.ExitCode +
                ' before ' + $Operation)
        }
    }
    if ($Clock.Elapsed.TotalSeconds -ge $Timeout) {
        throw ('Timed out ' + $Operation + ' from the elevated firewall helper')
    }
}

function Complete-FirewallCancellation($Pipe, $Pending, [bool]$Read) {
    # Disposing the pipe requests cancellation; its .NET Framework completion
    # callback still needs to signal the pending event. Never close it early,
    # including when IsCompleted has changed but the event is not yet signaled.
    try {
        $Pipe.Dispose()
        if ($Pending.AsyncWaitHandle.WaitOne(1000)) {
            if ($Read) {
                # EndRead releases the overlapped buffer and event, then may
                # throw the cancellation error. Keep the original failure.
                $null = $Pipe.EndRead($Pending)
            } else {
                # The connection callback releases its own overlapped state.
                # EndWaitForConnection cannot run after pipe disposal.
                $Pending.AsyncWaitHandle.Close()
            }
        }
        # If cancellation has not completed, leave its event alive for the
        # callback. This one-operation launcher exits after bounded cleanup.
    } catch {
        # Cleanup must not replace UAC cancellation or the original diagnostic.
    }
}

function Read-FirewallReceipt($Pipe, $Process, $Clock, [int]$Timeout) {
    # One UTF-8 JSON line, at most 16 KiB including its newline. Async reads
    # allow a helper that crashes or stalls to fail without hanging startup.
    $buffer = New-Object byte[] 16384
    $used = 0
    while ($used -lt $buffer.Length) {
        $pending = $Pipe.BeginRead($buffer, $used, $buffer.Length - $used, $null, $null)
        $endReadCalled = $false
        try {
            Wait-FirewallAsync $pending $Process $Clock $Timeout 'receiving its diagnostic receipt'
            $endReadCalled = $true
            $count = $Pipe.EndRead($pending)
        } finally {
            if (-not $endReadCalled) { Complete-FirewallCancellation $Pipe $pending $true }
        }
        if ($count -eq 0) { throw 'Elevated firewall helper closed its pipe without a complete receipt' }
        $end = $used + $count
        for ($index = $used; $index -lt $end; $index++) {
            if ($buffer[$index] -eq 10) {
                if ($index -ne $end - 1) { throw 'Elevated firewall helper returned extra receipt data' }
                $utf8 = New-Object Text.UTF8Encoding($false, $true)
                return $utf8.GetString($buffer, 0, $index).TrimEnd([char]13)
            }
        }
        $used = $end
    }
    throw 'Elevated firewall helper diagnostic receipt exceeded 16 KiB'
}

function New-FirewallResultPipe([string]$Name) {
    $security = New-Object IO.Pipes.PipeSecurity
    $security.SetAccessRuleProtection($true, $false)
    $owner = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $administrators = New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')
    $security.AddAccessRule((New-Object IO.Pipes.PipeAccessRule(
        $owner, [IO.Pipes.PipeAccessRights]::FullControl, 'Allow')))
    $security.AddAccessRule((New-Object IO.Pipes.PipeAccessRule(
        $administrators, [IO.Pipes.PipeAccessRights]::Write, 'Allow')))
    return New-Object IO.Pipes.NamedPipeServerStream(
        $Name, [IO.Pipes.PipeDirection]::In, 1, [IO.Pipes.PipeTransmissionMode]::Byte,
        [IO.Pipes.PipeOptions]::Asynchronous, 16384, 16384, $security)
}

function Invoke-FirewallElevation([string]$Encoded, [string]$PipeName, [int]$Timeout) {
    $pipe = $null
    $process = $null
    $connection = $null
    $endConnectionCalled = $false
    $clock = [Diagnostics.Stopwatch]::StartNew()
    try {
        $pipe = New-FirewallResultPipe $PipeName
        $connection = $pipe.BeginWaitForConnection($null, $null)
        # RunAs cannot redirect standard streams. Do not use Start-Process
        # -Wait: its job tracking can fail when crossing the elevation boundary.
        $process = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -Verb RunAs `
            -ArgumentList @('-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', $Encoded) `
            -WorkingDirectory $PSHOME -PassThru
        # Keep a process handle while the helper is alive so its exit status
        # remains available even if it finishes before a subsequent query.
        $null = $process.Handle
        Wait-FirewallAsync $connection $process $clock $Timeout 'connecting its diagnostic pipe'
        $endConnectionCalled = $true
        $pipe.EndWaitForConnection($connection)
        $connection = $null
        $line = Read-FirewallReceipt $pipe $process $clock $Timeout
        $receipt = $line | ConvertFrom-Json
        if ($receipt.profile -cne 'potbot.firewall-result/1' -or
            $receipt.purpose -cnotin @('custody', 'database') -or
            ($receipt.exit_code -isnot [int] -and $receipt.exit_code -isnot [long]) -or
            $receipt.exit_code -lt 0 -or $receipt.exit_code -gt 65535 -or
            $receipt.operation -isnot [string] -or $receipt.message -isnot [string]) {
            throw 'Elevated firewall helper returned an invalid diagnostic receipt'
        }
        if ($receipt.exit_code -eq 0 -and
            ($receipt.repair -isnot [bool] -or $receipt.repair)) {
            throw 'Elevated firewall helper did not confirm completed repair'
        }
        $remaining = [Math]::Max(0, [int](1000 * $Timeout - $clock.Elapsed.TotalMilliseconds))
        if (-not $process.WaitForExit($remaining)) {
            throw 'Elevated firewall helper did not exit after its diagnostic receipt'
        }
        $code = $process.ExitCode
        if ($null -eq $code -or $code -ne $receipt.exit_code) {
            throw ('Elevated firewall helper exit code disagrees with its receipt: ' + $code)
        }
        [Console]::Out.WriteLine($line)
        return [int]$code
    } catch {
        $failure = $_.Exception
        while ($failure) {
            if ($failure -is [ComponentModel.Win32Exception] -and
                $failure.NativeErrorCode -eq 1223) { return 1223 }
            $failure = $failure.InnerException
        }
        $message = [string]$_.Exception.Message
        if ($message.Length -gt 1500) { $message = $message.Substring(0, 1500) }
        [Console]::Error.WriteLine('Windows Firewall elevation failed: ' + $message)
        return 1
    } finally {
        if ($connection -and -not $endConnectionCalled) {
            Complete-FirewallCancellation $pipe $connection $false
        } elseif ($pipe) {
            $pipe.Dispose()
        }
        if ($process) { $process.Dispose() }
        $clock.Stop()
    }
}

if ($EncodedRepair) {
    if (-not $ResultPipe) { throw 'The firewall result pipe is required' }
    exit (Invoke-FirewallElevation $EncodedRepair $ResultPipe $TimeoutSeconds)
}

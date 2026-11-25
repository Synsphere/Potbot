# Exercises real child processes and named pipes, with only UAC launch mocked.
# Windows firewall policy is never queried or changed by this test.
param([switch]$InMemory)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '../../src/potbot/syn_release/custody_firewall_elevation.ps1')
$script:NativePipeFactory = (Get-Command New-FirewallResultPipe).ScriptBlock
$script:TestPowerShell = [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
$script:WindowsHost = [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT
$script:LaunchMode = 'child'
$script:Children = @()

function Assert-True($Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

if ($InMemory) {
    # Some CI/container policies forbid Unix sockets. Exercise the production
    # deadline, framing and launcher code with deterministic asynchronous I/O.
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Threading;
public sealed class PotbotFirewallPending : IAsyncResult {
    public int Count;
    public object AsyncState { get { return null; } }
    public WaitHandle AsyncWaitHandle { get; private set; }
    public bool CompletedSynchronously { get { return IsCompleted; } }
    public bool IsCompleted { get; private set; }
    public PotbotFirewallPending(bool ready, int count) {
        IsCompleted = ready; Count = count; AsyncWaitHandle = new ManualResetEvent(ready);
    }
}
public sealed class PotbotFirewallTestPipe : MemoryStream {
    public bool ConnectionReady = true, ReadReady = true, WasDisposed;
    public int ChunkSize = 16384;
    public PotbotFirewallTestPipe(byte[] bytes) : base(bytes) {}
    public IAsyncResult BeginWaitForConnection(AsyncCallback callback, object state) {
        return new PotbotFirewallPending(ConnectionReady, 0);
    }
    public void EndWaitForConnection(IAsyncResult pending) {}
    public override IAsyncResult BeginRead(byte[] bytes, int offset, int count, AsyncCallback callback, object state) {
        int read = ReadReady ? Read(bytes, offset, Math.Min(count, ChunkSize)) : 0;
        return new PotbotFirewallPending(ReadReady, read);
    }
    public override int EndRead(IAsyncResult pending) { return ((PotbotFirewallPending)pending).Count; }
    protected override void Dispose(bool disposing) { WasDisposed = true; base.Dispose(disposing); }
}
public sealed class PotbotFirewallTestProcess : IDisposable {
    public IntPtr Handle { get { return new IntPtr(1); } }
    public bool HasExited = true, WaitResult = true, WasDisposed, Waited;
    public int ExitCode;
    public bool WaitForExit(int milliseconds) { Waited = true; return WaitResult; }
    public void Dispose() { WasDisposed = true; }
}
'@
    function New-FirewallResultPipe([string]$Name) { return $script:TestPipe }
    function Start-Process {
        [CmdletBinding()]
        param([string]$FilePath, [string]$Verb, [string[]]$ArgumentList,
            [string]$WorkingDirectory, [switch]$PassThru)
        Assert-True ($Verb -eq 'RunAs' -and $PassThru) 'UAC launch flags changed'
        Assert-True ($WorkingDirectory -eq $PSHOME) 'System working directory is required'
        if ($script:LaunchMode -eq 'declined') {
            throw (New-Object ComponentModel.Win32Exception(1223, 'Approval cancelled'))
        }
        if ($script:LaunchMode -eq 'failure') { throw 'Simulated ShellExecute failure' }
        return $script:TestProcess
    }
    function Invoke-MemoryCase([byte[]]$Bytes, [int]$Code = 0, [string]$Scenario = '') {
        $script:TestPipe = New-Object PotbotFirewallTestPipe(,$Bytes)
        $script:TestProcess = New-Object PotbotFirewallTestProcess
        $script:TestProcess.ExitCode = $Code
        if ($Scenario -eq 'crash') { $script:TestPipe.ConnectionReady = $false }
        if ($Scenario -eq 'fragmented') { $script:TestPipe.ChunkSize = 3 }
        if ($Scenario -eq 'stall') {
            $script:TestPipe.ReadReady = $false; $script:TestProcess.HasExited = $false
        }
        if ($Scenario -eq 'wait') { $script:TestProcess.WaitResult = $false }
        $output = New-Object IO.StringWriter
        $errorOutput = New-Object IO.StringWriter
        $oldOut = [Console]::Out; $oldError = [Console]::Error
        try {
            [Console]::SetOut($output); [Console]::SetError($errorOutput)
            $code = Invoke-FirewallElevation 'encoded' ('potbot-firewall-' + ('a' * 32)) 1
            Assert-True $script:TestPipe.WasDisposed 'Pipe was not disposed'
            if ($script:LaunchMode -eq 'child') {
                Assert-True $script:TestProcess.WasDisposed 'Process handle was not disposed'
            }
            return [pscustomobject]@{ Code=$code; Output=$output.ToString(); Error=$errorOutput.ToString() }
        } finally {
            [Console]::SetOut($oldOut); [Console]::SetError($oldError)
            $output.Dispose(); $errorOutput.Dispose()
        }
    }
    function Get-ReceiptBytes([int]$Code = 0, [string]$Message = '', [bool]$Repair = $false) {
        $line = @{
            profile='potbot.firewall-result/1'; purpose='database'; exit_code=$Code
            operation='verify-policy'; message=$Message; repair=$Repair
        } | ConvertTo-Json -Compress
        return ,([Text.Encoding]::UTF8.GetBytes($line + "`n"))
    }
    $good = Get-ReceiptBytes
    $result = Invoke-MemoryCase $good
    Assert-True ($result.Code -eq 0 -and -not $result.Error) ('Success failed: ' + $result.Error)
    $result = Invoke-MemoryCase $good 0 'fragmented'
    Assert-True ($result.Code -eq 0) 'Fragmented UTF-8 receipt must survive multiple asynchronous reads'
    $result = Invoke-MemoryCase (Get-ReceiptBytes 1 'New-NetFirewallRule failed (0x80070005)') 1
    Assert-True ($result.Code -eq 1 -and $result.Output -like '*0x80070005*') 'Helper diagnostics lost'
    $script:LaunchMode = 'declined'
    Assert-True ((Invoke-MemoryCase $good).Code -eq 1223) 'UAC cancellation code changed'
    $script:LaunchMode = 'failure'
    $result = Invoke-MemoryCase $good
    Assert-True ($result.Error -like '*Simulated ShellExecute failure*') 'Launcher diagnostic lost'
    $script:LaunchMode = 'child'
    $result = Invoke-MemoryCase $good 7 'crash'
    Assert-True ($result.Error -like '*exited with code 7*') 'Early child exit must not wait for timeout'
    $result = Invoke-MemoryCase ([byte[]]@())
    Assert-True ($result.Error -like '*without a complete receipt*') 'Missing receipt accepted'
    $result = Invoke-MemoryCase ([Text.Encoding]::UTF8.GetBytes('{broken' + "`n"))
    Assert-True ($result.Code -eq 1) 'Malformed JSON accepted'
    $result = Invoke-MemoryCase ([byte[]]@(255, 10))
    Assert-True ($result.Code -eq 1) 'Invalid UTF-8 accepted'
    $result = Invoke-MemoryCase ($good + $good)
    Assert-True ($result.Error -like '*extra receipt data*') 'Multiple receipts accepted'
    $result = Invoke-MemoryCase ([Text.Encoding]::UTF8.GetBytes(('A' * 16384)))
    Assert-True ($result.Error -like '*exceeded 16 KiB*') 'Oversized receipt accepted'
    $result = Invoke-MemoryCase $good 9
    Assert-True ($result.Error -like '*disagrees with its receipt: 9*') 'Conflicting exit status accepted'
    $result = Invoke-MemoryCase (Get-ReceiptBytes 0 '' $true)
    Assert-True ($result.Error -like '*did not confirm completed repair*') 'Incomplete repair accepted'
    $result = Invoke-MemoryCase $good 0 'wait'
    Assert-True ($result.Error -like '*did not exit after*') 'A stuck process was accepted'
    $result = Invoke-MemoryCase $good 0 'stall'
    Assert-True ($result.Error -like '*Timed out*') 'A stuck read exceeded its deadline'
    Write-Output 'Custody firewall elevation tests passed (15 in-memory cases).'
    return
}

function New-FirewallResultPipe([string]$Name) {
    if ($script:WindowsHost) { return & $script:NativePipeFactory $Name }
    # Unix has no Windows DACL constructor; use its local named-pipe transport.
    return New-Object IO.Pipes.NamedPipeServerStream(
        $Name, [IO.Pipes.PipeDirection]::In, 1, [IO.Pipes.PipeTransmissionMode]::Byte,
        [IO.Pipes.PipeOptions]::Asynchronous, 16384, 16384)
}

function Start-Process {
    [CmdletBinding()]
    param([string]$FilePath, [string]$Verb, [string[]]$ArgumentList,
        [string]$WorkingDirectory, [switch]$PassThru)
    Assert-True ($Verb -eq 'RunAs' -and $PassThru) 'The helper must request UAC explicitly'
    Assert-True ($WorkingDirectory -eq $PSHOME) 'Elevation must use a known system directory'
    Assert-True ($FilePath -eq (Join-Path $PSHOME 'powershell.exe')) 'Use only system PowerShell'
    if ($script:LaunchMode -eq 'declined') {
        throw (New-Object ComponentModel.Win32Exception(1223, 'Approval cancelled'))
    }
    if ($script:LaunchMode -eq 'failure') { throw 'Simulated ShellExecute failure' }
    $child = Microsoft.PowerShell.Management\Start-Process -FilePath $script:TestPowerShell `
        -ArgumentList $ArgumentList -WorkingDirectory $PSScriptRoot -PassThru
    $script:Children += $child.Id
    return $child
}

function Invoke-Case([string]$Body, [int]$Timeout = 15) {
    $name = 'potbot-firewall-' + [Guid]::NewGuid().ToString('N')
    $source = $Body.Replace('PIPE_NAME', $name)
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($source))
    $output = New-Object IO.StringWriter
    $errorOutput = New-Object IO.StringWriter
    $oldOut = [Console]::Out
    $oldError = [Console]::Error
    try {
        [Console]::SetOut($output)
        [Console]::SetError($errorOutput)
        $code = Invoke-FirewallElevation $encoded $name $Timeout
        return [pscustomobject]@{ Code=$code; Output=$output.ToString(); Error=$errorOutput.ToString() }
    } finally {
        [Console]::SetOut($oldOut)
        [Console]::SetError($oldError)
        $output.Dispose()
        $errorOutput.Dispose()
    }
}

$clientPrefix = @'
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
    # Match the production helper: generic Write requests more permissions than
    # the Administrators ACE grants when UAC uses a different administrator.
    $client = New-Object IO.Pipes.NamedPipeClientStream(
        '.', 'PIPE_NAME', [IO.Pipes.PipeAccessRights]::WriteData, [IO.Pipes.PipeOptions]::None,
        [Security.Principal.TokenImpersonationLevel]::Anonymous, [IO.HandleInheritability]::None)
} else {
    $client = New-Object IO.Pipes.NamedPipeClientStream(
        '.', 'PIPE_NAME', [IO.Pipes.PipeDirection]::Out, [IO.Pipes.PipeOptions]::None,
        [Security.Principal.TokenImpersonationLevel]::Anonymous)
}
$client.Connect(5000)
'@
$receipt = @{
    profile='potbot.firewall-result/1'; purpose='database'; exit_code=0
    operation='verify-policy'; message=''; repair=$false
} | ConvertTo-Json -Compress
$writer = @'
$bytes = [Text.Encoding]::UTF8.GetBytes('RECEIPT' + "`n")
try { $client.Write($bytes, 0, $bytes.Length); $client.Flush() } finally { $client.Dispose() }
exit EXIT_CODE
'@

try {
    if ($script:WindowsHost) {
        $aclPipe = & $script:NativePipeFactory ('potbot-firewall-' + [Guid]::NewGuid().ToString('N'))
        try {
            $rules = $aclPipe.GetAccessControl().GetAccessRules(
                $true, $false, [Security.Principal.SecurityIdentifier])
            $adminRules = @($rules | Where-Object {
                $_.IdentityReference.Value -eq 'S-1-5-32-544' -and $_.AccessControlType -eq 'Allow'
            })
            Assert-True ($adminRules.Count -eq 1) 'The pipe must have one explicit Administrators ACE'
            $rights = $adminRules[0].PipeAccessRights
            Assert-True (($rights -band [IO.Pipes.PipeAccessRights]::WriteData) -ne 0) `
                'Administrators must be allowed to write the diagnostic receipt'
            Assert-True (($rights -band [IO.Pipes.PipeAccessRights]::CreateNewInstance) -eq 0) `
                'Administrators must not gain permission to create another pipe server instance'
        } finally {
            $aclPipe.Dispose()
        }
    }
    $successBody = $clientPrefix + "`n" + $writer.Replace('RECEIPT', $receipt).Replace('EXIT_CODE', '0')
    $result = Invoke-Case $successBody
    Assert-True ($result.Code -eq 0 -and -not $result.Error) ('Success failed: ' + $result.Error)
    Assert-True (($result.Output | ConvertFrom-Json).repair -eq $false) 'Success receipt was lost'

    $failedReceipt = $receipt.Replace('"exit_code":0', '"exit_code":1').Replace(
        '"message":""', '"message":"New-NetFirewallRule: access denied (0x80070005)"')
    $result = Invoke-Case ($clientPrefix + "`n" + $writer.Replace(
        'RECEIPT', $failedReceipt).Replace('EXIT_CODE', '1'))
    Assert-True ($result.Code -eq 1 -and $result.Output -like '*access denied*') `
        'The actual elevated failure must reach the parent'

    $script:LaunchMode = 'declined'
    $result = Invoke-Case 'exit 0'
    Assert-True ($result.Code -eq 1223) 'UAC cancellation must retain its exact exit code'
    $script:LaunchMode = 'failure'
    $result = Invoke-Case 'exit 0'
    Assert-True ($result.Code -eq 1 -and $result.Error -like '*Simulated ShellExecute failure*') `
        'Launcher errors must not disappear'
    $script:LaunchMode = 'child'

    $result = Invoke-Case 'exit 7'
    Assert-True ($result.Code -eq 1 -and $result.Error -like '*exited with code 7*') `
        'A helper exiting before connection must fail promptly with its exit code'

    $result = Invoke-Case ($clientPrefix + "`n" + '$client.Dispose(); exit 8')
    Assert-True ($result.Code -eq 1 -and $result.Error -like '*without a complete receipt*') `
        'Empty receipts cannot imply a successful repair'

    $result = Invoke-Case ($clientPrefix + "`n" + $writer.Replace(
        'RECEIPT', '{broken').Replace('EXIT_CODE', '0'))
    Assert-True ($result.Code -eq 1) 'Malformed JSON cannot imply a successful repair'

    $result = Invoke-Case ($successBody.Replace('exit 0', 'exit 9'))
    Assert-True ($result.Code -eq 1 -and $result.Error -like '*disagrees with its receipt: 9*') `
        'The real child exit code must agree with the receipt'

    $oversized = $clientPrefix + "`n" + @'
$bytes = New-Object byte[] 16384
for ($i = 0; $i -lt $bytes.Length; $i++) { $bytes[$i] = 65 }
try { $client.Write($bytes, 0, $bytes.Length); $client.Flush() } finally { $client.Dispose() }
exit 0
'@
    $result = Invoke-Case $oversized
    Assert-True ($result.Code -eq 1 -and $result.Error -like '*exceeded 16 KiB*') `
        'Receipt reading must have a fixed memory bound'

    $result = Invoke-Case ($clientPrefix + "`n" + 'Start-Sleep -Seconds 10; $client.Dispose(); exit 0') 2
    Assert-True ($result.Code -eq 1 -and $result.Error -like '*Timed out*') `
        'A connected helper that stalls must fail within the deadline'
    Write-Output 'Custody firewall elevation transport tests passed (10 cases).'
} finally {
    foreach ($childId in $script:Children) {
        $child = Get-Process -Id $childId -ErrorAction SilentlyContinue
        if ($child -and -not $child.HasExited) { Stop-Process -Id $childId -Force }
        if ($child) { $child.Dispose() }
    }
}

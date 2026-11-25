"""Stop the exact per-user scheduled task and its verified legacy process tree.

New supervisors use the instance lease and a Windows job. This helper handles an
upgrade from supervisors that predate those mechanisms, without selecting other
Python/Node processes by executable name or trusting a stale PID file.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

from potbot.domain.errors import CapabilityUnavailableError

_WINDOWS = os.name == "nt"

_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$Request = ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__REQUEST__')))
$Owned = New-Object 'System.Collections.Generic.List[Object]'
try {
    $CurrentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $Scheduler = New-Object -ComObject 'Schedule.Service'
    $Scheduler.Connect()
    $Task = $null
    try { $Task = $Scheduler.GetFolder('\').GetTask('Potbot-User-Service') }
    catch {
        $Failure = $_.Exception
        while ($Failure.InnerException) { $Failure = $Failure.InnerException }
        if (($Failure.HResult -band 0xFFFF) -ne 2) { throw }
    }
    function Normalize-Path([string] $Value) {
        return [IO.Path]::GetFullPath($Value.Trim('"')).TrimEnd('\').ToLowerInvariant()
    }
    if ($null -ne $Task) {
        $Principal = $Task.Definition.Principal.UserId
        try { $Sid = (New-Object Security.Principal.SecurityIdentifier($Principal)).Value }
        catch {
            $Sid = (New-Object Security.Principal.NTAccount($Principal)).Translate(
                [Security.Principal.SecurityIdentifier]).Value
        }
        if ($Sid -cne $CurrentSid -or $Task.Definition.Actions.Count -ne 1) { exit 20 }
        $Action = $Task.Definition.Actions.Item(1)
        if ($Action.Type -ne 0) { exit 20 }
        if ((Normalize-Path $Action.Path) -cne (Normalize-Path $Request.python)) { exit 20 }
    }

    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;
public sealed class PotbotStopHandle : IDisposable {
    [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetProcessTimes(IntPtr process, out long create, out long exit, out long kernel, out long user);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool TerminateProcess(IntPtr process, uint code);
    [DllImport("kernel32.dll", SetLastError=true)] static extern uint WaitForSingleObject(IntPtr handle, uint timeout);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [DllImport("shell32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr CommandLineToArgvW(string text, out int count);
    [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr value);
    IntPtr handle;
    public readonly uint Pid;
    public readonly long Created;
    public PotbotStopHandle(uint pid) {
        Pid = pid;
        handle = OpenProcess(0x00101001, false, pid); // query-limited, synchronize, terminate
        if (handle == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
        long created, exited, kernel, user;
        if (!GetProcessTimes(handle, out created, out exited, out kernel, out user)) {
            Dispose(); throw new Win32Exception(Marshal.GetLastWin32Error());
        }
        Created = created;
    }
    public bool Matches(DateTime observed) { return Created / 10 == observed.ToUniversalTime().ToFileTimeUtc() / 10; }
    public bool ParentOf(DateTime observed) {
        bool alive = Alive;
        long created, exited, kernel, user;
        if (!GetProcessTimes(handle, out created, out exited, out kernel, out user))
            throw new Win32Exception(Marshal.GetLastWin32Error());
        long child = observed.ToUniversalTime().ToFileTimeUtc() / 10;
        return child >= created / 10 && (alive || child <= exited / 10);
    }
    public bool Alive {
        get {
            uint result = WaitForSingleObject(handle, 0);
            if (result == 0xFFFFFFFF) throw new Win32Exception(Marshal.GetLastWin32Error());
            return result == 258;
        }
    }
    public void Stop() {
        if (Alive && !TerminateProcess(handle, 1) && Alive)
            throw new Win32Exception(Marshal.GetLastWin32Error());
    }
    public void Dispose() {
        if (handle != IntPtr.Zero) { CloseHandle(handle); handle = IntPtr.Zero; }
    }
    public static string[] Arguments(string text) {
        int count;
        IntPtr argv = CommandLineToArgvW(text, out count);
        if (argv == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
        try {
            var result = new string[count];
            for (int i=0; i<count; i++) result[i] = Marshal.PtrToStringUni(Marshal.ReadIntPtr(argv, i * IntPtr.Size));
            return result;
        } finally { LocalFree(argv); }
    }
}
'@
    function Test-ServiceArguments([string[]] $Words) {
        return ($Words.Count -eq 5 -and $Words[1] -ceq '-m' -and
            $Words[2] -ceq 'potbot.startup' -and $Words[3] -ceq 'service' -and
            $Words[4] -ceq 'run')
    }
    if ($null -ne $Task -and -not (Test-ServiceArguments ([PotbotStopHandle]::Arguments('python ' + $Action.Arguments)))) { exit 20 }
    function Test-DesktopArguments([string[]] $Words) {
        return ($Request.include_desktop -eq $true -and $Words.Count -in @(3, 4) -and
            $Words[1] -ceq '-m' -and $Words[2] -ceq 'potbot.startup' -and
            ($Words.Count -eq 3 -or $Words[3] -ceq 'start'))
    }

    function Capture-Process($Row) {
        $Handle = $null
        try {
            $Owner = Invoke-CimMethod -InputObject $Row -MethodName GetOwnerSid
            if ($Owner.ReturnValue -ne 0 -or $Owner.Sid -cne $CurrentSid) { throw 'Process owner changed' }
            $Handle = New-Object PotbotStopHandle([uint32]$Row.ProcessId)
            if (-not $Handle.Matches($Row.CreationDate)) { throw 'Process identity changed' }
            $Owned.Add($Handle)
            return $true
        }
        catch {
            if ($null -ne $Handle) { $Handle.Dispose() }
            # Short-lived probes can exit between the snapshot and OpenProcess.
            # Only absence permits a skip; a reused PID or access failure fails.
            $Now = @(Get-CimInstance Win32_Process -Filter ('ProcessId=' + $Row.ProcessId))
            if ($Now.Count -eq 0) { return $false }
            throw
        }
    }
    function Capture-Tree {
        $Rows = @(Get-CimInstance Win32_Process)
        foreach ($Row in $Rows) {
            if ($Row.ProcessId -eq $Request.caller_pid -or $Row.ProcessId -eq $PID) { continue }
            if (-not $Row.ExecutablePath -or -not $Row.CommandLine) { continue }
            if ((Normalize-Path $Row.ExecutablePath) -cne (Normalize-Path $Request.python)) { continue }
            $Words = [PotbotStopHandle]::Arguments($Row.CommandLine)
            if (-not (Test-ServiceArguments $Words) -and -not (Test-DesktopArguments $Words)) { continue }
            if ((Normalize-Path $Words[0]) -cne (Normalize-Path $Request.python)) { continue }
            $Known = @($Owned | Where-Object { $_.Pid -eq $Row.ProcessId -and $_.Matches($Row.CreationDate) })
            if ($Known.Count -eq 0) { $null = Capture-Process $Row }
        }
        do {
            $Added = $false
            foreach ($Row in $Rows) {
                if ($Row.ProcessId -eq $Request.caller_pid -or $Row.ProcessId -eq $PID) { continue }
                # The managed database is stopped through pg_ctl after the
                # application exits; never force-kill it with a legacy tree.
                if ($Row.ExecutablePath -and (Normalize-Path $Row.ExecutablePath).StartsWith(
                    (Normalize-Path $Request.postgres) + '\')) { continue }
                $Known = @($Owned | Where-Object { $_.Pid -eq $Row.ProcessId -and $_.Matches($Row.CreationDate) })
                if ($Known.Count -ne 0) { continue }
                $Parent = @($Owned | Where-Object { $_.Pid -eq $Row.ParentProcessId -and $_.ParentOf($Row.CreationDate) })
                if ($Parent.Count -ne 1) { continue }
                if (Capture-Process $Row) { $Added = $true }
            }
        } while ($Added)
    }

    # Pin every verified identity before asking the task manager to end it.
    # Parent creation/exit times also distinguish a raced child from an unrelated
    # process whose parent PID was recycled after that generation exited.
    Capture-Tree
    if ($null -ne $Task -and [int]$Task.State -in @(2, 4)) { $Task.Stop(0) }
    $Deadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        Capture-Tree
        foreach ($Handle in $Owned) { $Handle.Stop() }
        Start-Sleep -Milliseconds 100
        $Live = @($Owned | Where-Object { $_.Alive })
        if ([DateTime]::UtcNow -gt $Deadline) { exit 22 }
    } while ($Live.Count -ne 0 -or [int]$Task.State -in @(2, 4))
    # Final enumeration closes the gap for children created just before /End.
    Capture-Tree
    foreach ($Handle in $Owned) { $Handle.Stop() }
    foreach ($Handle in $Owned) {
        while ($Handle.Alive) {
            if ([DateTime]::UtcNow -gt $Deadline) { exit 22 }
            Start-Sleep -Milliseconds 100
        }
    }
    if ($null -eq $Task -and $Owned.Count -eq 0) { Write-Output 'POTBOT_TASK_STOP=absent' }
    else { Write-Output 'POTBOT_TASK_STOP=stopped' }
    exit 0
}
catch { exit 21 }
finally { foreach ($Handle in $Owned) { $Handle.Dispose() } }
'''


def stop_registered_task(root: Path, *, include_desktop: bool = False) -> None:
    """End only this checkout's task, after all new instance leases are idle."""
    if not _WINDOWS:
        return
    from potbot.syn_release.host import windows_environment

    root = root.resolve(strict=True)
    request = {
        "python": str(root / ".venv" / "Scripts" / "python.exe"),
        "postgres": str(root / ".potbot" / "native-postgres"),
        "include_desktop": include_desktop,
        "caller_pid": os.getpid(),
    }
    payload = base64.b64encode(json.dumps(request, ensure_ascii=True).encode("utf-8")).decode("ascii")
    command = _SCRIPT.replace("__REQUEST__", payload)
    encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    argv = [
        str(shell), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
        "Bypass", "-EncodedCommand", encoded,
    ]
    if len(subprocess.list2cmdline(argv)) >= 32760:
        raise CapabilityUnavailableError("Potbot's Windows stop command exceeds its size limit.")
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=90,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env=windows_environment(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise CapabilityUnavailableError(
            "The Windows Potbot task could not be stopped and verified; shutdown is incomplete."
        ) from None
    if result.returncode == 20:
        raise CapabilityUnavailableError(
            "Potbot-User-Service belongs to a different user, checkout or command; "
            "it was not stopped. Run stop.bat from its owning checkout."
        )
    if result.returncode != 0 or result.stdout.strip() not in {
        b"POTBOT_TASK_STOP=absent", b"POTBOT_TASK_STOP=stopped",
    }:
        raise CapabilityUnavailableError(
            "Windows could not verify every Potbot task/process identity or its exit. "
            "Shutdown is incomplete; close the earlier launch terminal and run stop.bat again."
        )

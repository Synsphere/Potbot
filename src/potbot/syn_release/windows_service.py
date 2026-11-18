"""User-owned Windows lifetime task for the native WSL operator service."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from potbot.domain.errors import StateMismatchError

from .host import WslOperatorHost, run_host
from .windows_custody import (
    SYN_HOST_CUSTODY_SOURCE_COMMIT,
    prepare_windows_host_custody,
)

_LEGACY_INSTALL_SHA256 = "f421766875e51a9fe56377a2465298b8ea45b1912b698689dc0849ff945cabd1"

INSTALL = r"""
param([Parameter(Mandatory=$true)][string]$WslExecutable,
      [Parameter(Mandatory=$true)][string]$InstallationId,
      [Parameter(Mandatory=$true)][string]$BrokerPython,
      [Parameter(Mandatory=$true)][string]$BrokerProject,
      [Parameter(Mandatory=$true)][string]$Runner,
      [Parameter(Mandatory=$true)][string]$Client,
      [Parameter(Mandatory=$true)][string]$LegacyWaitScript)
$ErrorActionPreference = 'Stop'
if ($InstallationId -notmatch '^[0-9a-f]{32}$') { throw 'Invalid managed host identity' }
foreach ($item in @($WslExecutable,$BrokerPython,$Runner,$Client)) {
    if (-not (Test-Path -LiteralPath $item -PathType Leaf)) {
        throw 'Managed lifetime executable is missing'
    }
}
if (-not (Test-Path -LiteralPath $BrokerProject -PathType Container)) {
    throw 'Managed host-custody project is missing'
}
function Quote-Argument([string]$Value) {
    if ($Value.Contains('"')) { throw 'Managed lifetime path contains a quote' }
    return '"' + $Value + '"'
}
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$taskName = 'Potbot-SynOperator'
$arguments = '-I -B ' + (Quote-Argument $Runner) +
    ' --wsl ' + (Quote-Argument $WslExecutable) +
    ' --project ' + (Quote-Argument $BrokerProject) +
    ' --client ' + (Quote-Argument $Client)
$description = 'Potbot managed Syn operator with Windows DPAPI custody; host ' +
    $InstallationId + '; no automatic restart after faults.'
$powershell = Join-Path $PSHOME 'powershell.exe'
$legacyArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -File "' +
    $LegacyWaitScript + '" -WslExecutable "' + $WslExecutable + '"'
$legacyDescription = 'Potbot managed Syn operator; host ' + $InstallationId +
    '; no automatic restart after faults.'
$existing = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue
if ($existing) {
    $principalId = $existing.Principal.UserId
    if ($principalId -notmatch '^S-1-') {
        $principalId = ([Security.Principal.NTAccount]$principalId).Translate(
            [Security.Principal.SecurityIdentifier]).Value
    }
    if ($principalId -cne $sid -or $existing.Principal.LogonType -ne 'Interactive' -or
        $existing.Principal.RunLevel -ne 'Limited' -or
        $existing.Settings.ExecutionTimeLimit -ne 'PT0S' -or
        $existing.Settings.RestartCount -ne 0 -or @($existing.Actions).Count -ne 1) {
        throw 'Existing task differs from this managed host'
    }
    $current = $existing.Actions[0]
    $matchesCurrent = (
        $existing.Description -ceq $description -and
        $current.Execute -ceq $BrokerPython -and
        $current.Arguments -ceq $arguments
    )
    $matchesLegacy = (
        $existing.Description -ceq $legacyDescription -and
        $current.Execute -ceq $powershell -and
        $current.Arguments -ceq $legacyArguments
    )
    if ($matchesLegacy) {
        Unregister-ScheduledTask -TaskName $taskName -TaskPath '\' -Confirm:$false
        $existing = $null
    } elseif (-not $matchesCurrent) {
        throw 'Existing task differs from this managed host'
    }
}
if (-not $existing) {
    $action = New-ScheduledTaskAction -Execute $BrokerPython -Argument $arguments
    $principal = New-ScheduledTaskPrincipal -UserId $sid -LogonType Interactive -RunLevel Limited
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $sid
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $taskName -TaskPath '\' -Action $action `
        -Principal $principal -Trigger $trigger -Settings $settings -Description $description |
        Out-Null
}
Start-ScheduledTask -TaskName $taskName -TaskPath '\'
@{profile='potbot.syn-wsl-lifetime/2';task=$taskName;state='start_requested';
  custody='windows-dpapi-host';automatic_fault_restart=$false} | ConvertTo-Json -Compress
"""

# Retained for the existing Windows PowerShell parser qualification. The actual
# lifetime wrapper is now the verified Python RUNNER_SOURCE staged with custody.
WAIT = r"""
$ErrorActionPreference = 'Stop'
Write-Error 'Potbot Syn operator lifetime now uses the Windows DPAPI custody runner.'
exit 2
"""


def _write_managed_script(path: Path, raw: bytes) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink():
            raise StateMismatchError("Existing Windows lifetime helper is redirected")
        current = path.read_bytes()
        if current == raw:
            return
        if hashlib.sha256(current).hexdigest() != _LEGACY_INSTALL_SHA256:
            raise StateMismatchError("Existing Windows lifetime helper differs")
        temporary = path.with_name("." + path.name + ".pending")
        temporary.unlink(missing_ok=True)
        with temporary.open("xb") as output:
            output.write(raw)
        temporary.replace(path)
        return
    with path.open("xb") as output:
        output.write(raw)


def install_windows_service(manager: WslOperatorHost) -> dict[str, object]:
    record = json.loads(manager.receipt.read_text())
    identity = record.get("installation_id", "")
    if not re.fullmatch(r"[0-9a-f]{32}", identity):
        raise StateMismatchError("Managed WSL host lacks its retained identity")

    runtime = prepare_windows_host_custody(manager.root)
    script = manager.root / "operator-user-task.ps1"
    _write_managed_script(script, INSTALL.encode("utf-8"))

    powershell = str(Path(manager.wsl).parent / "WindowsPowerShell/v1.0/powershell.exe")
    result = json.loads(
        run_host(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(script),
                "-WslExecutable",
                manager.wsl,
                "-InstallationId",
                identity,
                "-BrokerPython",
                str(runtime["python"]),
                "-BrokerProject",
                str(runtime["project"]),
                "-Runner",
                str(runtime["runner"]),
                "-Client",
                str(runtime["client"]),
                "-LegacyWaitScript",
                str(manager.root / "operator-lifetime.ps1"),
            ]
        )
    )
    if (
        result.get("profile") != "potbot.syn-wsl-lifetime/2"
        or result.get("task") != "Potbot-SynOperator"
        or result.get("custody") != "windows-dpapi-host"
    ):
        raise StateMismatchError("Windows lifetime task did not return its exact receipt")
    return {
        **dict(result),
        "syn_host_custody_source_commit": SYN_HOST_CUSTODY_SOURCE_COMMIT,
    }

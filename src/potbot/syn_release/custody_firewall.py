"""Narrow Windows Firewall repair for managed custody/database TCP listeners.

Only the firewall helper is elevated. The authority and its DPAPI owner remain
the interactive user, and neither tokens nor vault material enter this helper.
"""

from __future__ import annotations

import base64
import gzip
import ipaddress
import json
import ntpath
import os
import re
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from potbot.domain.errors import CapabilityUnavailableError

if TYPE_CHECKING:
    from .host import WslOperatorHost


class _InspectionNeedsAdmin(CapabilityUnavailableError):
    """Windows denied read-only firewall/process inspection to the current user."""


def _encode_command(source: str) -> str:
    # Compress the fixed helper before encoding: nested RunAs launch commands
    # otherwise exceed Windows' 32,767-character process command-line limit.
    compressed = base64.b64encode(gzip.compress(source.encode("utf-8"), mtime=0)).decode("ascii")
    loader = (
        "$b=[Convert]::FromBase64String('" + compressed + "');"
        "$m=New-Object IO.MemoryStream(,$b);"
        "$g=New-Object IO.Compression.GZipStream($m,[IO.Compression.CompressionMode]::Decompress);"
        "$r=New-Object IO.StreamReader($g,[Text.Encoding]::UTF8);"
        "& ([ScriptBlock]::Create($r.ReadToEnd()))"
    )
    return base64.b64encode(loader.encode("utf-16-le")).decode("ascii")


def _encoded_script(
    request: dict[str, Any], mode: str, *, result_pipe: str | None = None,
) -> str:
    if mode not in {"Inspect", "Repair"}:
        raise ValueError("Unknown custody firewall operation")
    # JSON is data, never PowerShell source. Base64 contains no quotes or escapes.
    payload = base64.b64encode(json.dumps(request).encode()).decode("ascii")
    source = Path(__file__).with_suffix(".ps1").read_text(encoding="utf-8")
    script = f"& {{\n{source}\n}} -RequestBase64 '{payload}' -Mode '{mode}'"
    if result_pipe is not None:
        if mode != "Repair" or re.fullmatch(r"potbot-firewall-[0-9a-f]{32}", result_pipe) is None:
            raise ValueError("Invalid firewall result pipe")
        script += f" -ResultPipe '{result_pipe}'"
    return _encode_command(script)


def _run(
    powershell: str, source: str, *, timeout: float, command: bool = False,
) -> subprocess.CompletedProcess[str]:
    from .host import windows_environment

    argv = [powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
            "-Command" if command else "-EncodedCommand", source]
    if len(subprocess.list2cmdline(argv)) >= 32760:
        raise CapabilityUnavailableError("Windows Firewall helper exceeds the process command limit")
    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
        env=windows_environment(),
        cwd=str(Path(powershell).parent),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _inspect(powershell: str, request: dict[str, Any]) -> dict[str, Any]:
    purpose = request.get("purpose", "custody")
    result = _run(powershell, _encoded_script(request, "Inspect"), timeout=30)
    if result.returncode == 5:
        raise _InspectionNeedsAdmin("Windows requires administrator access to inspect its firewall")
    if result.returncode or len(result.stdout) > 65536:
        # The helper only emits public network/policy diagnostics, never credentials.
        detail = " ".join(result.stderr.split())[-600:]
        raise CapabilityUnavailableError(f"Windows Firewall {purpose} check failed: " + detail)
    try:
        plan = json.loads(result.stdout.lstrip("\ufeff"))
        if (
            plan.get("profile") != f"potbot.{purpose}-firewall/1"
            or type(plan.get("repair")) is not bool
            or not isinstance(plan.get("blocks"), list)
            or ntpath.normcase(plan["program"])
            not in {ntpath.normcase(item) for item in request["programs"]}
        ):
            raise ValueError
        return dict(plan)
    except (KeyError, TypeError, ValueError):
        raise CapabilityUnavailableError("Windows Firewall returned an invalid check result") from None


def _elevate(powershell: str, request: dict[str, Any]) -> int:
    purpose = request.get("purpose", "custody")
    pipe_name = "potbot-firewall-" + uuid.uuid4().hex
    encoded = _encoded_script(request, "Repair", result_pipe=pipe_name)
    # The only elevated executable is the system PowerShell. No downloaded
    # interpreter, repository path or user-controlled command is run as admin.
    # Revalidation inside the helper prevents a late UAC acceptance after the
    # requesting authority/controller has gone away from changing the firewall.
    # RunAs cannot redirect the elevated child's console streams. The launcher
    # relays a bounded, one-way diagnostic receipt over a private local pipe.
    # This carries firewall status only; no keystore protocol or data uses it.
    source = Path(__file__).with_name("custody_firewall_elevation.ps1").read_text(encoding="utf-8")
    wrapper = f"& {{\n{source}\n}} -EncodedRepair '{encoded}' -ResultPipe '{pipe_name}'"
    result = _run(powershell, wrapper, timeout=180, command=True)
    if result.returncode == 1223:
        return 1223
    detail = " ".join(result.stderr.split())[-3000:]
    try:
        if len(result.stdout) > 16384:
            raise ValueError
        receipt = json.loads(result.stdout.lstrip("\ufeff"))
        if (
            not isinstance(receipt, dict)
            or receipt.get("profile") != "potbot.firewall-result/1"
            or receipt.get("purpose") != purpose
            or type(receipt.get("exit_code")) is not int
            or receipt["exit_code"] != result.returncode
            or not isinstance(receipt.get("message"), str)
            or len(receipt["message"]) > 4096
            or not isinstance(receipt.get("operation"), str)
            or re.fullmatch(r"[a-z-]{1,64}", receipt["operation"]) is None
            or (result.returncode == 0 and receipt.get("repair") is not False)
        ):
            raise ValueError
    except (TypeError, ValueError):
        if result.returncode:
            raise CapabilityUnavailableError(
                f"Windows Firewall {purpose} administrator repair failed "
                f"(exit {result.returncode}): " + (detail or "the elevated helper returned no diagnostic")
            ) from None
        raise CapabilityUnavailableError(
            f"Windows Firewall {purpose} administrator repair returned no valid completion result"
            + (": " + detail if detail else "")
        ) from None
    if result.returncode:
        message = " ".join(receipt["message"].split())
        raise CapabilityUnavailableError(
            f"Windows Firewall {purpose} administrator repair failed "
            f"(exit {result.returncode}, {receipt['operation']}): "
            + (message or detail or "no diagnostic returned")
        )
    return 0


def _programs(python: Path) -> list[str]:
    from .host import windows_environment

    result = subprocess.run(
        [str(python), "-I", "-B", "-c",
         "import json,sys;print(json.dumps([sys.executable,sys._base_executable]))"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
        env=windows_environment(),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        values = json.loads(result.stdout)
        if result.returncode or not isinstance(values, list) or not 1 <= len(values) <= 2:
            raise ValueError
        for value in values:
            if (
                not isinstance(value, str)
                or not ntpath.isabs(value)
                or not re.match(r"^[A-Za-z]:\\", value)
                or ntpath.basename(value).lower() not in {"python.exe", "pythonw.exe"}
            ):
                raise ValueError
        return list(dict.fromkeys(values))
    except (ValueError, TypeError):
        raise CapabilityUnavailableError("Cannot identify the managed Python executable") from None


def ensure_custody_firewall(
    manager: WslOperatorHost, *, python: Path, host: str, guest: str, port: int,
    authority_pid: int, controller_created: int, tcp_probe: Callable[[], bool],
    stage: Callable[[str], None], purpose: Literal["custody", "database"] = "custody",
) -> None:
    """Inspect a managed Python listener; elevate only after a failed WSL probe.

    Custody's existing rule identity is retained. The database relay uses its
    own rule and diagnostics; neither purpose changes the listener's protocol.
    """
    if purpose not in {"custody", "database"}:
        raise CapabilityUnavailableError("Unsupported managed firewall purpose")
    prefix = purpose + "-firewall"
    identity = manager._read_receipt().get("installation_id")
    if not isinstance(identity, str) or re.fullmatch(r"[0-9a-f]{32}", identity) is None:
        raise CapabilityUnavailableError(
            f"{purpose.title()} firewall requires the managed WSL host identity"
        )
    for value in (host, guest):
        address = ipaddress.ip_address(value)
        if not isinstance(address, ipaddress.IPv4Address) or not any(
            address in ipaddress.ip_network(network)
            for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        ):
            raise CapabilityUnavailableError(
                f"{purpose.title()} firewall requires private WSL IPv4 addresses"
            )
    powershell = str(Path(manager.wsl).parent / "WindowsPowerShell/v1.0/powershell.exe")
    stage(prefix)
    try:
        request = {
            "purpose": purpose,
            "installation_id": identity, "host_address": host, "guest_address": guest,
            "port": port, "authority_pid": authority_pid, "controller_pid": os.getpid(),
            "controller_created": str(controller_created), "programs": _programs(python),
        }
        problem: CapabilityUnavailableError | None = None
        try:
            plan = _inspect(powershell, request)
        except CapabilityUnavailableError as exc:
            problem, plan = exc, {}
        # A successful live TCP connection is decisive: no firewall mutation or
        # elevation is necessary even if policy inspection is restricted.
        if tcp_probe():
            return
        if problem is not None and not isinstance(problem, _InspectionNeedsAdmin):
            raise problem
        if problem is None and not plan["repair"]:
            raise CapabilityUnavailableError(
                f"Windows Firewall already permits {purpose}, but the WSL TCP probe failed; "
                "check VPN/third-party filtering and the WSL network"
            )
        details = (
            f"Inspect effective Windows policy and repair only matching {purpose} rules. "
            if problem is not None else
            f"Allow TCP from {guest} to {host} for {plan['program']}; "
            f"repair {len(plan['blocks'])} matching local application block rule(s). "
        )
        print(f"[Potbot {purpose}] Windows Firewall repair requires administrator approval. "
              + details + "Approve the Windows UAC prompt to continue.", flush=True)
        stage(prefix + "-admin")
        result = _elevate(powershell, request)
        if result == 1223:
            stage(prefix + "-declined")
            raise CapabilityUnavailableError(
                f"Windows Firewall administrator approval was declined for {purpose}; "
                "run start.bat again "
                "and approve the firewall repair"
            )
        if result:
            raise CapabilityUnavailableError(
                f"Windows Firewall {purpose} administrator repair failed; review the elevated "
                "helper's error and the effective firewall policy"
            )
        try:
            after = _inspect(powershell, request)
        except _InspectionNeedsAdmin:
            # The elevated helper already rechecked effective policy. This
            # caller may still lack read access; verify the real TCP path below.
            after = {"repair": False}
        if after["repair"]:
            raise CapabilityUnavailableError(
                f"Windows Firewall {purpose} repair did not become effective"
            )
        # Allow Windows Filtering Platform a short convergence window.
        for attempt in range(3):
            if tcp_probe():
                return
            if attempt < 2:
                time.sleep(0.5)
        raise CapabilityUnavailableError(
            f"Windows Firewall was repaired, but WSL-to-Windows {purpose} TCP still fails; "
            "check VPN/third-party filtering and the WSL network"
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        stage(prefix + "-failed")
        raise CapabilityUnavailableError(
            f"Windows Firewall {purpose} check/administrator repair did not finish; retry start.bat"
        ) from exc
    except CapabilityUnavailableError as exc:
        if "approval was declined" not in str(exc):
            stage(prefix + "-failed")
        raise

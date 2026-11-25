from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from potbot.domain.errors import CapabilityUnavailableError
from potbot.syn_release import custody_firewall as firewall


@pytest.fixture
def setup_firewall(monkeypatch):
    stages, elevations = [], []
    manager = SimpleNamespace(
        wsl="C:/Windows/System32/wsl.exe",
        _read_receipt=lambda: {"installation_id": "a" * 32},
    )
    monkeypatch.setattr(firewall, "_programs", lambda _python: [r"C:\uv\python.exe"])
    monkeypatch.setattr(firewall.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(firewall, "_elevate", lambda _ps, request: elevations.append(request) or 0)
    options = dict(
        python=Path("C:/custody/.venv/Scripts/python.exe"), host="172.27.32.1",
        guest="172.27.44.68", port=57880, authority_pid=123,
        controller_created=123456789012345678, stage=stages.append,
    )
    return manager, options, stages, elevations


def plan(repair=True):
    return {"repair": repair, "program": r"C:\uv\python.exe", "blocks": ["app-tcp-block"]}


def test_working_tcp_never_requests_elevation(setup_firewall, monkeypatch):
    manager, options, stages, elevations = setup_firewall
    inspections = []
    monkeypatch.setattr(firewall, "_inspect", lambda *_args: inspections.append(1) or plan())
    firewall.ensure_custody_firewall(manager, **options, tcp_probe=lambda: True)
    assert inspections == [1]
    assert not elevations
    assert stages == ["custody-firewall"]


def test_working_tcp_survives_restricted_policy_inspection(setup_firewall, monkeypatch):
    manager, options, _stages, elevations = setup_firewall

    def inspect(*_args):
        raise CapabilityUnavailableError("policy unavailable")

    monkeypatch.setattr(firewall, "_inspect", inspect)
    firewall.ensure_custody_firewall(manager, **options, tcp_probe=lambda: True)
    assert not elevations


def test_blocked_tcp_repairs_then_rechecks_policy_and_connection(setup_firewall, monkeypatch):
    manager, options, stages, elevations = setup_firewall
    inspections = iter([plan(), plan(False)])
    probes = iter([False, False, True])
    monkeypatch.setattr(firewall, "_inspect", lambda *_args: next(inspections))
    firewall.ensure_custody_firewall(manager, **options, tcp_probe=lambda: next(probes))
    assert stages == ["custody-firewall", "custody-firewall-admin"]
    assert len(elevations) == 1
    request = elevations[0]
    assert request["port"] == 57880
    assert request["programs"] == [r"C:\uv\python.exe"]
    assert set(request) == {
        "installation_id", "host_address", "guest_address", "port", "authority_pid",
        "controller_pid", "controller_created", "programs", "purpose",
    }
    assert request["purpose"] == "custody"


def test_uac_decline_has_specific_failure_and_no_retry(setup_firewall, monkeypatch):
    manager, options, stages, _elevations = setup_firewall
    monkeypatch.setattr(firewall, "_inspect", lambda *_args: plan())
    monkeypatch.setattr(firewall, "_elevate", lambda *_args: 1223)
    with pytest.raises(CapabilityUnavailableError, match="approval was declined"):
        firewall.ensure_custody_firewall(manager, **options, tcp_probe=lambda: False)
    assert stages[-1] == "custody-firewall-declined"


def test_no_uac_when_effective_policy_already_allows_tcp(setup_firewall, monkeypatch):
    manager, options, stages, elevations = setup_firewall
    monkeypatch.setattr(firewall, "_inspect", lambda *_args: plan(False))
    with pytest.raises(CapabilityUnavailableError, match="already permits"):
        firewall.ensure_custody_firewall(manager, **options, tcp_probe=lambda: False)
    assert not elevations
    assert stages[-1] == "custody-firewall-failed"


def test_unsupported_policy_never_elevates(setup_firewall, monkeypatch):
    manager, options, _stages, elevations = setup_firewall

    def inspect(*_args):
        raise CapabilityUnavailableError("Group policy disallows exceptions")

    monkeypatch.setattr(firewall, "_inspect", inspect)
    with pytest.raises(CapabilityUnavailableError, match="Group policy"):
        firewall.ensure_custody_firewall(manager, **options, tcp_probe=lambda: False)
    assert not elevations


def test_denied_inspection_elevates_only_when_tcp_also_fails(setup_firewall, monkeypatch):
    manager, options, stages, elevations = setup_firewall

    def inspect(*_args):
        raise firewall._InspectionNeedsAdmin("read access denied")

    monkeypatch.setattr(firewall, "_inspect", inspect)
    probes = iter([False, True])
    firewall.ensure_custody_firewall(manager, **options, tcp_probe=lambda: next(probes))
    assert len(elevations) == 1
    assert stages == ["custody-firewall", "custody-firewall-admin"]


def test_denied_inspection_with_working_tcp_never_elevates(setup_firewall, monkeypatch):
    manager, options, _stages, elevations = setup_firewall

    def inspect(*_args):
        raise firewall._InspectionNeedsAdmin("read access denied")

    monkeypatch.setattr(firewall, "_inspect", inspect)
    firewall.ensure_custody_firewall(manager, **options, tcp_probe=lambda: True)
    assert not elevations


def test_access_denied_status_is_distinct_from_other_inspection_failures(monkeypatch):
    request = {"programs": [r"C:\uv\python.exe"]}
    monkeypatch.setattr(firewall, "_run", lambda *_args, **_kw: SimpleNamespace(returncode=5))
    with pytest.raises(firewall._InspectionNeedsAdmin):
        firewall._inspect("powershell.exe", request)


@pytest.mark.parametrize("failure", [1, subprocess.TimeoutExpired("powershell", 180)])
def test_failed_or_timed_out_repair_stops_startup(setup_firewall, monkeypatch, failure):
    manager, options, stages, _elevations = setup_firewall
    monkeypatch.setattr(firewall, "_inspect", lambda *_args: plan())

    def elevate(*_args):
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(firewall, "_elevate", elevate)
    with pytest.raises(CapabilityUnavailableError):
        firewall.ensure_custody_firewall(manager, **options, tcp_probe=lambda: False)
    assert stages[-1] == "custody-firewall-failed"


def test_repair_is_not_success_until_real_tcp_works(setup_firewall, monkeypatch):
    manager, options, stages, _elevations = setup_firewall
    inspections = iter([plan(), plan(False)])
    monkeypatch.setattr(firewall, "_inspect", lambda *_args: next(inspections))
    with pytest.raises(CapabilityUnavailableError, match="TCP still fails"):
        firewall.ensure_custody_firewall(manager, **options, tcp_probe=lambda: False)
    assert stages[-1] == "custody-firewall-failed"


@pytest.mark.parametrize("address", ["0.0.0.0", "127.0.0.1", "8.8.8.8", "169.254.1.2", "::1"])
def test_firewall_exception_cannot_target_public_or_loopback_network(setup_firewall, address):
    manager, options, _stages, elevations = setup_firewall
    options["host"] = address
    with pytest.raises(CapabilityUnavailableError, match="private WSL IPv4"):
        firewall.ensure_custody_firewall(manager, **options, tcp_probe=lambda: False)
    assert not elevations


def test_uac_command_fits_windows_and_carries_only_public_identity(monkeypatch):
    request = {
        "installation_id": "a" * 32, "host_address": "172.27.32.1",
        "guest_address": "172.27.44.68", "port": 57880, "authority_pid": 456,
        "controller_pid": 123, "controller_created": "123456789012345678",
        "programs": [r"C:\Users\Kieran\AppData\Roaming\uv\python\cpython-3.12\python.exe",
                     r"C:\Users\Kieran\AppData\Local\Potbot\SynOperator\.venv\python.exe"],
    }
    commands = []
    monkeypatch.setattr(
        firewall, "_run",
        lambda _ps, encoded, **_kwargs: commands.append(encoded)
        or SimpleNamespace(returncode=1223),
    )
    assert firewall._elevate("powershell.exe", request) == 1223
    assert len(commands[0]) < 32000
    assert len(firewall._encoded_script(request, "Repair")) < 32000
    assert len(subprocess.list2cmdline([
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", commands[0],
    ])) < 32760
    assert "-Verb RunAs" in commands[0]


def test_actual_venv_and_base_interpreter_are_discovered(monkeypatch):
    def run(command, **kwargs):
        assert command[1:3] == ["-I", "-B"]
        assert "POTBOT_PARENT_KEYSTORE_SERVER_TOKEN" not in kwargs["env"]
        return SimpleNamespace(
            returncode=0, stdout=json.dumps([r"C:\venv\python.exe", r"C:\uv\python.exe"])
        )

    monkeypatch.setattr(firewall.subprocess, "run", run)
    monkeypatch.setenv("SystemRoot", "C:/Windows")
    monkeypatch.setenv("POTBOT_PARENT_KEYSTORE_SERVER_TOKEN", "must-not-reach-helper")
    assert firewall._programs(Path("python.exe")) == [r"C:\venv\python.exe", r"C:\uv\python.exe"]


def test_database_relay_uses_its_own_stage_labels_and_controller_identity(
    setup_firewall, monkeypatch, capsys,
):
    manager, options, stages, elevations = setup_firewall
    options["authority_pid"] = os.getpid()
    inspections = iter([plan(), plan(False)])
    probes = iter([False, True])
    monkeypatch.setattr(firewall, "_inspect", lambda *_args: next(inspections))
    firewall.ensure_custody_firewall(
        manager, **options, purpose="database", tcp_probe=lambda: next(probes),
    )
    assert stages == ["database-firewall", "database-firewall-admin"]
    assert len(elevations) == 1
    assert elevations[0]["purpose"] == "database"
    assert elevations[0]["authority_pid"] == elevations[0]["controller_pid"] == os.getpid()
    assert "[Potbot database]" in capsys.readouterr().out


@pytest.mark.parametrize("result,stage", [
    (1223, "database-firewall-declined"), (1, "database-firewall-failed"),
])
def test_database_repair_failure_cannot_be_mistaken_for_custody(
    setup_firewall, monkeypatch, result, stage,
):
    manager, options, stages, _elevations = setup_firewall
    monkeypatch.setattr(firewall, "_inspect", lambda *_args: plan())
    monkeypatch.setattr(firewall, "_elevate", lambda *_args: result)
    with pytest.raises(CapabilityUnavailableError, match="database"):
        firewall.ensure_custody_firewall(manager, **options, purpose="database", tcp_probe=lambda: False)
    assert stages[-1] == stage
    assert not any(value.startswith("custody-") for value in stages)


@pytest.mark.parametrize("purpose", ["custody", "database"])
def test_firewall_receipt_must_match_requested_purpose(monkeypatch, purpose):
    request = {"purpose": purpose, "programs": [r"C:\uv\python.exe"]}
    response = {**plan(), "profile": f"potbot.{purpose}-firewall/1"}
    monkeypatch.setattr(firewall, "_run", lambda *_a, **_kw: SimpleNamespace(
        returncode=0, stdout=json.dumps(response), stderr="",
    ))
    assert firewall._inspect("powershell.exe", request)["profile"] == response["profile"]
    response["profile"] = "potbot.database-firewall/1" if purpose == "custody" else "potbot.custody-firewall/1"
    with pytest.raises(CapabilityUnavailableError, match="invalid check result"):
        firewall._inspect("powershell.exe", request)


def test_unsupported_purpose_is_rejected_before_any_firewall_action(setup_firewall, monkeypatch):
    manager, options, stages, elevations = setup_firewall
    monkeypatch.setattr(firewall, "_programs", lambda _: pytest.fail("unsupported purpose reached host"))
    with pytest.raises(CapabilityUnavailableError, match="Unsupported managed firewall purpose"):
        firewall.ensure_custody_firewall(manager, **options, purpose="arbitrary", tcp_probe=lambda: False)
    assert not stages and not elevations


def repair_receipt(*, code=0, purpose="database", **changes):
    return {
        "profile": "potbot.firewall-result/1", "purpose": purpose,
        "exit_code": code, "operation": "complete" if code == 0 else "repair-policy",
        "message": "", "repair": code != 0, **changes,
    }


@pytest.mark.parametrize("purpose", ["custody", "database"])
def test_elevated_repair_requires_a_matching_verified_completion(monkeypatch, purpose):
    monkeypatch.setattr(firewall, "_run", lambda *_a, **_kw: SimpleNamespace(
        returncode=0, stdout=json.dumps(repair_receipt(purpose=purpose)), stderr="",
    ))
    assert firewall._elevate("powershell.exe", {"purpose": purpose}) == 0


@pytest.mark.parametrize("stdout", [
    "", "[]", "not-json", "x" * 16385,
    json.dumps(repair_receipt(purpose="custody")),
    json.dumps(repair_receipt(repair=True)),
    json.dumps(repair_receipt(exit_code=True)),
    json.dumps(repair_receipt(exit_code=1)),
    json.dumps(repair_receipt(operation="")),
    json.dumps(repair_receipt(message="x" * 4097)),
])
def test_process_exit_zero_without_valid_receipt_cannot_count_as_repair(monkeypatch, stdout):
    monkeypatch.setattr(firewall, "_run", lambda *_a, **_kw: SimpleNamespace(
        returncode=0, stdout=stdout, stderr="",
    ))
    with pytest.raises(CapabilityUnavailableError, match="no valid completion result"):
        firewall._elevate("powershell.exe", {"purpose": "database"})


def test_uac_approved_helper_error_reaches_the_startup_failure(setup_firewall, monkeypatch):
    manager, options, stages, _elevations = setup_firewall
    # Exercise the real elevation result parser through the caller, with the
    # Windows launcher itself stubbed. This is the previously lost error path.
    monkeypatch.undo()
    monkeypatch.setattr(firewall, "_programs", lambda _python: [r"C:\uv\python.exe"])
    monkeypatch.setattr(firewall, "_inspect", lambda *_args: plan())
    message = "New-NetFirewallRule: The parameter is incorrect [HRESULT=0x80070057]"
    monkeypatch.setattr(firewall, "_run", lambda *_a, **_kw: SimpleNamespace(
        returncode=1, stdout=json.dumps(repair_receipt(code=1, message=message)), stderr="",
    ))
    with pytest.raises(CapabilityUnavailableError) as failure:
        firewall.ensure_custody_firewall(
            manager, **options, purpose="database", tcp_probe=lambda: False,
        )
    assert message in str(failure.value)
    assert "database" in str(failure.value)
    assert "exit 1, repair-policy" in str(failure.value)
    assert "declined" not in str(failure.value)
    assert stages == ["database-firewall", "database-firewall-admin", "database-firewall-failed"]


def test_elevation_launcher_exception_keeps_its_error_and_exit_code(monkeypatch):
    monkeypatch.setattr(firewall, "_run", lambda *_a, **_kw: SimpleNamespace(
        returncode=1, stdout="", stderr="Windows Firewall elevation failed: Access is denied\n",
    ))
    with pytest.raises(CapabilityUnavailableError, match=r"database.*exit 1.*Access is denied"):
        firewall._elevate("powershell.exe", {"purpose": "database"})


def test_reported_access_denial_is_not_uac_cancellation(monkeypatch):
    monkeypatch.setattr(firewall, "_run", lambda *_a, **_kw: SimpleNamespace(
        returncode=5,
        stdout=json.dumps(repair_receipt(code=5, message="Firewall provider denied access")),
        stderr="",
    ))
    with pytest.raises(CapabilityUnavailableError, match=r"exit 5.*provider denied access"):
        firewall._elevate("powershell.exe", {"purpose": "database"})


@pytest.mark.parametrize("pipe", ["..\\report.txt", "potbot-firewall-'", "", "x" * 200])
def test_result_channel_is_only_a_generated_pipe_name(pipe):
    with pytest.raises(ValueError, match="Invalid firewall result pipe"):
        firewall._encoded_script({}, "Repair", result_pipe=pipe)


def test_firewall_shell_uses_its_system_directory_and_sanitized_environment(monkeypatch):
    calls = []
    monkeypatch.setenv("SystemRoot", "C:/Windows")
    monkeypatch.setenv("POTBOT_DSN", "must-not-reach-helper")
    monkeypatch.setenv("POTBOT_PARENT_KEYSTORE_SERVER_TOKEN", "must-not-reach-helper")
    monkeypatch.setattr(firewall.subprocess, "run", lambda command, **kwargs:
                        calls.append((command, kwargs)) or SimpleNamespace(returncode=0))
    powershell = "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    firewall._run(powershell, "Write-Output 'public status'", timeout=10, command=True)
    command, kwargs = calls[0]
    assert command[0] == powershell
    assert kwargs["cwd"] == str(Path(powershell).parent)
    assert "POTBOT_DSN" not in kwargs["env"]
    assert "POTBOT_PARENT_KEYSTORE_SERVER_TOKEN" not in kwargs["env"]


def test_oversized_helper_command_is_rejected_before_windows_launch(monkeypatch):
    monkeypatch.setattr(firewall.subprocess, "run", lambda *_a, **_kw:
                        pytest.fail("oversized command reached Windows"))
    with pytest.raises(CapabilityUnavailableError, match="process command limit"):
        firewall._run("powershell.exe", "x" * 32760, timeout=10)

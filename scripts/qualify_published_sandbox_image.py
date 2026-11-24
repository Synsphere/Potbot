"""Exercise an independently verified, unpacked image on a disposable CI host."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from potbot.domain.errors import CapabilityUnavailableError
from potbot.evaluation.sandbox_worker_acceptance import write_report
from potbot.tools.ai_config_history import AiConfigHistoryMonitor
from potbot.tools.scoped_launcher import DEFAULT_SOCKET
from potbot.tools.shared_workspace_isolation import SharedWorkspaceRuntime
from potbot.tools.worker_telemetry import WorkerMonitorConfig, qualify_scoped_worker
from scripts.qualify_shared_workspace import qualify as qualify_workspace
from scripts.qualify_worker_exit_accounting import qualify as qualify_accounting
from scripts.worker_audit_fixture import monitored_cog_cgroup

_SHIPPED_FILES = {
    "opt/potbot/context_client.py": "sandbox/clients/context_client.py",
    "usr/local/libexec/potbot-worker-probe": "sandbox/qualification/worker_probe.py",
}


def _canonical_source_bytes(path: Path) -> bytes:
    # Match Git's committed LF representation for shipped Python source.
    # A Windows checkout may retain CRLF bytes after .gitattributes changed
    # while Git still reports the file clean because its clean filter normalizes it.
    payload = path.read_bytes()
    if path.suffix == ".py":
        payload = payload.replace(b"\r\n", b"\n")
    return payload


def _failed_names(section: object) -> list[str]:
    if not isinstance(section, dict):
        return []
    return sorted(
        str(name)
        for name, result in section.items()
        if not isinstance(result, dict) or result.get("passed") is not True
    )


@contextmanager
def _monitor_scope(private: Path, audit: Path):
    if os.environ.get("POTBOT_OPERATOR_SCOPED_LAUNCHER"):
        installed = WorkerMonitorConfig.from_environment(scoped_worker=True)
        if installed.scoped_launcher is None:
            raise RuntimeError("scoped launcher environment did not select an endpoint")
        yield WorkerMonitorConfig(
            audit,
            installed.cgroup_root,
            installed.strace,
            installed.cgroup_join,
            installed.strace_sha256,
            installed.cgroup_join_sha256,
            max_trace_bytes=installed.max_trace_bytes,
            sample_seconds=installed.sample_seconds,
            scoped_launcher=installed.scoped_launcher,
        )
        return
    with monitored_cog_cgroup(private, audit) as monitor:
        yield monitor


def _accounting_gate() -> dict[str, object]:
    endpoint = os.environ.get("POTBOT_OPERATOR_SCOPED_LAUNCHER")
    if endpoint:
        if Path(endpoint) != DEFAULT_SOCKET:
            return {
                "schema": 1,
                "qualified": False,
                "source": "scoped-launcher-preflight",
                "error": "InvalidEndpoint",
            }
        try:
            qualify_scoped_worker(DEFAULT_SOCKET)
        except Exception as exc:
            result: dict[str, object] = {
                "schema": 1,
                "qualified": False,
                "source": "scoped-launcher-preflight",
                "error": type(exc).__name__,
            }
            if isinstance(exc, CapabilityUnavailableError):
                prefix = "Scoped worker preflight failed during "
                message = str(exc)
                phase = message.removeprefix(prefix) if message.startswith(prefix) else ""
                if phase in {
                    "monitor-config",
                    "cgroup-create",
                    "cgroup-readback",
                    "launcher-allocation",
                    "collector-probe",
                    "cleanup",
                }:
                    result["phase"] = phase
            return result
        return {
            "schema": 1,
            "qualified": True,
            "source": "scoped-launcher-preflight",
        }

    try:
        return qualify_accounting()
    except Exception as exc:
        return {
            "schema": 1,
            "qualified": False,
            "source": "linux-generic-netlink-taskstats",
            "error": type(exc).__name__,
        }


def _failure_summary(
    accounting: object,
    smoke: object,
    operator: object,
    worker: object,
) -> dict[str, object]:
    if not isinstance(accounting, dict) or accounting.get("qualified") is not True:
        result: dict[str, object] = {"stage": "taskstats"}
        if isinstance(accounting, dict):
            for name in ("source", "error", "phase"):
                value = accounting.get(name)
                if isinstance(value, str):
                    result[name] = value
        return result
    if not isinstance(smoke, dict) or smoke.get("qualified_smoke") is not True:
        return {
            "stage": "workspace-smoke",
            "failed_tests": _failed_names(smoke.get("tests") if isinstance(smoke, dict) else None),
        }
    if not isinstance(operator, dict) or operator.get("qualified") is not True:
        return {
            "stage": "operator-gate",
            "failed_checks": _failed_names(
                operator.get("checks") if isinstance(operator, dict) else None
            ),
        }
    if not isinstance(worker, dict) or worker.get("qualified_smoke") is not True:
        details = None
        if isinstance(worker, dict):
            details = worker.get("checks", worker.get("tests"))
        return {
            "stage": "worker-fork-smoke",
            "failed_checks": _failed_names(details),
        }
    return {"stage": "unknown"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rootfs", required=True, type=Path)
    parser.add_argument("--scratch", required=True, type=Path)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--image-source", required=True)
    parser.add_argument("--consumer", required=True)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    rootfs = args.rootfs.resolve(strict=True)
    scratch = args.scratch.resolve(strict=True)
    if rootfs.is_relative_to(scratch) or scratch.is_relative_to(rootfs):
        raise ValueError("scratch and unpacked image must be disjoint")
    if not args.report.is_absolute() or not args.report.parent.is_relative_to(scratch):
        raise ValueError("report must stay in the disposable qualification directory")

    source_root = Path(__file__).resolve().parents[1]
    shipped = {}
    for guest, source in _SHIPPED_FILES.items():
        candidate = rootfs / guest
        if candidate.resolve(strict=True) != candidate or not candidate.is_file():
            raise ValueError(f"unexpected installed path for {source}")
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        expected = hashlib.sha256(_canonical_source_bytes(source_root / source)).hexdigest()
        if digest != expected:
            raise ValueError(f"published image bytes differ for {source}")
        shipped[guest] = digest

    home, staging, models, tools, ai, history, private, audit = (
        scratch / name
        for name in ("home", "staging", "models", "tools", "ai", "history", "private", "audit")
    )
    expected_layout = {
        home: (os.geteuid(), os.getegid(), 0o755),
        staging: (os.geteuid(), os.getegid(), 0o755),
        models: (os.geteuid(), os.getegid(), 0o755),
        tools: (os.geteuid(), 69, 0o2775),
        ai: (os.geteuid(), os.getegid(), 0o775),
        history: (os.geteuid(), os.getegid(), 0o755),
        private: (os.geteuid(), os.getegid(), 0o700),
        audit: (os.geteuid(), os.getegid(), 0o700),
    }
    for path, (uid, gid, mode) in expected_layout.items():
        if path.is_symlink():
            raise RuntimeError("qualification scratch path is redirected")
        info = path.stat()
        if (
            not path.is_dir()
            or info.st_uid != uid
            or info.st_gid != gid
            or (info.st_mode & 0o7777) != mode
        ):
            raise RuntimeError("qualification scratch ownership or mode differs")
    (home / ".tmp").mkdir(mode=0o700)
    runtime = SharedWorkspaceRuntime(
        args.image_digest,
        rootfs,
        home,
        "0" * 64,  # A candidate only. No image admission or workspace pin is changed.
        model_staging=staging,
        shared_models=models,
        shared_tools=tools,
        ai_config_dir=ai,
        ai_history_dir=history,
    )
    accounting = _accounting_gate()
    worker = operator = None
    smoke: dict[str, object] = {"qualified_smoke": False, "tests": {}}
    if accounting["qualified"] is True:
        with _monitor_scope(private, audit) as monitor:
            with AiConfigHistoryMonitor(config_dir=ai, past_dir=history) as watcher:
                smoke = qualify_workspace(
                    runtime,
                    watcher,
                    workspace_id=uuid4(),
                    principal_id=uuid4(),
                    monitor_config=monitor,
                )
            if smoke["qualified_smoke"] is True:
                report_path = private / "worker.json"
                command = [
                    sys.executable,
                    "-B",
                    "scripts/qualify_operator_workers.py",
                    "--rootfs",
                    str(rootfs),
                    "--home",
                    str(home),
                    "--model-staging",
                    str(staging),
                    "--shared-models",
                    str(models),
                    "--shared-tools",
                    str(tools),
                    "--ai-config",
                    str(ai),
                    "--ai-past",
                    str(history),
                    "--image-digest",
                    args.image_digest,
                    "--qualification-sha256",
                    str(smoke["receipt_sha256"]),
                    "--report",
                    str(report_path),
                ]
                if os.environ.get("POTBOT_OPERATOR_SCOPED_LAUNCHER"):
                    command.append("--scoped-worker-launcher")
                execution = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=210,
                    check=False,
                    env={
                        **os.environ,
                        "POTBOT_OPERATOR_AUDIT_ROOT": str(monitor.audit_root),
                        "POTBOT_OPERATOR_CGROUP_ROOT": str(monitor.cgroup_root),
                        "POTBOT_OPERATOR_STRACE": str(monitor.strace),
                        "POTBOT_OPERATOR_STRACE_SHA256": monitor.strace_sha256,
                        "POTBOT_OPERATOR_CGROUP_JOIN": str(monitor.cgroup_join),
                        "POTBOT_OPERATOR_CGROUP_JOIN_SHA256": monitor.cgroup_join_sha256,
                    },
                )
                if execution.stdout.strip():
                    operator = json.loads(execution.stdout)
                if execution.returncode == 0 and operator and operator["qualified"] is True:
                    worker = json.loads(report_path.read_text())

    passed = (
        accounting["qualified"] is True and worker is not None and worker["qualified_smoke"] is True
    )
    failure = None if passed else _failure_summary(accounting, smoke, operator, worker)
    report = {
        "schema": "potbot.published-image-hosted-smoke/1",
        "scope": "disposable-linux-amd64",
        "qualified_smoke": passed,
        "image_digest": args.image_digest,
        "image_source_commit": args.image_source,
        "consumer_commit": args.consumer,
        "kernel_release": os.uname().release,
        "shipped_sha256": shipped,
        "taskstats": accounting,
        "workspace_smoke": smoke,
        "operator_gate": operator,
        "worker_fork_smoke": worker,
        "failure_summary": failure,
    }
    write_report(args.report, report)
    print(
        json.dumps(
            {
                "qualified_smoke": passed,
                "image_digest": args.image_digest,
                "failure": failure,
            },
            sort_keys=True,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

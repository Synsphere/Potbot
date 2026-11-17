#!/usr/bin/env python3
"""Opt-in Python profiler acceptance on an already installed Linux/WSL host.

Run with the installed unprivileged worker's monitor environment and a private
policy. The operator must already have provisioned the image, native Syn cohort,
Agent home, exact authored workload, active task, sealed catalogue and persistent
Cog grant. This command never provisions those resources or relaxes a failed gate.
POTBOT_PYTHON_PROFILE_DSN supplies the existing unprivileged PostgreSQL login.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import threading
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.domain.serialization import canonical_json
from potbot.evaluation.sandbox_worker_acceptance import (
    check_audit,
    read_private,
    unique_json,
    write_report,
)
from potbot.tools.execution_contracts import ExecutionPolicy, digest, identifier
from potbot.tools.python_profile_worker import EXECUTABLE, OPERATION, REQUEST

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "potbot.python-profile-installed-qualification/1"
WORKLOAD = "scripts/fixtures/python_profile_workload.py"
IMAGE = "ghcr.io/kieransimkin/potbot-sandbox"
SIGNER = (
    "https://github.com/kieransimkin/potbot/"
    ".github/workflows/sandbox-system-image.yml@refs/heads/main"
)
SOURCES = (
    "scripts/qualify_python_profile_cog.py",
    WORKLOAD,
    "src/potbot/tools/python_profile_worker.py",
    "src/potbot/tools/python_profile_cog.py",
    "src/potbot/controller/execution_cogs.py",
    "src/potbot/controller/execution_journal.py",
    "src/potbot/tools/sandbox_cog_worker.py",
    "src/potbot/tools/shared_workspace_isolation.py",
    "src/potbot/tools/supervisor.py",
    "src/potbot/tools/worker_telemetry.py",
    "src/potbot/tools/released_syn_nodes.py",
    "src/potbot/tools/gui_audit_verification.py",
)
_IDS = (
    "workspace_id",
    "principal_id",
    "agent_id",
    "snapshot_id",
    "task_revision_id",
    "repository_state_id",
    "invocation_id",
)
_FILES = ("operator_manifest", "native_profile", "image_smoke", "cosign")


def parse_policy(raw: dict[str, Any]) -> dict[str, Any]:
    required = {
        "profile",
        *_IDS,
        *_FILES,
        "source_file",
        "source_sha256",
        "interpreter_sha256",
        "execution_policy",
        "repetitions",
    }
    if set(raw) != required or raw["profile"] != SCHEMA:
        raise ValueError("Unsupported installed profiler qualification policy")
    for name in _IDS:
        if not isinstance(raw[name], str):
            raise ValueError("Canonical qualification UUID required")
        identifier(raw[name])
    for name in _FILES:
        value = raw[name]
        if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
            raise ValueError("Every host file requires an independent digest")
        path = value["path"]
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or "\\" in path
            or "\0" in path
            or len(path) > 4096
            or any(part in {"", ".", ".."} for part in path.split("/")[1:])
        ):
            raise ValueError("Canonical absolute host path required")
        digest(value["sha256"])
    if (
        not isinstance(raw["source_file"], str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\.py", raw["source_file"]) is None
        or type(raw["repetitions"]) is not int
        or raw["repetitions"] not in (2, 3)
    ):
        raise ValueError("Bounded profiling source and repetitions required")
    digest(raw["source_sha256"])
    digest(raw["interpreter_sha256"])
    selected = raw["execution_policy"]
    if not isinstance(selected, dict):
        raise ValueError("Explicit execution policy required")
    policy = ExecutionPolicy(**selected)
    if selected != policy.to_dict() or not 30_000 <= policy.max_wall_ms <= 60_000:
        raise ValueError("Exact bounded installed execution policy required")
    return unique_json(canonical_json(raw).encode())


def committed_sources(root: Path = ROOT) -> tuple[str, dict[str, str]]:
    def git(*args: str) -> bytes:
        return subprocess.check_output(
            ["git", *args], cwd=root, timeout=10, stderr=subprocess.DEVNULL
        )

    head = git("rev-parse", "HEAD").decode("ascii").strip()
    if re.fullmatch(r"[a-f0-9]{40}", head) is None:
        raise ValueError("Committed consumer identity required")
    package_names = {
        name.decode("utf-8")
        for name in git("ls-tree", "-r", "--name-only", "-z", head, "--", "src/potbot").split(b"\0")
        if name
    }
    paths = (*SOURCES, *(("src/potbot",) if package_names else ()))
    committed = {}
    with tarfile.open(
        fileobj=io.BytesIO(git("archive", "--format=tar", head, "--", *paths))
    ) as archive:
        for entry in archive:
            if entry.isdir():
                continue
            reader = archive.extractfile(entry) if entry.isfile() else None
            if reader is None:
                raise ValueError("Committed consumer must contain ordinary source files")
            committed[entry.name] = reader.read()
    hashes = {}
    for name, source in committed.items():
        actual = root / name
        if actual.resolve(strict=True) != actual or actual.read_bytes() != source:
            raise ValueError("Qualification source differs from the committed consumer")
        if name in SOURCES:
            hashes[name] = hashlib.sha256(source).hexdigest()
    if set(hashes) != set(SOURCES):
        raise ValueError("Required qualification source is absent from the commit")
    if package_names:
        package = importlib.import_module("potbot")
        if package.__file__ is None:
            raise ValueError("Imported consumer package is unavailable")
        package_root = Path(package.__file__).resolve(strict=True).parent
        expected = {name.removeprefix("src/potbot/") for name in package_names}
        for name in package_names:
            installed = package_root / name.removeprefix("src/potbot/")
            if (
                installed.resolve(strict=True) != installed
                or installed.read_bytes() != committed[name]
            ):
                raise ValueError("Imported controller differs from the selected consumer")
        if any(
            str(path.relative_to(package_root)) not in expected
            for path in package_root.rglob("*.py")
        ):
            raise ValueError("Imported consumer contains uncommitted Python source")
        for name, module in tuple(sys.modules.items()):
            if name == "potbot" or name.startswith("potbot."):
                origin = getattr(module, "__file__", None)
                if origin is None or not Path(origin).is_relative_to(package_root):
                    raise ValueError("Imported consumer module has an unexpected origin")
                selected = "src/potbot/" + str(Path(origin).relative_to(package_root))
                if selected not in committed or Path(origin).read_bytes() != committed[selected]:
                    raise ValueError("Imported controller differs from the selected consumer")
        # Keep the report bounded while binding every tracked package file, not
        # only the small set of implementation landmarks shown individually.
        hashes["package_tree_sha256"] = hashlib.sha256(
            canonical_json(
                {
                    name: hashlib.sha256(committed[name]).hexdigest()
                    for name in sorted(package_names)
                }
            ).encode()
        ).hexdigest()
    return head, hashes


def _pinned_file(
    selected: dict[str, str],
    *,
    maximum: int = 1_048_576,
    owner: int | None = None,
) -> Path:
    path = Path(selected["path"])
    if path.resolve(strict=True) != path or path.is_symlink():
        raise ValueError("Pinned host file was redirected")
    info = path.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid not in ((0, os.geteuid()) if owner is None else (owner,))
        or info.st_mode & 0o022
        or not 0 < info.st_size <= maximum
    ):
        raise ValueError("Pinned host file is not owner-controlled and bounded")
    with path.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != selected["sha256"]:
            raise ValueError("Pinned host file changed")
    return path


def check_image_smoke(
    evidence: dict[str, Any],
    *,
    image_digest: str,
    qualification: str,
    consumer: str,
    kernel: str,
) -> None:
    if (
        evidence.get("schema") != "potbot.published-image-hosted-smoke/1"
        or evidence.get("qualified_smoke") is not True
        or evidence.get("image_digest") != image_digest
        or evidence.get("consumer_commit") != consumer
        or evidence.get("kernel_release") != kernel
        or evidence.get("operator_gate", {}).get("qualified") is not True
        or evidence.get("taskstats", {}).get("qualified") is not True
    ):
        raise ValueError("Image smoke does not qualify this consumer, image and kernel")
    workspace = dict(evidence.get("workspace_smoke", {}))
    receipt = workspace.pop("receipt_sha256", None)
    if (
        receipt != qualification
        or workspace.get("qualified_smoke") is not True
        or workspace.get("image_digest") != image_digest
        or hashlib.sha256(canonical_json(workspace).encode()).hexdigest() != receipt
    ):
        raise ValueError("Image workspace qualification receipt differs from its pin")


def verify_image_signature(cosign: Path, image_digest: str) -> str:
    """Verify the fixed official publisher; no download, unpack or admission."""
    if not os.access(cosign, os.X_OK):
        raise ValueError("Pinned cosign executable is unavailable")
    result = subprocess.run(
        [
            str(cosign),
            "verify",
            "--certificate-identity",
            SIGNER,
            "--certificate-oidc-issuer",
            "https://token.actions.githubusercontent.com",
            f"{IMAGE}@sha256:{digest(image_digest)}",
        ],
        capture_output=True,
        check=False,
        timeout=45,
        env={
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "PATH",
                "HOME",
                "XDG_CACHE_HOME",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "HTTPS_PROXY",
                "HTTP_PROXY",
                "NO_PROXY",
            }
        },
    )
    if result.returncode != 0 or not 0 < len(result.stdout) <= 1_048_576:
        raise ValueError("Official worker image signature could not be verified")
    rows = json.loads(result.stdout)
    if not isinstance(rows, list) or not any(
        isinstance(row, dict)
        and row.get("critical", {}).get("image", {}).get("docker-manifest-digest")
        == f"sha256:{image_digest}"
        for row in rows
    ):
        raise ValueError("Image signature does not identify the admitted digest")
    return hashlib.sha256(result.stdout).hexdigest()


def check_comparison(
    value: dict[str, Any],
    *,
    source_sha256: str,
    interpreter_sha256: str,
    uid: int,
    gid: int,
    repetitions: int,
) -> None:
    from potbot.tools.python_profile_worker import report, summarize

    expected = {
        "uid": uid,
        "gid": gid,
        "total": sum((i * i) % 101 for i in range(200_000)),
        "network_denied": True,
        "root_write_denied": True,
    }
    result_pin = hashlib.sha256(canonical_json(expected).encode()).hexdigest()
    if (
        value.get("source_sha256") != source_sha256
        or value.get("interpreter_sha256") != interpreter_sha256
        or value.get("result_sha256") != result_pin
        or value.get("repetitions") != repetitions
        or value.get("host", {}).get("kernel") != os.uname().release
    ):
        raise ValueError("Profiling result differs from the installed workload or identity")
    samples = value.get("samples")
    if not isinstance(samples, dict) or set(samples) != {"baseline", "profile"}:
        raise ValueError("Both measured sample families are required")
    for mode, rows in samples.items():
        if not isinstance(rows, list) or len(rows) != repetitions:
            raise ValueError("Missing measured profiling repetitions")
        for row in rows:
            checked = report(canonical_json(row).encode(), profiled=mode == "profile")
            if checked["result_sha256"] != result_pin:
                raise ValueError("Baseline and profiled outputs differ")
            if mode == "profile" and not any(
                item["function"] == "item_cost" and item["calls"] == 200_000
                for item in checked["functions"]
            ):
                raise ValueError("Expected source-bound hotspot is missing")
    recalculated = summarize(samples, source_sha256, interpreter_sha256, repetitions)
    # The guest's CPU count may reflect its namespace; never substitute host metadata.
    for name in ("median_ns", "overhead_ns", "clock", "limits", "profiler", "unavailable"):
        if value.get(name) != recalculated[name]:
            raise ValueError("Profiling summary disagrees with the retained measurements")


async def qualify(
    config: dict[str, Any],
    head: str,
    hashes: dict[str, str],
    *,
    control_paths: tuple[Path, ...],
) -> dict[str, Any]:
    from potbot.cogs.contracts import CogRunContext, CogSignature, TypedCogValue
    from potbot.cogs.execution import WORK_COGS
    from potbot.cogs.registry import BoundCogRegistry
    from potbot.controller.execution_cogs import ExecutionCogRuntime
    from potbot.controller.execution_events import ExecutionEventService
    from potbot.controller.native_cog_dispatch import read_profile
    from potbot.domain.auth import WorkspaceContext
    from potbot.domain.errors import InvalidInputError, ScopeDeniedError
    from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
    from potbot.persistence.database import Database
    from potbot.syn_release.mesh_endpoint import NativeMeshClient
    from potbot.tools.agent_homes import AgentHomeRegistry
    from potbot.tools.execution_contracts import WORK_KINDS
    from potbot.tools.gui_audit_verification import verify_gui_run
    from potbot.tools.operator_cli import parse_manifest
    from potbot.tools.python_profile_cog import install_python_profile
    from potbot.tools.released_syn_nodes import ReleasedSynWorkerNodeMesh
    from potbot.tools.sandbox_cog_worker import SandboxedCogExecutor, SandboxedCogOperation
    from potbot.tools.shared_workspace_isolation import SharedWorkspaceBubblewrapBackend
    from potbot.tools.supervisor import ProcessSupervisor
    from potbot.tools.worker_telemetry import WorkerMonitorConfig
    from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger
    from potbot.tools.workspace_supervision import validate_runtime_pin
    from potbot.workspace_sandbox import load_workspace_sandbox

    if sys.platform != "linux" or os.geteuid() == 0:
        raise ValueError("Use the installed unprivileged Linux worker service account")
    if os.environ.get("POTBOT_OPERATOR_REQUIRED") != "1" or not os.environ.get(
        "POTBOT_OPERATOR_SCOPED_LAUNCHER"
    ):
        raise ValueError("Installed operator and scoped accounting launcher are mandatory")
    paths = parse_manifest(_pinned_file(config["operator_manifest"]))
    native, fingerprint = read_profile(_pinned_file(config["native_profile"]))
    actor = WorkspaceContext(UUID(config["workspace_id"]), UUID(config["principal_id"]))
    agent, invocation = UUID(config["agent_id"]), UUID(config["invocation_id"])
    if (
        paths.workspace_id != actor.workspace_id
        or native["workspace_id"] != str(actor.workspace_id)
        or native["image_digest"] != paths.image_digest
        or native["qualification_sha256"] != paths.qualification_sha256
        or str(agent) not in native["agents"]
        or fingerprint != config["native_profile"]["sha256"]
    ):
        raise ValueError("Installed native profile differs from the qualification scope")
    image_receipt = unique_json(_pinned_file(config["image_smoke"]).read_bytes(), maximum=1_048_576)
    check_image_smoke(
        image_receipt,
        image_digest=paths.image_digest,
        qualification=paths.qualification_sha256,
        consumer=head,
        kernel=os.uname().release,
    )
    signature_pin = verify_image_signature(
        _pinned_file(config["cosign"], maximum=256 * 1024 * 1024), paths.image_digest
    )
    runtime = paths.runtime()
    ledger = WorkspaceLeaseLedger(paths.controller_ledger, workspace_home=runtime.home)
    validate_runtime_pin(ledger, runtime, actor.workspace_id)
    homes = AgentHomeRegistry(ledger, workspace_name=native["workspace_name"])
    home = homes.lookup(actor.workspace_id, agent)
    if home is None or ledger.active(actor.workspace_id):
        raise ValueError("A registered, idle qualification Agent is required")
    workload = runtime.home / home.directory / config["source_file"]
    if config["source_sha256"] != hashes[WORKLOAD]:
        raise ValueError("Qualification requires the exact committed authored workload")
    _pinned_file({"path": str(workload), "sha256": hashes[WORKLOAD]}, owner=home.uid)
    executable = runtime.rootfs / EXECUTABLE.lstrip("/")
    worker_pin = hashes["src/potbot/tools/python_profile_worker.py"]
    _pinned_file({"path": str(executable), "sha256": worker_pin})
    policy = ExecutionPolicy(**config["execution_policy"])
    operation = install_python_profile(
        config["source_file"], hashes[WORKLOAD], config["interpreter_sha256"], worker_pin, policy
    )
    monitor = WorkerMonitorConfig.from_environment(scoped_worker=True)
    if monitor.scoped_launcher is None:
        raise ValueError("Scoped worker accounting is unavailable")
    protected = tuple(
        path
        for path in (
            runtime.rootfs,
            runtime.home,
            runtime.model_staging,
            runtime.shared_models,
            runtime.shared_tools,
            runtime.shared_skills,
            runtime.ai_config_dir,
            runtime.ai_history_dir,
        )
        if path is not None
    )
    for path in (
        *control_paths,
        monitor.audit_root,
        Path(native["socket"]),
        *(Path(config[name]["path"]) for name in _FILES),
    ):
        if any(path == mount or path.is_relative_to(mount) for mount in protected):
            raise ValueError("Qualification control state overlaps a guest mount")
    backend = SharedWorkspaceBubblewrapBackend(runtime)
    attachment = backend.operator_attachment
    if attachment is None:
        raise ValueError("The installed workspace operator must be attached")
    attachment.require_healthy()
    supervisor = ProcessSupervisor(backend, monitor_config=monitor)

    class ObservedExecutor(SandboxedCogExecutor):
        dispatches = 0

        def dispatch_count(self) -> int:
            return self.dispatches

        def _run_sync(
            self,
            op: SandboxedCogOperation,
            source: TypedCogValue,
            cog_context: CogRunContext,
            actor: WorkspaceContext,
            agent_id: UUID,
            cancel: threading.Event,
            syn_fault: threading.Event | None = None,
        ) -> TypedCogValue:
            # Count entry into the real worker path. Context projection may select
            # its own supervisor, so observing only the initial supervisor is wrong.
            # No argv, credentials, mounts, transport or monitor are substituted.
            self.dispatches += 1
            return super()._run_sync(op, source, cog_context, actor, agent_id, cancel, syn_fault)

    database = Database(os.environ["POTBOT_PYTHON_PROFILE_DSN"])
    sandbox = load_workspace_sandbox(database, actor)
    artifacts = ArtifactStore(database, LocalArtifactBackend(sandbox.path("artifacts")))
    registry = BoundCogRegistry.provenance(python_profile=True)
    context = CogRunContext(
        registry.snapshot.fingerprint,
        UUID(config["task_revision_id"]),
        UUID(config["repository_state_id"]),
        invocation,
    )
    signature = WORK_COGS[WORK_KINDS.index("script")]
    source = TypedCogValue(
        signature.input_type_ref,
        {
            "schema_version": 1,
            "operation_ref": OPERATION,
            "input": {"type_ref": REQUEST, "value": {"repetitions": config["repetitions"]}},
        },
    )

    def admit(selected: CogSignature, value: TypedCogValue, run: CogRunContext) -> None:
        if selected != signature or value.fingerprint != source.fingerprint or run != context:
            raise ScopeDeniedError("Qualification admission is limited to the pinned invocation")
        attachment.require_healthy()

    transport = NativeMeshClient(
        Path(native["socket"]),
        actor.workspace_id,
        manifest_sha256=digest(native["manifest_sha256"]),
        release_key_sha256=digest(native["release_key_sha256"]),
        group_id=identifier(native["group_id"]),
        group_epoch=native["group_epoch"],
        agents=frozenset(identifier(value) for value in native["agents"]),
    )
    try:
        workers = ReleasedSynWorkerNodeMesh(transport)
        with workers.running():
            executor = ObservedExecutor(runtime, ledger, homes, supervisor, workers)

            def controller(db: Database) -> ExecutionCogRuntime:
                return ExecutionCogRuntime(
                    database=db,
                    artifacts=artifacts,
                    actor=actor,
                    snapshot_id=UUID(config["snapshot_id"]),
                    task_revision_id=context.task_revision_id,
                    repository_state_id=context.repository_state_id,
                    emitter_run_id=invocation,
                    policy=policy,
                    operations=(operation,),
                    admit=admit,
                    sandbox_executor=executor,
                    sandbox_agent_id=agent,
                    persistent_cog_grants=True,
                )

            selected = controller(database)
            invalid = TypedCogValue(
                signature.input_type_ref,
                {
                    **source.value,
                    "input": {
                        "type_ref": REQUEST,
                        "value": {
                            "repetitions": config["repetitions"],
                            "filename": "unadmitted.py",
                        },
                    },
                },
            )
            try:
                await registry.invoke(str(signature.cog_ref), invalid, context, controller=selected)
            except (InvalidInputError, ScopeDeniedError):
                pass
            else:
                raise ValueError("Unadmitted profiler input was accepted")
            if executor.dispatch_count():
                raise ValueError("Rejected input launched a worker")
            outcome = await registry.invoke(
                str(signature.cog_ref), source, context, controller=selected
            )
            if executor.dispatch_count() != 1:
                raise ValueError("Expected exactly one installed worker launch")
            report = outcome.value.value["result"]["value"]
            check_comparison(
                report,
                source_sha256=hashes[WORKLOAD],
                interpreter_sha256=config["interpreter_sha256"],
                uid=home.uid,
                gid=home.gid,
                repetitions=config["repetitions"],
            )
            native_binding = executor.syn_provenance(actor, agent)
            if native_binding is None:
                raise ValueError("Native release provenance is absent")
            for repeat in (selected, controller(database)):
                try:
                    await registry.invoke(
                        str(signature.cog_ref), source, context, controller=repeat
                    )
                except ScopeDeniedError:
                    pass
                else:
                    raise ValueError("A completed profiler invocation was replayed")
            if executor.dispatch_count() != 1 or ledger.active(actor.workspace_id):
                raise ValueError("Replay dispatched or the workspace lease is unresolved")
    finally:
        transport.close()
    audit = check_audit(
        verify_gui_run(
            monitor.audit_root,
            actor.workspace_id,
            invocation,
            identity_kind="agent",
            subject_id=agent,
        ),
        workspace_id=str(actor.workspace_id),
        run_id=str(invocation),
    )
    # Read through a fresh DB handle after reconstructing the controller. This is
    # durable receipt readback, not a claim of a systemd or native-node restart.
    restored_db = Database(database.dsn)
    events = ExecutionEventService(restored_db).events(actor, run_id=invocation, limit=200)["items"]
    completed = [row for row in events if row["stage"] == "completed"]
    if len(completed) != 1 or not events or events[-1] != completed[0]:
        raise ValueError("Expected one durable terminal profiler event")
    terminal = completed[0]
    raw = ArtifactStore(restored_db, artifacts.backend).read(
        actor, UUID(terminal["output_artifact_ref"].removeprefix("artifact:"))
    )
    if (
        hashlib.sha256(raw).hexdigest() != terminal["output_sha256"]
        or json.loads(raw)["cog_receipt"] != outcome.receipt.to_dict()
        or b"potbot-profile-qualification-private-output" in raw
    ):
        raise ValueError("Retained profiler receipt or private-output boundary failed")
    validate_runtime_pin(ledger, runtime, actor.workspace_id)
    return {
        "schema": SCHEMA,
        "qualified": True,
        "scope": "installed_python_profiler_cog",
        "source_commit": head,
        "source_hashes": hashes,
        "image_digest": runtime.image_digest,
        "image_qualification_sha256": runtime.qualification_sha256,
        "image_signature_evidence_sha256": signature_pin,
        "native_profile_sha256": fingerprint,
        "native_release_authority_sha256": native["release_key_sha256"],
        "native_syn": native_binding,
        "kernel_release": os.uname().release,
        "workspace_id": str(actor.workspace_id),
        "agent_id": str(agent),
        "invocation_id": str(invocation),
        "registered_uid": home.uid,
        "registered_gid": home.gid,
        "worker_sha256": worker_pin,
        "interpreter_sha256": config["interpreter_sha256"],
        "source_sha256": hashes[WORKLOAD],
        "comparison": report,
        "audit": audit,
        "output_artifact_ref": terminal["output_artifact_ref"],
        "output_sha256": terminal["output_sha256"],
        "cog_receipt_sha256": outcome.receipt.fingerprint,
        "malformed_input_refused_before_dispatch": True,
        "worker_launches": 1,
        "fresh_controller_replay_refused": True,
        "postgres_receipt_readback": True,
        "workspace_lease_released": True,
        "scoped_launcher_used": True,
        "not_qualified": [
            "systemd_restart",
            "native_syn_restart",
            "syn_application_rpc_transport",
            "lost_reply_faults",
            "stable_performance_benchmark",
            "all_T07.3_backends",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--run-live", action="store_true")
    args = parser.parse_args(argv)
    phase = "explicit_live_opt_in"
    try:
        if not args.run_live:
            raise ValueError("Explicit live opt-in required")
        phase = "private_policy_and_output"
        config = parse_policy(unique_json(read_private(args.policy)))
        parent = args.report.parent
        if (
            not args.report.is_absolute()
            or parent.resolve(strict=True) != parent
            or parent.stat().st_uid != os.geteuid()
            or parent.stat().st_mode & 0o077
            or args.report.exists()
            or args.report.is_symlink()
        ):
            raise ValueError("Fresh output in an existing private directory required")
        phase = "committed_consumer"
        head, hashes = committed_sources()
        phase = "installed_profile_and_live_acceptance"
        result = asyncio.run(
            qualify(config, head, hashes, control_paths=(args.policy, args.report))
        )
        phase = "consumer_readback"
        if committed_sources() != (head, hashes):
            raise ValueError("Consumer changed during installed acceptance")
        phase = "private_report_write"
        write_report(args.report, result)
    except Exception as error:
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "qualified": False,
                    "phase": phase,
                    "error_class": type(error).__name__,
                }
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {"schema": SCHEMA, "qualified": True, "source_commit": head, "scope": result["scope"]}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

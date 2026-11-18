"""Installed MCP qualification through the existing task and execution owners.

Private suites are data. Every native case uses the normal gateway, reservation,
candidate, receipt and stopped-process boundaries; recovery never calls a tool.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    DomainError,
    IndeterminateExecutionError,
    InvalidInputError,
    OperationCancelledError,
    OperationTimeoutError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.execution import ActionProposal, CapabilityContext, ToolRegistry
from potbot.domain.serialization import canonical_json, content_hash
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.semantics.releases import read_task_pin
from potbot.tools.gateway import ToolGateway
from potbot.tools.mcp_discovery import bounded_metadata
from potbot.tools.mcp_discovery_host import (
    McpDiscoveryHost,
    McpInstalledProfile,
    _private_file,
    configured_mcp_discovery_host,
    read_mcp_profile,
)
from potbot.tools.mcp_protocol import parse_json_document, validate_arguments
from potbot.tools.mcp_qualification_oracle import evaluate_oracle, validate_oracle
from potbot.tools.receipts import RunReceiptStore
from potbot.tools.repository import CandidateWorkspaceManager, Lease
from potbot.tools.workspace_supervision import WorkspaceExecutorIdentity, WorkspaceLeaseGuard
from potbot.workspace_sandbox import WorkspaceSandbox

if TYPE_CHECKING:
    from potbot.persistence.database import Database
    from potbot.tools.agent_homes import AgentHome

SUITE_SCHEMA = "potbot-mcp-qualification-suite/v1"
CASE_SCHEMA = "potbot-mcp-qualification-case/v1"
REPORT_SCHEMA = "potbot-mcp-qualification-report/v1"
MAX_SUITE_BYTES = 512 * 1024
MAX_CASES = 8
MAX_ARGUMENT_BYTES = 32 * 1024
MAX_ATTEMPT_SECONDS = 600
_TOKEN = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_TASK_FIELDS = {
    "episodeId",
    "contractRevisionId",
    "contractSha256",
    "repositoryStateId",
    "repositorySha256",
    "semanticReleaseId",
    "semanticReleaseSha256",
}
_PREPARED_FIELDS = {
    "schema",
    "caseId",
    "phase",
    "caseSha256",
    "candidateId",
    "leaseOwner",
    "leaseGeneration",
    "actionId",
    "reservationId",
    "inputArtifactId",
    "inputSha256",
    "storageSha256",
    "nativePins",
}
_FINISHED_FIELDS = _PREPARED_FIELDS | {
    "runId",
    "resultArtifactId",
    "resultSha256",
    "receiptSha256",
    "invocationSha256",
    "resultCheck",
    "processesStopped",
    "isolationSha256",
    "elapsedMs",
    "reasonCode",
}


def _text(value: Any, maximum: int, *, empty: bool = False, multiline: bool = False) -> str:
    if (
        not isinstance(value, str)
        or not (0 if empty else 1) <= len(value) <= maximum
        or any(ord(c) < 32 and not (multiline and c in "\n\t") or ord(c) == 127 for c in value)
    ):
        raise InvalidInputError("Qualification text must be finite and bounded")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        raise InvalidInputError("Qualification text must be valid Unicode") from None
    return value


def _uuid(value: Any) -> UUID:
    try:
        result = UUID(value)
        if str(result) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise InvalidInputError("Qualification identifiers must be canonical UUIDs") from None
    return result


def _sha(value: Any) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise InvalidInputError("Qualification evidence requires an exact SHA-256")
    return value


@dataclass(frozen=True, slots=True)
class McpQualificationCase:
    id: str
    title: str
    tool_name: str
    _arguments: str
    _oracle: str

    @property
    def arguments(self) -> dict[str, Any]:
        value = parse_json_document(self._arguments.encode())
        if not isinstance(value, dict):
            raise InvalidInputError("Qualification arguments must be an object")
        return dict(value)

    @property
    def oracle(self) -> dict[str, Any]:
        value = parse_json_document(self._oracle.encode())
        if not isinstance(value, dict):
            raise InvalidInputError("Qualification oracle must be an object")
        return dict(value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "toolName": self.tool_name,
            "arguments": self.arguments,
            "oracle": self.oracle,
        }

    @property
    def sha256(self) -> str:
        return content_hash(self.to_dict())

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "toolName": self.tool_name,
            "caseSha256": self.sha256,
            "argumentsSha256": content_hash(self.arguments),
            "oracleSha256": content_hash(self.oracle),
            "assertionCount": len(self.oracle["assertions"]),
        }


@dataclass(frozen=True, slots=True)
class McpQualificationSuite:
    path: Path
    file_sha256: str
    name: str
    description: str
    cases: tuple[McpQualificationCase, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SUITE_SCHEMA,
            "name": self.name,
            "description": self.description,
            "cases": [case.to_dict() for case in self.cases],
        }

    @property
    def sha256(self) -> str:
        return content_hash(self.to_dict())

    def summary(self) -> dict[str, Any]:
        return {
            "schema": SUITE_SCHEMA,
            "name": self.name,
            "description": self.description,
            "cases": [case.summary() for case in self.cases],
        }


def read_mcp_qualification_suite(path: Path) -> McpQualificationSuite:
    """Read stable private bytes without fetching, importing or executing them."""
    with _private_file(path, maximum=MAX_SUITE_BYTES) as (_fd, raw):
        value = parse_json_document(raw)
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "name", "description", "cases"}
            or value["schema"] != SUITE_SCHEMA
            or not isinstance(value["cases"], list)
            or not 1 <= len(value["cases"]) <= MAX_CASES
        ):
            raise InvalidInputError("Invalid bounded MCP qualification suite")
        name = _text(value["name"], 96)
        description = _text(value["description"], 2048, empty=True, multiline=True)
        cases, seen = [], set()
        for item in value["cases"]:
            if not isinstance(item, dict) or set(item) != {
                "id",
                "title",
                "toolName",
                "arguments",
                "oracle",
            }:
                raise InvalidInputError("Qualification case fields must be exact")
            identity = _text(item["id"], 64)
            if _TOKEN.fullmatch(identity) is None or identity in seen:
                raise InvalidInputError("Qualification cases require distinct bounded identifiers")
            seen.add(identity)
            arguments = bounded_metadata(item["arguments"], maximum=MAX_ARGUMENT_BYTES)
            if not isinstance(arguments, dict):
                raise InvalidInputError("Qualification arguments must be an object")
            oracle = validate_oracle(item["oracle"])
            cases.append(
                McpQualificationCase(
                    identity,
                    _text(item["title"], 128),
                    _text(item["toolName"], 128),
                    canonical_json(arguments),
                    canonical_json(oracle),
                )
            )
        return McpQualificationSuite(
            Path(path), hashlib.sha256(raw).hexdigest(), name, description, tuple(cases)
        )


def qualification_consumer_sha256() -> str:
    """Pin current trusted implementation bytes without claiming a release commit."""
    root = Path(__file__).resolve().parents[1]
    modules = (
        "controller/mcp_qualification.py",
        "controller/mcp_discovery.py",
        "controller/execution.py",
        "controller/budgets.py",
        "controller/tasks.py",
        "tools/mcp_qualification_host.py",
        "tools/mcp_qualification_oracle.py",
        "domain/result_assertions.py",
        "tools/mcp_discovery_host.py",
        "tools/mcp_gateway.py",
        "tools/gateway.py",
        "tools/mcp_sandbox.py",
        "tools/mcp_protocol.py",
        "tools/interactive_supervisor.py",
        "tools/shared_workspace_isolation.py",
        "tools/workspace_supervision.py",
        "tools/workspace_concurrency.py",
        "tools/receipts.py",
        "tools/repository.py",
    )
    identities = {}
    for relative in modules:
        selected = root / relative
        if selected.resolve(strict=True) != selected or not selected.is_file():
            raise ScopeDeniedError("Qualification implementation source is redirected")
        before = selected.stat()
        raw = selected.read_bytes()
        after = selected.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or len(raw) != before.st_size:
            raise StateMismatchError("Qualification implementation changed during readback")
        identities[relative] = hashlib.sha256(raw).hexdigest()
    return content_hash({"schema": "potbot-mcp-qualification-consumer/v1", "modules": identities})


def read_mcp_task_binding(
    database: Database,
    context: WorkspaceContext,
    *,
    episode_id: UUID,
    repository_state_id: UUID,
    contract_revision_id: UUID | None = None,
) -> dict[str, Any]:
    """Read existing task authority; use no row locks inside gateway callbacks."""
    with database.transaction(context) as conn:
        episode = conn.execute(
            "SELECT status,repository_id,active_contract_id FROM episodes WHERE workspace_id=%s AND id=%s",
            (context.workspace_id, episode_id),
        ).fetchone()
        if episode is None:
            raise ScopeDeniedError("Qualification task is unavailable")
        selected = contract_revision_id or episode["active_contract_id"]
        if episode["status"] != "running" or selected != episode["active_contract_id"]:
            raise StateMismatchError("Qualification requires the current running task")
        contract = conn.execute(
            "SELECT id,content,content_hash FROM contract_revisions WHERE workspace_id=%s AND episode_id=%s AND id=%s",
            (context.workspace_id, episode_id, selected),
        ).fetchone()
        state = conn.execute(
            "SELECT id,repository_id,content_digest FROM repository_states WHERE workspace_id=%s AND id=%s",
            (context.workspace_id, repository_state_id),
        ).fetchone()
        if contract is None or state is None or state["repository_id"] != episode["repository_id"]:
            raise ScopeDeniedError("Qualification repository and contract must belong to its task")
        if content_hash(contract["content"]) != contract["content_hash"]:
            raise StateMismatchError("Qualification task contract integrity failed")
        pin = read_task_pin(conn, context, episode_id, required=True)
        assert pin is not None
        return {
            "episodeId": str(episode_id),
            "contractRevisionId": str(contract["id"]),
            "contractSha256": contract["content_hash"],
            "repositoryStateId": str(state["id"]),
            "repositorySha256": state["content_digest"],
            "semanticReleaseId": str(pin["release_id"]),
            "semanticReleaseSha256": pin["content_hash"],
        }


def _storage_identity(path: Path, mounts: tuple[Path, ...]) -> dict[str, Any]:
    if not path.is_absolute() or path.resolve(strict=True) != path or ".." in path.parts:
        raise ScopeDeniedError("Qualification storage requires a physical absolute directory")
    if any(path.is_relative_to(mount) or mount.is_relative_to(path) for mount in mounts):
        raise ScopeDeniedError("Qualification storage overlaps a worker mount")
    # Open each directory relative to the preceding held inode, including the
    # leaf. Path.resolve alone does not fence replacement during a readback.
    descriptors = [os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)]
    entries: list[tuple[int, str, os.stat_result]] = []
    try:
        for index, component in enumerate(path.parts[1:], start=1):
            parent = descriptors[-1]
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent,
            )
            descriptors.append(child)
            info = os.fstat(child)
            leaf = index == len(path.parts) - 1
            sticky = not leaf and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            if info.st_uid not in {0, os.geteuid()} or (
                info.st_mode & (0o077 if leaf else 0o022) and not sticky
            ):
                raise ScopeDeniedError(
                    "Qualification storage must remain private and host controlled"
                )
            entries.append((parent, component, info))
        for parent, component, before in entries:
            after = os.stat(component, dir_fd=parent, follow_symlinks=False)
            if any(
                getattr(before, field) != getattr(after, field)
                for field in ("st_dev", "st_ino", "st_uid", "st_gid", "st_mode")
            ):
                raise StateMismatchError("Qualification private storage changed during readback")
        info = os.fstat(descriptors[-1])
        if info.st_mode & 0o077:
            raise ScopeDeniedError("Qualification storage must remain private and host controlled")
        return {
            "path": str(path),
            "device": info.st_dev,
            "inode": info.st_ino,
            "uid": info.st_uid,
            "gid": info.st_gid,
            "mode": stat.S_IMODE(info.st_mode),
        }
    except OSError:
        raise ScopeDeniedError(
            "Qualification private storage is unavailable or redirected"
        ) from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


class McpQualificationHost:
    """One installed owner; fresh runs execute, recovery only reconciles receipts."""

    def __init__(
        self,
        database: Database,
        native: McpDiscoveryHost,
        *,
        artifacts_root: Path,
        receipts_root: Path,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(native, McpDiscoveryHost) or (clock is not None and not callable(clock)):
            raise ScopeDeniedError("Qualification requires the current installed native owner")
        self.database, self.native = database, native
        self.artifacts_root, self.receipts_root = Path(artifacts_root), Path(receipts_root)
        self.clock = clock or (lambda: datetime.now(UTC))
        self._storage = self._storage_readback()
        if self.artifacts_root.is_relative_to(
            self.receipts_root
        ) or self.receipts_root.is_relative_to(self.artifacts_root):
            raise ScopeDeniedError("Qualification artifact and receipt storage must be separate")
        self.artifacts = ArtifactStore(database, LocalArtifactBackend(self.artifacts_root))
        self.receipts = RunReceiptStore(self.receipts_root)

    def _storage_readback(self) -> str:
        runtime = self.native.runtime
        mounts = tuple(
            path.resolve(strict=True)
            for path in (
                runtime.rootfs,
                runtime.home,
                runtime.shared_tools,
                runtime.shared_skills,
                runtime.model_staging,
                runtime.shared_models,
                runtime.ai_config_dir,
                runtime.ai_history_dir,
            )
            if path is not None
        )
        return content_hash(
            {
                "schema": "potbot-mcp-qualification-storage/v1",
                "artifacts": _storage_identity(self.artifacts_root, mounts),
                "receipts": _storage_identity(self.receipts_root, mounts),
            }
        )

    @staticmethod
    def _task_shape(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != _TASK_FIELDS:
            raise InvalidInputError("Qualification task binding fields must be exact")
        for key, item in value.items():
            if key.endswith("Id"):
                _uuid(item)
            else:
                _sha(item)
        return dict(value)

    def _gateway(
        self,
        context: WorkspaceContext,
        profile: McpInstalledProfile,
        agent: AgentHome,
        authorize: Callable[[], None],
        *,
        recover: bool = False,
    ) -> ToolGateway:
        native = self.native
        sandbox = WorkspaceSandbox.existing(native.runtime.home / agent.directory)
        root = sandbox.path("candidates")
        if recover:
            # Recovery reads the exact existing owner. Constructing a new root
            # or ownership marker would hide missing native recovery evidence.
            try:
                root_info = root.lstat()
                marker = (root / ".potbot-candidate-root.json").lstat()
            except OSError:
                raise ScopeDeniedError("Qualification candidate owner is unavailable") from None
            if (
                not stat.S_ISDIR(root_info.st_mode)
                or not stat.S_ISREG(marker.st_mode)
                or marker.st_uid != os.geteuid()
                or marker.st_mode & 0o077
                or marker.st_nlink != 1
            ):
                raise ScopeDeniedError("Qualification candidate owner was redirected")
        candidates = CandidateWorkspaceManager(self.database, self.artifacts, root)
        info = root.lstat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ScopeDeniedError("Qualification candidate parent must remain private")
        registry = ToolRegistry((profile.installed.definition,))
        return ToolGateway(
            self.database,
            self.artifacts,
            candidates,
            registry,
            CapabilityContext(registry.inventory()),
            self.receipts,
            supervisor=native.supervisor,
            shared_runtime=native.runtime,
            shared_leases=native.ledger,
            agent_homes=native.homes,
            host_agent_id=agent.agent_id,
            mcp_installations=(profile.installed,),
            mcp_routes=(native.syn_route,),
            mcp_authorize=authorize,
        )

    @contextmanager
    def _preparation(
        self,
        context: WorkspaceContext,
        agent: AgentHome,
        attempt_id: UUID,
        stage: str,
        authorize: Callable[[], None],
    ) -> Iterator[None]:
        """Fence host materialization against every shared-home worker."""
        guard = WorkspaceLeaseGuard.exclusive_home(
            self.native.ledger,
            WorkspaceExecutorIdentity(
                context.workspace_id, agent.agent_id, self.native.runtime.image_digest
            ),
            operation="mcp.qualification.prepare",
            idempotency_key=f"mcpq-prepare-{attempt_id.hex}-{uuid4().hex}",
        )
        guard.start()
        try:
            authorize()
            guard.ensure_live()
            yield
            authorize()
            guard.ensure_live()
        finally:
            try:
                # No process is dispatched inside this scope. The existing
                # artifact/candidate/budget owners retain partial preparation;
                # a fenced workspace lease still stays indeterminate.
                guard.finish(
                    evidence_sha256=content_hash(
                        {
                            "schema": "potbot-mcp-qualification-preparation/v1",
                            "attemptId": str(attempt_id),
                            "stage": stage,
                            "dispatched": False,
                        }
                    ),
                    uncertain=guard.fenced,
                )
            finally:
                guard.abandon()

    def _check_case_event(
        self,
        case: McpQualificationCase,
        value: Any,
        *,
        attempt_id: UUID,
        native_pins: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(value, dict) or value.get("phase") not in {"prepared", "finished"}:
            raise StateMismatchError("Qualification case journal is unavailable")
        expected_fields = _PREPARED_FIELDS if value["phase"] == "prepared" else _FINISHED_FIELDS
        if (
            set(value) != expected_fields
            or value["schema"] != CASE_SCHEMA
            or value["caseId"] != case.id
            or value["caseSha256"] != case.sha256
            or value["storageSha256"] != self._storage
            or value["nativePins"] != native_pins
            or value["leaseOwner"] != f"mcp-qualification:{attempt_id}:{case.id}"
            or type(value["leaseGeneration"]) is not int
            or value["leaseGeneration"] < 1
        ):
            raise StateMismatchError("Qualification case journal differs from its native owner")
        for key in ("candidateId", "actionId", "reservationId", "inputArtifactId"):
            _uuid(value[key])
        _sha(value["inputSha256"])
        return dict(value)

    def _prepared(
        self,
        gateway: ToolGateway,
        context: WorkspaceContext,
        attempt_id: UUID,
        case: McpQualificationCase,
        profile: McpInstalledProfile,
        reference: dict[str, Any],
        task_binding: dict[str, Any],
        native_pins: dict[str, Any],
        authorize: Callable[[], None],
    ) -> tuple[dict[str, Any], Lease]:
        authorize()
        frozen = {
            "installation_hash": profile.installed.installation.fingerprint,
            "arguments": case.arguments,
            "mcp_reference": reference,
        }
        raw = canonical_json(frozen).encode()
        artifact = self.artifacts.put(context, raw, "application/json")
        key = f"mcpq:{attempt_id.hex}:{case.id}"
        inputs = gateway.store.create_inputs(
            context,
            _uuid(task_binding["episodeId"]),
            _uuid(task_binding["contractRevisionId"]),
            _uuid(task_binding["repositoryStateId"]),
            key + ":inputs",
        )
        action = gateway.store.propose(
            context,
            _uuid(inputs["node_id"]),
            ActionProposal(profile.installed.definition.name),
            key + ":action",
            stdin_artifact_id=artifact["id"],
        )
        prior = gateway.store.inspect(context, _uuid(action["id"]))
        if prior["run"] is not None:
            raise IndeterminateExecutionError("Qualification action already has a native attempt")
        authorize()
        candidate = gateway.candidates.allocate(context, _uuid(task_binding["repositoryStateId"]))
        owner = f"mcp-qualification:{attempt_id}:{case.id}"
        lease = gateway.candidates.claim(context, candidate["id"], owner, MAX_ATTEMPT_SECONDS + 60)
        reservation = None
        try:
            definition = profile.installed.definition
            reservation = gateway.budgets.reserve(
                context,
                _uuid(task_binding["episodeId"]),
                {
                    "calls": 2
                    if profile.installed.confirmation or profile.installed.watch_ms
                    else 1,
                    "tool_ms": definition.timeout_ms,
                    "concurrency": 1,
                },
                key + ":budget",
            )
            prepared = {
                "schema": CASE_SCHEMA,
                "phase": "prepared",
                "caseId": case.id,
                "caseSha256": case.sha256,
                "candidateId": str(candidate["id"]),
                "leaseOwner": lease.owner,
                "leaseGeneration": lease.generation,
                "actionId": action["id"],
                "reservationId": reservation["id"],
                "inputArtifactId": str(artifact["id"]),
                "inputSha256": hashlib.sha256(raw).hexdigest(),
                "storageSha256": self._storage,
                "nativePins": dict(native_pins),
            }
            return prepared, lease
        except BaseException:
            # No gateway invocation has started. These owners retain any failure
            # to release; no candidate tree or receipt is deleted here.
            if reservation is not None:
                gateway.budgets.release(
                    context,
                    _uuid(reservation["id"]),
                    "Qualification preparation failed",
                    confirmed_not_started=True,
                )
            gateway.candidates.release(context, lease)
            raise

    def _case_result(
        self,
        gateway: ToolGateway,
        context: WorkspaceContext,
        case: McpQualificationCase,
        prepared: dict[str, Any],
        profile: McpInstalledProfile,
        reference: dict[str, Any],
        task_binding: dict[str, Any],
    ) -> dict[str, Any]:
        selected = gateway.store.inspect(context, _uuid(prepared["actionId"]))
        action, run, completion = selected["action"], selected["run"], selected["completion"]
        expected_input = {
            "installation_hash": profile.installed.installation.fingerprint,
            "arguments": case.arguments,
            "mcp_reference": reference,
        }
        if (
            run is None
            or completion is None
            or action["id"] != prepared["actionId"]
            or action["episode_id"] != task_binding["episodeId"]
            or action["kind"] != profile.installed.definition.name
            or action["tool_definition_digest"] != profile.installed.definition.fingerprint
            or action["stdin_artifact_id"] != prepared["inputArtifactId"]
            or run["reservation_id"] != prepared["reservationId"]
            or run["candidate_id"] != prepared["candidateId"]
            or run["lease_owner"] != prepared["leaseOwner"]
            or run["lease_generation"] != prepared["leaseGeneration"]
            or selected["knowledge_view"]["contract_revision_id"]
            != task_binding["contractRevisionId"]
            or selected["knowledge_view"]["repository_state_id"]
            != task_binding["repositoryStateId"]
            or content_hash(expected_input) != prepared["inputSha256"]
        ):
            raise IndeterminateExecutionError("Qualification has no exact completed gateway case")
        input_raw = self.artifacts.read(context, _uuid(prepared["inputArtifactId"]))
        if input_raw != canonical_json(expected_input).encode():
            raise StateMismatchError("Qualification frozen input bytes were substituted")
        receipt = self.receipts.read(context.workspace_id, _uuid(run["id"]))
        if receipt is None:
            raise IndeterminateExecutionError("Qualification stopped receipt is unavailable")
        candidate = gateway.candidates.get(context, _uuid(prepared["candidateId"]))
        gateway._validate_receipt(context, run, receipt, candidate)
        if (
            content_hash(receipt) != completion["receipt_digest"]
            or receipt.get("mcp_discovery_reference_sha256") != content_hash(reference)
            or receipt.get("mcp_intent_hash") != completion["details"].get("mcp_intent_hash")
            or receipt.get("stdin_digest") != prepared["inputSha256"]
            or receipt.get("processes_stopped") is not True
            or type(receipt.get("elapsed_ms")) is not int
            or not 0 <= receipt["elapsed_ms"] <= MAX_ATTEMPT_SECONDS * 1000
        ):
            raise StateMismatchError("Qualification completion and stopped receipt disagree")
        raw = self.artifacts.read(context, _uuid(completion["stdout_artifact_id"]))
        if raw != base64.b64decode(receipt["stdout"], validate=True):
            raise StateMismatchError("Qualification result differs from its stopped receipt")
        invocation = _sha(receipt.get("mcp_intent_hash"))
        check = None
        reason: str | None = "tool_failed"
        if completion["outcome"] == "completed" and receipt["outcome"] == "completed":
            try:
                check = evaluate_oracle(
                    case.oracle,
                    raw,
                    invocation_sha256=invocation,
                    output_type_ref=profile.installed.capability.output_type_ref,
                )
                reason = None if check["passed"] else "assertion_mismatch"
            except DomainError:
                reason = "invalid_result_evidence"
        return {
            **prepared,
            "phase": "finished",
            "runId": run["id"],
            "resultArtifactId": completion["stdout_artifact_id"],
            "resultSha256": hashlib.sha256(raw).hexdigest(),
            "receiptSha256": completion["receipt_digest"],
            "invocationSha256": invocation,
            "resultCheck": check,
            "processesStopped": True,
            "isolationSha256": content_hash(receipt["isolation_identity"]),
            "elapsedMs": receipt["elapsed_ms"],
            "reasonCode": reason,
        }

    def _release_unstarted(
        self,
        gateway: ToolGateway,
        context: WorkspaceContext,
        prepared: dict[str, Any],
        task_binding: dict[str, Any],
    ) -> None:
        # Serialize with the gateway's episode lock before certifying absence.
        with self.database.transaction(context) as conn:
            conn.execute(
                "SELECT id FROM episodes WHERE workspace_id=%s AND id=%s FOR UPDATE",
                (context.workspace_id, _uuid(task_binding["episodeId"])),
            ).fetchone()
            if (
                conn.execute(
                    "SELECT id FROM action_runs WHERE workspace_id=%s AND action_id=%s",
                    (context.workspace_id, _uuid(prepared["actionId"])),
                ).fetchone()
                is not None
            ):
                raise IndeterminateExecutionError("Qualification dispatch raced with recovery")
            reservation = conn.execute(
                "SELECT status FROM budget_reservations WHERE workspace_id=%s AND id=%s",
                (context.workspace_id, _uuid(prepared["reservationId"])),
            ).fetchone()
            if reservation is None:
                raise StateMismatchError("Qualification reservation is unavailable")
            if reservation["status"] == "reserved":
                gateway.budgets.release(
                    context,
                    _uuid(prepared["reservationId"]),
                    "Qualification case was not dispatched",
                    confirmed_not_started=True,
                    connection=conn,
                )
            lease_row = conn.execute(
                "SELECT * FROM workspace_leases WHERE workspace_id=%s AND candidate_id=%s",
                (context.workspace_id, _uuid(prepared["candidateId"])),
            ).fetchone()
        if (
            lease_row is not None
            and lease_row["status"] == "active"
            and (
                lease_row["owner"] == prepared["leaseOwner"]
                and lease_row["generation"] == prepared["leaseGeneration"]
            )
        ):
            lease = Lease(
                _uuid(prepared["candidateId"]),
                prepared["leaseOwner"],
                prepared["leaseGeneration"],
                lease_row["expires_at"],
            )
            try:
                gateway.candidates.release(context, lease)
            except DomainError:
                pass  # Expired ownership remains fenced for existing recovery.

    async def run(self, context: WorkspaceContext, **options: Any) -> dict[str, Any]:
        return await self._observe(context, recover=False, **options)

    async def recover(self, context: WorkspaceContext, **options: Any) -> dict[str, Any]:
        return await self._observe(context, recover=True, **options)

    async def _observe(
        self,
        context: WorkspaceContext,
        *,
        recover: bool,
        attempt_id: UUID,
        profile: McpInstalledProfile,
        suite: McpQualificationSuite,
        reference: dict[str, Any],
        task_binding: dict[str, Any],
        authorize_attempt: Callable[[], Any],
        record_case: Callable[[dict[str, Any]], Any],
        load_case: Callable[[str], dict[str, Any] | None],
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if (
            not isinstance(attempt_id, UUID)
            or not isinstance(profile, McpInstalledProfile)
            or not isinstance(suite, McpQualificationSuite)
            or not isinstance(reference, dict)
            or not all(callable(value) for value in (authorize_attempt, record_case, load_case))
            or (cancelled is not None and not callable(cancelled))
        ):
            raise InvalidInputError("Qualification requires exact native owners and callbacks")
        binding = profile.installed.discovery_binding_id
        if (
            binding is None
            or reference.get("bindingId") != str(binding)
            or not profile.private_paths
        ):
            raise ScopeDeniedError("Qualification requires an explicitly linked MCP installation")
        task_binding = self._task_shape(task_binding)
        self.native.validate_private_paths((*profile.private_paths, suite.path))
        agent = self.native.homes.lookup(context.workspace_id, profile.agent_id)
        if agent is None:
            raise ScopeDeniedError("Qualification Agent home is unavailable")
        baseline = self.native._runtime(context, agent, profile.installed, verify_bytes=True)
        consumer = qualification_consumer_sha256()
        native_pins = {
            "runtimeSha256": baseline,
            "imageSha256": self.native.runtime.image_digest.removeprefix("sha256:"),
            "qualificationSha256": self.native.runtime.qualification_sha256,
            "agentHomeRevision": agent.revision,
            "consumerSha256": consumer,
            "configurationSha256": profile.configuration_sha256,
            "referenceSha256": content_hash(reference),
        }
        started = time.monotonic()
        observed = self.clock()
        if not isinstance(observed, datetime) or observed.utcoffset() != timedelta(0):
            raise InvalidInputError("Qualification requires a UTC observation clock")
        for case in suite.cases:
            if case.tool_name != profile.installed.tool.name:
                raise ScopeDeniedError("Suite case selects a tool outside its installation")
            validate_arguments(profile.installed.tool, case.arguments)
        with self.database.transaction(context) as conn:
            contract = conn.execute(
                "SELECT content FROM contract_revisions WHERE workspace_id=%s AND id=%s AND episode_id=%s",
                (
                    context.workspace_id,
                    _uuid(task_binding["contractRevisionId"]),
                    _uuid(task_binding["episodeId"]),
                ),
            ).fetchone()
            if (
                contract is None
                or content_hash(contract["content"]) != task_binding["contractSha256"]
            ):
                raise ScopeDeniedError("Qualification task contract is unavailable")
            CapabilityContext((profile.installed.definition.name,)).authorize(
                profile.installed.definition, contract["content"]
            )

        def authorize() -> None:
            if time.monotonic() - started >= MAX_ATTEMPT_SECONDS:
                raise OperationTimeoutError("Qualification exceeded its bounded native deadline")
            if authorize_attempt() is not None:
                raise ScopeDeniedError("Qualification authority must raise on denial")
            if cancelled is not None and cancelled():
                raise OperationCancelledError("Qualification was cancelled")
            if self._storage_readback() != self._storage:
                raise StateMismatchError("Qualification private storage changed")
            readback = read_mcp_profile(context, profile.private_paths[0], binding_id=binding)
            current_suite = read_mcp_qualification_suite(suite.path)
            if (
                readback.configuration_sha256 != profile.configuration_sha256
                or current_suite.sha256 != suite.sha256
                or current_suite.file_sha256 != suite.file_sha256
                or qualification_consumer_sha256() != consumer
                or self.native._runtime(context, agent, profile.installed) != baseline
            ):
                raise StateMismatchError("Qualification native profile or implementation changed")
            if (
                not recover
                and read_mcp_task_binding(
                    self.database,
                    context,
                    episode_id=_uuid(task_binding["episodeId"]),
                    repository_state_id=_uuid(task_binding["repositoryStateId"]),
                    contract_revision_id=_uuid(task_binding["contractRevisionId"]),
                )
                != task_binding
            ):
                raise StateMismatchError("Qualification task binding changed")

        authorize()
        with self._preparation(context, agent, attempt_id, "candidate-owner", authorize):
            gateway = self._gateway(context, profile, agent, authorize, recover=recover)
        if not recover and any(load_case(case.id) is not None for case in suite.cases):
            raise IndeterminateExecutionError("Qualification attempt already has prepared cases")
        try:
            async with asyncio.timeout(MAX_ATTEMPT_SECONDS - (time.monotonic() - started)):
                for case in suite.cases:
                    authorize()
                    if recover:
                        saved = load_case(case.id)
                        if saved is None:
                            continue
                        prepared = self._check_case_event(
                            case, saved, attempt_id=attempt_id, native_pins=native_pins
                        )
                        selected = gateway.store.inspect(context, _uuid(prepared["actionId"]))
                        if selected["run"] is None:
                            if prepared["phase"] == "finished":
                                raise StateMismatchError(
                                    "Qualification finished evidence has no retained run"
                                )
                            self._release_unstarted(gateway, context, prepared, task_binding)
                            continue
                        result = gateway.reconcile(context, _uuid(selected["run"]["id"]))
                        if result.get("status") == "indeterminate":
                            raise IndeterminateExecutionError(
                                "Qualification gateway outcome remains unknown"
                            )
                    else:
                        with self._preparation(context, agent, attempt_id, case.id, authorize):
                            prepared, lease = self._prepared(
                                gateway,
                                context,
                                attempt_id,
                                case,
                                profile,
                                reference,
                                task_binding,
                                native_pins,
                                authorize,
                            )
                            record_case(prepared)
                            if load_case(case.id) != prepared:
                                raise StateMismatchError(
                                    "Qualification preparation was not retained exactly"
                                )
                        try:
                            authorize()
                            await gateway.execute_mcp(
                                context,
                                _uuid(prepared["actionId"]),
                                lease,
                                _uuid(prepared["reservationId"]),
                            )
                        except BaseException:
                            # A missing completion is never recast as a stopped
                            # failure. The existing gateway owns reconciliation.
                            selected = gateway.store.inspect(context, _uuid(prepared["actionId"]))
                            if selected["run"] is None:
                                self._release_unstarted(gateway, context, prepared, task_binding)
                            raise
                    finished = self._case_result(
                        gateway, context, case, prepared, profile, reference, task_binding
                    )
                    if recover and prepared["phase"] == "finished" and finished != prepared:
                        raise StateMismatchError(
                            "Qualification finished journal differs from retained gateway evidence"
                        )
                    record_case(finished)
                    if load_case(case.id) != finished:
                        raise StateMismatchError("Qualification result was not retained exactly")
                    if not recover:
                        gateway.candidates.release(context, lease)
                    authorize()
        except TimeoutError:
            raise IndeterminateExecutionError(
                "Qualification deadline requires receipt reconciliation"
            ) from None
        authorize()
        if self.native._runtime(context, agent, profile.installed, verify_bytes=True) != baseline:
            raise StateMismatchError("Qualification runtime bytes changed during execution")
        completed = self.clock()
        if (
            not isinstance(completed, datetime)
            or completed.utcoffset() != timedelta(0)
            or not observed <= completed
            or completed - observed > timedelta(seconds=MAX_ATTEMPT_SECONDS)
        ):
            raise StateMismatchError("Qualification observation clock is inconsistent")
        cases = [value for case in suite.cases if (value := load_case(case.id)) is not None]
        return {
            "schema": REPORT_SCHEMA,
            "workspaceId": str(context.workspace_id),
            "principalId": str(context.principal_id),
            "attemptId": str(attempt_id),
            "agentId": str(agent.agent_id),
            "suiteSha256": suite.sha256,
            "suiteFileSha256": suite.file_sha256,
            "consumerSha256": consumer,
            "configurationSha256": profile.configuration_sha256,
            "installationSha256": profile.installed.installation.fingerprint,
            "definitionSha256": profile.installed.definition.fingerprint,
            "runtimeSha256": baseline,
            "imageSha256": self.native.runtime.image_digest.removeprefix("sha256:"),
            "qualificationSha256": self.native.runtime.qualification_sha256,
            "agentHomeRevision": agent.revision,
            "storageSha256": self._storage,
            "referenceSha256": content_hash(reference),
            "taskBinding": task_binding,
            "observedAt": observed.isoformat(),
            "completedAt": completed.isoformat(),
            "casesSha256": content_hash(cases),
        }


@contextmanager
def configured_mcp_qualification_host(
    database: Database,
    context: WorkspaceContext,
    *,
    manifest: Path,
    workspace_name: str,
    artifacts_root: Path,
    receipts_root: Path,
) -> Iterator[McpQualificationHost]:
    with configured_mcp_discovery_host(
        database, context, manifest=manifest, workspace_name=workspace_name
    ) as native:
        yield McpQualificationHost(
            database, native, artifacts_root=artifacts_root, receipts_root=receipts_root
        )

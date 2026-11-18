"""Measured runtime prerequisites for one reviewed discovery candidate.

This is an explicit operator action, not an execution grant, task admission,
dependency installer, credential broker, skill activation or qualification.
The fixed guest probe never runs source code or a declared dependency. Existing
runtime pins, registered identities, S7 supervision, S12 holds and workspace
leases remain their respective authorities. Normal operations still perform
their own admission, even when a recent prerequisite report passed.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    CapabilityUnavailableError,
    IndeterminateExecutionError,
    InvalidInputError,
    OperationCancelledError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.serialization import canonical_json, content_hash
from potbot.tools.agent_homes import AgentHome, AgentHomeRegistry
from potbot.tools.shared_tool_release_markers import blocked_directories, read_hold
from potbot.tools.shared_workspace_isolation import (
    SharedWorkspaceBubblewrapBackend,
    SharedWorkspaceRuntime,
)
from potbot.tools.skill_dependency_probe import (
    BASE_ENVIRONMENT,
    IMAGE_EXECUTABLE,
    MAX_REQUIREMENTS,
    MAX_WIRE_BYTES,
    PROBE_PROGRAM,
    PROBE_SHA256,
    binary_supported,
    parse_observation,
    probe_input,
)
from potbot.tools.supervisor import ProcessRequest, ProcessSupervisor
from potbot.tools.workspace_concurrency import AccessRequest, AccessResource, WorkspaceLeaseLedger
from potbot.tools.workspace_supervision import (
    WorkspaceLeaseGuard,
    admission_digest,
    assert_shared_execution_allowed,
)

if TYPE_CHECKING:
    from potbot.persistence.database import Database

SCHEMA = "potbot-skill-eligibility/v1"
VALID_SECONDS = 120
MAX_REPORT_BYTES = 131072
MAX_REQUIREMENT_BYTES = 65536
REQUIREMENT_KINDS = frozenset(
    {
        "binary",
        "any_binary",
        "environment",
        "configuration",
        "operating_system",
        "tool",
        "compatibility",
    }
)
REASON_CODES = frozenset(
    {
        "sandbox_probe_completed",
        "binary_available",
        "binary_missing",
        "binary_not_executable",
        "binary_hash_unavailable",
        "binary_measurement_limit",
        "binary_changed_during_check",
        "binary_name_unsupported",
        "binary_path_unsupported",
        "shared_tool_not_admitted",
        "shared_tool_held",
        "shared_tool_pin_mismatch",
        "any_binary_available",
        "any_binary_missing",
        "any_binary_unresolved",
        "environment_present",
        "environment_missing",
        "environment_injection_unsupported",
        "operating_system_matches",
        "operating_system_mismatch",
        "operating_system_unsupported",
        "configuration_gate_unsupported",
        "capability_alias_unsupported",
        "compatibility_text_unsupported",
    }
)
_PASSED_REASONS = frozenset(
    {
        "sandbox_probe_completed",
        "binary_available",
        "any_binary_available",
        "environment_present",
        "operating_system_matches",
    }
)
_UNSUPPORTED_REASONS = frozenset(
    {
        "binary_measurement_limit",
        "binary_name_unsupported",
        "any_binary_unresolved",
        "binary_path_unsupported",
        "environment_injection_unsupported",
        "operating_system_unsupported",
        "configuration_gate_unsupported",
        "capability_alias_unsupported",
        "compatibility_text_unsupported",
    }
)
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_BINDING_HASHES = frozenset(
    {
        "runtimeSha256",
        "imageSha256",
        "qualificationSha256",
        "probeSha256",
        "interpreterSha256",
        "observationSha256",
        "isolationSha256",
    }
)


def _digest(value: object) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise InvalidInputError("Eligibility identities require exact lowercase SHA-256")
    return value


def _uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise InvalidInputError("Eligibility identity requires a canonical UUID")
    try:
        selected = UUID(value)
        if str(selected) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise InvalidInputError("Eligibility identity requires a canonical UUID") from None
    return selected


def _name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 512
        or any(ord(char) < 32 and char not in "\n\r\t" for char in value)
    ):
        raise InvalidInputError("Eligibility requirement names must be bounded text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise InvalidInputError("Eligibility requirement names must be UTF-8") from None
    return value


def parse_requirements(value: object) -> list[dict[str, str]]:
    """Normalize declared clauses, retaining OR semantics without changing packages.

    Legacy package manifests may carry ``status=unverified``. That label is not
    an observation and is omitted from this semantic digest. The separately
    bound candidate digest retains the original source record exactly.
    """
    if type(value) is not list or len(value) > MAX_REQUIREMENTS:
        raise InvalidInputError("Eligibility accepts at most 128 declared requirements")
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in value:
        if (
            not isinstance(row, dict)
            or not {"kind", "name"} <= set(row) <= {"kind", "name", "status", "group"}
            or not isinstance(row["kind"], str)
            or row["kind"] not in REQUIREMENT_KINDS
            or ("status" in row and row["status"] != "unverified")
            or (
                "group" in row
                and (
                    row["kind"] not in {"any_binary", "operating_system"}
                    or row["group"] != row["kind"]
                )
            )
        ):
            raise InvalidInputError("Eligibility requirements differ from the declared contract")
        kind, name = row["kind"], _name(row["name"])
        if (kind, name) in seen:
            raise InvalidInputError("Eligibility requirements must be distinct")
        seen.add((kind, name))
        normalized.append(
            {"kind": kind, "name": name}
            | ({"group": kind} if kind in {"any_binary", "operating_system"} else {})
        )
    result = sorted(normalized, key=lambda row: (row["kind"], row["name"]))
    if len(canonical_json(result).encode("utf-8")) > MAX_REQUIREMENT_BYTES:
        raise InvalidInputError("Eligibility declared requirements exceed their 64 KiB limit")
    return result


def requirements_digest(value: object) -> str:
    return content_hash(parse_requirements(value))


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 32:
        raise InvalidInputError("Eligibility timestamps require bounded UTC instants")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.utcoffset() != timedelta(0):
            raise ValueError
    except ValueError:
        raise InvalidInputError("Eligibility timestamps require bounded UTC instants") from None
    return result


def parse_eligibility_report(value: object) -> dict[str, Any]:
    """Validate the complete public, value-blind measurement envelope."""
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema",
            "checkId",
            "agentId",
            "candidateSha256",
            "sourceBindingSha256",
            "requirementsSha256",
            "checkedAt",
            "expiresAt",
            "eligible",
            "checks",
            "bindings",
        }
        or value["schema"] != SCHEMA
        or type(value["eligible"]) is not bool
        or type(value["checks"]) is not list
        or not 1 <= len(value["checks"]) <= MAX_REQUIREMENTS + 1
    ):
        raise InvalidInputError("Eligibility report differs from the versioned public contract")
    _uuid(value["checkId"])
    _uuid(value["agentId"])
    for field in ("candidateSha256", "sourceBindingSha256", "requirementsSha256"):
        _digest(value[field])
    checked, expires = _timestamp(value["checkedAt"]), _timestamp(value["expiresAt"])
    if not timedelta(0) < expires - checked <= timedelta(seconds=VALID_SECONDS):
        raise InvalidInputError("Eligibility observations expire within 120 seconds")
    bindings = value["bindings"]
    if (
        not isinstance(bindings, dict)
        or set(bindings) != _BINDING_HASHES | {"workspaceId", "principalId", "agentHomeRevision"}
        or type(bindings["agentHomeRevision"]) is not int
        or not 1 <= bindings["agentHomeRevision"] <= 2**53 - 1
    ):
        raise InvalidInputError("Eligibility runtime binding is incomplete")
    for field in _BINDING_HASHES:
        _digest(bindings[field])
    for field in ("workspaceId", "principalId"):
        _uuid(bindings[field])
    seen: set[tuple[str, str]] = set()
    for row in value["checks"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"kind", "name", "status", "reasonCode"}
            or not isinstance(row["kind"], str)
            or row["kind"] not in REQUIREMENT_KINDS | {"runtime"}
            or not isinstance(row["status"], str)
            or row["status"] not in {"passed", "blocked", "unsupported"}
            or not isinstance(row["reasonCode"], str)
            or row["reasonCode"] not in REASON_CODES
        ):
            raise InvalidInputError("Eligibility result contains an unsupported public field")
        name = _name(row["name"])
        reason = row["reasonCode"]
        expected_status = (
            "passed"
            if reason in _PASSED_REASONS
            else ("unsupported" if reason in _UNSUPPORTED_REASONS else "blocked")
        )
        reason_kind = next(
            (
                kind
                for prefix, kind in (
                    ("sandbox_", "runtime"),
                    ("any_binary_", "any_binary"),
                    ("binary_", "binary"),
                    ("shared_tool_", "binary"),
                    ("environment_", "environment"),
                    ("operating_system_", "operating_system"),
                    ("configuration_", "configuration"),
                    ("capability_", "tool"),
                    ("compatibility_", "compatibility"),
                )
                if reason.startswith(prefix)
            ),
            None,
        )
        if row["status"] != expected_status or row["kind"] != reason_kind:
            raise InvalidInputError("Eligibility reason differs from its clause and outcome")
        group_name = {
            "any_binary": "any declared binary",
            "operating_system": "any declared operating system",
            "runtime": "selected agent sandbox",
        }.get(row["kind"])
        if group_name is not None and name != group_name:
            raise InvalidInputError("Eligibility alternative group is not canonical")
        key = (row["kind"], name)
        if key in seen:
            raise InvalidInputError("Eligibility result clauses must be distinct")
        seen.add(key)
    if value["eligible"] != all(row["status"] == "passed" for row in value["checks"]) or value[
        "checks"
    ][0] != _check("runtime", "selected agent sandbox", "passed", "sandbox_probe_completed"):
        raise InvalidInputError("Eligibility result is inconsistent with measured clauses")
    if len(canonical_json(value).encode("utf-8")) > MAX_REPORT_BYTES:
        raise InvalidInputError("Eligibility report exceeds its public size limit")
    return value


def _check(kind: str, name: str, status: str, reason: str) -> dict[str, str]:
    return {"kind": kind, "name": name, "status": status, "reasonCode": reason}


def _physical(path: Path) -> list[int]:
    if path.is_symlink() or path.resolve(strict=True) != path:
        raise ScopeDeniedError("Eligibility runtime mounts require physical registered paths")
    details = path.stat()
    if not stat.S_ISDIR(details.st_mode):
        raise ScopeDeniedError("Eligibility runtime mount is not a directory")
    return [details.st_dev, details.st_ino, details.st_uid, details.st_gid, details.st_mode]


def _interpreter_hash(runtime: SharedWorkspaceRuntime, guest: str) -> str:
    if not IMAGE_EXECUTABLE.fullmatch(guest):
        raise InvalidInputError("Eligibility probe must use a fixed image interpreter path")
    selected = (runtime.rootfs / guest.removeprefix("/")).resolve(strict=True)
    if not selected.is_relative_to(runtime.rootfs):
        raise ScopeDeniedError("Eligibility interpreter escapes the pinned image")
    fd = os.open(selected, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not opened.st_mode & 0o111
            or not 1 <= opened.st_size <= 33554432
        ):
            raise ScopeDeniedError("Eligibility interpreter is not a bounded pinned executable")
        hasher = hashlib.sha256()
        count = 0
        while count <= opened.st_size:
            chunk = os.read(fd, min(65536, opened.st_size + 1 - count))
            if not chunk:
                break
            count += len(chunk)
            hasher.update(chunk)
        after = os.fstat(fd)
        fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        if count != opened.st_size or any(
            getattr(opened, field) != getattr(after, field) for field in fields
        ):
            raise StateMismatchError("Eligibility interpreter changed during measurement")
        return hasher.hexdigest()
    finally:
        os.close(fd)


class SkillEligibilityEvaluator:
    """One explicit measurement using an existing configured supervisor.

    Constructors accept trusted runtime owners, never request payload objects.
    Production wiring requires the installed operator attachment. Tests may
    supply explicit supervisor/attachment fixtures and cannot qualify a host.
    """

    def __init__(
        self,
        *,
        runtime: SharedWorkspaceRuntime,
        ledger: WorkspaceLeaseLedger,
        homes: AgentHomeRegistry,
        supervisor: ProcessSupervisor,
        probe_executable_sha256: str,
        probe_executable: str = "/usr/bin/python3",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            not isinstance(runtime, SharedWorkspaceRuntime)
            or not isinstance(ledger, WorkspaceLeaseLedger)
            or not isinstance(homes, AgentHomeRegistry)
            or homes.ledger is not ledger
            or not isinstance(supervisor, ProcessSupervisor)
            or not isinstance(supervisor.backend, SharedWorkspaceBubblewrapBackend)
            or supervisor.backend.runtime is not runtime
            or runtime.tool_maintenance_probe is not None
            or not isinstance(probe_executable, str)
            or not IMAGE_EXECUTABLE.fullmatch(probe_executable)
        ):
            raise ScopeDeniedError("Eligibility needs the current registered shared sandbox owners")
        if supervisor.backend.operator_attachment is None:
            raise CapabilityUnavailableError("Eligibility requires the live installed operator")
        self.runtime, self.ledger, self.homes, self.supervisor = runtime, ledger, homes, supervisor
        self.backend = supervisor.backend
        self.probe_executable = probe_executable
        self.probe_executable_sha256 = _digest(probe_executable_sha256)
        self.clock = clock or (lambda: datetime.now(UTC))

    def _runtime(self, context: WorkspaceContext, agent: AgentHome) -> tuple[str, set[str]]:
        runtime = self.runtime
        home = runtime.home / agent.directory
        assert_shared_execution_allowed(
            runtime=runtime,
            ledger=self.ledger,
            workspace_id=context.workspace_id,
            candidate_path=str(home),
        )
        current = self.homes.lookup(context.workspace_id, agent.agent_id)
        if current != agent:
            raise StateMismatchError("Eligibility agent registration changed")
        attachment = self.backend.operator_attachment
        if attachment is None:
            raise CapabilityUnavailableError("Eligibility operator attachment disappeared")
        health = attachment.require_healthy()
        if _interpreter_hash(runtime, self.probe_executable) != self.probe_executable_sha256:
            raise StateMismatchError("Eligibility interpreter differs from its operator pin")
        held = set(blocked_directories(runtime))
        holds: dict[str, Any] = {}
        if runtime.shared_tools is not None:
            for path, _digest_value in runtime.tool_entrypoints:
                slug = path.removeprefix("/usr/local/tools/").split("/", 1)[0]
                holds[slug] = read_hold(runtime.shared_tools, slug)
        binding = {
            "workspace": str(context.workspace_id),
            "agent": str(agent.agent_id),
            "homeRevision": agent.revision,
            "uid": agent.uid,
            "gid": agent.gid,
            "image": runtime.image_digest,
            "qualification": runtime.qualification_sha256,
            "operatorEpoch": health["epoch"],
            "mounts": {
                str(path): _physical(path)
                for path in (
                    runtime.rootfs,
                    runtime.home,
                    home,
                    runtime.shared_tools,
                    runtime.model_staging,
                    runtime.shared_models,
                    runtime.ai_config_dir,
                    runtime.ai_history_dir,
                )
                if path is not None
            },
            "entrypoints": runtime.tool_entrypoints,
            "holds": holds,
            "blocked": sorted(held),
        }
        return content_hash(binding), held

    def evaluate(
        self,
        context: WorkspaceContext,
        *,
        check_id: UUID,
        agent_id: UUID,
        candidate_sha256: str,
        source_binding_sha256: str,
        requirements: list[dict[str, str]],
        authorize_attempt: Callable[[], None],
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if (
            not isinstance(context, WorkspaceContext)
            or not isinstance(check_id, UUID)
            or not isinstance(agent_id, UUID)
            or not callable(authorize_attempt)
            or (cancelled is not None and not callable(cancelled))
        ):
            raise InvalidInputError("Eligibility requires one explicitly authorized agent/check")
        _digest(candidate_sha256)
        _digest(source_binding_sha256)
        clauses = parse_requirements(requirements)
        agent = self.homes.lookup(context.workspace_id, agent_id)
        if agent is None:
            raise ScopeDeniedError("Eligibility requires an existing registered agent")
        guard = WorkspaceLeaseGuard(
            self.ledger,
            AccessRequest(
                context.workspace_id,
                agent_id,
                "skill.eligibility",
                "skill-eligibility-" + check_id.hex,
                "read",
                (
                    AccessResource("workspace", "*", "read"),
                    AccessResource("shared_tool", "*", "read"),
                ),
                self.runtime.image_digest,
            ),
        )
        guard.start()
        uncertain = False
        try:
            authorize_attempt()
            guard.ensure_live()
            if cancelled is not None and cancelled():
                raise OperationCancelledError(
                    "Eligibility measurement was cancelled before dispatch"
                )
            runtime_hash, held = self._runtime(context, agent)
            pins = dict(self.runtime.tool_entrypoints)
            if len(pins) != len(self.runtime.tool_entrypoints):
                raise StateMismatchError("Eligibility runtime has duplicate executable pins")
            binaries: list[str] = []
            unavailable: dict[str, tuple[str, str]] = {}
            for name in sorted(
                {row["name"] for row in clauses if row["kind"] in {"binary", "any_binary"}}
            ):
                if not binary_supported(name):
                    unavailable[name] = ("unsupported", "binary_name_unsupported")
                elif name.startswith("/usr/local/tools/"):
                    slug = name.removeprefix("/usr/local/tools/").split("/", 1)[0]
                    if name not in pins:
                        unavailable[name] = ("blocked", "shared_tool_not_admitted")
                    elif slug in held:
                        unavailable[name] = ("blocked", "shared_tool_held")
                    else:
                        binaries.append(name)
                else:
                    binaries.append(name)
            environment = sorted(
                {
                    row["name"]
                    for row in clauses
                    if row["kind"] == "environment" and row["name"] in BASE_ENVIRONMENT
                }
            )
            request = ProcessRequest(
                argv=(self.probe_executable, "-I", "-S", "-B", "-c", PROBE_PROGRAM),
                stdin=probe_input(binaries, environment),
                cwd=self.runtime.home / agent.directory,
                workspace_root=self.runtime.home,
                sandbox_root=self.runtime.home,
                timeout_ms=5000,
                max_output_bytes=MAX_WIRE_BYTES,
                memory_bytes=256 * 1024**2,
                cpu_seconds=5,
                max_file_bytes=1024**2,
                guest_uid=agent.uid,
                guest_gid=agent.gid,
                telemetry_workspace_id=context.workspace_id,
                telemetry_agent_id=agent_id,
                telemetry_identity_kind="agent",
                telemetry_run_id=check_id,
            )
            # Runtime/source readback can involve several existing owners. A
            # fenced review must not dispatch merely because it was valid
            # before that readback began.
            authorize_attempt()
            guard.ensure_live()
            if cancelled is not None and cancelled():
                raise OperationCancelledError(
                    "Eligibility measurement was cancelled before dispatch"
                )
            uncertain = True
            result = self.supervisor.run(
                request, cancelled=lambda: guard.fenced or (cancelled is not None and cancelled())
            )
            if result.processes_stopped is True:
                uncertain = False
            else:
                raise IndeterminateExecutionError(
                    "Dependency probe stop is not confirmed by its supervisor"
                )
            if not (
                result.dispatched is True
                and result.processes_stopped is True
                and result.outcome == "completed"
                and result.exit_code == 0
                and result.stdout_truncated is False
                and result.stderr_truncated is False
                and result.stderr == b""
                and isinstance(result.isolation_identity, str)
                and _SHA.fullmatch(result.isolation_identity)
            ):
                raise CapabilityUnavailableError(
                    "Dependency probe has no completed supervised receipt"
                )
            authorize_attempt()
            guard.ensure_live()
            if cancelled is not None and cancelled():
                raise OperationCancelledError(
                    "Eligibility measurement was cancelled before publication"
                )
            if self._runtime(context, agent) != (runtime_hash, held):
                raise StateMismatchError("Eligibility runtime changed during measurement")
            observed = parse_observation(
                result.stdout,
                binaries=binaries,
                environment=environment,
                uid=agent.uid,
                gid=agent.gid,
            )
            checks = self._checks(clauses, observed, unavailable, pins)
            now = self.clock()
            if now.utcoffset() != timedelta(0):
                raise InvalidInputError("Eligibility worker clock must provide UTC instants")
            report = parse_eligibility_report(
                {
                    "schema": SCHEMA,
                    "checkId": str(check_id),
                    "agentId": str(agent_id),
                    "candidateSha256": candidate_sha256,
                    "sourceBindingSha256": source_binding_sha256,
                    "requirementsSha256": requirements_digest(clauses),
                    "checkedAt": now.isoformat(),
                    "expiresAt": (now + timedelta(seconds=VALID_SECONDS)).isoformat(),
                    "eligible": all(row["status"] == "passed" for row in checks),
                    "checks": checks,
                    "bindings": {
                        "workspaceId": str(context.workspace_id),
                        "principalId": str(context.principal_id),
                        "agentHomeRevision": agent.revision,
                        "runtimeSha256": runtime_hash,
                        "imageSha256": self.runtime.image_digest,
                        "qualificationSha256": self.runtime.qualification_sha256,
                        "probeSha256": PROBE_SHA256,
                        "interpreterSha256": self.probe_executable_sha256,
                        "observationSha256": content_hash(observed),
                        "isolationSha256": result.isolation_identity,
                    },
                }
            )
            guard.finish(evidence_sha256=content_hash(report))
            return report
        except BaseException:
            try:
                guard.finish(
                    evidence_sha256=admission_digest("skill-eligibility-rejected"),
                    uncertain=uncertain,
                )
            except StateMismatchError:
                guard.abandon()
            raise

    @staticmethod
    def _checks(
        clauses: list[dict[str, str]],
        observed: dict[str, Any],
        unavailable: dict[str, tuple[str, str]],
        pins: dict[str, str],
    ) -> list[dict[str, str]]:
        outcomes = dict(unavailable)
        reasons = {
            "present": ("passed", "binary_available"),
            "missing": ("blocked", "binary_missing"),
            "not_executable": ("blocked", "binary_not_executable"),
            "unreadable": ("blocked", "binary_hash_unavailable"),
            "limit": ("unsupported", "binary_measurement_limit"),
            "changed": ("blocked", "binary_changed_during_check"),
            "outside_scope": ("unsupported", "binary_path_unsupported"),
        }
        for row in observed["binaries"]:
            outcome = reasons[row["state"]]
            if (
                row["state"] == "present"
                and row["name"] in pins
                and row["sha256"] != pins[row["name"]]
            ):
                outcome = ("blocked", "shared_tool_pin_mismatch")
            outcomes[row["name"]] = outcome
        environment = {row["name"]: row["present"] for row in observed["environment"]}
        result = [_check("runtime", "selected agent sandbox", "passed", "sandbox_probe_completed")]
        for row in clauses:
            kind, name = row["kind"], row["name"]
            if kind in {"any_binary", "operating_system"}:
                continue
            if kind == "binary":
                status, reason = outcomes[name]
            elif kind == "environment":
                if name not in BASE_ENVIRONMENT:
                    status, reason = "unsupported", "environment_injection_unsupported"
                elif environment[name]:
                    status, reason = "passed", "environment_present"
                else:
                    status, reason = "blocked", "environment_missing"
            else:
                status, reason = (
                    "unsupported",
                    {
                        "configuration": "configuration_gate_unsupported",
                        "tool": "capability_alias_unsupported",
                        "compatibility": "compatibility_text_unsupported",
                    }[kind],
                )
            result.append(_check(kind, name, status, reason))
        alternatives = [outcomes[row["name"]][0] for row in clauses if row["kind"] == "any_binary"]
        if alternatives:
            if "passed" in alternatives:
                status, reason = "passed", "any_binary_available"
            elif "unsupported" in alternatives:
                status, reason = "unsupported", "any_binary_unresolved"
            else:
                status, reason = "blocked", "any_binary_missing"
            result.append(_check("any_binary", "any declared binary", status, reason))
        systems = [row["name"] for row in clauses if row["kind"] == "operating_system"]
        if systems:
            if observed["operatingSystem"] in systems:
                status, reason = "passed", "operating_system_matches"
            elif any(name not in {"linux", "darwin", "win32", "freebsd"} for name in systems):
                status, reason = "unsupported", "operating_system_unsupported"
            else:
                status, reason = "blocked", "operating_system_mismatch"
            result.append(
                _check("operating_system", "any declared operating system", status, reason)
            )
        return result


def configured_skill_eligibility(
    database: Database,
    context: WorkspaceContext,
    *,
    manifest: Path,
    workspace_name: str,
    probe_executable_sha256: str,
    probe_executable: str = "/usr/bin/python3",
) -> SkillEligibilityEvaluator:
    """Attach to the existing installed owner; never start a second watcher.

    These arguments are host CLI/configuration, never HTTP/model payloads. The
    retained operator check still has to validate its signed claim inside the
    evaluator's lease. A manifest or old result cannot supply that authority.
    """
    from potbot.tools.operator_cli import parse_manifest
    from potbot.tools.tool_release_gate_s12 import bind_installed_tool_releases

    if os.environ.get("POTBOT_OPERATOR_REQUIRED") != "1" or not os.environ.get(
        "POTBOT_OPERATOR_SCOPED_LAUNCHER"
    ):
        raise ScopeDeniedError("Eligibility requires the installed scoped worker service")
    paths = parse_manifest(manifest)
    if paths.workspace_id != context.workspace_id:
        raise ScopeDeniedError("Eligibility manifest belongs to another workspace")
    ledger = WorkspaceLeaseLedger(paths.controller_ledger, workspace_home=paths.home)
    homes = AgentHomeRegistry(ledger, workspace_name=workspace_name)
    runtime = bind_installed_tool_releases(database, context, paths.runtime(), ledger, homes)
    backend = SharedWorkspaceBubblewrapBackend(runtime)
    if backend.operator_attachment is None:
        raise CapabilityUnavailableError("Eligibility requires the existing live operator")
    backend.operator_attachment.require_healthy()
    return SkillEligibilityEvaluator(
        runtime=runtime,
        ledger=ledger,
        homes=homes,
        supervisor=ProcessSupervisor(backend),
        probe_executable_sha256=probe_executable_sha256,
        probe_executable=probe_executable,
    )

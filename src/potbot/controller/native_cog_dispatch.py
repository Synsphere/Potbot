"""Dispatch an explicitly installed read-only Cog from the ordinary leased job worker."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from potbot.cogs.contracts import CogRunContext, TypedCogValue
from potbot.context.installed_syn_route import installed_context_route
from potbot.controller.cog_capability_policy import CogPolicyDenied, Denial, installation_hash
from potbot.controller.cog_grant_store import current_cog_policy
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    CapabilityUnavailableError,
    IndeterminateExecutionError,
    OperationCancelledError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.jobs import CogJobInput, JobLease
from potbot.domain.serialization import canonical_json, content_hash
from potbot.domain.syn import type_identity
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.syn_release.contracts import DOCUMENT_LIMIT, document, fields
from potbot.syn_release.mesh_endpoint import NativeMeshClient, root_path
from potbot.tools.agent_homes import AgentHomeRegistry
from potbot.tools.execution_contracts import ExecutionPolicy, WorkBinding, digest, identifier
from potbot.tools.operator_cli import OperatorPaths, parse_manifest
from potbot.tools.released_syn_nodes import ReleasedSynWorkerNodeMesh
from potbot.tools.sandbox_cog_worker import SandboxedCogExecutor, SandboxedCogOperation
from potbot.tools.shared_workspace_isolation import SharedWorkspaceBubblewrapBackend
from potbot.tools.supervisor import ProcessSupervisor
from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger
from potbot.workspace_sandbox import load_workspace_sandbox

if TYPE_CHECKING:
    from potbot.controller.jobs import JobService
    from potbot.persistence.database import Database


def read_profile(path: Path) -> tuple[dict[str, Any], str]:
    info = root_path(path)
    if info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISREG(info.st_mode):
        raise ScopeDeniedError("Native Cog job profile must be administrator-owned.")
    with path.open("rb") as reader:
        data = reader.read(DOCUMENT_LIMIT + 1)
    raw = document(data)
    version = raw.get("profile")
    if version not in (
        "potbot.hosted-native-cog/1",
        "potbot.hosted-native-cog/2",
        "potbot.hosted-native-cog/3",
    ):
        raise ScopeDeniedError("Unknown native Cog job profile.")
    profile = fields(
        raw,
        {
            "profile",
            "workspace_id",
            "workspace_name",
            "socket",
            "manifest_sha256",
            "release_key_sha256",
            "group_id",
            "group_epoch",
            "agents",
            "operations",
            "image_digest",
            "qualification_sha256",
        }
        | ({"context_rpc"} if version != "potbot.hosted-native-cog/1" else set()),
    )
    if version != "potbot.hosted-native-cog/1":
        fields(profile["context_rpc"], {"store", "operator_root", "policy", "policy_sha256"})
    return profile, hashlib.sha256(data).hexdigest()


class NativeCogDispatcher:
    def __init__(
        self,
        executor: SandboxedCogExecutor,
        operations: tuple[SandboxedCogOperation, ...],
        agents: frozenset[UUID],
        profile_hash: str,
        *,
        persistent_cog_grants: bool = False,
    ) -> None:
        self.executor = executor
        self.operations = {op.binding.operation_ref: op for op in operations}
        if not self.operations or len(self.operations) != len(operations):
            raise ScopeDeniedError("Native Cog installation requires distinct operations.")
        self.agents, self.profile_hash = agents, profile_hash
        self.persistent_cog_grants = persistent_cog_grants

    def _prepare(
        self, jobs: JobService, actor: WorkspaceContext, lease: JobLease
    ) -> tuple[CogJobInput, SandboxedCogOperation, TypedCogValue, CogRunContext]:
        """Shared exact-input checks; this method grants and dispatches no work."""
        request = CogJobInput.parse(lease.input)
        operation = self.operations.get(request.operation_ref)
        if (
            request.profile_sha256 != self.profile_hash
            or request.agent_id not in self.agents
            or operation is None
            or type_identity(request.type_ref) != type_identity(operation.binding.input_type_ref)
        ):
            raise ScopeDeniedError("Job differs from the installed native Cog profile.")
        with jobs.database.transaction(actor) as connection:
            state = connection.execute(
                "SELECT content_digest FROM repository_states WHERE workspace_id=%s AND id=%s",
                (actor.workspace_id, request.repository_state_id),
            ).fetchone()
        if state is None or state["content_digest"] != operation.binding.policy.source_digest:
            raise ScopeDeniedError("Job repository state differs from the installed Cog pin.")
        # Public canonical aliases identify the same type. Preserve the signed
        # request above, then use the installed spelling at the worker wire.
        value = TypedCogValue(operation.binding.input_type_ref, request.value)
        invocation = CogRunContext(
            self.profile_hash, lease.contract_revision_id, request.repository_state_id, lease.job_id
        )
        return request, operation, value, invocation

    def eligibility(
        self,
        jobs: JobService,
        actor: WorkspaceContext,
        lease: JobLease,
        *,
        check: Callable[[str, Callable[[], Any]], Any],
    ) -> tuple[dict[str, Any], dict[str, int]]:
        """Observe the configured worker without work dispatch, grant or reservation.

        The caller controls report formatting and safe error classification. The
        ordinary run path still revalidates its admission and one-use Syn grant.
        """
        request, operation, _value, invocation = check(
            "native_profile", lambda: self._prepare(jobs, actor, lease)
        )
        bindings: dict[str, Any] = {
            "profileSha256": self.profile_hash,
            "installationSha256": installation_hash(operation),
            "repositorySha256": operation.binding.policy.source_digest,
        }

        def task() -> None:
            with jobs.database.transaction(actor) as connection:
                row = connection.execute(
                    "SELECT c.content FROM episodes e JOIN contract_revisions c "
                    "ON c.workspace_id=e.workspace_id AND c.id=e.active_contract_id "
                    "JOIN repository_states r ON r.workspace_id=e.workspace_id "
                    "AND r.repository_id=e.repository_id WHERE e.workspace_id=%s "
                    "AND e.id=%s AND e.status='running' AND c.id=%s AND r.id=%s",
                    (
                        actor.workspace_id,
                        lease.episode_id,
                        lease.contract_revision_id,
                        request.repository_state_id,
                    ),
                ).fetchone()
            if row is None or (
                "cog.execute:" + operation.binding.operation_ref
                not in row["content"]["capabilities"]
            ):
                raise ScopeDeniedError("The active task does not authorize this native Cog.")

        check("native_task_capability", task)

        def protocol() -> dict[str, Any]:
            # The ordinary job dispatcher has no directed provider broker. Its
            # admission must not promise a callback that run() never supplies.
            if operation.directed_rest or operation.directed_graphql or operation.directed_grpc:
                raise CapabilityUnavailableError(
                    "This directed operation requires an independently installed broker."
                )
            binding = self.executor._context_binding
            if binding is not None:
                if binding.database is not jobs.database or binding.principal != actor:
                    raise ScopeDeniedError("The Context controller belongs to another scope.")
                binding.check(self.executor, actor)
            identity: dict[str, Any] = {"protocol": operation.protocol}
            if operation.protocol == "context-rpc/v1":
                from potbot.context.syn_context_route import SynContextRoute

                route = self.executor._syn_context_route
                if (
                    binding is None
                    or not isinstance(route, SynContextRoute)
                    or route.workspace_id != actor.workspace_id
                ):
                    raise ScopeDeniedError("The installed native Context route is unavailable.")
                identity.update(
                    workspace=str(route.workspace_id),
                    operator_boot=route.operator_boot,
                    caller_uid=route.caller_uid,
                )
            return identity

        protocol_identity = check("native_protocol", protocol)

        def runtime() -> None:
            from potbot.tools.workspace_supervision import assert_shared_execution_allowed

            selected = self.executor.runtime
            home = self.executor.homes.lookup(actor.workspace_id, request.agent_id)
            if (
                home is None
                or home.agent_id != request.agent_id
                or home.workspace_id != actor.workspace_id
            ):
                raise ScopeDeniedError("Native Cog Agent home is unavailable.")
            if selected.tool_maintenance_probe is not None:
                raise ScopeDeniedError(
                    "Normal operation checks cannot borrow maintenance authority."
                )
            assert_shared_execution_allowed(
                runtime=selected,
                ledger=self.executor.ledger,
                workspace_id=actor.workspace_id,
                candidate_path=str(selected.home / home.directory),
            )
            backend = self.executor.supervisor.backend
            if (
                not isinstance(backend, SharedWorkspaceBubblewrapBackend)
                or backend.runtime != selected
            ):
                raise ScopeDeniedError("Native Cog supervisor differs from its installed runtime.")
            attachment = backend.operator_attachment
            if attachment is None:
                raise CapabilityUnavailableError("A live operator attachment is required.")
            attachment.require_healthy()
            bindings["runtimeSha256"] = content_hash(
                {
                    "image": selected.image_digest,
                    "qualification": selected.qualification_sha256,
                    "home": str(selected.home.resolve(strict=True)),
                    "rootfs": str(selected.rootfs.resolve(strict=True)),
                    "uid": home.uid,
                    "gid": home.gid,
                    "entrypoints": selected.tool_entrypoints,
                    "context": protocol_identity,
                }
            )

        check("native_runtime", runtime)
        check("native_executable", lambda: self.executor._executable(operation))

        def release() -> None:
            from potbot.tools.shared_tool_release_markers import blocked_directories, read_hold

            selected = self.executor.runtime
            blocked = blocked_directories(selected)
            executable = operation.argv[0]
            if executable.startswith("/usr/local/tools/"):
                slug = executable.removeprefix("/usr/local/tools/").split("/", 1)[0]
                if slug in blocked or selected.shared_tools is None:
                    raise ScopeDeniedError("The installed tool remains held by its release gate.")
                bindings["releaseSha256"] = content_hash(read_hold(selected.shared_tools, slug))

        check("tool_release", release)

        def syn() -> dict[str, Any]:
            mesh = self.executor.syn_mesh
            if not isinstance(mesh, ReleasedSynWorkerNodeMesh):
                raise CapabilityUnavailableError("A released native Syn installation is required.")
            mesh.probe(actor.workspace_id, request.agent_id, installation=True)
            native = self.executor.syn_provenance(actor, request.agent_id)
            if native is None:
                raise CapabilityUnavailableError("Native installation provenance is unavailable.")
            bindings["nativeSha256"] = content_hash(native)
            return native

        native = check("native_syn", syn)

        def grants() -> None:
            if not self.persistent_cog_grants:
                raise CapabilityUnavailableError(
                    "Task checks require persistent native Cog grants."
                )
            with jobs.database.transaction(actor) as connection:
                current = current_cog_policy(connection, actor, request.agent_id)
                if current is None:
                    raise CogPolicyDenied(Denial.INSTALLATION)
                selected = current.policy.select(
                    actor, request.agent_id, native, operation, parent=None
                )
                current.policy.require_active_task(connection, invocation, selected)
            bindings["grantGeneration"] = current.generation
            bindings["grantSha256"] = current.policy.fingerprint
            bindings["policySha256"] = current.policy.fingerprint

        check("native_capability_policy", grants)
        return bindings, {
            "calls": 1,
            "tool_ms": operation.binding.policy.max_wall_ms,
            "concurrency": 1,
        }

    def run(
        self, jobs: JobService, actor: WorkspaceContext, lease: JobLease, abort: threading.Event
    ) -> dict[str, str]:
        request, operation, value, invocation = self._prepare(jobs, actor, lease)
        if abort.is_set() or jobs.cancelled(actor, lease):
            raise OperationCancelledError("Native Cog was cancelled before admission.")
        native = (
            self.executor.syn_provenance(actor, request.agent_id)
            if self.persistent_cog_grants
            else None
        )
        generation: int | None = None

        def check_cog_grant(connection: Any) -> dict[str, Any]:
            current = current_cog_policy(
                connection, actor, request.agent_id, for_dispatch=generation is not None
            )
            if current is None:
                raise CogPolicyDenied(Denial.INSTALLATION)
            if generation is not None and current.generation != generation:
                raise CogPolicyDenied(Denial.REPLACED)
            selected = current.policy.select(
                actor, request.agent_id, native, operation, parent=None
            )
            current.policy.require_active_task(connection, invocation, selected)
            return {
                "generation": current.generation,
                "sha256": current.policy.fingerprint,
                "capabilities": sorted(cap.value for cap in selected.capabilities),
                "decision": "allow",
            }

        if self.persistent_cog_grants:
            with jobs.database.transaction(actor) as connection:
                generation = check_cog_grant(connection)["generation"]
        grant = self.executor.authorize_syn(operation, value, invocation, actor, request.agent_id)
        if grant is None:
            raise ScopeDeniedError("Native Cog dispatch requires a released Syn grant.")
        if abort.is_set() or jobs.cancelled(actor, lease):
            raise OperationCancelledError("Native Cog was cancelled before dispatch.")
        jobs.mark_dispatched(
            actor,
            lease,
            cog_max_wall_ms=operation.binding.policy.max_wall_ms,
            check_cog_grant=check_cog_grant if self.persistent_cog_grants else None,
        )

        async def execute() -> TypedCogValue:
            pending = asyncio.create_task(
                self.executor.execute(
                    operation, value, invocation, actor, request.agent_id, syn_authorization=grant
                )
            )
            try:
                while not pending.done() and not abort.is_set():
                    await asyncio.wait((pending,), timeout=0.1)
                if abort.is_set() and not pending.done():
                    pending.cancel()
                return await pending
            except asyncio.CancelledError as error:
                raise IndeterminateExecutionError(
                    "Native Cog was cancelled after dispatch."
                ) from error
            finally:
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)

        result = asyncio.run(execute())
        if abort.is_set() or jobs.cancelled(actor, lease):
            raise IndeterminateExecutionError("Native Cog lease changed before result publication.")
        sandbox = load_workspace_sandbox(jobs.database, actor)
        artifacts = ArtifactStore(jobs.database, LocalArtifactBackend(sandbox.path("artifacts")))
        artifact = artifacts.put(
            actor,
            canonical_json({"type_ref": result.type_ref, "value": result.value}).encode(),
            "application/json",
        )
        return {"artifact_id": str(artifact["id"]), "operation_id": str(lease.job_id)}


def installed_cog_operations(
    profile: dict[str, Any], fingerprint: str, paths: OperatorPaths
) -> tuple[SandboxedCogOperation, ...]:
    if (
        identifier(profile["workspace_id"]) != paths.workspace_id
        or paths.image_digest != profile["image_digest"]
        or paths.qualification_sha256 != profile["qualification_sha256"]
    ):
        raise ScopeDeniedError("Native Cog profile differs from the installed workspace or image.")
    if not isinstance(profile["operations"], list) or not 1 <= len(profile["operations"]) <= 32:
        raise ScopeDeniedError("Native Cog profile has invalid operation bounds.")
    operations = []
    for raw in profile["operations"]:
        op = fields(
            raw,
            {
                "operation_ref",
                "entrypoint",
                "input_type_ref",
                "output_type_ref",
                "repository_digest",
                "max_wall_ms",
            },
        )
        image_pin = dict(paths.tool_entrypoints).get(op["entrypoint"])
        if (
            image_pin is None
            or type(op["max_wall_ms"]) is not int
            or not 1 <= op["max_wall_ms"] <= 300_000
        ):
            raise ScopeDeniedError("Native Cog operation is outside the installed executable pins.")
        for port in ("input_type_ref", "output_type_ref"):
            TypedCogValue(op[port], None)
        operations.append(
            SandboxedCogOperation(
                WorkBinding(
                    "tool",
                    op["operation_ref"],
                    op["input_type_ref"],
                    op["output_type_ref"],
                    image_pin,
                    "read_only",
                    ExecutionPolicy(
                        fingerprint,
                        paths.qualification_sha256,
                        digest(op["repository_digest"]),
                        profile["profile"].removeprefix("potbot."),
                        max_wall_ms=op["max_wall_ms"],
                    ),
                ),
                (op["entrypoint"],),
                image_pin,
                protocol="context-rpc/v1",
            )
        )
    return tuple(operations)


@contextmanager
def configured_native_cogs(
    db: Database, actor: WorkspaceContext
) -> Iterator[NativeCogDispatcher | None]:
    selected = os.environ.get("POTBOT_NATIVE_COG_CONFIG")
    if selected is None:
        yield None
        return
    if os.environ.get("POTBOT_OPERATOR_REQUIRED") != "1" or not os.environ.get(
        "POTBOT_OPERATOR_SCOPED_LAUNCHER"
    ):
        raise ScopeDeniedError("Native Cog jobs require the installed scoped worker service.")
    profile, fingerprint = read_profile(Path(selected))
    paths = parse_manifest(Path(os.environ["POTBOT_OPERATOR_MANIFEST"]))
    if paths.workspace_id != actor.workspace_id:
        raise ScopeDeniedError("Native Cog profile differs from the installed workspace or image.")
    operations = installed_cog_operations(profile, fingerprint, paths)
    endpoint = Path(profile["socket"])
    if any(
        private_path.is_relative_to(mount)
        for private_path in (endpoint, Path(selected))
        for mount in (
            paths.rootfs,
            paths.home,
            paths.model_staging,
            paths.shared_models,
            paths.shared_tools,
            paths.ai_config_dir,
            paths.ai_history_dir,
        )
    ):
        raise ScopeDeniedError("Native Syn dispatcher cannot be mounted inside a worker.")
    if not isinstance(profile["agents"], list) or not 1 <= len(profile["agents"]) <= 8:
        raise ScopeDeniedError("Native Cog profile requires a bounded Agent cohort.")
    agents = frozenset(identifier(agent) for agent in profile["agents"])
    if (
        len(agents) != len(profile["agents"])
        or type(profile["group_epoch"]) is not int
        or profile["group_epoch"] <= 0
    ):
        raise ScopeDeniedError("Native Cog profile has invalid Agent bounds.")
    runtime = paths.runtime()
    ledger = WorkspaceLeaseLedger(paths.controller_ledger, workspace_home=paths.home)
    homes = AgentHomeRegistry(ledger, workspace_name=profile["workspace_name"])
    if any(homes.lookup(actor.workspace_id, agent) is None for agent in agents):
        raise ScopeDeniedError("Native Cog cohort has an unregistered Agent workspace home.")
    from potbot.tools.tool_release_gate_s12 import bind_installed_tool_releases

    runtime = bind_installed_tool_releases(db, actor, runtime, ledger, homes)
    backend = SharedWorkspaceBubblewrapBackend(runtime)
    if backend.operator_attachment is None:
        raise StateMismatchError("Native Cog jobs require a live installed operator.")
    backend.operator_attachment.require_healthy()
    transport = NativeMeshClient(
        endpoint,
        actor.workspace_id,
        manifest_sha256=digest(profile["manifest_sha256"]),
        release_key_sha256=digest(profile["release_key_sha256"]),
        group_id=identifier(profile["group_id"]),
        group_epoch=profile["group_epoch"],
        agents=agents,
    )
    try:
        workers = ReleasedSynWorkerNodeMesh(transport)
        route_context = (
            installed_context_route(
                profile["context_rpc"],
                actor.workspace_id,
                protected_mounts=tuple(
                    path.resolve(strict=True)
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
                ),
            )
            if profile["profile"] != "potbot.hosted-native-cog/1"
            else nullcontext(None)
        )
        with route_context as route, workers.running():
            executor = SandboxedCogExecutor(
                runtime, ledger, homes, ProcessSupervisor(backend), workers, syn_context_route=route
            )
            executor.bind_context(db, actor, operations)
            yield NativeCogDispatcher(
                executor,
                operations,
                agents,
                fingerprint,
                persistent_cog_grants=profile["profile"] == "potbot.hosted-native-cog/3",
            )
    finally:
        transport.close()

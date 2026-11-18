"""Host-owned MCP metadata reconciliation on the installed native runtime.

Profiles are private operator files. They never come from a registry, browser,
model proposal or retained catalogue. This owner reuses the current Agent home,
workspace lease, qualified interactive supervisor, Syn route and OAuth broker.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
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
    OperationTimeoutError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.execution import CapabilityContext, ToolDefinition, ToolRegistry
from potbot.domain.serialization import content_hash
from potbot.tools.agent_homes import AgentHome, AgentHomeRegistry
from potbot.tools.capability_contracts import (
    ToolCapabilityDescriptor,
    ToolImplementationDescriptor,
    ToolInstallationDescriptor,
)
from potbot.tools.interactive_supervisor import InteractiveExit, InteractiveProcessSupervisor
from potbot.tools.mcp_discovery import McpDiscoveryCatalog
from potbot.tools.mcp_gateway import InstalledMcpTool
from potbot.tools.mcp_protocol import (
    MODERN,
    McpCatalogChangedError,
    McpToolInfo,
    parse_json_document,
)
from potbot.tools.mcp_sandbox import ManagedMcpScope, SandboxedMcpClient
from potbot.tools.shared_tool_release_markers import blocked_directories, read_hold
from potbot.tools.shared_workspace_isolation import (
    SharedWorkspaceBubblewrapBackend,
    SharedWorkspaceRuntime,
)
from potbot.tools.syn_mcp_route import SynMcpRoute, installed_mcp_route
from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger
from potbot.tools.workspace_supervision import (
    WorkspaceExecutorIdentity,
    WorkspaceLeaseGuard,
    assert_shared_execution_allowed,
)
from potbot.workspace_sandbox import WorkspaceSandbox

if TYPE_CHECKING:
    from potbot.persistence.database import Database
    from potbot.service_credentials import ServiceCredentials

PROFILE_SCHEMA = "potbot-mcp-installed-profile/v1"
OBSERVATION_SCHEMA = "potbot-mcp-observation/v1"
MAX_PROFILE_BYTES = 512 * 1024
MAX_ATTEMPT_SECONDS = 120
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_FILE_FIELDS = (
    "st_dev",
    "st_ino",
    "st_uid",
    "st_gid",
    "st_mode",
    "st_nlink",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
)


def _digest(value: Any) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise InvalidInputError("MCP native binding requires a SHA-256 digest")
    return value


def _identifier(value: Any) -> UUID:
    try:
        identity = UUID(value)
        if str(identity) != value:
            raise ValueError
        return identity
    except (ValueError, TypeError, AttributeError):
        raise InvalidInputError("MCP profile requires canonical UUID identities") from None


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return all(getattr(left, key) == getattr(right, key) for key in _FILE_FIELDS)


@contextmanager
def _private_file(path: Path, *, maximum: int, private: bool = True) -> Iterator[tuple[int, bytes]]:
    """Hold an inode through parsing; never follow a leaf or ancestor link."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or path.resolve(strict=True) != path:
        raise ScopeDeniedError("MCP operator files require existing physical absolute paths")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    descriptor: int | None = None
    try:
        for component in path.parts[1:-1]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory,
            )
            os.close(directory)
            directory = child
            info = os.fstat(directory)
            sticky = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            if info.st_uid not in {0, os.geteuid()} or (info.st_mode & 0o022 and not sticky):
                raise ScopeDeniedError("MCP operator file has an untrusted writable ancestor")
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=directory,
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid not in {0, os.geteuid()}
            or before.st_mode & (0o077 if private else 0o022)
            or before.st_nlink != 1
            or not 0 < before.st_size <= maximum
        ):
            raise ScopeDeniedError("MCP operator file must be private, regular and bounded")
        raw = bytearray()
        while len(raw) <= before.st_size:
            chunk = os.read(descriptor, min(65_536, before.st_size + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) != before.st_size or not _same_file(before, os.fstat(descriptor)):
            raise StateMismatchError("MCP operator file changed during read")
        yield descriptor, bytes(raw)
        if not _same_file(before, os.fstat(descriptor)) or not _same_file(
            before, os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        ):
            raise StateMismatchError("MCP operator file changed while parsing")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


@dataclass(frozen=True, slots=True)
class McpInstalledProfile:
    installed: InstalledMcpTool
    agent_id: UUID
    configuration_sha256: str
    private_paths: tuple[Path, ...] = field(default=(), repr=False)


def read_mcp_profile(
    context: WorkspaceContext,
    path: Path,
    *,
    binding_id: UUID | None = None,
    credentials: ServiceCredentials | None = None,
) -> McpInstalledProfile:
    """Read one exact local installation; reading never acquires OAuth credentials."""
    if not isinstance(context, WorkspaceContext) or (
        binding_id is not None and not isinstance(binding_id, UUID)
    ):
        raise InvalidInputError("MCP profile requires an exact workspace and optional binding")
    with _private_file(path, maximum=MAX_PROFILE_BYTES) as (_descriptor, raw):
        value = parse_json_document(raw)
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "schema",
                "agentId",
                "definition",
                "tool",
                "capability",
                "implementation",
                "installation",
                "executableSha256",
                "oauthRegistration",
            }
            or value["schema"] != PROFILE_SCHEMA
        ):
            raise InvalidInputError("Invalid installed MCP profile envelope")
        agent_id = _identifier(value["agentId"])
        definition = value["definition"]
        if not isinstance(definition, dict) or set(definition) != {
            field.name for field in fields(ToolDefinition)
        }:
            raise InvalidInputError("MCP profile requires every exact ToolDefinition field")
        definition = dict(definition)
        if not isinstance(definition["argv_prefix"], list):
            raise InvalidInputError("MCP installed argv must be an array")
        definition["argv_prefix"] = tuple(definition["argv_prefix"])
        for name in ("environment", "secret_refs"):
            if not isinstance(definition[name], list) or any(
                not isinstance(pair, list) or len(pair) != 2 for pair in definition[name]
            ):
                raise InvalidInputError("MCP installed environment must contain exact pairs")
            definition[name] = tuple(tuple(pair) for pair in definition[name])
        provider = None
        registration_hash = None
        registration_bytes_hash = None
        registration_path = value["oauthRegistration"]
        if registration_path is not None:
            from potbot.api.hosted_mcp_oauth import read_mcp_oauth_registration
            from potbot.service_credentials import ServiceCredentials

            if not isinstance(registration_path, str):
                raise InvalidInputError("MCP OAuth registration must be a private native path")
            with _private_file(Path(registration_path), maximum=16_384) as (fd, oauth_raw):
                # The existing parser opens this already-verified inode, avoiding
                # a second pathname lookup that could select different bytes.
                provider = read_mcp_oauth_registration(
                    Path(f"/proc/self/fd/{fd}"), context, credentials or ServiceCredentials()
                ).provider
                registration_hash = provider.registration_fingerprint
                registration_bytes_hash = hashlib.sha256(oauth_raw).hexdigest()
        configuration_sha256 = content_hash(
            {
                "schema": "potbot-mcp-installed-profile-binding/v1",
                "profileSha256": hashlib.sha256(raw).hexdigest(),
                "oauthRegistrationSha256": registration_hash,
                "oauthRegistrationFileSha256": registration_bytes_hash,
            }
        )
        installed = InstalledMcpTool(
            definition=ToolDefinition(**definition),
            tool=McpToolInfo.from_wire(value["tool"], allow_http_headers=provider is not None),
            executable_sha256=_digest(value["executableSha256"]),
            capability=ToolCapabilityDescriptor.from_dict(value["capability"]),
            implementation=ToolImplementationDescriptor.from_dict(value["implementation"]),
            installation=ToolInstallationDescriptor.from_dict(value["installation"]),
            http_provider=provider,
            principal_id=context.principal_id,
            discovery_binding_id=binding_id,
            discovery_configuration_sha256=configuration_sha256 if binding_id else None,
        )
        installed.authorize(context)
        result = McpInstalledProfile(
            installed,
            agent_id,
            configuration_sha256,
            (Path(path),) + (() if registration_path is None else (Path(registration_path),)),
        )
    return result


def load_discovery_installation(
    context: WorkspaceContext,
    path: Path,
    binding_id: UUID,
    credentials: ServiceCredentials | None = None,
) -> InstalledMcpTool:
    """Load an execution registration with mandatory exact discovery linkage."""
    if not isinstance(binding_id, UUID):
        raise InvalidInputError("A discovery-linked installation requires its exact binding UUID")
    return read_mcp_profile(context, path, binding_id=binding_id, credentials=credentials).installed


def _physical(path: Path) -> list[int]:
    if path.is_symlink() or path.resolve(strict=True) != path:
        raise ScopeDeniedError("MCP runtime requires physical registered paths")
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise ScopeDeniedError("MCP runtime mount is not a directory")
    return [info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode]


def _entrypoint(
    runtime: SharedWorkspaceRuntime, installed: InstalledMcpTool, *, verify_bytes: bool
) -> dict[str, Any]:
    guest = installed.definition.argv_prefix[0]
    if not guest.startswith("/") or any(part in {"", ".", ".."} for part in guest.split("/")[1:]):
        raise ScopeDeniedError("MCP executable must be an exact absolute installed path")
    shared = guest.startswith("/usr/local/tools/")
    base = runtime.shared_tools if shared else runtime.rootfs
    if base is None:
        raise ScopeDeniedError("MCP shared executable has no installed tool store")
    if shared and dict(runtime.tool_entrypoints).get(guest) != installed.executable_sha256:
        raise ScopeDeniedError("MCP shared executable has no matching operator pin")
    relative = guest.removeprefix("/usr/local/tools/") if shared else guest.removeprefix("/")
    original = base / relative
    selected = original.resolve(strict=True)
    if not selected.is_relative_to(base.resolve(strict=True)) or (shared and original != selected):
        raise ScopeDeniedError("MCP executable escapes or redirects its admitted native root")
    fd = os.open(selected, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or not before.st_mode & 0o111
            or not (1 <= before.st_size <= 32 * 1024 * 1024)
        ):
            raise ScopeDeniedError("MCP entrypoint is not a bounded installed executable")
        if verify_bytes:
            digest = hashlib.sha256()
            count = 0
            while count <= before.st_size:
                chunk = os.read(fd, min(65_536, before.st_size + 1 - count))
                if not chunk:
                    break
                count += len(chunk)
                digest.update(chunk)
            if count != before.st_size or digest.hexdigest() != installed.executable_sha256:
                raise StateMismatchError("MCP executable bytes differ from the installed pin")
        if not _same_file(before, os.fstat(fd)) or not _same_file(before, selected.stat()):
            raise StateMismatchError("MCP executable changed during readback")
        return {
            "path": str(selected),
            "physical": [getattr(before, key) for key in _FILE_FIELDS],
            "sha256": installed.executable_sha256,
        }
    finally:
        os.close(fd)


def build_mcp_observation(
    context: WorkspaceContext,
    *,
    installed: InstalledMcpTool,
    agent: AgentHome,
    configuration_sha256: str,
    runtime: SharedWorkspaceRuntime,
    runtime_sha256: str,
    observed_at: datetime,
    completed_at: datetime,
    completion: InteractiveExit,
) -> dict[str, Any]:
    """Build bounded evidence from an actual supervisor stop, never from a catalogue."""
    if (
        not isinstance(completion, InteractiveExit)
        or completion.processes_stopped is not True
        or type(completion.return_code) is not int
        or completion.status_sha256 is None
    ):
        raise IndeterminateExecutionError(
            "MCP process shutdown lacks confirmed supervisor evidence"
        )
    installed.authorize(context)
    _digest(completion.status_sha256)
    if agent.workspace_id != context.workspace_id or not 1 <= agent.revision < 2**53:
        raise ScopeDeniedError("MCP observation Agent identity differs from its workspace")
    if any(
        not isinstance(value, datetime) or value.utcoffset() != timedelta(0)
        for value in (observed_at, completed_at)
    ) or not timedelta(0) <= completed_at - observed_at <= timedelta(seconds=MAX_ATTEMPT_SECONDS):
        raise OperationTimeoutError("MCP observation exceeds its bounded UTC lifetime")
    return {
        "schema": OBSERVATION_SCHEMA,
        "workspaceId": str(context.workspace_id),
        "principalId": str(context.principal_id),
        "agentId": str(agent.agent_id),
        "installationSha256": installed.installation.fingerprint,
        "configurationSha256": _digest(configuration_sha256),
        "definitionSha256": installed.definition.fingerprint,
        "executableSha256": installed.executable_sha256,
        "runtimeSha256": _digest(runtime_sha256),
        "imageSha256": runtime.image_digest,
        "qualificationSha256": runtime.qualification_sha256,
        "agentHomeRevision": agent.revision,
        "observedAt": observed_at.isoformat(),
        "completedAt": completed_at.isoformat(),
        "isolationSha256": _digest(completion.isolation_identity),
        "stopReceiptSha256": content_hash(asdict(completion)),
        "processesStopped": True,
    }


class McpDiscoveryHost:
    """One explicitly claimed attempt; no daemon, scheduler, replay or installation."""

    def __init__(
        self,
        runtime: SharedWorkspaceRuntime,
        homes: AgentHomeRegistry,
        supervisor: InteractiveProcessSupervisor,
        syn_route: SynMcpRoute,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            not isinstance(runtime, SharedWorkspaceRuntime)
            or not isinstance(homes, AgentHomeRegistry)
            or not isinstance(supervisor, InteractiveProcessSupervisor)
            or not isinstance(supervisor.backend, SharedWorkspaceBubblewrapBackend)
            or supervisor.backend.runtime is not runtime
            or supervisor.backend.tool_write_grant is not None
            or runtime.tool_maintenance_probe is not None
            or not isinstance(syn_route, SynMcpRoute)
            or (clock is not None and not callable(clock))
        ):
            raise ScopeDeniedError("MCP discovery requires the current registered native owners")
        if supervisor.backend.operator_attachment is None:
            raise CapabilityUnavailableError("MCP discovery requires the live installed operator")
        self.runtime, self.homes, self.supervisor, self.syn_route = (
            runtime,
            homes,
            supervisor,
            syn_route,
        )
        self.ledger, self.backend = homes.ledger, supervisor.backend
        self.clock = clock or (lambda: datetime.now(UTC))

    def validate_private_paths(self, paths: tuple[Path, ...]) -> tuple[Path, ...]:
        """Preflight local operator selections before claiming a durable attempt."""
        if not isinstance(paths, tuple) or not 1 <= len(paths) <= 2:
            raise InvalidInputError("MCP profile requires bounded private native paths")
        mounts = tuple(
            path.resolve(strict=True)
            for path in (
                self.runtime.rootfs,
                self.runtime.home,
                self.runtime.shared_tools,
                self.runtime.shared_skills,
                self.runtime.model_staging,
                self.runtime.shared_models,
                self.runtime.ai_config_dir,
                self.runtime.ai_history_dir,
            )
            if path is not None
        )
        checked = []
        for path in paths:
            if not isinstance(path, Path):
                raise InvalidInputError("MCP private selections must be native Paths")
            with _private_file(path, maximum=MAX_PROFILE_BYTES):
                if any(path.is_relative_to(mount) for mount in mounts):
                    raise ScopeDeniedError("MCP operator configuration overlaps a worker mount")
            checked.append(path)
        return tuple(checked)

    def _runtime(
        self,
        context: WorkspaceContext,
        agent: AgentHome,
        installed: InstalledMcpTool,
        *,
        verify_bytes: bool = False,
    ) -> str:
        runtime = self.runtime
        home = runtime.home / agent.directory
        installed.authorize(context)
        assert_shared_execution_allowed(
            runtime=runtime,
            ledger=self.ledger,
            workspace_id=context.workspace_id,
            candidate_path=str(home),
        )
        if self.homes.lookup(context.workspace_id, agent.agent_id) != agent:
            raise StateMismatchError("MCP Agent registration changed during observation")
        attachment = self.backend.operator_attachment
        if attachment is None:
            raise CapabilityUnavailableError("MCP operator attachment disappeared")
        health = attachment.require_healthy()
        held = set(blocked_directories(runtime))
        guest = installed.definition.argv_prefix[0]
        if guest.startswith("/usr/local/tools/") and guest.split("/")[4] in held:
            raise ScopeDeniedError("MCP shared executable is held pending release")
        holds = {
            guest_name: read_hold(runtime.shared_tools, guest_name.split("/")[4])
            for guest_name, _pin in runtime.tool_entrypoints
            if runtime.shared_tools is not None
        }
        return content_hash(
            {
                "workspace": str(context.workspace_id),
                "agent": str(agent.agent_id),
                "homeRevision": agent.revision,
                "uid": agent.uid,
                "gid": agent.gid,
                "installation": installed.installation.fingerprint,
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
                        runtime.shared_skills,
                        runtime.ai_config_dir,
                        runtime.ai_history_dir,
                    )
                    if path is not None
                },
                "entrypoints": runtime.tool_entrypoints,
                "holds": holds,
                "blocked": sorted(held),
                "executable": _entrypoint(runtime, installed, verify_bytes=verify_bytes),
            }
        )

    def _candidate(
        self,
        context: WorkspaceContext,
        agent: AgentHome,
        attempt_id: UUID,
        authorize: Callable[[], None],
    ) -> Path:
        guard = WorkspaceLeaseGuard.exclusive_home(
            self.ledger,
            WorkspaceExecutorIdentity(
                context.workspace_id, agent.agent_id, self.runtime.image_digest
            ),
            operation="mcp.discovery.prepare",
            idempotency_key="mcp-discovery-prepare-" + attempt_id.hex,
        )
        guard.start()
        uncertain = False
        descriptors: list[int] = []
        name = "mcp-discovery-" + attempt_id.hex
        try:
            authorize()
            guard.ensure_live()
            parent = self.homes._dir_fd()
            descriptors.append(parent)
            for component in (agent.directory, "candidates"):
                if component == "candidates":
                    try:
                        os.mkdir(component, 0o700, dir_fd=parent)
                    except FileExistsError:
                        pass
                parent = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=parent,
                )
                descriptors.append(parent)
            info = os.fstat(parent)
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ScopeDeniedError("MCP candidate parent must remain private and host-owned")
            try:
                os.mkdir(name, 0o700, dir_fd=parent)
            except FileExistsError:
                raise StateMismatchError(
                    "MCP attempt candidate already exists; no automatic replay"
                ) from None
            uncertain = True
            child = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent,
            )
            descriptors.append(child)
            os.fchmod(child, 0o700)
            os.fsync(child)
            os.fsync(parent)
            candidate = self.runtime.home / agent.directory / "candidates" / name
            if _physical(candidate) != [
                os.fstat(child).st_dev,
                os.fstat(child).st_ino,
                os.fstat(child).st_uid,
                os.fstat(child).st_gid,
                os.fstat(child).st_mode,
            ]:
                raise StateMismatchError("MCP attempt candidate changed during preparation")
            uncertain = False
            authorize()
            return candidate
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            try:
                guard.finish(evidence_sha256=content_hash({"candidate": name}), uncertain=uncertain)
            finally:
                guard.abandon()

    async def observe(
        self,
        context: WorkspaceContext,
        *,
        attempt_id: UUID,
        installed: InstalledMcpTool,
        configuration_sha256: str,
        agent_id: UUID,
        watch_ms: int,
        authorize_attempt: Callable[[], Any],
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[McpDiscoveryCatalog, dict[str, Any]]:
        if (
            not isinstance(context, WorkspaceContext)
            or not isinstance(attempt_id, UUID)
            or not isinstance(agent_id, UUID)
            or not isinstance(installed, InstalledMcpTool)
            or not callable(authorize_attempt)
            or (cancelled is not None and not callable(cancelled))
            or self.syn_route.workspace_id != context.workspace_id
            or type(watch_ms) is not int
            or (watch_ms != 0 and not 250 <= watch_ms <= 5000)
        ):
            raise InvalidInputError("MCP observation requires one bounded admitted native attempt")
        _digest(configuration_sha256)
        if (
            installed.confirmation is not None
            or installed.watch_ms
            or (watch_ms and installed.http_provider is None)
        ):
            raise ScopeDeniedError(
                "MCP discovery supports baseline installations and a separate HTTP metadata watch"
            )
        installed.authorize(context)
        started = time.monotonic()
        observed_at = self.clock()
        if not isinstance(observed_at, datetime) or observed_at.utcoffset() != timedelta(0):
            raise InvalidInputError("MCP discovery clock must provide UTC instants")
        agent = self.homes.lookup(context.workspace_id, agent_id)
        if agent is None:
            raise ScopeDeniedError("MCP discovery requires an existing registered Agent home")
        baseline = self._runtime(context, agent, installed, verify_bytes=True)
        candidate: Path | None = None
        candidate_identity: list[int] | None = None

        def authorize() -> None:
            if time.monotonic() - started >= MAX_ATTEMPT_SECONDS:
                raise OperationTimeoutError("MCP discovery exceeded its total attempt deadline")
            if authorize_attempt() is not None:
                raise ScopeDeniedError("MCP discovery authority must raise on denial")
            if cancelled is not None and cancelled():
                raise OperationCancelledError("MCP discovery attempt was cancelled")
            if self._runtime(context, agent, installed) != baseline or (
                candidate is not None and _physical(candidate) != candidate_identity
            ):
                raise StateMismatchError("MCP native runtime changed during observation")

        authorize()
        candidate = self._candidate(context, agent, attempt_id, authorize)
        candidate_identity = _physical(candidate)
        session = SandboxedMcpClient(
            supervisor=self.supervisor,
            sandbox=WorkspaceSandbox.existing(self.runtime.home / agent.directory),
            candidate_root=candidate,
            server_kind=installed.definition.name,
            registry=ToolRegistry((installed.definition,)),
            capabilities=CapabilityContext(()),
            task_contract={},
            executable_sha256=installed.executable_sha256,
            permitted=(),
            approve_call=_deny_invocation,
            cancelled=cancelled,
            syn_route=self.syn_route,
            workspace_id=context.workspace_id,
            managed_scope=ManagedMcpScope(context, agent_id, attempt_id, self.homes),
            http_provider=installed.http_provider,
            http_header_tool=installed.tool,
            watch_ms=watch_ms,
            discovery_only=True,
            authorize_discovery=authorize,
        )
        try:
            remaining = MAX_ATTEMPT_SECONDS - (time.monotonic() - started)
            async with asyncio.timeout(remaining):
                async with session:
                    catalog = await session.discover_catalog()
                    claims = catalog.to_dict()
                    if watch_ms and any(claims["notificationSupport"].values()):
                        if claims["protocolVersion"] != MODERN:
                            raise CapabilityUnavailableError(
                                "Bounded MCP metadata watch requires modern HTTP"
                            )
                        if await session.watch_catalog():
                            raise McpCatalogChangedError(
                                "MCP catalogue changed during the admitted watch"
                            )
                    authorize()
        except TimeoutError:
            if session.completion is None or session.completion.processes_stopped is not True:
                raise IndeterminateExecutionError(
                    "MCP timed out without confirmed process shutdown"
                ) from None
            raise OperationTimeoutError(
                "MCP discovery exceeded its total attempt deadline"
            ) from None
        except BaseException:
            if (
                session.completion is not None and session.completion.processes_stopped is not True
            ) or (session.completion is None and session.isolation_identity is not None):
                raise IndeterminateExecutionError(
                    "MCP rejected observation has no confirmed process shutdown"
                ) from None
            raise
        if session.completion is None or session.completion.processes_stopped is not True:
            raise IndeterminateExecutionError("MCP observation has no confirmed process shutdown")
        authorize()
        if self._runtime(context, agent, installed, verify_bytes=True) != baseline:
            raise StateMismatchError("MCP native executable or runtime changed during observation")
        completed_at = self.clock()
        report = build_mcp_observation(
            context,
            installed=installed,
            agent=agent,
            configuration_sha256=configuration_sha256,
            runtime=self.runtime,
            runtime_sha256=baseline,
            observed_at=observed_at,
            completed_at=completed_at,
            completion=session.completion,
        )
        return catalog, report


def _deny_invocation(*_: Any) -> None:
    raise ScopeDeniedError("MCP metadata discovery does not admit tool invocation")


@contextmanager
def configured_mcp_discovery_host(
    database: Database,
    context: WorkspaceContext,
    *,
    manifest: Path,
    workspace_name: str,
) -> Iterator[McpDiscoveryHost]:
    """Attach to existing qualified native owners for one host CLI lifetime."""
    from potbot.controller.native_cog_dispatch import read_profile
    from potbot.tools.operator_cli import parse_manifest
    from potbot.tools.tool_release_gate_s12 import bind_installed_tool_releases

    selected = os.environ.get("POTBOT_NATIVE_COG_CONFIG")
    if (
        os.environ.get("POTBOT_OPERATOR_REQUIRED") != "1"
        or not os.environ.get("POTBOT_OPERATOR_SCOPED_LAUNCHER")
        or not selected
    ):
        raise ScopeDeniedError("MCP discovery requires the installed scoped worker and Syn service")
    with _private_file(manifest, maximum=8192, private=False):
        paths = parse_manifest(manifest)
    profile, _fingerprint = read_profile(Path(selected))
    if (
        paths.workspace_id != context.workspace_id
        or profile["workspace_id"] != str(context.workspace_id)
        or profile["workspace_name"] != workspace_name
        or profile["image_digest"] != paths.image_digest
        or profile["qualification_sha256"] != paths.qualification_sha256
        or profile["profile"] not in {"potbot.hosted-native-cog/2", "potbot.hosted-native-cog/3"}
    ):
        raise ScopeDeniedError("MCP native service selection differs from its workspace or image")
    # Reject the host selections before binding releases or constructing a route.
    # Runtime manifests retain the existing 0644-compatible public-read policy,
    # but their owner, ancestors and write authority must remain host-controlled.
    manifest_mounts = (
        paths.rootfs,
        paths.home,
        paths.shared_tools,
        paths.model_staging,
        paths.shared_models,
        paths.ai_config_dir,
        paths.ai_history_dir,
    )
    if any(
        private_path.is_relative_to(mount.resolve(strict=True))
        for private_path in (Path(manifest), Path(selected))
        for mount in manifest_mounts
    ):
        raise ScopeDeniedError("MCP native selections must remain outside every worker mount")
    ledger = WorkspaceLeaseLedger(paths.controller_ledger, workspace_home=paths.home)
    homes = AgentHomeRegistry(ledger, workspace_name=workspace_name)
    runtime = bind_installed_tool_releases(database, context, paths.runtime(), ledger, homes)
    backend = SharedWorkspaceBubblewrapBackend(runtime)
    if backend.operator_attachment is None:
        raise CapabilityUnavailableError("MCP discovery requires the existing live operator")
    backend.operator_attachment.require_healthy()
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
    if any(
        private_path.is_relative_to(mount)
        for private_path in (Path(selected), Path(manifest))
        for mount in mounts
    ):
        raise ScopeDeniedError("MCP native Syn selection must remain outside worker mounts")
    with installed_mcp_route(
        profile["context_rpc"], context.workspace_id, protected_mounts=mounts
    ) as route:
        yield McpDiscoveryHost(runtime, homes, InteractiveProcessSupervisor(backend), route)

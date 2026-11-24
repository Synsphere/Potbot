"""Native remote workspace contracts. Values alone never grant execution authority."""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import content_hash

CONTROL_PERMISSIONS = frozenset(
    {
        "workspace.inspect",
        "tool.execute",
        "model.invoke",
        "apps.launch",
        "apps.view",
        "apps.input",
        "apps.terminate",
    }
)
NODE_STATES = (
    "registered",
    "inspected",
    "trusted",
    "installing",
    "enrolled",
    "qualifying",
    "ready",
    "awaiting_unlock",
    "needs_reconciliation",
    "offline",
    "fenced",
    "retired",
)


def _uuid(value: UUID) -> None:
    if not isinstance(value, UUID) or value.int == 0:
        raise InvalidInputError("A nonzero UUID is required.")


def _text(value: str, label: str, limit: int = 200) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= limit
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise InvalidInputError(f"{label} must be bounded text without control characters.")


def _digest(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise InvalidInputError("An exact lowercase SHA-256 identity is required.")


def _positive(value: int, label: str) -> None:
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise InvalidInputError(f"{label} must be a positive bounded integer.")


def _time(value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise InvalidInputError("An explicit timezone is required.")


def _native_root(value: str) -> None:
    _text(value, "Native root", 4096)
    path = PurePosixPath(value)
    if (
        not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or ".." in path.parts
        or str(path) != value
        or value == "/"
    ):
        raise InvalidInputError("A normalized absolute Linux project path is required.")


@dataclass(frozen=True, slots=True)
class RemoteNode:
    """Operator-reviewed public SSH registration, without login key bytes."""

    id: UUID
    workspace_id: UUID
    owner_principal_id: UUID
    hostname: str
    port: int
    username: str
    ssh_public_key: str
    ssh_fingerprint: str
    credential_reference: str
    generation: int = 1

    def __post_init__(self) -> None:
        for value in (self.id, self.workspace_id, self.owner_principal_id):
            _uuid(value)
        _text(self.hostname, "SSH hostname", 253)
        if "%" in self.hostname:
            raise InvalidInputError(
                "Scoped IPv6 hostnames need a separately supported SSH adapter."
            )
        try:
            ipaddress.ip_address(self.hostname)
        except ValueError:
            if not all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in self.hostname.split(".")
            ):
                raise InvalidInputError("SSH hostname must be a literal IP or DNS name.") from None
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise InvalidInputError("SSH port must be an integer from 1 to 65535.")
        if not isinstance(self.username, str) or not re.fullmatch(
            r"[a-z_][a-z0-9_-]{0,31}", self.username
        ):
            raise InvalidInputError("A bounded native Linux SSH user is required.")
        # First slice supports exactly one fully parsed public-key format. Other
        # key algorithms and host CAs need their own validated adapters.
        if not isinstance(self.ssh_public_key, str):
            raise InvalidInputError(
                "An independently verified Ed25519 public host key is required."
            )
        parts = self.ssh_public_key.split(" ")
        if len(parts) != 2 or parts[0] != "ssh-ed25519" or len(parts[1]) > 100:
            raise InvalidInputError(
                "An independently verified Ed25519 public host key is required."
            )
        try:
            wire = base64.b64decode(parts[1], validate=True)
        except (ValueError, binascii.Error):
            raise InvalidInputError("The public host key encoding is invalid.") from None
        prefix = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
        if (
            len(wire) != len(prefix) + 32
            or not wire.startswith(prefix)
            or base64.b64encode(wire).decode() != parts[1]
        ):
            raise InvalidInputError("The public host key does not encode an Ed25519 key.")
        fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(wire).digest()).decode().rstrip(
            "="
        )
        if self.ssh_fingerprint != fingerprint:
            raise InvalidInputError("The host key does not match the independently verified pin.")
        if self.credential_reference != f"secret://remote-nodes/{self.id}/ssh":
            raise InvalidInputError("The SSH credential reference must belong to this node.")
        _positive(self.generation, "Node generation")

    def to_dict(self) -> dict[str, Any]:
        return {
            key: str(value) if isinstance(value, UUID) else value
            for key, value in asdict(self).items()
        }


@dataclass(frozen=True, slots=True)
class NativeEnvironment:
    """A native environment fingerprint, separate from sandbox image qualification."""

    os_id: str
    os_version: str
    architecture: str
    interpreter: str
    runtime_sha256: str
    dependencies_sha256: str
    runner_policy_sha256: str
    device: str = "cpu"
    driver: str = "none"

    def __post_init__(self) -> None:
        for name in ("os_id", "os_version", "architecture", "interpreter", "device", "driver"):
            _text(getattr(self, name), name)
        for value in (self.runtime_sha256, self.dependencies_sha256, self.runner_policy_sha256):
            _digest(value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "workspace.execution-environment/1",
            "execution_mode": "base_os",
            **asdict(self),
        }

    @property
    def sha256(self) -> str:
        return content_hash(self.to_dict())


@dataclass(frozen=True, slots=True)
class WorkspaceDeployment:
    workspace_id: UUID
    node_workspace_id: UUID
    node_id: UUID
    owner_principal_id: UUID
    root_path: str
    execution_account: str
    environment: NativeEnvironment
    revision: int = 1

    def __post_init__(self) -> None:
        for value in (
            self.workspace_id,
            self.node_workspace_id,
            self.node_id,
            self.owner_principal_id,
        ):
            _uuid(value)
        _native_root(self.root_path)
        if not isinstance(self.execution_account, str) or not re.fullmatch(
            r"potbot-w-[0-9a-f]{16}", self.execution_account
        ):
            raise InvalidInputError("A dedicated non-root Potbot workspace account is required.")
        if not isinstance(self.environment, NativeEnvironment):
            raise InvalidInputError("A tagged native environment is required.")
        _positive(self.revision, "Deployment revision")

    def to_dict(self) -> dict[str, Any]:
        result = {
            key: str(value) if isinstance(value, UUID) else value
            for key, value in asdict(self).items()
        }
        result["environment"] = self.environment.to_dict()
        result["environment_sha256"] = self.environment.sha256
        result["execution_mode"] = "base_os"
        return result


@dataclass(frozen=True, slots=True)
class WorkspaceControlGrant:
    id: UUID
    workspace_id: UUID
    issuer_principal_id: UUID
    source_workspace_id: UUID
    principal_id: UUID
    permissions: tuple[str, ...]
    expires_at: datetime
    revision: int = 1
    revocation_generation: int = 1
    revoked: bool = False

    def __post_init__(self) -> None:
        for value in (
            self.id,
            self.workspace_id,
            self.issuer_principal_id,
            self.source_workspace_id,
            self.principal_id,
        ):
            _uuid(value)
        if self.workspace_id == self.source_workspace_id:
            raise InvalidInputError("A control grant needs distinct source and target workspaces.")
        if (
            not isinstance(self.permissions, tuple)
            or not self.permissions
            or any(not isinstance(value, str) for value in self.permissions)
            or len(set(self.permissions)) != len(self.permissions)
            or not set(self.permissions) <= CONTROL_PERMISSIONS
        ):
            raise InvalidInputError("Explicit unique workspace control permissions are required.")
        _time(self.expires_at)
        _positive(self.revision, "Grant revision")
        _positive(self.revocation_generation, "Revocation generation")
        if type(self.revoked) is not bool:
            raise InvalidInputError("Revocation must be a boolean.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "workspace.control-grant/1",
            "id": str(self.id),
            "workspace_id": str(self.workspace_id),
            "issuer_principal_id": str(self.issuer_principal_id),
            "source_workspace_id": str(self.source_workspace_id),
            "principal_id": str(self.principal_id),
            "permissions": sorted(self.permissions),
            "expires_at": self.expires_at.isoformat(),
            "revision": self.revision,
            "revocation_generation": self.revocation_generation,
            "revoked": self.revoked,
            "delegation": False,
        }

    def authorize(
        self,
        context: WorkspaceContext,
        *,
        target_workspace_id: UUID,
        permission: str,
        expected_revision: int,
        revocation_generation: int,
        now: datetime,
    ) -> None:
        """Check a CURRENT trusted record, after transport authentication.

        Callers must load the latest record from the target authority. A client
        copy, a signature alone or this pure validator cannot authenticate it.
        No native dispatcher is enabled by this implementation slice.
        """
        _time(now)
        if (
            not isinstance(context, WorkspaceContext)
            or context.workspace_id != self.source_workspace_id
            or context.principal_id != self.principal_id
            or target_workspace_id != self.workspace_id
            or permission not in self.permissions
            or type(expected_revision) is not int
            or expected_revision != self.revision
            or type(revocation_generation) is not int
            or revocation_generation != self.revocation_generation
            or self.revoked
            or now >= self.expires_at
        ):
            raise ScopeDeniedError("The requested remote workspace control is not authorized.")

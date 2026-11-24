"""Read-only host observations and deterministic native provisioning manifests.

Probe runs on the selected host. Imported observations are explicitly untrusted
planning inputs, never authenticated SSH/Syn or installed-host qualification.
No installer, identity enrollment, model download or executor is called here.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.domain.errors import InvalidInputError
from potbot.domain.remote_workspaces import RemoteNode, _digest, _native_root, _text, _uuid
from potbot.domain.serialization import content_hash


@dataclass(frozen=True, slots=True)
class ReleasePin:
    component: str
    version: str
    payload_sha256: str
    manifest_sha256: str
    signer_root_sha256: str

    def __post_init__(self) -> None:
        if self.component not in {"syn", "potbot-runner"}:
            raise InvalidInputError("Only selected Syn and minimal Potbot runner releases apply.")
        _text(self.version, "Release version", 80)
        for value in (self.payload_sha256, self.manifest_sha256, self.signer_root_sha256):
            _digest(value)


@dataclass(frozen=True, slots=True)
class HostObservation:
    system: str
    os_id: str
    os_version: str
    architecture: str
    python_version: str
    boot_id: str
    systemd_running: bool
    available_disk_bytes: int

    def __post_init__(self) -> None:
        for name in ("system", "os_id", "os_version", "architecture", "python_version", "boot_id"):
            _text(getattr(self, name), name)
        if type(self.systemd_running) is not bool:
            raise InvalidInputError("systemd_running must be a boolean observation.")
        if type(self.available_disk_bytes) is not int or self.available_disk_bytes < 0:
            raise InvalidInputError("Available disk must be a nonnegative integer byte count.")


def probe_host() -> HostObservation:
    """Read native host facts without changing accounts, services, keys or policy."""
    release = platform.freedesktop_os_release() if sys.platform == "linux" else {}
    boot_path = Path("/proc/sys/kernel/random/boot_id")
    return HostObservation(
        system=platform.system(),
        os_id=release.get("ID", "unknown"),
        os_version=release.get("VERSION_ID", "unknown"),
        architecture=platform.machine(),
        python_version=platform.python_version(),
        boot_id=boot_path.read_text(encoding="ascii").strip() if boot_path.is_file() else "unknown",
        systemd_running=Path("/run/systemd/system").is_dir()
        and shutil.which("systemctl") is not None,
        available_disk_bytes=shutil.disk_usage(
            "/var/lib" if sys.platform == "linux" else Path.cwd()
        ).free,
    )


def provisioning_manifest(
    node: RemoteNode,
    observation: HostObservation,
    releases: tuple[ReleasePin, ...],
    *,
    target_workspace_id: UUID,
    project_root: str,
    minimum_disk_bytes: int = 1_073_741_824,
) -> dict[str, Any]:
    """Produce a reviewable plan; neither imported facts nor pins establish trust."""
    if not isinstance(node, RemoteNode) or not isinstance(observation, HostObservation):
        raise InvalidInputError("Validated node registration and host observation are required.")
    _uuid(target_workspace_id)
    _native_root(project_root)
    if (
        not isinstance(releases, tuple)
        or len(releases) != 2
        or any(not isinstance(item, ReleasePin) for item in releases)
        or {item.component for item in releases} != {"syn", "potbot-runner"}
    ):
        raise InvalidInputError(
            "Select exactly one pinned Syn and one minimal Potbot runner release."
        )
    if type(minimum_disk_bytes) is not int or minimum_disk_bytes <= 0:
        raise InvalidInputError("An explicit positive installation disk floor is required.")
    blockers = []
    if (
        observation.system != "Linux"
        or observation.os_id != "ubuntu"
        or observation.os_version != "24.04"
        or observation.architecture not in {"x86_64", "amd64"}
    ):
        blockers.append("initial_backend_requires_ubuntu_24_04_x86_64")
    try:
        version = tuple(int(part) for part in observation.python_version.split("."))
        python_ok = len(version) == 3 and version >= (3, 12, 0)
    except ValueError:
        python_ok = False
    if not python_ok:
        blockers.append("python_3_12_or_later_required")
    if not observation.systemd_running:
        blockers.append("running_systemd_required")
    if observation.available_disk_bytes < minimum_disk_bytes:
        blockers.append("insufficient_installation_disk")
    try:
        _uuid(UUID(observation.boot_id))
    except (ValueError, InvalidInputError):
        blockers.append("native_boot_identity_unavailable")
    account = "potbot-w-" + target_workspace_id.hex[:16]
    # These are desired resources for review, not shell code or permission to
    # create them. Model/dataset capacity must be planned for the selected job.
    body = {
        "schema": "workspace.provisioning-manifest/1",
        "node": node.to_dict(),
        "target_workspace_id": str(target_workspace_id),
        "execution_mode": "base_os",
        "project_root": project_root,
        "models_root": project_root + "/models",
        "observation": asdict(observation),
        "observation_source": "supplied_host_facts",
        "minimum_disk_bytes": minimum_disk_bytes,
        "releases": [asdict(item) for item in sorted(releases, key=lambda item: item.component)],
        "host_facts_compatible": not blockers,
        "host_fact_blockers": blockers,
        "required_privilege_changes": [
            {"kind": "service_account", "name": "potbot-node"},
            {"kind": "workspace_account", "name": account},
            {
                "kind": "controller_directory",
                "path": f"/var/lib/potbot/nodes/{node.id}",
                "mode": "0700",
                "owner": "potbot-node",
            },
            {"kind": "workspace_directory", "path": project_root, "owner": account},
            {"kind": "versioned_environments", "path": f"/opt/potbot/nodes/{node.id}"},
            {"kind": "native_systemd_services", "node_id": str(node.id)},
        ],
        "remaining_gates": [
            "independently_verified_ssh_registration",
            "authenticated_fresh_remote_host_inspection",
            "verified_release_signatures_and_installed_bytes",
            "released_native_syn_host_role_and_custody",
            "released_syn_remote_transport_and_fresh_challenge",
            "reviewed_privileged_apply_not_implemented",
        ],
        "capability_readiness": {"tools": False, "models": False, "gui": False},
        "apply_available": False,
    }
    return {"manifest": body, "manifest_sha256": content_hash(body)}


def observation_receipt() -> dict[str, Any]:
    return {
        "schema": "workspace.host-observation/1",
        "source": "local_native_probe",
        "observed_at": datetime.now(UTC).isoformat(),
        "pid": os.getpid(),
        "observation": asdict(probe_host()),
        "remote_route_qualified": False,
    }

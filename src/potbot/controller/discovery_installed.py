"""Read-only preference from the existing acquired-skill authority.

This is an exact source match, not dependency qualification or invocation
permission. Source-only registry claims never establish installation.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from potbot.context.skill_acquisition import _verify_control
from potbot.domain.auth import WorkspaceContext
from potbot.domain.discovery import RegistryCandidate
from potbot.domain.errors import DomainError, InvalidInputError

MAX_INSTALLED_SKILLS = 256


def _matches(candidate: RegistryCandidate, package: dict[str, Any]) -> bool:
    if candidate.kind != "skill" or candidate.status != "active":
        return False
    target, source = candidate.distribution, package["source"]
    if target["kind"] == "github_skill":
        return bool(
            source.get("kind") == "github"
            and source.get("url") == target["repository"]
            and source.get("commit") == target["commit"]
            and source.get("requested_path") == target["path"]
            and source.get("verification") == "content-integrity-only"
        )
    if target["kind"] not in {"clawhub_skill", "skills_sh_snapshot"}:
        return False
    if any(
        source.get(key) != expected
        for key, expected in {
            "registry_adapter": candidate.adapter,
            "registry_identity": candidate.canonical_id,
            "registry_version": candidate.version,
            "registry_candidate_sha256": candidate.sha256,
            "registry_source_sha256": candidate.source_sha256,
        }.items()
    ):
        return False
    # Only the complete resolved inventory can establish equality. A search
    # summary's same name/version is insufficient, even if currently installed.
    expected = {item["path"]: (item["size"], item["sha256"]) for item in target["files"]}
    files = package["manifest"].get("files")
    if not isinstance(files, list):
        return False
    actual = {}
    for entry in files:
        if not isinstance(entry, dict) or not {"path", "size", "sha256"} <= entry.keys():
            return False
        path = entry["path"]
        if not isinstance(path, str) or path in actual:
            return False
        actual[path] = (entry["size"], entry["sha256"])
    return actual == expected


def installed_skill_preference(
    conn: Any,
    context: WorkspaceContext,
    token: str | None,
    candidates: Iterable[RegistryCandidate],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Return matching digests and verified head bindings for cursor invalidation.

    Only this principal's enabled acquired skills participate. Shared authored
    skills and native tools retain their existing discovery/execution owners.
    The 256-row bound limits this optional preference, not skill visibility.
    """
    candidates = tuple(candidates)
    if len(candidates) > 4096 or any(
        not isinstance(item, RegistryCandidate) for item in candidates
    ):
        raise InvalidInputError("Installed preference requires bounded validated candidates")
    selected = [item for item in candidates if item.kind == "skill"]
    if not selected or token is None:
        return [], []
    rows = conn.execute(
        "SELECT * FROM workspace_context_skills WHERE workspace_id=%s "
        "AND owner_principal_id=%s AND origin='acquired' AND published=true "
        "AND enabled=true AND state='active' ORDER BY id LIMIT %s",
        (context.workspace_id, context.principal_id, MAX_INSTALLED_SKILLS + 1),
    ).fetchall()
    installed = set()
    bindings = []
    for raw in rows[:MAX_INSTALLED_SKILLS]:
        row = dict(raw)
        if row.get("owner_principal_id") != context.principal_id:
            continue
        try:
            # This checks current enabled state, independent setup MAC, exact
            # package/source/manifest identities and immutable control receipt.
            package = _verify_control(conn, context, row, token, active=True)
            if not isinstance(package.get("source"), dict):
                continue
            matches = [candidate.sha256 for candidate in selected if _matches(candidate, package)]
        except (DomainError, KeyError, TypeError, ValueError):
            # A revoked, rotated, replaced or malformed control supplies no
            # installed preference. Other candidate discovery remains usable.
            continue
        if not matches:
            continue
        installed.update(matches)
        bindings.append(
            {
                "skillId": str(row["id"]),
                "ownerPrincipalId": str(row["owner_principal_id"]),
                "controlRevision": row["control_revision"],
                "packageId": str(row["current_package_id"]),
                "releaseSha256": row["latest_sha256"],
                "sourceSha256": package["source_sha256"],
                "candidateSha256": sorted(matches),
            }
        )
    if len(rows) > MAX_INSTALLED_SKILLS:
        bindings.append({"preferenceLimit": MAX_INSTALLED_SKILLS, "truncated": True})
    return sorted(installed), bindings

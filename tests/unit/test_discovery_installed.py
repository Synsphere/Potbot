from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from potbot.controller.discovery_installed import installed_skill_preference
from potbot.controller.registry_sources import parse_curated_manifest
from potbot.domain.auth import WorkspaceContext
from potbot.domain.discovery import RegistryCandidate
from potbot.domain.errors import ScopeDeniedError
from potbot.domain.serialization import content_hash

ROOT = Path(__file__).resolve().parents[2]
ACTOR = WorkspaceContext(workspace_id=UUID(int=1), principal_id=UUID(int=2))
TOKEN = "t" * 40


def candidate() -> RegistryCandidate:
    raw = parse_curated_manifest(
        (ROOT / "examples/registry/curated-capabilities.json").read_bytes()
    )[0].to_dict()
    raw.update(
        kind="skill",
        distribution={
            "kind": "github_skill",
            "repository": "https://github.com/example/skills",
            "commit": "a" * 40,
            "path": "skills/demo",
        },
    )
    return RegistryCandidate.from_dict(raw)


def row() -> dict[str, Any]:
    return {
        "id": UUID(int=3),
        "owner_principal_id": ACTOR.principal_id,
        "control_revision": 4,
        "current_package_id": UUID(int=5),
        "latest_sha256": "c" * 64,
        "enabled": True,
    }


def package() -> dict[str, Any]:
    return {
        "source": {
            "kind": "github",
            "url": "https://github.com/example/skills",
            "commit": "a" * 40,
            "requested_path": "skills/demo",
            "verification": "content-integrity-only",
        },
        "source_sha256": "d" * 64,
        "manifest": {"files": []},
    }


class Connection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls = []

    def execute(self, sql: str, params: Any) -> Any:
        self.calls.append((sql, params))
        return self

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


def test_preference_requires_existing_current_control_and_exact_git_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item, current = candidate(), row()
    verified = []

    def authority(
        conn: Any, context: Any, selected: Any, token: str, *, active: bool
    ) -> dict[str, Any]:
        verified.append((context, selected, token, active))
        return package()

    monkeypatch.setattr("potbot.controller.discovery_installed._verify_control", authority)
    conn = Connection([current])
    digests, bindings = installed_skill_preference(conn, ACTOR, TOKEN, [item])
    assert digests == [item.sha256]
    assert verified == [(ACTOR, current, TOKEN, True)]
    assert "owner_principal_id=%s" in conn.calls[0][0]
    assert conn.calls[0][1] == (ACTOR.workspace_id, ACTOR.principal_id, 257)
    assert bindings[0]["controlRevision"] == 4
    assert TOKEN not in str(bindings)


@pytest.mark.parametrize(
    "field,value",
    [
        ("commit", "b" * 40),
        ("url", "https://github.com/other/skills"),
        ("requested_path", "other"),
        ("verification", "claimed"),
    ],
)
def test_nearby_git_names_and_mutable_claims_never_match(
    monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    altered = package()
    altered["source"][field] = value
    monkeypatch.setattr(
        "potbot.controller.discovery_installed._verify_control", lambda *_, **__: altered
    )
    assert installed_skill_preference(Connection([row()]), ACTOR, TOKEN, [candidate()]) == ([], [])


def test_invalid_or_rotated_signature_cannot_supply_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject(*_: Any, **__: Any) -> None:
        raise ScopeDeniedError("Current acquisition authority is absent")

    monkeypatch.setattr("potbot.controller.discovery_installed._verify_control", reject)
    assert installed_skill_preference(Connection([row()]), ACTOR, TOKEN, [candidate()]) == ([], [])
    conn = Connection([row()])
    assert installed_skill_preference(conn, ACTOR, None, [candidate()]) == ([], [])
    assert not conn.calls


def test_update_disable_and_revoke_invalidate_preference_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "potbot.controller.discovery_installed._verify_control", lambda *_, **__: package()
    )
    current = row()
    first = installed_skill_preference(Connection([current]), ACTOR, TOKEN, [candidate()])
    current["control_revision"] += 1
    second = installed_skill_preference(Connection([current]), ACTOR, TOKEN, [candidate()])
    assert content_hash(first[1]) != content_hash(second[1])
    assert content_hash(second[1]) != content_hash(
        installed_skill_preference(Connection([]), ACTOR, TOKEN, [candidate()])[1]
    )


def test_other_principals_and_native_tool_claims_are_not_preferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "potbot.controller.discovery_installed._verify_control",
        lambda *_, **__: pytest.fail("Unrelated owner must not be inspected"),
    )
    another = {**row(), "owner_principal_id": UUID(int=99)}
    assert installed_skill_preference(Connection([another]), ACTOR, TOKEN, [candidate()]) == (
        [],
        [],
    )
    tool = parse_curated_manifest(
        (ROOT / "examples/registry/curated-capabilities.json").read_bytes()
    )[0]
    assert installed_skill_preference(Connection([row()]), ACTOR, TOKEN, [tool]) == ([], [])


def test_exact_registry_preference_requires_every_file_and_stored_candidate_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = candidate().to_dict()
    files = [{"path": "SKILL.md", "size": 5, "sha256": hashlib.sha256(b"exact").hexdigest()}]
    raw.update(
        adapter="clawhub",
        canonicalId="alice/demo",
        version="1.0.0",
        publisher={"id": "alice", "evidence": "registry_claim"},
        sourceUrl="https://clawhub.ai/alice/skills/demo",
        distribution={
            "kind": "clawhub_skill",
            "owner": "alice",
            "slug": "demo",
            "version": "1.0.0",
            "files": files,
        },
    )
    item = RegistryCandidate.from_dict(raw)
    current_package = package()
    current_package["source"] = {
        "registry_adapter": item.adapter,
        "registry_identity": item.canonical_id,
        "registry_version": item.version,
        "registry_candidate_sha256": item.sha256,
        "registry_source_sha256": item.source_sha256,
    }
    current_package["manifest"] = {"files": files}
    monkeypatch.setattr(
        "potbot.controller.discovery_installed._verify_control", lambda *_, **__: current_package
    )
    assert installed_skill_preference(Connection([row()]), ACTOR, TOKEN, [item])[0] == [item.sha256]
    current_package["manifest"]["files"] = files + [
        {"path": "extra", "size": 0, "sha256": "e" * 64}
    ]
    assert installed_skill_preference(Connection([row()]), ACTOR, TOKEN, [item]) == ([], [])
    current_package["manifest"]["files"] = files
    current_package["source"]["registry_candidate_sha256"] = "f" * 64
    assert installed_skill_preference(Connection([row()]), ACTOR, TOKEN, [item]) == ([], [])

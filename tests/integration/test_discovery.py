"""Real PostgreSQL discovery authority and existing-owner acquisition handoffs.

Registry/package bytes are explicit local source fixtures. Runtime reports use
the production evaluator, real image/home/lease owners, and the explicitly
marked supervisor fixture; these tests do not qualify an installed host.
"""

from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from potbot.api.ui_discovery import register_discovery_routes
from potbot.api.wire import OperationSemantics, error_response
from potbot.context.skill_acquisition import SkillAcquisition
from potbot.context.skill_packages import package_from_files
from potbot.controller.capability_discovery import DiscoveryService, _body
from potbot.controller.registry_sources import RegistrySources, parse_curated_manifest
from potbot.domain.discovery import RegistryPage
from potbot.domain.errors import (
    DomainError,
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
)
from potbot.domain.serialization import canonical_json, content_hash
from potbot.persistence.database import (
    Database,
    admin_connection,
    grant_application_role,
    migrate,
    provision_principal,
    provision_workspace,
)
from potbot.persistence.schema import application_migrations
from potbot.tools.tool_acquisition import ToolAcquisitionService, ToolRecipe
from tests.unit import test_registry_sources as registry_fixtures
from tests.unit import test_skill_eligibility as runtime_fixtures
from tests.unit.test_tool_acquisition_recipe import recipe_document

pytestmark = pytest.mark.postgres
TOKEN = "discovery-authority-fixture-" + "a" * 48
runtime_rig = runtime_fixtures.rig


def skill_package(*, changed=False):
    return package_from_files(
        {
            "SKILL.md": (
                b"---\nname: portable-fixture\ndescription: Read a retained fixture reference\n"
                b"metadata:\n  openclaw:\n    requires:\n      bins: [python3]\n"
                b"      anyBins: [curl, unavailable]\n      env: [PATH]\n    os: [linux, darwin]\n"
                b"  numeric:\n    zero: -0.0\n    integral: 12.0\n    exponent: 1.25e-7\n"
                b"---\nRead references/guide.txt.\n"
            ),
            "references/guide.txt": b"changed fixture bytes"
            if changed
            else b"retained fixture bytes",
        },
        source={
            "kind": "github",
            "url": "https://github.com/fixture/skills",
            "commit": "b" * 40,
            "requested_path": "skills/portable-fixture",
        },
    )


def candidate_entry(*, recipe_id=None, changed=False):
    is_tool = recipe_id is not None
    return {
        "canonicalId": "fixture/tool" if is_tool else "fixture/portable-fixture",
        "kind": "tool" if is_tool else "skill",
        "version": "b" * 40,
        "name": "Fixture executable" if is_tool else "Portable fixture",
        "description": "Explicit local discovery fixture" + (" changed" if changed else ""),
        "publisher": {"id": "fixture", "evidence": "operator_claim"},
        "sourceUrl": "https://github.com/fixture/tool"
        if is_tool
        else "https://github.com/fixture/skills",
        "status": "active",
        "capabilities": {
            "classes": ["file_inspection"],
            "inputTypes": [],
            "outputTypes": [],
            "effects": ["read_only"],
            "scopes": [],
            "platforms": ["linux"],
        },
        "requirements": [{"kind": "binary", "name": "python3"}],
        "distribution": {
            "kind": "tool_recipe",
            "repository": "https://github.com/fixture/tool",
            "commit": "b" * 40,
            "recipeId": str(recipe_id),
        }
        if is_tool
        else {
            "kind": "github_skill",
            "repository": "https://github.com/fixture/skills",
            "commit": "b" * 40,
            "path": "skills/portable-fixture",
        },
        "manifest": {"fixture": True, "numeric": {"zero": 0, "fractional": 1.25e-7}},
        "unsupported": [],
    }


class FixtureSources(RegistrySources):
    """Real curated parsing/search, with explicit local bytes at the fetch seam."""

    def __init__(self, raw, clock):
        super().__init__(curated=parse_curated_manifest(raw), now=clock)
        self.calls = []
        self.package = skill_package()
        self.failure = None

    def search(self, adapter, query, cursor=None, limit=25):
        self.calls.append("search")
        if self.failure:
            current = self.now()
            return RegistryPage(
                adapter,
                query,
                (),
                None,
                current.isoformat(),
                (current + timedelta(seconds=60)).isoformat(),
                None,
                "0" * 64,
                self.failure,
            )
        return super().search(adapter, query, cursor=cursor, limit=limit)

    def fetch_skill(self, candidate):
        self.calls.append("fetch")
        assert candidate.kind == "skill" and candidate.version == "b" * 40
        return self.package


@pytest.fixture
def rig(app_env, runtime_rig):
    runtime = runtime_rig
    # The filesystem fixture uses genuine owner classes with a separately
    # generated workspace. Register the same image/agent in this PG scope.
    runtime.ledger.pin_image(
        app_env.context.workspace_id,
        digest=runtime.runtime.image_digest,
        image_ref="ghcr.io/test/image@sha256:" + runtime.runtime.image_digest,
        build_commit="b" * 40,
        platform="linux/amd64",
        rootfs_path=runtime.rootfs,
        qualification_sha256=runtime.runtime.qualification_sha256,
    )
    agent = uuid4()
    runtime.homes.provision(app_env.context.workspace_id, agent)
    token = [TOKEN]
    clock = [runtime.now]
    raw = canonical_json(
        {"schema": "potbot-curated-registry/v1", "entries": [candidate_entry()]}
    ).encode()
    client = FixtureSources(raw, lambda: clock[0])
    service = DiscoveryService(
        app_env.db, setup_token=lambda: token[0], sources=client, clock=lambda: clock[0]
    )
    result = SimpleNamespace(
        env=app_env,
        actor=app_env.context,
        runtime=runtime,
        agent=agent,
        token=token,
        clock=clock,
        raw=raw,
        client=client,
        service=service,
    )
    result.source = add_source(result)
    return result


def add_source(r, *, key="add-source"):
    return r.service.add_source(
        r.actor,
        {
            "adapter": "curated",
            "name": "Fixture registry",
            "ttlSeconds": 900,
            "curatedManifestSha256": hashlib.sha256(r.raw).hexdigest(),
        },
        key,
        setup_token=r.token[0],
    )["source"]


def refresh(r, *, key="refresh"):
    r.source = r.service.refresh(
        r.actor,
        UUID(r.source["id"]),
        "",
        key,
        setup_token=r.token[0],
        curated_manifest_sha256=hashlib.sha256(r.raw).hexdigest(),
    )["source"]
    return r.source


def control(r, kind, current, action, *, key=None):
    return r.service.control(
        r.actor,
        kind,
        UUID(current["id"]),
        action,
        current["revision"],
        current["bindingSha256"],
        key or uuid4().hex,
        setup_token=r.token[0],
    )[kind]


def admit(r):
    refresh(r)
    publisher = r.service.publishers(r.actor)["items"][0]
    r.publisher = control(r, "publisher", publisher, "approve", key="approve-publisher")
    return r.service.candidates(r.actor)["items"][0]


def review(r, *, candidate=None, key="review", agent=None):
    selected = candidate or admit(r)
    return r.service.create_check(
        r.actor,
        UUID(selected["id"]),
        UUID(selected["snapshotId"]),
        selected["candidateSha256"],
        agent or r.agent,
        key,
        setup_token=r.token[0],
    )["check"]


def claim(r, current, *, key="claim"):
    return r.service.claim_measurement(
        r.actor,
        UUID(current["id"]),
        current["revision"],
        current["bindingSha256"],
        key,
        setup_token=r.token[0],
    )


def measure(r, current, *, claim_value=None):
    claimed = claim_value or claim(r, current)
    report = r.runtime.evaluator.evaluate(
        r.actor,
        check_id=UUID(str(claimed["id"])),
        agent_id=UUID(str(claimed["agentId"])),
        candidate_sha256=claimed["candidateSha256"],
        source_binding_sha256=claimed["sourceBindingSha256"],
        requirements=claimed["requirements"],
        authorize_attempt=lambda: r.service.validate_measurement(
            r.actor, UUID(str(claimed["id"])), UUID(str(claimed["workerToken"]))
        ),
    )
    return claimed, report


def complete(r, current):
    claimed, report = measure(r, current)
    return r.service.complete_measurement(
        r.actor, UUID(str(claimed["id"])), UUID(str(claimed["workerToken"])), report
    )["check"]


def action(r, current, name, *, key=None):
    return r.service.action(
        r.actor,
        UUID(current["id"]),
        name,
        current["revision"],
        current["bindingSha256"],
        key or uuid4().hex,
        setup_token=r.token[0],
    )["check"]


def test_source_review_measurement_and_skill_handoff_use_existing_owners(rig):
    r = rig
    refresh(r)
    candidate = r.service.candidates(r.actor)["items"][0]
    assert not candidate["canReview"] and "publisher_not_admitted" in candidate["reasonCodes"]
    with pytest.raises(ScopeDeniedError):
        review(r, candidate=candidate)
    publisher = r.service.publishers(r.actor)["items"][0]
    r.publisher = control(r, "publisher", publisher, "approve")
    prepared = review(r, candidate=candidate)
    assert prepared["status"] == "awaiting_measurement" and prepared["measurement"] is None
    packages = SkillAcquisition(r.env.db, setup_token_resolver=lambda: TOKEN)
    retained = packages.package(r.actor, UUID(prepared["packageId"]))["package"]
    assert retained["releaseSha256"] == r.client.package.release_sha256
    assert retained["manifest"] == r.client.package.manifest
    measured = complete(r, prepared)
    assert measured["status"] == "measured" and measured["measurementCurrent"]
    assert measured["measurement"]["eligible"] is True
    assert "handoff" in measured["actions"]
    handed = action(r, measured, "handoff", key="skill-handoff")
    assert handed["status"] == "handed_off" and handed["target"] is not None
    assert str(prepared["packageId"]) in json.dumps(handed["target"])
    assert action(r, measured, "handoff", key="skill-handoff") == handed
    with r.env.db.transaction(r.actor) as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM workspace_context_skill_packages").fetchone()[
                "n"
            ]
            == 1
        )
        assert (
            conn.execute("SELECT count(*) AS n FROM workspace_context_skills").fetchone()["n"] == 0
        )
        assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 0
    assert r.client.calls == ["search", "fetch"]
    public = json.dumps([prepared, measured, handed, r.service.checks(r.actor)])
    assert TOKEN not in public and "workerToken" not in public and '"mac"' not in public


def test_discovery_is_private_even_to_another_principal_in_the_same_workspace(rig):
    r = rig
    current = review(r)
    peer = provision_principal(
        r.env.admin_dsn,
        r.env.schema,
        urlsplit(r.env.other_db.dsn).username,
        r.actor.workspace_id,
        principal_id=r.env.other_context.principal_id,
    )
    other = DiscoveryService(
        r.env.other_db, setup_token=lambda: TOKEN, sources=r.client, clock=lambda: r.clock[0]
    )
    for context in (peer, r.env.other_context):
        assert other.sources(context)["items"] == []
        assert other.publishers(context)["items"] == []
        assert other.candidates(context)["items"] == []
        assert other.checks(context)["items"] == []
        with pytest.raises(ScopeDeniedError):
            other.source(context, UUID(r.source["id"]))
        with pytest.raises(ScopeDeniedError):
            other.check(context, UUID(current["id"]))
        with r.env.other_db.transaction(context) as conn:
            for table in (
                "discovery_sources",
                "discovery_publishers",
                "discovery_snapshots",
                "discovery_checks",
                "discovery_events",
            ):
                assert conn.execute(f"SELECT * FROM {table}").fetchall() == []
    independent = other.add_source(
        peer,
        {
            "adapter": "curated",
            "name": "Fixture registry",
            "ttlSeconds": 900,
            "curatedManifestSha256": hashlib.sha256(r.raw).hexdigest(),
        },
        "add-source",
        setup_token=TOKEN,
    )["source"]
    assert independent["id"] != r.source["id"]


def test_command_replays_do_not_refetch_or_dispatch_a_retired_check(rig):
    r = rig
    selected = admit(r)
    first = review(r, candidate=selected)
    replay_args = (r.actor, UUID(first["id"]), first["revision"], first["bindingSha256"])
    assert r.service.measurement_replay(*replay_args, "claim", setup_token=TOKEN) is None
    claimed = claim(r, first)
    ongoing = r.service.measurement_replay(*replay_args, "claim", setup_token=TOKEN)
    assert ongoing["check"]["status"] == "measuring"
    assert r.service.claim_measurement(
        *replay_args, "claim", setup_token=TOKEN, fresh_only=True
    ) == {"replay": ongoing}
    assert r.runtime.supervisor.calls == []
    claimed, report = measure(r, first, claim_value=claimed)
    measured = r.service.complete_measurement(
        r.actor, UUID(first["id"]), UUID(str(claimed["workerToken"])), report
    )["check"]
    assert review(r, candidate=selected) == first
    assert claim(r, first) == claimed
    with pytest.raises(DomainError):
        r.service.validate_measurement(
            r.actor, UUID(first["id"]), UUID(str(claimed["workerToken"]))
        )
    assert refresh(r)["snapshotId"] == selected["snapshotId"]
    assert r.client.calls == ["search", "fetch"]
    assert len(r.runtime.supervisor.calls) == 1
    assert r.service.check(r.actor, UUID(first["id"]))["check"] == measured
    assert (
        r.service.measurement_replay(*replay_args, "claim", setup_token=TOKEN)["check"] == measured
    )
    assert (
        r.service.claim_measurement(*replay_args, "claim", setup_token=TOKEN, fresh_only=True)[
            "replay"
        ]["check"]
        == measured
    )
    with pytest.raises(RevisionConflictError):
        r.service.measurement_replay(
            r.actor,
            UUID(first["id"]),
            first["revision"] + 1,
            first["bindingSha256"],
            "claim",
            setup_token=TOKEN,
        )
    assert r.service.measurement_replay(*replay_args, "new-claim", setup_token=TOKEN) is None
    with pytest.raises(RevisionConflictError):
        r.service.claim_measurement(*replay_args, "new-claim", setup_token=TOKEN, fresh_only=True)
    assert len(r.runtime.supervisor.calls) == 1
    with pytest.raises(RevisionConflictError):
        review(r, candidate=selected, agent=uuid4())


@pytest.mark.parametrize(
    "subject,decision",
    [("source", "disable"), ("source", "revoke"), ("publisher", "block"), ("publisher", "revoke")],
)
def test_current_source_and_publisher_controls_fence_retained_measurements(rig, subject, decision):
    r = rig
    first = review(r)
    claimed = claim(r, first)
    original = r.source if subject == "source" else r.publisher
    control(r, subject, original, decision)
    with pytest.raises(DomainError):
        r.service.validate_measurement(
            r.actor, UUID(first["id"]), UUID(str(claimed["workerToken"]))
        )
    current = r.service.check(r.actor, UUID(first["id"]))["check"]
    assert not current["measurementCurrent"] and "handoff" not in current["actions"]
    assert len(r.runtime.supervisor.calls) == 0


def test_source_refresh_and_report_expiry_require_new_exact_review(rig):
    r = rig
    first = complete(r, review(r))
    r.clock[0] += timedelta(seconds=121)
    current = r.service.check(r.actor, UUID(first["id"]))["check"]
    assert not current["measurementCurrent"] and "measurement_expired" in current["reasonCodes"]
    with pytest.raises(DomainError):
        action(r, current, "handoff")
    refresh(r, key="new-snapshot")
    selected = r.service.candidates(r.actor)["items"][0]
    assert selected["snapshotId"] != first["snapshotId"]
    with pytest.raises(ScopeDeniedError):
        r.service.candidate(r.actor, UUID(first["candidateId"]))
    with pytest.raises(DomainError):
        action(r, r.service.check(r.actor, UUID(first["id"]))["check"], "handoff")


def test_native_setup_key_rotation_invalidates_old_authority_without_deleting_evidence(rig):
    r = rig
    current = complete(r, review(r))
    r.token[0] = "rotated-" + "r" * 64
    source = r.service.source(r.actor, UUID(r.source["id"]))["source"]
    assert not source["approvalValid"]
    retained = r.service.check(r.actor, UUID(current["id"]))["check"]
    assert retained["measurement"] == current["measurement"] and not retained["measurementCurrent"]
    with pytest.raises(ScopeDeniedError):
        action(r, retained, "handoff")
    with pytest.raises(ScopeDeniedError):
        control(r, "source", source, "enable")
    r.source = add_source(r, key="rotated-source-admission")
    refresh(r, key="rotated-refresh")
    publisher = r.service.publishers(r.actor, source_id=UUID(r.source["id"]))["items"][0]
    assert publisher["state"] == "unreviewed"
    r.publisher = control(r, "publisher", publisher, "approve")
    selected = r.service.candidates(r.actor)["items"][0]
    assert selected["canReview"]
    fresh = review(r, candidate=selected, key="rotated-review")
    assert fresh["id"] != current["id"]


@pytest.mark.parametrize("target", ["source", "publisher", "check"])
def test_raw_database_row_changes_cannot_become_signed_admission(rig, target):
    r = rig
    current = review(r)
    with r.env.db.transaction(r.actor) as conn:
        if target == "source":
            conn.execute(
                "UPDATE discovery_sources SET config=jsonb_set(config,'{name}','\"forged source\"') WHERE id=%s",
                (UUID(r.source["id"]),),
            )
        elif target == "publisher":
            conn.execute(
                "UPDATE discovery_publishers SET evidence='registry_claim' WHERE id=%s",
                (UUID(r.publisher["id"]),),
            )
        else:
            conn.execute(
                "UPDATE discovery_checks SET payload=jsonb_set(payload,'{sourceBindingSha256}',%s) WHERE id=%s",
                (Jsonb("0" * 64), UUID(current["id"])),
            )
    if target == "check":
        with pytest.raises(ScopeDeniedError):
            r.service.check(r.actor, UUID(current["id"]))
    with pytest.raises(DomainError):
        claim(r, current)
    assert r.runtime.supervisor.calls == []


@pytest.mark.parametrize(
    "subject,nonterminal", [("source", ("disable", "enable")), ("publisher", ("block", "approve"))]
)
def test_unsigned_event_and_row_cannot_be_laundered_through_nonterminal_controls(
    rig, subject, nonterminal
):
    r = rig
    review(r)
    original = r.source if subject == "source" else r.publisher
    table = "discovery_sources" if subject == "source" else "discovery_publishers"
    with r.env.db.transaction(r.actor) as conn:
        if subject == "source":
            row = conn.execute(
                "UPDATE discovery_sources SET revision=revision+1,"
                "config=jsonb_set(config,'{curatedManifestSha256}',%s) WHERE id=%s RETURNING *",
                (Jsonb("f" * 64), UUID(original["id"])),
            ).fetchone()
        else:
            row = conn.execute(
                "UPDATE discovery_publishers SET revision=revision+1,name='forged-publisher',"
                "evidence='registry_claim' WHERE id=%s RETURNING *",
                (UUID(original["id"]),),
            ).fetchone()
        # An application writer can append an observation and compute its public
        # digest. Neither capability supplies the independent setup signature.
        conn.execute(
            "INSERT INTO discovery_events(workspace_id,id,principal_id,subject_kind,"
            "subject_id,revision,action,binding_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                r.actor.workspace_id,
                uuid4(),
                r.actor.principal_id,
                subject,
                row["id"],
                row["revision"],
                "forged_observation",
                content_hash(_body(subject, row)),
            ),
        )
    if subject == "source":
        forged = r.service.source(r.actor, UUID(original["id"]))["source"]
    else:
        forged = r.service.publishers(r.actor, source_id=UUID(r.source["id"]))["items"][0]
    assert not forged["approvalValid"] and forged["actions"] == ["revoke"]
    for decision in nonterminal:
        with pytest.raises(ScopeDeniedError):
            control(r, subject, forged, decision)
    with r.env.db.transaction(r.actor) as conn:
        retained = conn.execute(f"SELECT * FROM {table} WHERE id=%s", (row["id"],)).fetchone()
    assert retained["revision"] == row["revision"] and retained["mac"] == row["mac"]
    contained = control(r, subject, forged, "revoke")
    assert contained["state"] == "revoked" and contained["actions"] == []
    for decision in nonterminal:
        with pytest.raises(ScopeDeniedError):
            control(r, subject, contained, decision)
    assert r.runtime.supervisor.calls == []


def test_revoked_publisher_is_terminal_and_new_source_requires_a_new_review(rig):
    r = rig
    admit(r)
    revoked = control(r, "publisher", r.publisher, "revoke")
    assert revoked["actions"] == []
    for decision in ("approve", "block", "revoke"):
        with pytest.raises(ScopeDeniedError):
            control(r, "publisher", revoked, decision)
    refresh(r, key="refresh-after-revoke")
    retained = r.service.publishers(r.actor, source_id=UUID(r.source["id"]))["items"][0]
    assert retained["id"] == revoked["id"] and retained["state"] == "revoked"
    assert not r.service.candidates(r.actor, source_id=UUID(r.source["id"]))["items"][0][
        "canReview"
    ]
    r.source = add_source(r, key="fresh-source-after-revoke")
    refresh(r, key="fresh-source-refresh")
    fresh = r.service.publishers(r.actor, source_id=UUID(r.source["id"]))["items"][0]
    assert fresh["id"] != revoked["id"] and fresh["state"] == "unreviewed"
    control(r, "publisher", fresh, "approve")
    assert r.service.candidates(r.actor, source_id=UUID(r.source["id"]))["items"][0]["canReview"]


def test_source_capacity_counts_disabled_but_allows_revoked_history_and_replacement(rig):
    r = rig
    prepared = review(r)
    original = r.source
    others = [add_source(r, key=f"source-capacity-{index}") for index in range(15)]
    control(r, "source", others[0], "disable")
    with pytest.raises(InvalidInputError, match="allowance"):
        add_source(r, key="source-capacity-exceeded")
    revoked = control(r, "source", original, "revoke")
    r.source = add_source(r, key="source-capacity-replacement")
    refresh(r, key="source-capacity-replacement-refresh")
    publisher = r.service.publishers(r.actor, source_id=UUID(r.source["id"]))["items"][0]
    control(r, "publisher", publisher, "approve")
    ranked = r.service.candidates(r.actor, query="fixture")["items"]
    assert len(ranked) == 1 and ranked[0]["sourceId"] == r.source["id"]
    assert ranked[0]["canReview"]
    assert r.service.candidates(r.actor, source_id=UUID(revoked["id"]))["items"] == []
    with pytest.raises(ScopeDeniedError):
        r.service.candidate(r.actor, UUID(prepared["candidateId"]))
    retained = r.service.check(r.actor, UUID(prepared["id"]))["check"]
    assert retained["status"] == "awaiting_measurement"
    assert retained["candidateSha256"] == prepared["candidateSha256"]
    assert "discovery_binding_stale" in retained["reasonCodes"]
    assert r.service.source(r.actor, UUID(revoked["id"]))["source"]["state"] == "revoked"
    with r.env.db.transaction(r.actor) as conn:
        count = conn.execute(
            "SELECT count(*) AS total, count(*) FILTER(WHERE state<>'revoked') AS current, "
            "count(*) FILTER(WHERE state='disabled') AS disabled FROM discovery_sources"
        ).fetchone()
    assert dict(count) == {"total": 17, "current": 16, "disabled": 1}
    assert r.runtime.supervisor.calls == []


@pytest.mark.parametrize("adapter", ["clawhub", "skills_sh"])
def test_real_remote_search_resolves_and_retains_exact_package_at_review(rig, adapter):
    r = rig
    calls = []
    claw = registry_fixtures.ClawTransport()
    token = "dedicated-registry-fixture-token"

    def transport(url, *, headers, max_bytes):
        calls.append(url)
        assert max_bytes > 0
        parsed = urlsplit(url)
        if adapter == "clawhub":
            assert parsed.hostname == "clawhub.ai" and "Authorization" not in headers
            if parsed.path == "/api/v1/search":
                return registry_fixtures.response(
                    {
                        "results": [
                            {
                                "ownerHandle": "alice",
                                "slug": "demo",
                                "displayName": "Demo",
                                "summary": "Inspect evidence.",
                                "version": "1.2.3",
                            }
                        ]
                    }
                )
            return claw(url, headers=headers, max_bytes=max_bytes)
        assert parsed.hostname == "skills.sh" and headers["Authorization"] == "Bearer " + token
        if parsed.path == "/api/v1/skills/search":
            return registry_fixtures.response(
                {
                    "data": [
                        {
                            "id": "example/skills/demo",
                            "source": "example/skills",
                            "slug": "demo",
                            "name": "Demo",
                            "description": "Inspect evidence.",
                        }
                    ]
                }
            )
        assert parsed.path == "/api/v1/skills/example/skills/demo"
        return registry_fixtures.response(
            {
                "id": "example/skills/demo",
                "source": "example/skills",
                "slug": "demo",
                "hash": "f" * 64,
                "files": [
                    {"path": path, "contents": data.decode()}
                    for path, data in registry_fixtures.FILES.items()
                ],
            }
        )

    client = RegistrySources(
        transport=transport, skills_sh_token=lambda: token, now=lambda: r.clock[0]
    )
    r.service.source_client = client
    source = r.service.add_source(
        r.actor,
        {
            "adapter": adapter,
            "name": "Recorded remote registry",
            "ttlSeconds": 900,
            "curatedManifestSha256": None,
        },
        "remote-source",
        setup_token=TOKEN,
    )["source"]
    source = r.service.refresh(
        r.actor, UUID(source["id"]), "evidence", "remote-refresh", setup_token=TOKEN
    )["source"]
    publisher = r.service.publishers(r.actor, source_id=UUID(source["id"]))["items"][0]
    control(r, "publisher", publisher, "approve")
    selected = r.service.candidates(r.actor, source_id=UUID(source["id"]))["items"][0]
    assert selected["canReview"]
    assert selected["observation"]["distribution"] == {"kind": "inspect_only"}
    assert selected["observation"]["version"] == ("1.2.3" if adapter == "clawhub" else None)
    prepared = review(r, candidate=selected)
    exact = prepared["candidate"]["observation"]
    assert exact["distribution"]["kind"] == (
        "clawhub_skill" if adapter == "clawhub" else "skills_sh_snapshot"
    )
    assert exact["distribution"]["files"] == registry_fixtures.inventory()
    assert prepared["requestedCandidateSha256"] == selected["candidateSha256"]
    assert prepared["candidateSha256"] != prepared["requestedCandidateSha256"]
    assert prepared["measurement"] is None and prepared["status"] == "awaiting_measurement"
    packages = SkillAcquisition(r.env.db, setup_token_resolver=lambda: TOKEN)
    retained = packages.package(r.actor, UUID(prepared["packageId"]))["package"]
    assert retained["source"]["registry_candidate_sha256"] == prepared["candidateSha256"]
    assert retained["source"]["registry_adapter"] == adapter
    assert [
        {key: entry[key] for key in ("path", "size", "sha256")}
        for entry in retained["manifest"]["files"]
    ] == registry_fixtures.inventory()
    requests = list(calls)
    assert review(r, candidate=selected) == prepared and calls == requests
    assert token not in canonical_json(prepared) and TOKEN not in canonical_json(prepared)
    assert r.runtime.supervisor.calls == []
    with r.env.db.transaction(r.actor) as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM workspace_context_skill_packages").fetchone()[
                "n"
            ]
            == 1
        )
        assert (
            conn.execute("SELECT count(*) AS n FROM workspace_context_skills").fetchone()["n"] == 0
        )
        assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 0


def two_curated_candidates(r):
    first = candidate_entry()
    second = {**candidate_entry(), "canonicalId": "fixture/other-fixture"}
    second["distribution"] = {**second["distribution"], "path": "skills/other-fixture"}
    r.raw = canonical_json(
        {"schema": "potbot-curated-registry/v1", "entries": [first, second]}
    ).encode()
    r.client.curated = parse_curated_manifest(r.raw)
    r.source = add_source(r, key="two-candidate-source")
    refresh(r, key="two-candidate-refresh")
    publisher = r.service.publishers(r.actor, source_id=UUID(r.source["id"]))["items"][0]
    r.publisher = control(r, "publisher", publisher, "approve")


@pytest.mark.parametrize("change", ["publisher", "expiry"])
def test_duplicate_source_ranking_keeps_admitted_copy_and_fences_changed_page(rig, change):
    r = rig
    two_curated_candidates(r)
    first_publisher = r.publisher
    r.source = add_source(r, key="identical-second-source")
    refresh(r, key="identical-second-refresh")
    second_publisher = r.service.publishers(r.actor, source_id=UUID(r.source["id"]))["items"][0]
    second_publisher = control(r, "publisher", second_publisher, "approve")
    control(r, "publisher", first_publisher, "block")
    all_ranked = r.service.candidates(r.actor, query="fixture")
    assert len(all_ranked["items"]) == 2
    assert {item["sourceId"] for item in all_ranked["items"]} == {r.source["id"]}
    first_page = r.service.candidates(r.actor, query="fixture", limit=1)
    assert len(first_page["items"]) == 1 and first_page["nextCursor"]
    following = r.service.candidates(
        r.actor, query="fixture", limit=1, after=first_page["nextCursor"]
    )
    assert (
        len(following["items"]) == 1 and following["items"][0]["id"] != first_page["items"][0]["id"]
    )
    if change == "publisher":
        control(r, "publisher", second_publisher, "block")
    else:
        r.clock[0] += timedelta(seconds=901)
    with pytest.raises(RevisionConflictError):
        r.service.candidates(r.actor, query="fixture", limit=1, after=first_page["nextCursor"])
    assert r.service.candidates(r.actor, query="fixture")["items"] == []


def test_signed_active_skill_receives_preference_until_disabled_and_cursor_is_fenced(rig):
    r = rig
    two_curated_candidates(r)
    r.client.package = package_from_files(
        r.client.package.files,
        source={**r.client.package.source, "verification": "content-integrity-only"},
    )
    candidates = r.service.candidates(r.actor, query="fixture")["items"]
    selected = next(
        item
        for item in candidates
        if item["observation"]["canonicalId"] == "fixture/portable-fixture"
    )
    current = review(r, candidate=selected)
    packages = SkillAcquisition(r.env.db, setup_token_resolver=lambda: TOKEN)
    package_id = UUID(current["packageId"])
    plan = packages.plan(r.actor, package_id)["plan"]
    installed = packages.install(
        r.actor,
        package_id,
        plan["sha256"],
        plan["expectedRevision"],
        "rank-install",
        setup_token=TOKEN,
    )["skill"]
    preference = "current_owner_installed_route_preferred"
    assert all(
        preference not in item["scoreReasons"]
        for item in r.service.candidates(r.actor, query="fixture")["items"]
    )
    active = packages.control(
        r.actor,
        UUID(installed["id"]),
        "activate",
        installed["revision"],
        "rank-activate",
        setup_token=TOKEN,
    )["skill"]
    page = r.service.candidates(r.actor, query="fixture", limit=1)
    assert page["items"][0]["candidateSha256"] == selected["candidateSha256"]
    assert preference in page["items"][0]["scoreReasons"] and page["nextCursor"]
    packages.control(
        r.actor,
        UUID(active["id"]),
        "disable",
        active["revision"],
        "rank-disable",
        setup_token=TOKEN,
    )
    with pytest.raises(RevisionConflictError):
        r.service.candidates(r.actor, query="fixture", limit=1, after=page["nextCursor"])
    assert all(
        preference not in item["scoreReasons"]
        for item in r.service.candidates(r.actor, query="fixture")["items"]
    )
    assert r.runtime.supervisor.calls == []


@pytest.mark.parametrize(
    "change",
    [
        "candidateSha256",
        "sourceBindingSha256",
        "requirementsSha256",
        "agentId",
        "checkId",
        "principalId",
        "future",
    ],
)
def test_measurement_cannot_substitute_another_source_agent_or_scope(rig, change):
    r = rig
    current = review(r)
    claimed, report = measure(r, current)
    replacement = deepcopy(report)
    if change in {"candidateSha256", "sourceBindingSha256", "requirementsSha256"}:
        replacement[change] = "0" * 64
    elif change in {"agentId", "checkId"}:
        replacement[change] = str(uuid4())
    elif change == "principalId":
        replacement["bindings"][change] = str(uuid4())
    else:
        replacement["checkedAt"] = (r.clock[0] + timedelta(seconds=5)).isoformat()
        replacement["expiresAt"] = (r.clock[0] + timedelta(seconds=125)).isoformat()
    with pytest.raises(DomainError):
        r.service.complete_measurement(
            r.actor, UUID(current["id"]), UUID(str(claimed["workerToken"])), replacement
        )
    assert r.service.check(r.actor, UUID(current["id"]))["check"]["status"] == "measuring"
    accepted = r.service.complete_measurement(
        r.actor, UUID(current["id"]), UUID(str(claimed["workerToken"])), report
    )["check"]
    assert accepted["measurement"] == report


def test_cancel_fences_exact_worker_token_and_preserves_unknown_process_handling(rig):
    r = rig
    first = review(r)
    claimed, report = measure(r, first)
    current = r.service.check(r.actor, UUID(first["id"]))["check"]
    cancelled = action(r, current, "cancel", key="cancel")
    assert cancelled["status"] == "cancelled"
    assert action(r, current, "cancel", key="cancel") == cancelled
    with pytest.raises(DomainError):
        r.service.validate_measurement(
            r.actor, UUID(first["id"]), UUID(str(claimed["workerToken"]))
        )
    with pytest.raises(DomainError):
        r.service.complete_measurement(
            r.actor, UUID(first["id"]), UUID(str(claimed["workerToken"])), report
        )
    with pytest.raises(DomainError):
        r.service.fail_measurement(
            r.actor, UUID(first["id"]), UUID(str(claimed["workerToken"])), "measurement_failed"
        )
    assert r.service.check(r.actor, UUID(first["id"]))["check"]["status"] == "cancelled"


def test_failed_refresh_retains_old_bytes_but_never_preserves_current_admission(rig):
    r = rig
    current = review(r)
    r.client.failure = "rate_limited"
    failed = refresh(r, key="failed-refresh")
    assert (
        failed["snapshotId"] == current["snapshotId"] and failed["lastErrorCode"] == "rate_limited"
    )
    observed = r.service.candidates(r.actor)["items"][0]
    assert observed["stale"] and not observed["canReview"]
    with pytest.raises(DomainError):
        claim(r, current)


def test_changed_curated_bytes_cannot_borrow_the_previous_manifest_admission(rig):
    r = rig
    refresh(r)
    original = r.source["snapshotId"]
    changed = canonical_json(
        {"schema": "potbot-curated-registry/v1", "entries": [candidate_entry(changed=True)]}
    ).encode()
    r.client.curated = parse_curated_manifest(changed)
    try:
        failed = refresh(r, key="changed-manifest")
    except DomainError:
        failed = r.service.source(r.actor, UUID(r.source["id"]))["source"]
    assert failed["snapshotId"] == original
    assert (
        r.service.candidates(r.actor)["items"][0]["observation"]["description"]
        == "Explicit local discovery fixture"
    )


def test_exact_tool_handoff_reuses_signed_recipe_and_existing_u04_x_operation(rig):
    r = rig
    tools = ToolAcquisitionService(r.env.db, setup_token_resolver=lambda: r.token[0])
    recipe = ToolRecipe.from_dict(recipe_document())
    published = tools.publish_recipe(r.actor, recipe, r.agent, "tool-recipe", setup_token=TOKEN)[
        "recipe"
    ]
    r.raw = canonical_json(
        {
            "schema": "potbot-curated-registry/v1",
            "entries": [candidate_entry(recipe_id=published["id"])],
        }
    ).encode()
    r.client.curated = parse_curated_manifest(r.raw)
    r.source = add_source(r, key="tool-source")
    refresh(r)
    publishers = r.service.publishers(r.actor, source_id=UUID(r.source["id"]))["items"]
    r.publisher = control(r, "publisher", publishers[0], "approve")
    selected = r.service.candidates(r.actor, source_id=UUID(r.source["id"]))["items"][0]
    with pytest.raises(ScopeDeniedError):
        review(r, candidate=selected, agent=uuid4(), key="wrong-agent")
    current = review(r, candidate=selected)
    measured = complete(r, current)
    handed = action(r, measured, "handoff", key="tool-handoff")
    assert handed["status"] == "handed_off" and str(published["id"]) == current["recipeId"]
    operations = tools.list(r.actor)["items"]
    assert len(operations) == 1
    assert operations[0]["phase"] == "source_queued" and operations[0]["newCommit"] == "b" * 40
    assert operations[0]["agentId"] == str(r.agent)
    assert operations[0]["id"] in json.dumps(handed["target"])
    assert action(r, measured, "handoff", key="tool-handoff") == handed
    assert len(tools.list(r.actor)["items"]) == 1
    assert "fetch" not in r.client.calls


def test_retained_package_provenance_cannot_change_behind_a_measured_review(rig):
    r = rig
    measured = complete(r, review(r))
    package_id = UUID(measured["packageId"])
    changed_source = {**r.client.package.source, "commit": "c" * 40}
    # Retained package tables are append-only for application roles. Explicit
    # disposable-database corruption exercises the signed review's independent
    # source binding even when original bytes and release digest are unchanged.
    with admin_connection(r.env.admin_dsn, r.env.schema) as conn:
        conn.execute(
            "ALTER TABLE workspace_context_skill_packages DISABLE TRIGGER immutable_record"
        )
        conn.execute(
            "UPDATE workspace_context_skill_packages SET source=%s,source_sha256=%s WHERE id=%s",
            (Jsonb(changed_source), content_hash(changed_source), package_id),
        )
        conn.execute("ALTER TABLE workspace_context_skill_packages ENABLE TRIGGER immutable_record")
    with pytest.raises(DomainError):
        action(r, measured, "handoff")
    assert r.service.check(r.actor, UUID(measured["id"]))["check"]["status"] == "measured"


def test_completion_cannot_launder_old_review_authority_into_a_rotated_setup_key(rig):
    r = rig
    current = review(r)
    claimed, report = measure(r, current)
    reads = []

    def rotating_key():
        reads.append(True)
        return TOKEN if len(reads) == 1 else "rotated-during-commit-" + "r" * 64

    r.service.setup_token = rotating_key
    with pytest.raises(ScopeDeniedError):
        r.service.complete_measurement(
            r.actor, UUID(current["id"]), UUID(str(claimed["workerToken"])), report
        )
    retained = r.service.check(r.actor, UUID(current["id"]))["check"]
    assert retained["status"] == "measuring" and retained["measurement"] is None
    assert not retained["measurementCurrent"]


def test_blocked_prerequisites_can_handoff_data_for_review_without_installing(rig):
    r = rig
    current = review(r)
    r.runtime.supervisor.available.clear()
    measured = complete(r, current)
    assert measured["measurementCurrent"] and not measured["measurement"]["eligible"]
    handed = action(r, measured, "handoff")
    assert handed["target"] == {"kind": "skill_package", "id": current["packageId"]}
    with r.env.db.transaction(r.actor) as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM workspace_context_skills").fetchone()["n"] == 0
        )
        assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 0


def test_actual_http_discovery_lifecycle_exports_only_public_contracts(rig):
    r = rig
    refresh(r)
    app = FastAPI()
    origin = "http://127.0.0.1:3100"
    reads = {"Authorization": "Bearer discovery-fixture"}
    writes = {**reads, "Origin": origin, "X-Potbot-Setup-Token": TOKEN}

    def authenticate(value):
        if value != reads["Authorization"]:
            raise ScopeDeniedError("Unknown fixture identity")
        return r.actor

    def authorize(value):
        if value != r.token[0]:
            raise ScopeDeniedError("Missing fixture setup authority")

    @app.exception_handler(DomainError)
    async def failed(request, error):
        response = error_response(
            error,
            semantics=OperationSemantics(
                read_only=request.method == "GET",
                idempotency_key=getattr(request.state, "idempotency_key", None) is not None,
            ),
        )
        return JSONResponse(response.body, status_code=response.status)

    register_discovery_routes(
        app, r.env.db, authenticate, (origin,), r.actor.workspace_id, authorize, service=r.service
    )
    fixture = {}
    with TestClient(app) as client:

        def get(suffix):
            response = client.get("/v1/ui/discovery" + suffix, headers=reads)
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "private, no-store"
            return response.json()

        def post(suffix, body, key):
            response = client.post(
                "/v1/ui/discovery" + suffix, headers={**writes, "Idempotency-Key": key}, json=body
            )
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "private, no-store"
            return response.json()

        def change(collection, current, decision, key):
            return post(
                f"/{collection}/{current['id']}/{decision}",
                {
                    "revision": current["revision"],
                    "digest": current["bindingSha256"],
                    "confirm": True,
                },
                key,
            )

        fixture["sources"] = get("/sources")
        fixture["source"] = get("/sources/" + r.source["id"])
        fixture["publishers"] = get("/publishers")
        fixture["unreviewedCandidates"] = get("/candidates")
        fixture["emptyChecks"] = get("/checks")
        publisher = fixture["publishers"]["items"][0]
        fixture["approvedPublisher"] = change("publishers", publisher, "approve", "http-approve")
        fixture["candidates"] = get("/candidates")
        candidate = fixture["candidates"]["items"][0]
        fixture["candidate"] = get("/candidates/" + candidate["id"])

        def prepare(key):
            return post(
                f"/candidates/{candidate['id']}/checks",
                {
                    "snapshotId": candidate["snapshotId"],
                    "candidateSha256": candidate["candidateSha256"],
                    "agentId": str(r.agent),
                    "confirm": True,
                },
                key,
            )

        fixture["pendingCheck"] = prepare("http-review")
        assert prepare("http-review") == fixture["pendingCheck"]
        pending = fixture["pendingCheck"]["check"]
        native = claim(r, pending, key="http-native-claim")
        fixture["measuringCheck"] = get("/checks/" + pending["id"])
        _, report = measure(r, pending, claim_value=native)
        r.service.complete_measurement(
            r.actor, UUID(pending["id"]), UUID(str(native["workerToken"])), report
        )
        fixture["measuredCheck"] = get("/checks/" + pending["id"])
        fixture["handedOffCheck"] = change(
            "checks", fixture["measuredCheck"]["check"], "handoff", "http-handoff"
        )
        assert (
            change("checks", fixture["measuredCheck"]["check"], "handoff", "http-handoff")
            == fixture["handedOffCheck"]
        )
        cancelled = prepare("http-cancel-review")["check"]
        fixture["cancelledCheck"] = change("checks", cancelled, "cancel", "http-cancel")
        expires = prepare("http-expiring-review")["check"]
        expiring_claim, expiring_report = measure(
            r, expires, claim_value=claim(r, expires, key="http-expiring-claim")
        )
        r.service.complete_measurement(
            r.actor, UUID(expires["id"]), UUID(str(expiring_claim["workerToken"])), expiring_report
        )
        failed_check = prepare("http-failed-review")["check"]
        failed_claim = claim(r, failed_check, key="http-failed-claim")
        r.service.fail_measurement(
            r.actor,
            UUID(failed_check["id"]),
            UUID(str(failed_claim["workerToken"])),
            "measurement_failed",
        )
        fixture["failedCheck"] = get("/checks/" + failed_check["id"])
        r.clock[0] += timedelta(seconds=121)
        fixture["expiredCheck"] = get("/checks/" + expires["id"])
        assert not fixture["expiredCheck"]["check"]["measurementCurrent"]
        fixture["checks"] = get("/checks")
        fixture["disabledSource"] = change("sources", r.source, "disable", "http-disable-source")
        fixture["enabledSource"] = change(
            "sources", fixture["disabledSource"]["source"], "enable", "http-enable-source"
        )
        fixture["blockedPublisher"] = change(
            "publishers", fixture["approvedPublisher"]["publisher"], "block", "http-block-publisher"
        )

    encoded = json.dumps(fixture, indent=2, sort_keys=True)
    assert all(
        secret not in encoded
        for secret in (TOKEN, "workerToken", '"mac"', str(r.runtime.rootfs), str(r.runtime.home))
    )
    assert len(r.runtime.supervisor.calls) == 2
    if destination := os.environ.get("POTBOT_DISCOVERY_CONTRACT_OUTPUT"):
        output = Path(destination)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded + "\n", encoding="utf-8")


def test_populated_v41_migration_preserves_existing_recipes_and_checksums(app_env, tmp_path):
    schema, migrations = "discovery_upgrade", application_migrations()
    assert migrate(app_env.admin_dsn, schema, migrations[:41]) == tuple(range(1, 42))
    workspace = provision_workspace(
        app_env.admin_dsn, schema, "discovery upgrade", sandbox_root=tmp_path / "legacy"
    )
    role = urlsplit(app_env.db.dsn).username
    actor = provision_principal(app_env.admin_dsn, schema, role, workspace)
    database = Database(app_env.db.dsn, schema)
    tools = ToolAcquisitionService(database, setup_token_resolver=lambda: TOKEN)
    recipe = tools.publish_recipe(
        actor,
        ToolRecipe.from_dict(recipe_document()),
        uuid4(),
        "preserved-recipe",
        setup_token=TOKEN,
    )["recipe"]
    with database.transaction(actor) as conn:
        before = conn.execute("SELECT * FROM ui_tool_acquisition_recipes").fetchall()
    with admin_connection(app_env.admin_dsn, schema) as conn:
        checksums = conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
    assert migrate(app_env.admin_dsn, schema, migrations[:42]) == tuple(range(1, 43))
    grant_application_role(app_env.admin_dsn, schema, role)
    assert migrate(app_env.admin_dsn, schema, migrations[:42]) == tuple(range(1, 43))
    with database.transaction(actor) as conn:
        assert conn.execute("SELECT * FROM ui_tool_acquisition_recipes").fetchall() == before
        for table in (
            "discovery_sources",
            "discovery_publishers",
            "discovery_snapshots",
            "discovery_checks",
            "discovery_events",
        ):
            assert conn.execute(f"SELECT * FROM {table}").fetchall() == []
    with admin_connection(app_env.admin_dsn, schema) as conn:
        assert (
            conn.execute(
                "SELECT * FROM schema_migrations WHERE version<=41 ORDER BY version"
            ).fetchall()
            == checksums
        )
    assert tools.recipes(actor)["items"][0]["id"] == recipe["id"]

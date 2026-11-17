"""Complete HTTP acquisition lifecycle on PostgreSQL, exported to the UI contract gate."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import zipfile
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from potbot.api.ui_skills import register_skill_acquisition_routes
from potbot.api.wire import OperationSemantics, error_response
from potbot.context.skill_acquisition import SkillAcquisition
from potbot.domain.errors import DomainError, ScopeDeniedError

pytestmark = pytest.mark.postgres
TOKEN = "skill-acquisition-http-operator-canary-" + "x" * 32
ORIGIN = "http://127.0.0.1:3100"
SKILL = b"""---
name: review-documents
description: >-
  Review document packages.
license: MIT
allowed-tools: Read
metadata:
  version: "1.0"
  openclaw:
    requires:
      bins: [python]
      env: [OPTIONAL_PROVIDER_TOKEN]
    install:
      - kind: uv
        package: declared-only
---
Read references/guide.md.
"""


def zip_body(resource: bytes) -> dict[str, str]:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in {
            "SKILL.md": SKILL,
            "references/guide.md": resource,
            "assets/empty.bin": b"",
            "AGENTS.md": b"Package data, not live Context instructions.\n",
        }.items():
            entry = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, data)
    return {"archiveBase64": base64.b64encode(output.getvalue()).decode(), "subdirectory": ""}


def application(env):
    actor = env.context
    service = SkillAcquisition(env.db, setup_token_resolver=lambda: TOKEN)
    app = FastAPI()

    def authenticate(value):
        if value != "Bearer owned":
            raise ScopeDeniedError("Unknown fixture identity")
        return actor

    def setup(value):
        if value != TOKEN:
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

    register_skill_acquisition_routes(
        app, env.db, authenticate, (ORIGIN,), actor.workspace_id, setup, service=service
    )
    return app


def test_http_import_review_install_resource_update_rollback_and_revoke(app_env):
    path = "/v1/ui/skills"
    read = {"Authorization": "Bearer owned"}
    writes = {**read, "Origin": ORIGIN, "X-Potbot-Setup-Token": TOKEN}
    first_bytes = b"Reference content.\n" * 4000
    fixture = {}
    with TestClient(application(app_env)) as client:

        def post(suffix, body, key):
            result = client.post(
                path + suffix, json=body, headers={**writes, "Idempotency-Key": key}
            )
            assert result.status_code == 200, result.text
            assert result.headers["cache-control"] == "private, no-store"
            assert TOKEN not in result.text
            return result.json()

        def get(suffix):
            result = client.get(path + suffix, headers=read)
            assert result.status_code == 200, result.text
            return result.json()

        imported = post("/imports/zip", zip_body(first_bytes), "http-import-v1")
        assert imported == post("/imports/zip", zip_body(first_bytes), "http-import-v1")
        package = imported["package"]
        package_id = package["id"]
        detail = get(f"/packages/{package_id}")
        assert detail["content"] == SKILL.decode() and detail["contentTruncated"] is False
        plan = get(f"/packages/{package_id}/plan")["plan"]
        assert plan["willDisable"] is True and plan["executable"] is False
        assert set(plan["added"]) == {
            "SKILL.md",
            "references/guide.md",
            "assets/empty.bin",
            "AGENTS.md",
        }
        install_request = {
            "planSha256": plan["sha256"],
            "expectedRevision": 0,
            "skillId": None,
            "confirm": True,
        }
        installed = post(f"/packages/{package_id}/install", install_request, "http-install-v1")
        assert installed == post(
            f"/packages/{package_id}/install", install_request, "http-install-v1"
        )
        skill = installed["skill"]
        skill_id = skill["id"]
        assert skill["enabled"] is False and skill["approvalValid"] is True
        active = post(
            f"/{skill_id}/activate",
            {"expectedRevision": skill["revision"], "releaseSha256": None, "confirm": True},
            "http-activate-v1",
        )
        skill = active["skill"]
        assert skill["enabled"] is True
        resource_path = (
            f"/{skill_id}/resources?releaseSha256={skill['releaseSha256']}&path=references/guide.md"
        )
        resource = get(resource_path)
        assert resource["resource"]["sha256"] == hashlib.sha256(first_bytes).hexdigest()
        assert resource["nextOffset"] == 65536
        tail = get(resource_path + "&offset=65536")
        assert (
            base64.b64decode(resource["dataBase64"]) + base64.b64decode(tail["dataBase64"])
            == first_bytes
        )
        empty = get(
            f"/{skill_id}/resources?releaseSha256={skill['releaseSha256']}&path=assets/empty.bin"
        )
        assert empty["dataBase64"] == "" and empty["nextOffset"] is None
        second_package = post("/imports/zip", zip_body(b"Updated guide\n"), "http-import-v2")[
            "package"
        ]
        assert second_package["contentSha256"] == package["contentSha256"]
        assert second_package["releaseSha256"] != package["releaseSha256"]
        update_plan = get(f"/packages/{second_package['id']}/plan?skillId={skill_id}")["plan"]
        assert update_plan["changed"] == ["references/guide.md"]
        assert update_plan["added"] == update_plan["removed"] == []
        updated = post(
            f"/packages/{second_package['id']}/install",
            {
                "planSha256": update_plan["sha256"],
                "expectedRevision": skill["revision"],
                "skillId": skill_id,
                "confirm": True,
            },
            "http-install-v2",
        )
        assert updated["skill"]["enabled"] is False
        stale = client.post(
            path + f"/{skill_id}/activate",
            json={"expectedRevision": skill["revision"], "releaseSha256": None, "confirm": True},
            headers={**writes, "Idempotency-Key": "http-stale"},
        )
        assert stale.status_code == 409
        rolled = post(
            f"/{skill_id}/rollback",
            {
                "expectedRevision": updated["skill"]["revision"],
                "releaseSha256": package["releaseSha256"],
                "confirm": True,
            },
            "http-rollback",
        )
        assert rolled["skill"]["releaseSha256"] == package["releaseSha256"]
        assert rolled["skill"]["enabled"] is False
        reactivated = post(
            f"/{skill_id}/activate",
            {
                "expectedRevision": rolled["skill"]["revision"],
                "releaseSha256": None,
                "confirm": True,
            },
            "http-reactivate",
        )
        history = get(f"/{skill_id}/history?limit=2")
        assert len(history["events"]) == 2 and history["nextCursor"]
        previous_history = get(f"/{skill_id}/history?limit=2&after={history['nextCursor']}")
        assert set(item["revision"] for item in history["events"]).isdisjoint(
            item["revision"] for item in previous_history["events"]
        )
        revoked = post(
            f"/{skill_id}/revoke",
            {
                "expectedRevision": reactivated["skill"]["revision"],
                "releaseSha256": None,
                "confirm": True,
            },
            "http-revoke",
        )
        assert revoked["skill"]["state"] == "revoked"
        rejected = client.post(
            path + f"/{skill_id}/activate",
            json={
                "expectedRevision": revoked["skill"]["revision"],
                "releaseSha256": None,
                "confirm": True,
            },
            headers={**writes, "Idempotency-Key": "http-resurrect"},
        )
        assert rejected.status_code in {404, 409}
        fixture = {
            "packages": get("/packages"),
            "package": detail,
            "plan": {
                "schema": imported["schema"],
                "workspaceId": imported["workspaceId"],
                "principalId": imported["principalId"],
                "plan": plan,
            },
            "installed": installed,
            "active": active,
            "resource": resource,
            "updated": updated,
            "rolledBack": rolled,
            "history": history,
            "previousHistory": previous_history,
            "revoked": revoked,
            "list": get(""),
        }
        assert TOKEN not in json.dumps(fixture)
    with app_env.db.transaction(app_env.context) as connection:
        rows = connection.execute(
            "SELECT session_id,content_sha256,sha256 FROM workspace_context_skill_releases WHERE skill_id=%s ORDER BY source_revision",
            (UUID(skill_id),),
        ).fetchall()
        assert len(rows) == 2 and all(row["session_id"] is None for row in rows)
        assert rows[0]["content_sha256"] == rows[1]["content_sha256"]
        assert rows[0]["sha256"] != rows[1]["sha256"]
    if target := os.environ.get("POTBOT_SKILL_CONTRACT_OUTPUT"):
        destination = Path(target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(fixture, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
        )


def test_failed_import_is_atomic_and_source_retry_does_not_refetch(app_env, monkeypatch):
    from potbot.api import ui_skills
    from potbot.context.skill_packages import parse_skill_zip

    calls = []
    good = zip_body(b"guide")
    data = base64.b64decode(good["archiveBase64"])

    def fetch(*args):
        calls.append(args)
        return parse_skill_zip(
            data,
            source={"kind": "github", "url": args[0], "commit": args[1], "requested_path": args[2]},
        )

    monkeypatch.setattr(ui_skills, "fetch_github_skill", fetch)
    headers = {
        "Authorization": "Bearer owned",
        "Origin": ORIGIN,
        "X-Potbot-Setup-Token": TOKEN,
        "Idempotency-Key": "pinned-git-import",
    }
    body = {
        "repository": "https://github.com/example/skills",
        "commit": "a" * 40,
        "path": "review-documents",
    }
    with TestClient(application(app_env)) as client:
        response = client.post("/v1/ui/skills/imports/github", json=body, headers=headers)
        assert response.status_code == 200, response.text
        monkeypatch.setattr(
            ui_skills,
            "fetch_github_skill",
            lambda *args: pytest.fail("Committed retry fetched upstream"),
        )
        replay = client.post("/v1/ui/skills/imports/github", json=body, headers=headers)
        assert replay.status_code == 200 and replay.json() == response.json()
        assert len(calls) == 1
        changed = client.post(
            "/v1/ui/skills/imports/github", json={**body, "commit": "b" * 40}, headers=headers
        )
        assert changed.status_code == 409
        bad_headers = {**headers, "Idempotency-Key": "failed-package"}
        failed = client.post(
            "/v1/ui/skills/imports/zip",
            json={"archiveBase64": "eA==", "subdirectory": ""},
            headers=bad_headers,
        )
        assert failed.status_code == 400
        recovered = client.post("/v1/ui/skills/imports/zip", json=good, headers=bad_headers)
        assert recovered.status_code == 200

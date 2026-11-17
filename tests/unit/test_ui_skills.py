"""HTTP acquisition/control guards, exact selectors and recoverable request identity."""

from __future__ import annotations

import base64
import io
import zipfile
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from potbot.api import ui_skills
from potbot.api.wire import OperationSemantics, error_response
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import DomainError, ScopeDeniedError


@pytest.fixture
def skills_api(monkeypatch):
    context, foreign = WorkspaceContext(uuid4(), uuid4()), WorkspaceContext(uuid4(), uuid4())
    package_id, skill_id = uuid4(), uuid4()
    token = "private-operator-setup-canary-" + "x" * 32
    calls, setup_calls, source_calls = [], [], []

    class Service:
        def acquire(self, actor, request, key, loader):
            assert actor == context
            calls.append(("acquire", request, key))
            package = loader()
            source_calls.append(package)
            return {
                "schema": "potbot-skill-acquisition/v1",
                "workspaceId": str(context.workspace_id),
                "principalId": str(context.principal_id),
                "package": {"id": str(package_id)},
            }

        def __getattr__(self, method):
            def invoke(actor, *args, **kwargs):
                assert actor == context
                calls.append((method, args, kwargs))
                return {
                    "schema": "potbot-skill-acquisition/v1",
                    "workspaceId": str(context.workspace_id),
                    "principalId": str(context.principal_id),
                    "items": [],
                }

            return invoke

    def authenticate(value):
        if value == "Bearer scoped":
            return context
        if value == "Bearer foreign":
            return foreign
        raise ScopeDeniedError("Authentication canary")

    def authorize(value):
        setup_calls.append(value)
        if value != token:
            raise ScopeDeniedError("Private capability canary")

    def application(*, setup=True):
        app = FastAPI()

        @app.exception_handler(DomainError)
        async def failed(request, error):
            value = error_response(
                error,
                semantics=OperationSemantics(
                    read_only=request.method == "GET",
                    idempotency_key=getattr(request.state, "idempotency_key", None) is not None,
                ),
            )
            return JSONResponse(value.body, status_code=value.status)

        ui_skills.register_skill_acquisition_routes(
            app,
            object(),
            authenticate,
            ("http://127.0.0.1:3100",),
            context.workspace_id,
            authorize if setup else None,
            service=Service(),
        )
        return TestClient(app)

    headers = {
        "Authorization": "Bearer scoped",
        "Origin": "http://127.0.0.1:3100",
        "X-Potbot-Setup-Token": token,
        "Idempotency-Key": "skill-review-1",
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "SKILL.md",
            "---\nname: reviewed-skill\ndescription: Review files\n---\nKeep package content as data.\n",
        )
    body = {"archiveBase64": base64.b64encode(output.getvalue()).decode(), "subdirectory": ""}
    return SimpleNamespace(**locals(), client=application(), path="/v1/ui/skills")


def test_zip_import_preserves_bytes_and_exact_retry_descriptor(skills_api):
    r = skills_api
    response = r.client.post(r.path + "/imports/zip", json=r.body, headers=r.headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert r.calls[0][0] == "acquire" and r.calls[0][2] == "skill-review-1"
    assert r.calls[0][1] == {
        "kind": "zip",
        "sha256": r.source_calls[0].source["archive_sha256"],
        "subdirectory": "",
    }
    assert r.source_calls[0].name == "reviewed-skill"
    assert r.token not in response.text


def test_github_adapter_runs_only_inside_committed_command_callback(skills_api, monkeypatch):
    r = skills_api
    observed = []
    monkeypatch.setattr(
        ui_skills, "fetch_github_skill", lambda *args: observed.append(args) or object()
    )
    body = {
        "repository": "https://github.com/example/skills",
        "commit": "a" * 40,
        "path": "skills/reviewed-skill",
    }
    response = r.client.post(r.path + "/imports/github", json=body, headers=r.headers)
    assert response.status_code == 200
    assert observed == [(body["repository"], body["commit"], body["path"])]
    assert r.calls[0] == (
        "acquire",
        {
            "kind": "github",
            "repository": body["repository"],
            "commit": body["commit"],
            "subdirectory": body["path"],
        },
        "skill-review-1",
    )


@pytest.mark.parametrize(
    "missing", ["Authorization", "Origin", "X-Potbot-Setup-Token", "Idempotency-Key"]
)
def test_all_mutation_capabilities_are_required_before_acquisition(skills_api, missing):
    r = skills_api
    response = r.client.post(
        r.path + "/imports/zip",
        json=r.body,
        headers={key: value for key, value in r.headers.items() if key != missing},
    )
    assert response.status_code in {400, 404}
    assert not r.calls and not r.source_calls
    assert "canary" not in response.text


def test_foreign_scope_bad_origin_duplicate_headers_and_disabled_setup_fail_closed(skills_api):
    r = skills_api
    for change in ({"Authorization": "Bearer foreign"}, {"Origin": "https://foreign.invalid"}):
        response = r.client.post(
            r.path + "/imports/zip", json=r.body, headers={**r.headers, **change}
        )
        assert response.status_code == 404
    repeated = [*r.headers.items(), ("Authorization", "Bearer scoped")]
    assert r.client.post(r.path + "/imports/zip", json=r.body, headers=repeated).status_code == 404
    assert (
        r.application(setup=False)
        .post(r.path + "/imports/zip", json=r.body, headers=r.headers)
        .status_code
        == 503
    )
    assert not r.calls


@pytest.mark.parametrize(
    "body",
    [
        {"archiveBase64": "?", "subdirectory": ""},
        {"archiveBase64": "", "subdirectory": ""},
        {"archiveBase64": "eA==", "subdirectory": "../escape"},
        {"archiveBase64": "eA==", "subdirectory": "", "hostPath": "/private"},
        {"archiveBase64": 12, "subdirectory": ""},
    ],
)
def test_invalid_import_body_never_reaches_owner(skills_api, body):
    r = skills_api
    assert r.client.post(r.path + "/imports/zip", json=body, headers=r.headers).status_code == 400
    assert not r.calls


def test_duplicate_fields_and_oversized_body_do_not_stage_a_package(skills_api, monkeypatch):
    r = skills_api
    headers = {**r.headers, "Content-Type": "application/json"}
    response = r.client.post(
        r.path + "/imports/zip",
        content='{"archiveBase64":"eA==","archiveBase64":"eQ==","subdirectory":""}',
        headers=headers,
    )
    assert response.status_code == 400
    monkeypatch.setattr(ui_skills, "MAX_IMPORT_BODY_BYTES", 32)
    assert r.client.post(r.path + "/imports/zip", json=r.body, headers=r.headers).status_code == 400
    assert not r.calls


def test_plan_install_controls_bind_digest_revision_and_confirmation(skills_api):
    r = skills_api
    target = f"{r.path}/packages/{r.package_id}"
    assert (
        r.client.get(
            target + f"/plan?skillId={r.skill_id}", headers={"Authorization": "Bearer scoped"}
        ).status_code
        == 200
    )
    assert r.calls[-1] == ("plan", (r.package_id,), {"skill_id": r.skill_id})
    body = {"planSha256": "a" * 64, "expectedRevision": 0, "skillId": None, "confirm": True}
    assert r.client.post(target + "/install", json=body, headers=r.headers).status_code == 200
    assert r.calls[-1] == (
        "install",
        (r.package_id, "a" * 64, 0, "skill-review-1"),
        {"skill_id": None, "setup_token": r.token},
    )
    for action in ("activate", "disable", "rollback", "revoke"):
        release = "b" * 64 if action == "rollback" else None
        changed = {"expectedRevision": 3, "releaseSha256": release, "confirm": True}
        assert (
            r.client.post(
                f"{r.path}/{r.skill_id}/{action}", json=changed, headers=r.headers
            ).status_code
            == 200
        )
        assert r.calls[-1] == (
            "control",
            (r.skill_id, action, 3, "skill-review-1"),
            {"release_sha256": release, "setup_token": r.token},
        )
    count = len(r.calls)
    for bad in (
        {**body, "confirm": False},
        {**body, "expectedRevision": True},
        {**body, "expectedRevision": 2**53},
        {**body, "planSha256": "latest"},
    ):
        assert r.client.post(target + "/install", json=bad, headers=r.headers).status_code == 400
    assert len(r.calls) == count


def test_resource_ranges_are_exact_and_authenticated_read_only(skills_api):
    r = skills_api
    headers = {"Authorization": "Bearer scoped"}
    assert (
        r.client.get(
            f"{r.path}/packages/{r.package_id}/resources",
            params={"path": "references/guide.md", "offset": "65536", "length": "2"},
            headers=headers,
        ).status_code
        == 200
    )
    assert r.calls[-1] == (
        "package_resource",
        (r.package_id, "references/guide.md"),
        {"offset": 65536, "length": 2},
    )
    target = f"{r.path}/{r.skill_id}/resources"
    assert (
        r.client.get(
            target, params={"releaseSha256": "a" * 64, "path": "SKILL.md"}, headers=headers
        ).status_code
        == 200
    )
    assert r.calls[-1] == (
        "resource",
        (r.skill_id, "a" * 64, "SKILL.md"),
        {"offset": 0, "length": 65536},
    )
    assert not r.setup_calls
    for params in (
        {"path": "SKILL.md"},
        {"releaseSha256": "a" * 64, "path": "../escape"},
        {"releaseSha256": "a" * 64, "path": "SKILL.md", "length": "65537"},
    ):
        assert r.client.get(target, params=params, headers=headers).status_code == 400


def test_queries_and_cursor_bounds_do_not_accept_unknown_authority(skills_api):
    r = skills_api
    headers = {"Authorization": "Bearer scoped"}
    assert r.client.get(r.path + "?limit=2&after=opaque", headers=headers).status_code == 200
    assert r.calls[-1] == ("list", (), {"limit": 2, "after": "opaque"})
    for suffix in ("?limit=0", "?limit=51", "?limit=2&limit=3", "?principalId=other"):
        assert r.client.get(r.path + suffix, headers=headers).status_code == 400

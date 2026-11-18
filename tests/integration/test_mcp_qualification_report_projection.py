"""Retained report JSON cannot override independent projection authority."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from potbot.api.ui_mcp_qualification import register_mcp_qualification_routes
from potbot.api.wire import OperationSemantics, error_response
from potbot.domain.errors import DomainError, StateMismatchError
from potbot.domain.serialization import content_hash
from tests.integration import test_mcp_qualification as native_cases

pytestmark = pytest.mark.postgres
qualification = native_cases.qualification
rig = native_cases.rig
runtime_rig = native_cases.runtime_rig
__all__ = ["qualification", "rig", "runtime_rig"]


@pytest.mark.parametrize(
    "field",
    ["approvalValid", "id", "suiteId", "attemptId", "bindingId", "sha256"],
)
def test_untrusted_report_payload_cannot_replace_projection_authority(qualification, field):
    r = qualification
    completed = native_cases.complete(r)
    retained = r.service.report(r.actor, UUID(completed["reportId"]))["report"]
    assert retained["approvalValid"] and retained["passed"]
    # Start from real gateway/receipt evidence, then use the application's actual
    # INSERT privilege to forge another report's public hash but not its MAC.
    attempt = native_cases.request(r, key="untrusted-report")
    identity = uuid4()
    payload = {key: deepcopy(retained[key]) for key in ("complete", "passed", "evidence", "cases")}
    payload[field] = (
        True if field == "approvalValid" else "0" * 64 if field == "sha256" else str(uuid4())
    )
    with r.env.db.transaction(r.actor) as conn:
        conn.execute(
            "INSERT INTO mcp_qualification_reports(workspace_id,id,principal_id,suite_id,"
            "attempt_id,payload,sha256,mac) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                r.actor.workspace_id,
                identity,
                r.actor.principal_id,
                UUID(r.admitted["id"]),
                UUID(attempt["id"]),
                Jsonb(payload),
                content_hash(payload),
                "0" * 64,
            ),
        )
    with pytest.raises(StateMismatchError, match="integrity"):
        r.service.report(r.actor, identity)

    app = FastAPI()

    @app.exception_handler(DomainError)
    async def failure(_request, error):
        response = error_response(error, semantics=OperationSemantics(read_only=True))
        return JSONResponse(response.body, status_code=response.status)

    register_mcp_qualification_routes(
        app,
        r.env.db,
        lambda _value: r.actor,
        ("https://operator.example",),
        r.actor.workspace_id,
        None,
        service=r.service,
    )
    client = TestClient(app)
    response = client.get(
        f"/v1/ui/mcp-qualification/reports/{identity}",
        headers={"Authorization": "Bearer fixture"},
    )
    assert response.status_code == 409
    assert "report" not in response.json()
    actual = client.get(
        f"/v1/ui/mcp-qualification/reports/{completed['reportId']}",
        headers={"Authorization": "Bearer fixture"},
    )
    assert actual.status_code == 200 and actual.json()["report"]["approvalValid"] is True
    assert len(r.calls) == len(r.suite.cases)

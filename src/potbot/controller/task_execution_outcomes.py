"""Independent literal checks over complete, owned operation result evidence.

The original selection, input and assertions come from the signed check binding.
The existing queue, gateway, budget and artifact owners supply the result. No
client verdict, truncated preview or model declaration can establish a pass.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

from potbot.controller.jobs import _public, _read
from potbot.controller.tasks import json_record
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import DomainError, StateMismatchError
from potbot.domain.jobs import CHECK_JOB, COG_JOB, MAX_JOB_OUTPUT_BYTES, TERMINAL_JOB_STATES
from potbot.domain.result_assertions import bounded_value, evaluate_result_assertions
from potbot.domain.serialization import canonical_json, content_hash
from potbot.domain.syn import type_identity
from potbot.models.hosted_policy import HostedPolicy

if TYPE_CHECKING:
    from potbot.controller.operation_workbench import OperationWorkbench
    from potbot.controller.task_execution import TaskExecutionService

SCHEMA = "potbot-task-result-verification/v1"
ORACLE_SCHEMA = "potbot-task-result-oracle/v1"
MAX_RESULT_BYTES = MAX_JOB_OUTPUT_BYTES
MAX_OUTPUT_EVENTS = 4096
RANGE_BYTES = 262_144


class _Unavailable(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str = "result_evidence_invalid") -> None:
    if not condition:
        raise _Unavailable(code)


def _terminal_evidence(
    connection: Any, actor: WorkspaceContext, row: dict[str, Any]
) -> dict[str, Any]:
    events = connection.execute(
        "SELECT id,sequence,event_type,data FROM job_events WHERE workspace_id=%s AND job_id=%s "
        "AND event_type IN ('job.succeeded','job.failed','job.cancelled','job.indeterminate') "
        "ORDER BY sequence LIMIT 2",
        (actor.workspace_id, row["id"]),
    ).fetchall()
    _require(len(events) == 1)
    event = dict(events[0])
    _require(event["event_type"] == "job.succeeded" and event["data"] == {"job": _public(row)})
    _require(row["finished_at"] is not None and row["error_code"] is None)
    return dict(json_record(event))


def _settlement(
    connection: Any,
    actor: WorkspaceContext,
    row: dict[str, Any],
    key: str,
) -> dict[str, Any]:
    records = connection.execute(
        "SELECT b.id,b.status,b.amounts,b.actual,b.origin,u.id AS usage_id,"
        "u.amounts AS usage_amounts,u.origin AS usage_origin FROM budget_reservations b "
        "JOIN budget_usage u ON u.workspace_id=b.workspace_id AND u.reservation_id=b.id "
        "AND u.episode_id=b.episode_id WHERE b.workspace_id=%s AND b.episode_id=%s "
        "AND b.reservation_key=%s LIMIT 2",
        (actor.workspace_id, row["episode_id"], key),
    ).fetchall()
    _require(len(records) == 1, "accounting_evidence_unavailable")
    record = dict(records[0])
    _require(
        record["status"] == "settled"
        and record["actual"] == record["usage_amounts"]
        and record["origin"] == record["usage_origin"]
        and isinstance(record["actual"], dict)
        and set(record["actual"]) == set(record["amounts"]) - {"concurrency"}
        and record["actual"].get("calls") == 1,
        "accounting_evidence_unavailable",
    )
    return dict(json_record(record))


def _artifact_bytes(
    workbench: OperationWorkbench,
    actor: WorkspaceContext,
    operation_id: str,
    job_id: UUID,
    artifact_id: str,
) -> tuple[bytes, dict[str, Any]]:
    data, metadata = bytearray(), None
    while True:
        page = workbench.artifact(actor, operation_id, job_id, offset=len(data), length=RANGE_BYTES)
        current = page.get("artifact")
        _require(
            isinstance(current, dict)
            and set(current) == {"id", "sha256", "bytes", "mediaType"}
            and current["id"] == artifact_id
            and current["mediaType"] == "application/json"
            and type(current["bytes"]) is int
            and 0 < current["bytes"] <= MAX_RESULT_BYTES,
            "output_bound_or_type_unavailable",
        )
        assert isinstance(current, dict)
        if metadata is None:
            metadata = dict(current)
        _require(current == metadata and page["offset"] == len(data))
        try:
            chunk = base64.b64decode(page["dataBase64"], validate=True)
        except (ValueError, TypeError, binascii.Error):
            raise _Unavailable("result_evidence_invalid") from None
        _require(len(chunk) == min(RANGE_BYTES, metadata["bytes"] - len(data)))
        data.extend(chunk)
        expected_next = len(data) if len(data) < metadata["bytes"] else None
        _require(page["nextOffset"] == expected_next)
        if expected_next is None:
            break
    _require(hashlib.sha256(data).hexdigest() == metadata["sha256"])
    return bytes(data), metadata


def _typed_output(raw: bytes, capabilities: dict[str, Any]) -> dict[str, Any]:
    def unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise ValueError
            value[key] = item
        return value

    def constant(_value: str) -> None:
        raise ValueError

    try:
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=constant
        )
        document = bounded_value(document, maximum=MAX_RESULT_BYTES)
        _require(isinstance(document, dict) and set(document) == {"type_ref", "value"})
        _require(canonical_json(document).encode("utf-8") == raw)
        output_type = type_identity(document["type_ref"])
        _require(
            len(capabilities["outputTypes"]) == 1
            and output_type == type_identity(capabilities["outputTypes"][0]),
            "output_type_mismatch",
        )
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, DomainError):
        raise _Unavailable("result_evidence_invalid") from None
    return {"typeRef": document["type_ref"], "value": document["value"]}


def _native_result(
    service: TaskExecutionService,
    connection: Any,
    actor: WorkspaceContext,
    row: dict[str, Any],
    binding: dict[str, Any],
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    retained = row["result"]
    _require(
        isinstance(retained, dict)
        and set(retained) == {"artifact_id", "operation_id"}
        and retained["operation_id"] == str(row["id"])
    )
    accounting = _settlement(connection, actor, row, f"native-cog-{row['id']}")
    raw, metadata = _artifact_bytes(
        service.workbench, actor, binding["operationId"], row["id"], retained["artifact_id"]
    )
    view = _typed_output(raw, binding["capabilities"])
    return view, metadata["sha256"], {"artifact": metadata, "accounting": accounting}


def _hosted_result(
    connection: Any,
    actor: WorkspaceContext,
    row: dict[str, Any],
    terminal: dict[str, Any],
    dispatch: dict[str, Any],
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    retained = row["result"]
    _require(
        isinstance(retained, dict)
        and set(retained) == {"invocation_ids", "completed_models", "accounting", "usage"}
        and retained["completed_models"] == 1
        and len(retained["invocation_ids"]) == 1
        and len(row["selected_models"]) == 1
        and retained["accounting"] != "pending_reconciliation"
    )
    selected = row["selected_models"][0]
    invocations = connection.execute(
        "SELECT i.id,i.status,i.reservation_id,i.result,r.episode_id,r.ordinal,r.request_hash,"
        "r.policy_hash,r.request,r.policy FROM hosted_invocations i JOIN hosted_requests r "
        "USING(workspace_id,id) WHERE r.workspace_id=%s AND r.job_id=%s ORDER BY r.ordinal LIMIT 2",
        (actor.workspace_id, row["id"]),
    ).fetchall()
    _require(len(invocations) == 1, "gateway_evidence_unavailable")
    invocation = dict(invocations[0])
    observed = invocation["result"]
    policy = HostedPolicy.from_dict(invocation["policy"])
    generation, allowance, _rate = policy.request(selected, row["input"])
    descriptor = {
        "generation": generation.to_dict(),
        "role": selected["role"],
        "contract_revision_id": str(row["contract_revision_id"]),
        "input_token_allowance": allowance,
        "input_accounting": "conservative_byte_estimate",
    }
    _require(
        str(invocation["id"]) == retained["invocation_ids"][0]
        and invocation["episode_id"] == row["episode_id"]
        and invocation["ordinal"] == 0
        and invocation["status"] == "completed"
        and invocation["request"] == descriptor
        and invocation["request_hash"] == content_hash(descriptor)
        and invocation["policy_hash"] == policy.fingerprint
        and isinstance(observed, dict)
        and observed.get("invocation_id") == str(invocation["id"])
        and observed.get("status") == "completed"
        and observed.get("finish_reason") == "stop"
        and observed.get("error_code") is None
        and {key: observed.get(key) for key in ("provider", "model", "role")} == selected,
        "gateway_evidence_unavailable",
    )
    events = connection.execute(
        "SELECT id,kind,details FROM hosted_invocation_events WHERE workspace_id=%s "
        "AND invocation_id=%s AND kind IN ('observed','completed') ORDER BY created_at,id LIMIT 3",
        (actor.workspace_id, invocation["id"]),
    ).fetchall()
    _require(
        len(events) == 2
        and {event["kind"] for event in events} == {"observed", "completed"}
        and all(event["details"] == observed for event in events),
        "gateway_evidence_unavailable",
    )
    accounting = _settlement(connection, actor, row, "hosted:" + str(invocation["id"]))
    _require(
        accounting["id"] == str(invocation["reservation_id"])
        and accounting["actual"] == observed.get("accounted_amounts") == retained["usage"]
        and accounting["origin"] == observed.get("accounting") == retained["accounting"],
        "accounting_evidence_unavailable",
    )
    chunks = connection.execute(
        "SELECT id,sequence,data FROM job_events WHERE workspace_id=%s AND job_id=%s "
        "AND event_type='job.output' ORDER BY sequence LIMIT %s",
        (actor.workspace_id, row["id"], MAX_OUTPUT_EVENTS + 1),
    ).fetchall()
    _require(len(chunks) <= MAX_OUTPUT_EVENTS, "output_bound_or_type_unavailable")
    texts = []
    for chunk in chunks:
        value = chunk["data"]
        _require(
            dispatch["sequence"] < chunk["sequence"] < terminal["sequence"]
            and isinstance(value, dict)
            and set(value) == {"text", "provider", "model", "role"}
            and isinstance(value["text"], str)
            and {key: value[key] for key in ("provider", "model", "role")} == selected
        )
        texts.append(value["text"])
    text = "".join(texts)
    raw = text.encode("utf-8", "strict")
    _require(len(raw) == row["output_bytes"] <= MAX_RESULT_BYTES, "output_incomplete")
    digest = hashlib.sha256(raw).hexdigest()
    evidence = {
        "invocationId": str(invocation["id"]),
        "requestSha256": invocation["request_hash"],
        "policySha256": invocation["policy_hash"],
        "observationSha256": content_hash(observed),
        "gatewayEvents": [str(event["id"]) for event in events],
        "outputEvents": [str(chunk["id"]) for chunk in chunks],
        "outputBytes": len(raw),
        "accounting": accounting,
    }
    return {"text": text}, digest, evidence


def verify_result(
    service: TaskExecutionService,
    actor: WorkspaceContext,
    job_id: UUID,
    binding: dict[str, Any],
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "unavailable",
        "bindingSha256": binding["sha256"],
        "acceptanceSha256": binding["acceptanceSha256"],
        "outputSha256": None,
        "evidenceSha256": None,
        "resultKind": None,
        "checks": [],
        "reasonCodes": [],
    }
    try:
        with service.database.transaction(actor) as connection:
            row = _read(connection, actor, job_id)
            if row["job_type"] == CHECK_JOB:
                raise _Unavailable("check_is_not_an_execution")
            if row["state"] not in TERMINAL_JOB_STATES:
                report.update(status="pending", reasonCodes=["execution_not_complete"])
                return report
            if row["state"] != "succeeded":
                raise _Unavailable("execution_" + row["state"])
            verified = service.execution_binding(actor, job_id)
            _require(verified == binding, "execution_binding_invalid")
            terminal = _terminal_evidence(connection, actor, row)
            dispatches = connection.execute(
                "SELECT id,sequence,data FROM job_events WHERE workspace_id=%s AND job_id=%s "
                "AND event_type='job.dispatched' ORDER BY sequence LIMIT 2",
                (actor.workspace_id, job_id),
            ).fetchall()
            _require(len(dispatches) == 1 and row["dispatched_at"] is not None)
            dispatch = dict(dispatches[0])
            _require(dispatch["sequence"] < terminal["sequence"])
            _require(
                all(
                    dispatch["data"]["job"][key] == _public(row)[key]
                    for key in (
                        "id",
                        "episode_id",
                        "contract_revision_id",
                        "job_type",
                        "selected_models",
                        "operation_check_id",
                        "owner_principal_id",
                    )
                )
            )
            if row["job_type"] == COG_JOB:
                view, output_sha, evidence = _native_result(
                    service, connection, actor, row, binding
                )
                kind = "typed_cog"
            elif row["job_type"] == "model.generate":
                view, output_sha, evidence = _hosted_result(
                    connection, actor, row, terminal, dispatch
                )
                kind = "hosted_text"
            else:
                raise _Unavailable("result_route_unavailable")
            checks = evaluate_result_assertions(binding["acceptance"], view, schema=ORACLE_SCHEMA)
            _require(_public(_read(connection, actor, job_id)) == _public(row))
            report.update(
                status="passed" if all(item["passed"] for item in checks) else "failed",
                outputSha256=output_sha,
                evidenceSha256=content_hash(
                    {
                        "bindingSha256": binding["sha256"],
                        "terminal": terminal,
                        "dispatch": json_record(dispatch),
                        "result": evidence,
                    }
                ),
                resultKind=kind,
                checks=checks,
                reasonCodes=[]
                if all(item["passed"] for item in checks)
                else ["assertion_mismatch"],
            )
    except _Unavailable as error:
        report["reasonCodes"] = [error.code]
    except (DomainError, ValueError, TypeError, KeyError, UnicodeError, OSError, RecursionError):
        report["reasonCodes"] = ["result_evidence_unavailable"]
    return report


def result(
    service: TaskExecutionService,
    actor: WorkspaceContext,
    selection_id: UUID,
    job_id: UUID,
) -> dict[str, Any]:
    owned = service.inspect(actor, selection_id, job_id)
    verification = verify_result(service, actor, job_id, owned["binding"])
    try:
        output = service.workbench.result(actor, owned["operationId"], job_id)
    except (DomainError, OSError):
        # A missing/corrupt artifact must leave the owned job and its negative
        # verification visible without publishing an unverified preview.
        if verification["status"] in {"passed", "failed"}:
            raise StateMismatchError("Verified operation result changed during readback") from None
        output = {
            "schema": "potbot-operation-workbench/v1",
            "workspaceId": str(actor.workspace_id),
            "principalId": str(actor.principal_id),
            "operationId": owned["operationId"],
            "job": owned["job"],
            "output": "",
            "truncated": bool(owned["job"]["output_bytes"]),
            "artifact": None,
            "artifactText": None,
            "artifactTruncated": False,
        }
    return {**owned, "result": output, "verification": verification}

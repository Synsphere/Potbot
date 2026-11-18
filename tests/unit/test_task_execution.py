"""Selected provenance cannot be substituted under an existing authorization."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from potbot.controller.jobs import JobService
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import DomainError, ScopeDeniedError
from potbot.domain.jobs import JobLease
from potbot.domain.operation_workbench import (
    sign_authorization,
    verify_check_authorization,
    verify_launch_authorization,
)
from potbot.domain.serialization import content_hash
from potbot.domain.task_execution import (
    BINDING_SCHEMA,
    ORACLE_SCHEMA,
    execution_consumer_sha256,
    parse_binding,
)

TOKEN = "task-execution-test-operator-authority-1234567890"
NOW = datetime(2026, 11, 18, tzinfo=UTC)


def binding():
    actor = WorkspaceContext(uuid4(), uuid4())
    task = {
        "episodeId": str(uuid4()),
        "contractRevisionId": str(uuid4()),
        "contractSha256": "a" * 64,
        "repositoryStateId": str(uuid4()),
        "repositorySha256": "b" * 64,
        "semanticReleaseId": str(uuid4()),
        "semanticReleaseSha256": "c" * 64,
    }
    request = {
        "job_type": "model.generate",
        "episode_id": task["episodeId"],
        "input": {"prompt": "private task input", "max_output_tokens": 8},
        "selected_models": [{"provider": "openai", "model": "fixture", "role": "reasoning"}],
    }
    acceptance = {
        "schema": ORACLE_SCHEMA,
        "assertions": [{"op": "equals", "pointer": "/text", "value": "expected"}],
    }
    value = {
        "schema": BINDING_SCHEMA,
        "workspaceId": str(actor.workspace_id),
        "principalId": str(actor.principal_id),
        "selectionId": str(uuid4()),
        "selectionSha256": "d" * 64,
        "searchId": str(uuid4()),
        "resultId": str(uuid4()),
        "candidateId": "e" * 64,
        "operationId": "f" * 64,
        "ownerSha256": "1" * 64,
        "consumerSha256": execution_consumer_sha256(),
        "taskBinding": task,
        "capabilities": {
            key: []
            for key in ("classes", "inputTypes", "outputTypes", "effects", "scopes", "platforms")
        },
        "request": request,
        "requestSha256": content_hash(request),
        "acceptance": acceptance,
        "acceptanceSha256": content_hash(acceptance),
    }
    value["sha256"] = content_hash(value)
    return actor, value


def signed(actor, value):
    return {
        "operation_id": value["operationId"],
        "request": value["request"],
        "task_execution_sha256": value["sha256"],
        "authorization": sign_authorization(
            actor,
            value["operationId"],
            value["request"],
            TOKEN,
            purpose="check",
            now=NOW,
            task_execution_sha256=value["sha256"],
        ),
    }


@pytest.mark.parametrize(
    "field",
    ["taskBinding", "acceptance", "request", "capabilities", "sha256", "extra", "consumerSha256"],
)
def test_binding_rejects_modified_semantics_without_digest(field):
    _actor, value = binding()
    assert parse_binding(value) == value
    mutated = deepcopy(value)
    if field == "taskBinding":
        mutated[field]["repositoryStateId"] = str(uuid4())
    elif field == "acceptance":
        mutated[field]["assertions"][0]["value"] = "changed"
    elif field == "request":
        mutated[field]["input"]["prompt"] = "changed"
    elif field == "capabilities":
        mutated[field]["inputTypes"] = ["syn://type/other"]
    elif field in {"consumerSha256", "sha256"}:
        mutated[field] = "0" * 64
    else:
        mutated["extra"] = "not a supported field"
    with pytest.raises(DomainError):
        parse_binding(mutated)


@pytest.mark.parametrize("mutation", ["replace", "drop", "oracle", "principal", "consumer"])
def test_check_mac_cannot_authorize_another_binding_even_if_rehashed(mutation):
    actor, value = binding()
    data = signed(actor, value)
    assert verify_check_authorization(actor, data, TOKEN, now=NOW)[0] == value["operationId"]
    if mutation == "drop":
        del data["task_execution_sha256"]
    elif mutation == "replace":
        data["task_execution_sha256"] = "0" * 64
    elif mutation == "principal":
        actor = WorkspaceContext(actor.workspace_id, uuid4())
    else:
        mutated = deepcopy(value)
        if mutation == "oracle":
            mutated["acceptance"]["assertions"][0]["value"] = "changed"
            mutated["acceptanceSha256"] = content_hash(mutated["acceptance"])
        else:
            mutated["consumerSha256"] = "0" * 64
        data["task_execution_sha256"] = content_hash(
            {key: item for key, item in mutated.items() if key != "sha256"}
        )
    with pytest.raises(ScopeDeniedError):
        verify_check_authorization(actor, data, TOKEN, now=NOW)


def test_historical_verification_ignores_only_expiry_not_scope_or_mac():
    actor, value = binding()
    data = signed(actor, value)
    future = NOW + timedelta(days=1)
    with pytest.raises(ScopeDeniedError):
        verify_check_authorization(actor, data, TOKEN, now=future)
    assert (
        verify_check_authorization(actor, data, TOKEN, now=future, require_current=False)[0]
        == value["operationId"]
    )
    for token in (None, TOKEN + "rotated"):
        with pytest.raises(ScopeDeniedError):
            verify_check_authorization(actor, data, token, now=future, require_current=False)
    data["authorization"]["mac"] = "0" * 64
    with pytest.raises(ScopeDeniedError):
        verify_check_authorization(actor, data, TOKEN, now=future, require_current=False)


def test_launch_signature_retains_original_exact_binding_and_distinct_check():
    actor, value = binding()
    data, check_id = signed(actor, value), uuid4()
    authorization = sign_authorization(
        actor,
        value["operationId"],
        value["request"],
        TOKEN,
        purpose="launch",
        check_id=check_id,
        now=NOW,
        task_execution_sha256=value["sha256"],
    )
    lease = JobLease(
        actor.workspace_id,
        uuid4(),
        1,
        "worker",
        NOW + timedelta(minutes=1),
        "model.generate",
        UUID(value["taskBinding"]["episodeId"]),
        value["request"]["input"],
        value["request"]["selected_models"],
        UUID(value["taskBinding"]["contractRevisionId"]),
        actor.principal_id,
        check_id,
        authorization,
        UUID(value["selectionId"]),
        value,
    )
    verify_launch_authorization(actor, lease, data, TOKEN, now=NOW)
    verify_launch_authorization(
        actor, lease, data, TOKEN, now=NOW + timedelta(days=1), require_current=False
    )
    for changed in (
        replace(lease, task_execution_binding=None),
        replace(lease, operation_check_id=uuid4()),
        replace(lease, operation_authorization=data["authorization"]),
        replace(lease, task_execution_binding={**value, "sha256": "0" * 64}),
    ):
        with pytest.raises(ScopeDeniedError):
            verify_launch_authorization(actor, changed, data, TOKEN, now=NOW)


def test_selected_check_without_binding_rejected_before_database():
    actor, value = binding()

    class Untouched:
        def transaction(self, *_args):
            pytest.fail("Malformed selected work must not enter the database")

    with pytest.raises(ScopeDeniedError):
        JobService(Untouched()).submit(
            actor,
            {
                "job_type": "operation.check",
                "episode_id": value["taskBinding"]["episodeId"],
                "input": signed(actor, value),
                "selected_models": [],
            },
            "missing-binding",
            trusted_operation=True,
        )


def test_consumer_pin_is_stable_lowercase_sha256():
    pin = execution_consumer_sha256()
    assert (
        pin == execution_consumer_sha256()
        and len(pin) == 64
        and all(char in "0123456789abcdef" for char in pin)
    )


def test_native_prepare_preserves_signed_alias_and_uses_installed_wire_spelling():
    from contextlib import contextmanager
    from types import SimpleNamespace

    from potbot.controller.native_cog_dispatch import NativeCogDispatcher

    actor, value = binding()
    agent = uuid4()
    operation = SimpleNamespace(
        binding=SimpleNamespace(
            input_type_ref="syn://type/potbot-data-text",
            policy=SimpleNamespace(source_digest="9" * 64),
        )
    )
    dispatcher = NativeCogDispatcher.__new__(NativeCogDispatcher)
    dispatcher.operations = {"syn://thing/fixture": operation}
    dispatcher.profile_hash, dispatcher.agents = "8" * 64, frozenset({agent})

    class Connection:
        def execute(self, *_args):
            return self

        def fetchone(self):
            return {"content_digest": "9" * 64}

    class Database:
        @contextmanager
        def transaction(self, _actor):
            yield Connection()

    data = {
        "agent_id": str(agent),
        "repository_state_id": value["taskBinding"]["repositoryStateId"],
        "operation_ref": "syn://thing/fixture",
        "profile_sha256": "8" * 64,
        "type_ref": "synny://type/potbot-data-text",
        "value": "typed input",
    }
    lease = JobLease(
        actor.workspace_id,
        uuid4(),
        1,
        "fixture",
        NOW,
        "cog.execute",
        UUID(value["taskBinding"]["episodeId"]),
        data,
        [],
        UUID(value["taskBinding"]["contractRevisionId"]),
        actor.principal_id,
    )
    request, selected, prepared, _invocation = dispatcher._prepare(
        SimpleNamespace(database=Database()), actor, lease
    )
    assert selected is operation
    assert request.type_ref == data["type_ref"] == "synny://type/potbot-data-text"
    assert prepared.type_ref == "syn://type/potbot-data-text" and prepared.value == "typed input"
    with pytest.raises(ScopeDeniedError):
        dispatcher._prepare(
            SimpleNamespace(database=Database()),
            actor,
            replace(lease, input={**data, "type_ref": "syn://type/unrelated"}),
        )

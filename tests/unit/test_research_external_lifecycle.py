"""Admission authenticity and typed research provenance without native effects."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from potbot.controller.research_external import _limits, _signed, _usage, _verify_signed
from potbot.controller.research_external_worker import ExternalResearchWorker
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.serialization import content_hash
from potbot.research.external_binding import (
    BATCH_SCHEMA,
    authorization_mac,
    bounded_key,
    require_setup,
)
from potbot.research.external_cog import EXTERNAL_RESEARCH_COG, external_research_outcome
from potbot.research.source_policy import ExternalResearchPolicy, ResearchProviderRule
from tests.unit.test_phase7_research import action


@pytest.mark.parametrize(
    "field", ["runId", "querySha256", "policySha256", "reservationId", "actorPrincipalId"]
)
def test_native_authorization_authenticates_each_retained_binding(field):
    body = {
        "runId": str(uuid4()),
        "querySha256": "a" * 64,
        "policySha256": "b" * 64,
        "reservationId": str(uuid4()),
        "actorPrincipalId": str(uuid4()),
    }
    signed = _signed(body, "operator-secret")
    assert _verify_signed(signed, "operator-secret") == body
    changed = deepcopy(signed)
    changed[field] = "c" * 64
    with pytest.raises(StateMismatchError):
        _verify_signed(changed, "operator-secret")
    with pytest.raises(StateMismatchError):
        _verify_signed(signed, "rotated-secret")


def test_setup_requires_trusted_nonempty_match_and_never_accepts_missing_capability():
    assert require_setup("authority", "authority") == "authority"
    for supplied, expected in (
        ("authority", None),
        ("", ""),
        (None, "authority"),
        ("other", "authority"),
    ):
        with pytest.raises(ScopeDeniedError):
            require_setup(supplied, expected)
    with pytest.raises(ScopeDeniedError):
        authorization_mac({}, "")


@pytest.mark.parametrize("key", ["", "has spaces", "x" * 201, "line\nbreak", None])
def test_command_identity_is_bounded_and_literal(key):
    with pytest.raises(InvalidInputError):
        bounded_key(key)


def test_budget_covers_all_requests_and_supervised_document_time():
    policy = ExternalResearchPolicy(
        True,
        (ResearchProviderRule("brave", 11, 1000),),
        max_requests=7,
        max_documents=3,
        timeout_seconds=2,
    )
    assert _limits(policy) == {
        "calls": 7,
        "tool_ms": 36000,
        "bytes_read": 7 * 262144,
        "money_microunits": 77,
        "concurrency": 1,
    }


def test_unknown_transport_usage_stays_unknown_and_declared_cost_is_not_measured():
    policy = ExternalResearchPolicy(True, (ResearchProviderRule("brave", 11, 0),))
    receipt = {
        "request": {"request": {"provider": "brave"}},
        "response": {
            "usageOrigin": "measured",
            "response": {
                "requestSent": True,
                "status": "fetched",
                "elapsedMs": 3,
                "receivedBytes": 15,
            },
        },
    }
    usage, origin, unknown = _usage([receipt], policy)
    assert usage == {"calls": 1, "tool_ms": 3, "bytes_read": 15, "money_microunits": 11}
    assert origin == "conservative_estimate" and unknown is False
    assert _usage([{"response": None}], policy)[2] is True
    timed_out = deepcopy(receipt)
    timed_out["response"]["response"]["status"] = "timed_out"
    assert _usage([timed_out], policy)[2] is True
    synthetic = deepcopy(receipt)
    synthetic["response"]["usageOrigin"] = "synthetic"
    assert _usage([synthetic], policy)[1] == "synthetic"
    with pytest.raises(StateMismatchError):
        _usage([receipt, synthetic], policy)


def test_mock_transports_cannot_present_their_usage_as_native_measurements():
    policy = ExternalResearchPolicy(True, (ResearchProviderRule("brave", 0),))
    with pytest.raises(InvalidInputError):
        ExternalResearchWorker(SimpleNamespace(), policy, transport_factory=lambda *_: None)
    ExternalResearchWorker(
        SimpleNamespace(), policy, transport_factory=lambda *_: None, usage_origin="synthetic"
    )


def test_external_cog_has_single_ports_and_exact_controller_scope_receipt():
    source = action()
    run_id = uuid4()
    auth = content_hash({"action": source.identity, "run": str(run_id)})
    batch = {
        "schema": BATCH_SCHEMA,
        "runId": str(run_id),
        "actionId": str(source.action_id),
        "actionIdentity": source.identity,
        "authorizationSha256": auth,
        "documents": [],
    }
    outcome = external_research_outcome(source, batch, run_id=run_id, authorization_sha256=auth)
    assert outcome.value.type_ref == EXTERNAL_RESEARCH_COG.output_type_ref
    assert outcome.receipt.input_type_ref == EXTERNAL_RESEARCH_COG.input_type_ref
    assert outcome.receipt.output_hash == outcome.value.fingerprint
    assert outcome.receipt.invocation_id == run_id
    assert outcome.receipt.upstream_receipt_hash == auth
    assert outcome.receipt.scope_hash == content_hash(source.to_dict()["scope"])
    with pytest.raises(StateMismatchError):
        external_research_outcome(
            source, {**batch, "runId": str(uuid4())}, run_id=run_id, authorization_sha256=auth
        )

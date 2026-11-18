from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

from potbot.controller.registry_sources import parse_curated_manifest
from potbot.controller.task_discovery_ranking import (
    normalize_constraints,
    policy_sha256,
    rank_task_candidates,
    semantic_pool,
    validate_task_candidates,
)
from potbot.domain.errors import InvalidInputError
from potbot.domain.serialization import content_hash

ROOT = Path(__file__).resolve().parents[2]
GOLDENS = json.loads((ROOT / "tests/fixtures/discovery/golden-requests.json").read_text())["cases"]


def candidate(name: str = "fixture", **changes: Any) -> dict[str, Any]:
    value = {
        "id": content_hash(["candidate", name]),
        "kind": "registry",
        "name": name,
        "description": "Inspect repository commits and revision history.",
        "ownerId": str(uuid5(NAMESPACE_URL, name)),
        "bindingId": None,
        "ownerSha256": content_hash(["owner", name]),
        "capabilities": {
            "classes": ["repository_inspection"],
            "inputTypes": [],
            "outputTypes": [],
            "effects": ["read_only"],
            "scopes": [],
            "platforms": ["linux"],
        },
        "requiredTaskCapability": None,
        "installed": False,
        "authorityCurrent": True,
        "reasonCodes": [],
        "taskEvidence": {
            "status": "none",
            "suiteId": None,
            "reportId": None,
            "reportSha256": None,
            "caseCount": 0,
            "passedCases": 0,
        },
    }
    value.update(changes)
    return value


def curated() -> list[dict[str, Any]]:
    records = parse_curated_manifest(
        (ROOT / "examples/registry/curated-capabilities.json").read_bytes()
    )
    return [
        candidate(
            record.name,
            description=record.description,
            capabilities=record.capabilities,
            ownerSha256=record.sha256,
        )
        for record in records
    ]


def rank(
    items: list[dict[str, Any]], query: str = "Inspect the commit history", **kwargs: Any
) -> dict[str, Any]:
    return rank_task_candidates(
        items,
        query,
        kwargs.pop("constraints", None),
        task_capabilities=kwargs.pop("task_capabilities", []),
        **kwargs,
    )


def report(status: str) -> dict[str, Any]:
    return {
        "status": status,
        "suiteId": str(uuid5(NAMESPACE_URL, "suite")),
        "reportId": str(uuid5(NAMESPACE_URL, "report")),
        "reportSha256": "e" * 64,
        "caseCount": 2,
        "passedCases": 1 if status == "matching_failure" else 2,
    }


def test_original_forty_golden_requests_meet_frozen_ninety_percent_recall_at_three() -> None:
    records = curated()
    assert len(GOLDENS) == 40
    recalled = 0
    for case in GOLDENS:
        result = rank(records, case["query"], limit=3)
        recalled += any(
            case["expectedClass"] in item["capabilities"]["classes"] for item in result["items"]
        )
        assert all(item["executable"] is False for item in result["items"])
        assert case["runtimeQualified"] is False
    assert recalled / len(GOLDENS) >= 0.90


@pytest.mark.parametrize(
    "query,expected",
    [
        (
            "Check the revision history before discussing these modifications",
            "repository_inspection",
        ),
        ("Which call site introduced the regression?", "code_search"),
        ("Read the table formulas in this workbook", "spreadsheet_analysis"),
        ("Inspect memory allocation around this bottleneck", "profiling"),
        ("Find a remote breakpoint for this remote process", "remote_debugging"),
        ("Check the hardware breakpoint using SWD", "hardware_debugging"),
        ("Extract pages from this document", "document_analysis"),
        ("Get the API response from the service endpoint", "structured_api_read"),
    ],
)
def test_held_out_requests_use_reviewed_class_aliases(query: str, expected: str) -> None:
    result = rank(curated(), query, limit=3)
    assert any(expected in item["capabilities"]["classes"] for item in result["items"])


def test_names_ownership_registration_order_and_repeated_keywords_do_not_boost_relevance() -> None:
    first = candidate("plain")
    promoted = candidate("commit famous recommended", description=first["description"])
    repeated = candidate("repeated", description=(first["description"] + " ") * 20)
    result = rank([first, promoted, repeated])
    assert len({row["score"] for row in result["items"]}) == 1
    assert result == rank([repeated, first, promoted])
    assert all(row["features"]["installed"] == 0 for row in result["items"])


def test_types_check_direction_and_both_syn_schemes_without_inventing_data_native_edges() -> None:
    broad_input = candidate()
    broad_input["capabilities"]["inputTypes"] = ["synny://type/information-object"]
    broad_input["capabilities"]["outputTypes"] = ["syn://type/document"]
    result = rank(
        [broad_input],
        constraints={
            "inputTypes": ["syn://type/document"],
            "outputTypes": ["synny://type/information-object"],
        },
    )
    assert result["items"][0]["typeFit"] == "compatible"
    assert result["items"][0]["features"]["types"] == 200
    exact = rank([broad_input], constraints={"outputTypes": ["synny://type/document"]})
    assert exact["items"][0]["typeFit"] == "exact"
    wrong_input = rank([broad_input], constraints={"inputTypes": ["syn://type/animal-organism"]})
    assert wrong_input["excluded"][0]["reasonCodes"] == ["required_types_incompatible"]
    narrow = copy.deepcopy(broad_input)
    narrow["capabilities"]["inputTypes"] = ["syn://type/document"]
    assert rank([narrow], constraints={"inputTypes": ["syn://type/information-object"]})[
        "noSuitableCandidate"
    ]
    data = candidate()
    data["capabilities"]["outputTypes"] = ["syn://type/potbot-data-document"]
    assert rank([data], constraints={"outputTypes": ["syn://type/document"]})["noSuitableCandidate"]
    assert (
        rank([data], constraints={"outputTypes": ["synny://type/potbot-data-document"]})["items"][
            0
        ]["typeFit"]
        == "exact"
    )


def test_unknown_declared_type_and_missing_port_remain_unknown_under_hard_type_constraints() -> (
    None
):
    record = candidate()
    record["capabilities"]["inputTypes"] = ["syn://type/unregistered-private-type"]
    result = rank([record], constraints={"inputTypes": ["syn://type/document"]})
    assert result["excluded"][0]["reasonCodes"] == ["required_types_unknown"]
    assert rank([candidate()], constraints={"outputTypes": ["syn://type/document"]})[
        "noSuitableCandidate"
    ]
    with pytest.raises(InvalidInputError, match="sealed catalogue"):
        normalize_constraints({"inputTypes": ["syn://type/unregistered-private-type"]})


@pytest.mark.parametrize(
    "constraint,reason",
    [
        ({"classes": ["hardware_debugging"]}, "required_class_not_declared"),
        ({"permittedEffects": []}, "effects_denied_or_unknown"),
        ({"platform": "darwin"}, "platform_unavailable_or_unknown"),
        ({"requireTaskEvidence": True}, "matching_task_evidence_required"),
    ],
)
def test_hard_denials_win_over_maximum_semantic_installation_and_task_report_preferences(
    constraint: dict[str, Any],
    reason: str,
) -> None:
    record = candidate(installed=True)
    result = rank([record], constraints=constraint, semantic_scores={record["id"]: 1_000_000})
    assert result["noSuitableCandidate"]
    assert reason in result["excluded"][0]["reasonCodes"]
    assert semantic_pool([record], "commit", constraint, task_capabilities=[]) == []


def test_task_capabilities_are_frozen_permissions_and_unknown_is_not_a_grant() -> None:
    record = candidate(requiredTaskCapability="repository.read")
    denied = rank([record], task_capabilities=[])
    assert denied["excluded"][0]["reasonCodes"] == ["task_capability_denied"]
    allowed = rank([record], task_capabilities=["repository.read"])
    assert allowed["items"][0]["taskPermission"] == "permitted"
    unknown = rank([candidate()])["items"][0]
    assert unknown["taskPermission"] == "unknown" and unknown["executable"] is False


def test_scopes_effects_locality_and_current_owner_are_hard_constraints() -> None:
    record = candidate(installed=True, authorityCurrent=False)
    record["capabilities"].update(
        effects=["destructive"], scopes=["repository.write"], platforms=["remote"]
    )
    result = rank(
        [record],
        constraints={
            "permittedEffects": ["read_only"],
            "permittedScopes": [],
            "requireLocal": True,
        },
    )
    assert set(result["excluded"][0]["reasonCodes"]) == {
        "current_owner_authority_unavailable",
        "effects_denied_or_unknown",
        "scopes_denied_or_unknown",
        "local_runtime_unproved",
    }
    unknown = candidate(reasonCodes=["canonical_scopes_unknown"])
    assert rank([unknown], constraints={"permittedScopes": []})["noSuitableCandidate"]
    assert rank([candidate()], constraints={"permittedScopes": []})["items"]


def test_matching_current_task_pass_is_a_local_preference_and_historical_pass_is_not_quality() -> (
    None
):
    passing = candidate("pass", taskEvidence=report("matching_pass"))
    historical = candidate("historical", taskEvidence=report("historical"))
    failed = candidate("failed", taskEvidence=report("matching_failure"))
    result = rank([historical, failed, passing])
    by_id = {row["id"]: row for row in result["items"]}
    assert by_id[passing["id"]]["features"]["evidence"] == 250
    assert by_id[historical["id"]]["features"]["evidence"] == 0
    assert by_id[failed["id"]]["features"]["evidence"] == 0
    strict = rank([historical, failed, passing], constraints={"requireTaskEvidence": True})
    assert [row["id"] for row in strict["items"]] == [passing["id"]]
    assert rank([passing], "submarine pastry teleportation")["noSuitableCandidate"]


def test_semantic_pool_can_retrieve_a_paraphrase_without_letting_ownership_create_relevance() -> (
    None
):
    record = candidate(installed=True, taskEvidence=report("matching_pass"))
    query = "archeology of revisions"
    assert rank([record], query)["noSuitableCandidate"]
    assert semantic_pool([record], query, None, task_capabilities=[]) == validate_task_candidates(
        [record]
    )
    assert rank([record], query, semantic_scores={record["id"]: 0})["noSuitableCandidate"]
    positive = rank([record], query, semantic_scores={record["id"]: 750_000})["items"][0]
    assert positive["features"]["semantic"] == 300
    assert positive["score"] == sum(positive["features"].values())
    assert positive["executable"] is False
    smallest = rank([record], query, semantic_scores={record["id"]: 1})["items"][0]
    assert smallest["features"]["semantic"] == 1


def test_empty_query_needs_explicit_classes_or_types_and_no_result_is_explained() -> None:
    assert rank([candidate()], "")["noSuitableCandidate"]
    assert rank([candidate()], "", constraints={"classes": ["repository_inspection"]})["items"]
    no_match = rank(curated(), "submarine pastry teleportation")
    assert no_match["items"] == [] and no_match["noSuitableCandidate"]
    assert all(row["reasonCodes"] == ["no_supported_task_match"] for row in no_match["excluded"])


def test_normalization_is_canonical_idempotent_and_distinguishes_absent_and_empty_allowance() -> (
    None
):
    normalized = normalize_constraints(
        {"inputTypes": ["syn://type/potbot-data-document"], "permittedScopes": []}
    )
    assert normalized == {
        "classes": [],
        "inputTypes": ["synny://type/potbot-data-document"],
        "outputTypes": [],
        "platform": None,
        "permittedEffects": None,
        "permittedScopes": [],
        "requireLocal": False,
        "requireTaskEvidence": False,
    }
    assert normalize_constraints(normalized) == normalized
    assert len(policy_sha256()) == 64 and policy_sha256() == rank([candidate()])["policySha256"]


@pytest.mark.parametrize(
    "constraints",
    [
        {"unknown": True},
        {"classes": ["invented"]},
        {"permittedEffects": ["safe"]},
        {"inputTypes": ["syn://type/document", "synny://type/document"]},
        {"requireTaskEvidence": "true"},
        {"requireLocal": 1},
        {"platform": []},
        {"permittedScopes": ["same", "same"]},
        {"outputTypes": "syn://type/document"},
    ],
)
def test_malformed_constraints_are_rejected(constraints: dict[str, Any]) -> None:
    with pytest.raises(InvalidInputError):
        normalize_constraints(constraints)


def test_candidate_count_bytes_identity_and_provider_score_limits_fail_without_truncation() -> None:
    with pytest.raises(InvalidInputError, match="256-candidate"):
        rank([candidate(str(i)) for i in range(257)])
    with pytest.raises(InvalidInputError, match="one-MiB"):
        rank([candidate(str(i), description="x" * 4096) for i in range(256)])
    with pytest.raises(InvalidInputError, match="unique"):
        rank([candidate(), candidate()])
    with pytest.raises(InvalidInputError):
        rank([candidate()], "x" * 501)
    record = candidate()
    for value in (True, -1, 1_000_001, 0.5):
        with pytest.raises(InvalidInputError):
            rank([record], semantic_scores={record["id"]: value})
    with pytest.raises(InvalidInputError):
        rank([record], semantic_scores={"0" * 64: 1})
    with pytest.raises(InvalidInputError):
        semantic_pool([record], "commit", None, task_capabilities=[], limit=17)
    many = [candidate(str(i)) for i in range(32)]
    assert len(rank(many, limit=32)["items"]) == 32
    assert len(semantic_pool(many, "commit", None, task_capabilities=[])) == 16


def test_ranking_does_not_mutate_owner_observations_or_constraints() -> None:
    record = candidate()
    original = copy.deepcopy(record)
    constraints = {"permittedEffects": ["read_only"], "platform": "linux"}
    before = copy.deepcopy(constraints)
    rank([record], constraints=constraints)
    assert record == original and constraints == before


def test_empty_description_never_enters_semantic_pool_or_borrows_its_name_for_relevance() -> None:
    record = candidate("commit revision history", description="")
    record["capabilities"]["classes"] = []
    assert semantic_pool([record], "commit history", None, task_capabilities=[]) == []
    assert rank([record], "commit history")["noSuitableCandidate"]

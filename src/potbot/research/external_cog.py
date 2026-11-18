"""Declared single-input/output research transformation and causal receipt.

The controller supplies retained, independently reconstructed transport evidence.
This installed signature is not a claim that a deployed SYN release qualified
the network profile. Side effects remain with the research admission owner.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from potbot.cogs.contracts import (
    CogOutcome,
    CogReceipt,
    CogSignature,
    TypedCogValue,
    check_cog_input,
    check_cog_output,
)
from potbot.domain.errors import StateMismatchError
from potbot.domain.serialization import content_hash
from potbot.domain.syn import SynRef
from potbot.research.contracts import ResearchAction
from potbot.research.external_binding import BATCH_SCHEMA

EXTERNAL_RESEARCH_COG = CogSignature(
    SynRef("syn://cog/potbot-research-external"),
    "synny://type/potbot-research-external-cog",
    "synny://type/potbot-research-action",
    "synny://type/potbot-research-external-batch",
    "potbot.cogs.external_research",
    1,
    requires_controller_scope=True,
)
EXTERNAL_RESEARCH_SIGNATURE_SHA256 = content_hash([EXTERNAL_RESEARCH_COG.to_dict()])


def external_research_outcome(
    action: ResearchAction,
    batch: dict[str, Any],
    *,
    run_id: UUID,
    authorization_sha256: str,
) -> CogOutcome:
    if (
        batch.get("schema") != BATCH_SCHEMA
        or batch.get("runId") != str(run_id)
        or batch.get("actionId") != str(action.action_id)
        or batch.get("actionIdentity") != action.identity
        or batch.get("authorizationSha256") != authorization_sha256
    ):
        raise StateMismatchError("External research output does not match its controller scope")
    source = TypedCogValue(EXTERNAL_RESEARCH_COG.input_type_ref, action.to_dict())
    output = TypedCogValue(EXTERNAL_RESEARCH_COG.output_type_ref, batch)
    check_cog_input(EXTERNAL_RESEARCH_COG, source)
    check_cog_output(EXTERNAL_RESEARCH_COG, output)
    receipt = CogReceipt(
        cog_ref=str(EXTERNAL_RESEARCH_COG.cog_ref),
        cog_type_ref=EXTERNAL_RESEARCH_COG.cog_type_ref,
        binding_hash=EXTERNAL_RESEARCH_COG.binding_hash,
        cog_snapshot_hash=EXTERNAL_RESEARCH_SIGNATURE_SHA256,
        task_revision_id=action.scope.contract_revision_id,
        repository_state_id=action.scope.repository_state_id,
        invocation_id=run_id,
        input_type_ref=source.type_ref,
        input_hash=source.fingerprint,
        output_type_ref=output.type_ref,
        output_hash=output.fingerprint,
        input_artifact_id=None,
        upstream_receipt_hash=authorization_sha256,
        scope_hash=content_hash(action.to_dict()["scope"]),
    )
    return CogOutcome(output, receipt)

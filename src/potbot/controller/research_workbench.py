"""Operator research controls over existing need, action, run and evidence owners."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from potbot.context.service import native_skill_setup_token
from potbot.controller.information_needs import InformationNeedStore, NeedSpec
from potbot.controller.research_store import ResearchStore
from potbot.controller.research_worker import ResearchRunStore
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import (
    CapabilityUnavailableError,
    DomainError,
    InvalidInputError,
    RevisionConflictError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.research_evidence import digest, identity
from potbot.domain.result_assertions import bounded_value
from potbot.domain.serialization import content_hash
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.persistence.database import Database, idempotent_command
from potbot.research.contracts import ResearchAction, ResearchQuery, ResearchScope
from potbot.research.external_binding import bounded_key, require_setup
from potbot.research.providers import provider_descriptor
from potbot.research.source_policy import ExternalResearchPolicy
from potbot.semantics.releases import read_task_pin
from potbot.workspace_sandbox import load_workspace_sandbox

SCHEMA = "potbot-research-workbench/v1"
_NEED_FIELDS = {
    "schema_version",
    "question",
    "pending_decision",
    "alternatives",
    "required_evidence",
    "resolution_criteria",
    "scope",
    "known_evidence",
    "priority",
    "budget",
    "query_profiles",
}


def parse_need_spec(value: object) -> NeedSpec:
    checked = bounded_value(value, maximum=65_536)
    if (
        not isinstance(checked, dict)
        or set(checked) != _NEED_FIELDS
        or type(checked["schema_version"]) is not int
        or checked["schema_version"] != 1
        or not isinstance(checked["budget"], dict)
        or any(
            not isinstance(checked[key], list)
            for key in (
                "alternatives",
                "required_evidence",
                "resolution_criteria",
                "known_evidence",
                "query_profiles",
            )
        )
    ):
        raise InvalidInputError("Research need fields are invalid")
    return NeedSpec(
        question=checked["question"],
        pending_decision=checked["pending_decision"],
        alternatives=tuple(checked["alternatives"]),
        required_evidence=tuple(checked["required_evidence"]),
        resolution_criteria=tuple(checked["resolution_criteria"]),
        scope=checked["scope"],
        known_evidence=tuple(identity(item) for item in checked["known_evidence"]),
        priority=checked["priority"],
        budget=tuple(sorted(checked["budget"].items())),
        query_profiles=tuple(checked["query_profiles"]),
    )


def command_key(actor: WorkspaceContext, value: str) -> str:
    return content_hash([str(actor.principal_id), bounded_key(value)])


def _query_text(value: str, *, characters: int = 1024) -> str:
    return value[:characters].encode("utf-8")[:1024].decode("utf-8", errors="ignore")


def _need(row: dict[str, Any], *, summary: bool = False) -> dict[str, Any]:
    content = row["content"]
    if content_hash(content) != row["content_digest"]:
        raise StateMismatchError("Retained information need integrity failed")
    result = {
        "id": str(row["id"]),
        "revisionId": str(row["current_revision_id"]),
        "contentSha256": row["content_digest"],
        "status": row["status"],
    }
    if summary:
        result["question"] = content["question"][:512]
    else:
        result["spec"] = parse_need_spec({key: content[key] for key in _NEED_FIELDS}).to_dict()
    return result


class ResearchWorkbench:
    """Workspace-shared research with exact current context and explicit effects."""

    def __init__(
        self,
        database: Database,
        *,
        artifacts: ArtifactStore | None = None,
        policy: ExternalResearchPolicy | None = None,
        setup_token: Any = native_skill_setup_token,
    ) -> None:
        if artifacts is not None and artifacts.db != database:
            raise InvalidInputError("Research workbench artifacts require the same database")
        self.database, self.artifacts = database, artifacts
        self.configured_policy, self.setup_token = policy, setup_token

    @staticmethod
    def envelope(actor: WorkspaceContext, **fields: Any) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "scope": {
                "workspaceId": str(actor.workspace_id),
                "principalId": str(actor.principal_id),
            },
            **fields,
        }

    def policy(self) -> ExternalResearchPolicy:
        if self.configured_policy is not None:
            return self.configured_policy
        configured = os.getenv("POTBOT_RESEARCH_POLICY")
        if not configured:
            raise CapabilityUnavailableError("External research policy is not configured")
        path = Path(configured)
        if not path.is_absolute() or path.resolve() != path or not path.is_file():
            raise ScopeDeniedError("Research policy must be an existing physical absolute file")
        return ExternalResearchPolicy.read(path)

    def require_setup(self, supplied: object) -> None:
        require_setup(supplied, self.setup_token())

    def _command(
        self,
        actor: WorkspaceContext,
        name: str,
        key: str,
        request: dict[str, Any],
        write: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        # Recover the committed original response before consulting mutable
        # task, need, policy or source state. Child owners retain their own
        # idempotency, so an interrupted facade cannot create a second run.
        with self.database.transaction(actor) as conn:
            return idempotent_command(conn, actor, name, command_key(actor, key), request, write)

    def artifact_store(self, actor: WorkspaceContext) -> ArtifactStore:
        if self.artifacts is not None:
            return self.artifacts
        sandbox = load_workspace_sandbox(self.database, actor)
        return ArtifactStore(
            self.database, LocalArtifactBackend(sandbox.path("artifacts"), create_root=False)
        )

    def runs(self, actor: WorkspaceContext) -> ResearchRunStore:
        return ResearchRunStore(
            self.database, self.artifact_store(actor), setup_token=self.setup_token
        )

    def _context(
        self,
        conn: Any,
        actor: WorkspaceContext,
        episode_id: UUID,
        repository_state_id: UUID,
        need_id: UUID | None,
        *,
        lock: bool = False,
    ) -> dict[str, Any]:
        episode = conn.execute(
            "SELECT status,repository_id,active_contract_id FROM episodes "
            "WHERE workspace_id=%s AND id=%s" + (" FOR SHARE" if lock else ""),
            (actor.workspace_id, episode_id),
        ).fetchone()
        state = conn.execute(
            "SELECT repository_id,content_digest FROM repository_states WHERE workspace_id=%s AND id=%s",
            (actor.workspace_id, repository_state_id),
        ).fetchone()
        if episode is None or state is None or episode["repository_id"] != state["repository_id"]:
            raise ScopeDeniedError("Research task and repository state are unavailable")
        contract = conn.execute(
            "SELECT content,content_hash FROM contract_revisions "
            "WHERE workspace_id=%s AND episode_id=%s AND id=%s",
            (actor.workspace_id, episode_id, episode["active_contract_id"]),
        ).fetchone()
        if contract is None or content_hash(contract["content"]) != contract["content_hash"]:
            raise StateMismatchError("Research task contract integrity failed")
        pin = read_task_pin(conn, actor, episode_id, required=True)
        assert pin is not None
        task = {
            "episodeId": str(episode_id),
            "contractRevisionId": str(episode["active_contract_id"]),
            "repositoryStateId": str(repository_state_id),
            # The pin's own digest hashes its snapshot envelope. ResearchScope
            # names the underlying semantic release, as ResearchStore verifies.
            "semanticReleaseSha256": pin["content"]["release_digest"],
            "status": episode["status"],
        }
        select = (
            "SELECT n.id,n.episode_id,s.current_revision_id,s.status,r.content,r.content_digest "
            "FROM information_needs n JOIN need_status s ON s.workspace_id=n.workspace_id AND s.need_id=n.id "
            "JOIN need_revisions r ON r.workspace_id=n.workspace_id AND r.id=s.current_revision_id "
            "WHERE n.workspace_id=%s AND n.episode_id=%s"
        )
        rows = conn.execute(
            select + " ORDER BY n.created_at DESC,n.id DESC LIMIT 25",
            (
                actor.workspace_id,
                episode_id,
            ),
        ).fetchall()
        need = None
        if need_id is not None:
            row = conn.execute(
                select + " AND n.id=%s" + (" FOR SHARE OF s" if lock else ""),
                (
                    actor.workspace_id,
                    episode_id,
                    need_id,
                ),
            ).fetchone()
            if row is None:
                raise ScopeDeniedError("Research need does not belong to this task")
            need = _need(dict(row))
        reasons = []
        policy: dict[str, Any] | None = None
        descriptors = []
        try:
            configured = self.policy()
            policy = {"sha256": configured.fingerprint, "content": configured.to_dict()}
            descriptors = [
                provider_descriptor(item["provider"]) for item in policy["content"]["providers"]
            ]
            if not configured.allow_query_disclosure:
                reasons.append("query_disclosure_disabled")
            if not configured.providers:
                reasons.append("research_providers_unavailable")
            elif configured.max_requests < configured.provider_request_allowance:
                reasons.append("research_request_limit_insufficient")
        except (DomainError, OSError, ValueError):
            reasons.append("research_policy_unavailable")
        if episode["status"] != "running":
            reasons.append("task_not_running")
        if need is None:
            reasons.append("need_not_selected")
        elif need["status"] != "open":
            reasons.append("need_not_open")
        template = None
        if need is not None and policy is not None and not reasons:
            spec = need["spec"]
            # Seed review from the actual need; these descriptions never become
            # authority for additional destinations, providers or effects.
            template = {
                "category": "documentation",
                "operation": "query_source",
                "locator": "external-research",
                "search_text": _query_text(spec["question"], characters=500),
                "required_identifiers": [],
                "expected_observation": _query_text("\n".join(spec["required_evidence"])),
                "decision_effect": _query_text(spec["pending_decision"]),
                "evidence_requirement": _query_text("\n".join(spec["required_evidence"])),
                "stopping_condition": _query_text("\n".join(spec["resolution_criteria"])),
                "fallback": "Leave the need unresolved if authorized sources cannot supply the required evidence.",
                "target_version": policy["sha256"],
                "freshness_seconds": 0,
                "max_results": policy["content"]["max_results"],
                "max_source_bytes": policy["content"]["max_source_bytes"],
            }
        scope = {"workspaceId": str(actor.workspace_id), "principalId": str(actor.principal_id)}
        context_sha = content_hash(
            {
                "schema": SCHEMA,
                "scope": scope,
                "task": task,
                "need": need,
                "policySha256": policy["sha256"] if policy else None,
                "contractSha256": contract["content_hash"],
                "repositorySha256": state["content_digest"],
            }
        )
        return self.envelope(
            actor,
            task=task,
            needs=[_need(dict(row), summary=True) for row in rows],
            need=need,
            policy=policy,
            providerDescriptors=descriptors,
            contextSha256=context_sha,
            queryTemplate=template,
            current=not reasons,
            reasonCodes=reasons,
            visibility="workspace",
        )

    def context(
        self,
        actor: WorkspaceContext,
        episode_id: UUID,
        repository_state_id: UUID,
        need_id: UUID | None = None,
    ) -> dict[str, Any]:
        with self.database.transaction(actor) as conn:
            return self._context(conn, actor, episode_id, repository_state_id, need_id)

    def create_need(
        self,
        actor: WorkspaceContext,
        episode_id: UUID,
        repository_state_id: UUID,
        spec: object,
        key: str,
        *,
        setup_token: str,
    ) -> dict[str, Any]:
        self.require_setup(setup_token)
        checked = parse_need_spec(spec)

        def write() -> dict[str, Any]:
            current = self.context(actor, episode_id, repository_state_id)
            if current["task"]["status"] != "running":
                raise RevisionConflictError("Research needs require the current running task")
            row = InformationNeedStore(self.database).create(
                actor, episode_id, checked, command_key(actor, key)
            )
            return self.envelope(actor, need=_need(row))

        return self._command(
            actor,
            "research_workbench_need",
            key,
            {
                "episodeId": str(episode_id),
                "repositoryStateId": str(repository_state_id),
                "spec": checked.to_dict(),
            },
            write,
        )

    def admit(
        self,
        actor: WorkspaceContext,
        episode_id: UUID,
        repository_state_id: UUID,
        need_id: UUID,
        context_sha256: str,
        query: object,
        key: str,
        *,
        setup_token: str,
    ) -> dict[str, Any]:
        self.require_setup(setup_token)
        checked = ResearchQuery.from_dict(query)
        stable_key = command_key(actor, key)
        action_id = uuid5(NAMESPACE_URL, f"potbot-research:{actor.workspace_id}:{stable_key}")

        def write() -> dict[str, Any]:
            current = self.context(actor, episode_id, repository_state_id, need_id)
            if context_sha256 != current["contextSha256"] or not current["current"]:
                raise RevisionConflictError(
                    "Research context changed; review the current task and need"
                )
            policy = self.policy()
            if (
                checked.category.value != "documentation"
                or checked.operation.value != "query_source"
                or checked.locator != "external-research"
                or checked.target_version != policy.fingerprint
                or checked.max_results != policy.max_results
                or checked.max_source_bytes != policy.max_source_bytes
                or not 1 <= len(checked.search_text) <= 500
            ):
                raise ScopeDeniedError("Research query must use the reviewed configured policy")
            task, need = current["task"], current["need"]
            action = ResearchAction(
                action_id,
                ResearchScope(
                    actor.workspace_id,
                    episode_id,
                    UUID(task["contractRevisionId"]),
                    need_id,
                    UUID(need["revisionId"]),
                    repository_state_id,
                    task["semanticReleaseSha256"],
                ),
                checked,
            )
            store = ResearchStore(self.database)
            receipt = store.admit(actor, action, stable_key)
            retained = store.inspect(actor, UUID(receipt["action_id"]))
            return self.envelope(
                actor, action=retained, actionIdentity=receipt["identity"], reused=receipt["reused"]
            )

        return self._command(
            actor,
            "research_workbench_action",
            key,
            {
                "episodeId": str(episode_id),
                "repositoryStateId": str(repository_state_id),
                "needId": str(need_id),
                "contextSha256": digest(context_sha256),
                "query": checked.to_dict(),
            },
            write,
        )

    def queue(
        self,
        actor: WorkspaceContext,
        action_id: UUID,
        policy_sha256: str,
        key: str,
        *,
        setup_token: str,
        disclosure_confirmed: bool,
    ) -> dict[str, Any]:
        self.require_setup(setup_token)
        if disclosure_confirmed is not True:
            raise ScopeDeniedError("External research requires explicit query disclosure")

        def write() -> dict[str, Any]:
            policy = self.policy()
            if policy_sha256 != policy.fingerprint:
                raise RevisionConflictError("Research policy changed before admission")
            execution = self.runs(actor).queue_external(
                actor,
                action_id,
                policy,
                command_key(actor, key),
                setup_token=setup_token,
                disclosure_confirmed=True,
            )
            return self.envelope(actor, execution=execution)

        return self._command(
            actor,
            "research_workbench_run",
            key,
            {
                "actionId": str(action_id),
                "policySha256": digest(policy_sha256),
                "disclosureConfirmed": True,
            },
            write,
        )

    def list_runs(
        self,
        actor: WorkspaceContext,
        *,
        episode_id: UUID | None = None,
        limit: int = 25,
        after: str | None = None,
    ) -> dict[str, Any]:
        return self.envelope(
            actor,
            **self.runs(actor).list_external(
                actor, episode_id=episode_id, limit=limit, after=after
            ),
        )

    def inspect(self, actor: WorkspaceContext, run_id: UUID) -> dict[str, Any]:
        return self.envelope(actor, execution=self.runs(actor).inspect_external(actor, run_id))

    def events(
        self, actor: WorkspaceContext, run_id: UUID, *, after: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        store = self.runs(actor)
        return self.envelope(
            actor,
            execution=store.inspect_external(actor, run_id),
            **store.events_external(actor, run_id, after=after, limit=limit),
        )

    def cancel(self, actor: WorkspaceContext, run_id: UUID, key: str) -> dict[str, Any]:
        return self.envelope(
            actor,
            execution=self.runs(actor).cancel_external(actor, run_id, command_key(actor, key)),
        )

    def result(self, actor: WorkspaceContext, run_id: UUID) -> dict[str, Any]:
        store = self.runs(actor)
        execution = store.inspect_external(actor, run_id)
        batch, reasons = None, []
        state = execution["run"]["state"]
        verification = "pending" if state in {"queued", "running"} else "unavailable"
        if state == "succeeded":
            try:
                audited = store.audit_external(actor, run_id)
                batch = {
                    key: value
                    for key, value in audited.items()
                    if key not in {"resultArtifactId", "resultSha256", "receiptSha256", "receipt"}
                }
                verification = "verified"
            except DomainError as error:
                reasons.append(error.code.value)
        elif verification == "unavailable":
            reasons.append("research_did_not_complete")
        return self.envelope(
            actor, execution=execution, batch=batch, verification=verification, reasonCodes=reasons
        )

    def source(self, actor: WorkspaceContext, run_id: UUID, source_id: str) -> dict[str, Any]:
        from potbot.controller.research_evidence import ResearchEvidence

        return ResearchEvidence(self).source(actor, run_id, source_id)

    def citation(self, actor: WorkspaceContext, spec: object) -> dict[str, Any]:
        from potbot.controller.research_evidence import ResearchEvidence

        return self.envelope(actor, citation=ResearchEvidence(self).citation(actor, spec))

    def compare(
        self,
        actor: WorkspaceContext,
        episode_id: UUID,
        need_id: UUID,
        context_sha256: str,
        claims: object,
        key: str,
        *,
        setup_token: str,
    ) -> dict[str, Any]:
        from potbot.controller.research_evidence import ResearchEvidence

        self.require_setup(setup_token)
        return ResearchEvidence(self).compare(
            actor, episode_id, need_id, context_sha256, claims, key
        )

    def comparison(self, actor: WorkspaceContext, evidence_id: UUID) -> dict[str, Any]:
        from potbot.controller.research_evidence import ResearchEvidence

        return ResearchEvidence(self).read(actor, evidence_id)

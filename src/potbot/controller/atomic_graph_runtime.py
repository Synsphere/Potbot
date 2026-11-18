"""R7.2-B: durable atomic DAG scheduling *through* the checked job queue.

This service creates no worker authority. It persists an immutable typed plan,
selects eligible leaves and calls OperationWorkbench.launch, whose successful
job INSERT atomically binds its graph node via migration 49. The database
trigger is the concurrency/interrupt linearization point, not this read path.
Existing JobService owns leases, receipts, budgets, cancellations and terminal
states. Failed/indeterminate effects are never automatically retried.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID

from potbot.cogs.contracts import TypedCogValue
from potbot.controller.atomic_scheduler import AtomicAdmissionPlanner
from potbot.controller.jobs import JobService
from potbot.controller.operation_workbench import OperationWorkbench
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.jobs import CHECK_JOB, COG_JOB, CogJobInput, JobSpec
from potbot.domain.operation_workbench import operation_id, parse_check_input, parse_request
from potbot.domain.serialization import canonical_json, content_hash
from potbot.persistence.database import Database

_ACTIVE = frozenset({"queued", "running", "cancelling"})
_FAILED = frozenset({"failed", "cancelled", "indeterminate"})


@dataclass(frozen=True, slots=True)
class AtomicLaunchBinding:
    """Controller-supplied, exact checked operation for a planned atomic leaf."""

    operation_id: str
    check_job_id: UUID
    request: dict[str, Any]

    def __post_init__(self) -> None:
        operation_id(self.operation_id)
        if not isinstance(self.check_job_id, UUID):
            raise InvalidInputError("Atomic launch needs a checked job identity")
        spec = parse_request(self.request)
        if spec.job_type != COG_JOB:
            raise InvalidInputError("R7.2 leaf execution requires a checked native Cog")
        # A later caller can mutate its dict; use a canonical private copy.
        object.__setattr__(self, "request", json.loads(canonical_json(spec.to_dict())))


class AtomicGraphService:
    """Workspace/principal-owned DAG tied to existing immutable checked jobs."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.workbench = OperationWorkbench(database)
        self.jobs = JobService(database)

    @staticmethod
    def _ident(graph_id: UUID) -> UUID:
        if not isinstance(graph_id, UUID):
            raise InvalidInputError("Atomic graph identity must be a UUID")
        return graph_id

    @staticmethod
    def _manifest(
        planner: AtomicAdmissionPlanner,
        bindings: Mapping[str, AtomicLaunchBinding],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if not isinstance(planner, AtomicAdmissionPlanner) or not isinstance(bindings, Mapping):
            raise InvalidInputError("A validated atomic planner and bindings are required")
        if planner.graph.unresolved():
            raise InvalidInputError("All atomic leaves must be admitted before scheduling")
        admitted = planner.admitted()
        if not 1 <= len(admitted) <= 128 or set(bindings) != {c.task_id for c in admitted}:
            raise InvalidInputError("Every admitted leaf needs one checked job binding")
        leaf_ids = {candidate.task_id for candidate in admitted}
        positions = {name: index for index, name in enumerate(planner.graph.nodes)}
        structure = [
            {
                "id": n.task_id,
                "parent": n.parent_id,
                "description": n.description,
                "depth": n.depth,
                "dependencies": list(n.depends_on),
                "state": n.state,
                "operation": n.operation,
            }
            for n in planner.graph.nodes.values()
        ]
        leaves: list[dict[str, Any]] = []
        checks: set[UUID] = set()
        for candidate in admitted:
            if not set(candidate.dependencies) <= leaf_ids:
                raise InvalidInputError("A dependency may only target an executable atomic leaf")
            binding = bindings[candidate.task_id]
            if not isinstance(binding, AtomicLaunchBinding) or binding.check_job_id in checks:
                raise InvalidInputError("A checked execution cannot be shared by two leaves")
            checks.add(binding.check_job_id)
            selected = planner.registry[candidate.operation_name]
            if (
                selected.signature.binding_hash != candidate.binding_hash
                or str(selected.signature.cog_ref) != candidate.cog_ref
                or selected.operation.max_runtime_ms > 300_000
            ):
                raise InvalidInputError("The leaf differs from its bounded registered Cog")
            spec = JobSpec.from_dict(binding.request)
            request = CogJobInput.parse(spec.input)
            expected_ref = candidate.cog_ref.replace("syn://cog/", "syn://thing/", 1)
            fingerprint = TypedCogValue(request.type_ref, request.value).fingerprint
            size = len(canonical_json(request.value).encode("utf-8"))
            if (
                spec.job_type != COG_JOB
                or spec.selected_models
                or request.operation_ref.replace("synny://", "syn://") != expected_ref
                or fingerprint != candidate.input_fingerprint
                or size > selected.operation.max_input_bytes
            ):
                raise InvalidInputError("Checked request differs from the typed admitted leaf")
            leaves.append(
                {
                    "node_id": candidate.task_id,
                    "sort_order": positions[candidate.task_id],
                    "operation_name": candidate.operation_name,
                    "cog_ref": candidate.cog_ref,
                    "binding_hash": candidate.binding_hash,
                    "input_fingerprint": candidate.input_fingerprint,
                    "operation_id": binding.operation_id,
                    "check_job_id": binding.check_job_id,
                    "request": spec.to_dict(),
                    "dependencies": list(candidate.dependencies),
                    "lock_keys": list(candidate.lock_keys),
                }
            )
        document = {
            "schema": "potbot-atomic-graph/v1",
            "objective": planner.graph.nodes["root"].description,
            "max_parallel": planner.max_parallel,
            "structure": structure,
            "leaves": [
                {**leaf, "check_job_id": str(leaf["check_job_id"])} for leaf in leaves
            ],
        }
        if len(canonical_json(document).encode("utf-8")) > 262_144:
            raise InvalidInputError("Atomic graph manifest exceeds the durable bound")
        return document, leaves

    def admit(
        self,
        context: WorkspaceContext,
        graph_id: UUID,
        planner: AtomicAdmissionPlanner,
        bindings: Mapping[str, AtomicLaunchBinding],
    ) -> dict[str, Any]:
        """One transaction for all leaf/check identities; ID is the replay key.

        Registration is not dispatch. Each check must already have an observed
        success, and the later workbench launch must revalidate its current
        signed approval and installation. A failed check never joins a graph.
        """
        from psycopg.types.json import Jsonb

        graph_id = self._ident(graph_id)
        document, leaves = self._manifest(planner, bindings)
        digest = content_hash(document)
        with self.database.transaction(context) as conn:
            prior = conn.execute(
                "SELECT manifest_hash,state FROM atomic_graph_runs "
                "WHERE workspace_id=%s AND id=%s AND principal_id=%s FOR SHARE",
                (context.workspace_id, graph_id, context.principal_id),
            ).fetchone()
            if prior is not None:
                if prior["manifest_hash"] != digest:
                    raise StateMismatchError("Atomic graph replay changed the admitted manifest")
                return {"graph_id": str(graph_id), "state": prior["state"], "replayed": True}
            checks = conn.execute(
                "SELECT id,job_type,state,owner_principal_id,episode_id,contract_revision_id,input "
                "FROM jobs WHERE workspace_id=%s AND owner_principal_id=%s "
                "AND id=ANY(%s) ORDER BY id FOR UPDATE",
                (
                    context.workspace_id,
                    context.principal_id,
                    [leaf["check_job_id"] for leaf in leaves],
                ),
            ).fetchall()
            indexed = {row["id"]: row for row in checks}
            if len(indexed) != len(leaves):
                raise ScopeDeniedError("A checked atomic operation is unavailable")
            for leaf in leaves:
                check = indexed[leaf["check_job_id"]]
                if check["job_type"] != CHECK_JOB or check["state"] != "succeeded":
                    raise ScopeDeniedError("Atomic admission requires an observed successful check")
                checked_op, checked_spec = parse_check_input(check["input"])
                spec = JobSpec.from_dict(leaf["request"])
                if (
                    checked_op != leaf["operation_id"]
                    or checked_spec.to_dict() != spec.to_dict()
                    or check["episode_id"] != spec.episode_id
                ):
                    raise ScopeDeniedError("Atomic launch differs from its checked request")
            # The successful check rows are held FOR UPDATE above, the same
            # order used by OperationWorkbench.launch. A simultaneous launch
            # cannot slip between this check and graph registration.
            used = conn.execute(
                "SELECT 1 FROM jobs WHERE workspace_id=%s AND owner_principal_id=%s "
                "AND operation_check_id=ANY(%s) LIMIT 1",
                (context.workspace_id, context.principal_id,
                 [leaf["check_job_id"] for leaf in leaves]),
            ).fetchone()
            if used is not None:
                raise StateMismatchError("Atomic admission cannot reuse an already launched check")
            created = conn.execute(
                "INSERT INTO atomic_graph_runs(workspace_id,id,principal_id,objective,structure,"
                "manifest_hash,max_parallel) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (workspace_id,id) DO NOTHING RETURNING id",
                (
                    context.workspace_id,
                    graph_id,
                    context.principal_id,
                    document["objective"],
                    Jsonb({"schema": document["schema"], "nodes": document["structure"]}),
                    digest,
                    planner.max_parallel,
                ),
            ).fetchone()
            if created is None:
                existing = conn.execute(
                    "SELECT manifest_hash,state FROM atomic_graph_runs "
                    "WHERE workspace_id=%s AND id=%s AND principal_id=%s",
                    (context.workspace_id, graph_id, context.principal_id),
                ).fetchone()
                if existing is None:
                    raise ScopeDeniedError("Atomic graph belongs to another owner")
                if existing["manifest_hash"] != digest:
                    raise StateMismatchError("Atomic graph replay changed the admitted manifest")
                return {"graph_id": str(graph_id), "state": existing["state"], "replayed": True}
            for leaf in leaves:
                conn.execute(
                    "INSERT INTO atomic_graph_nodes (workspace_id,graph_id,principal_id,"
                    "node_id,sort_order,operation_name,cog_ref,binding_hash,input_fingerprint,"
                    "operation_id,check_job_id,request,dependencies,lock_keys) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        context.workspace_id,
                        graph_id,
                        context.principal_id,
                        leaf["node_id"],
                        leaf["sort_order"],
                        leaf["operation_name"],
                        leaf["cog_ref"],
                        leaf["binding_hash"],
                        leaf["input_fingerprint"],
                        leaf["operation_id"],
                        leaf["check_job_id"],
                        Jsonb(leaf["request"]),
                        leaf["dependencies"],
                        leaf["lock_keys"],
                    ),
                )
        return {"graph_id": str(graph_id), "state": "active", "replayed": False}

    def _read(
        self, context: WorkspaceContext, graph_id: UUID
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        graph_id = self._ident(graph_id)
        with self.database.transaction(context) as conn:
            graph = conn.execute(
                "SELECT id,objective,max_parallel,state,manifest_hash,created_at,interrupted_at "
                "FROM atomic_graph_runs WHERE workspace_id=%s AND id=%s AND principal_id=%s",
                (context.workspace_id, graph_id, context.principal_id),
            ).fetchone()
            if graph is None:
                raise ScopeDeniedError("Atomic graph is unavailable")
            rows = conn.execute(
                "SELECT n.node_id,n.sort_order,n.operation_name,n.operation_id,n.check_job_id,"
                "n.dependencies,n.lock_keys,n.request,n.job_id,j.state AS job_state,"
                "j.error_code,j.dispatched_at,"
                "(SELECT count(*) FROM atomic_graph_resource_holds h "
                " WHERE h.workspace_id=n.workspace_id AND h.job_id=n.job_id AND h.active) "
                " AS resource_holds "
                "FROM atomic_graph_nodes n LEFT JOIN jobs j "
                "ON j.workspace_id=n.workspace_id AND j.id=n.job_id "
                "WHERE n.workspace_id=%s AND n.graph_id=%s AND n.principal_id=%s "
                "ORDER BY n.sort_order,n.node_id",
                (context.workspace_id, graph_id, context.principal_id),
            ).fetchall()
            owner_holds = conn.execute(
                "SELECT lock_key FROM atomic_graph_resource_holds "
                "WHERE workspace_id=%s AND principal_id=%s AND active",
                (context.workspace_id, context.principal_id),
            ).fetchall()
        result = dict(graph)
        result["owner_held_locks"] = tuple(item["lock_key"] for item in owner_holds)
        return result, [dict(row) for row in rows]

    @staticmethod
    def _ready_rows(
        graph: dict[str, Any], rows: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], ...]:
        if graph["state"] != "active":
            return ()
        states = {row["node_id"]: row["job_state"] for row in rows}
        active = [row for row in rows if row["job_state"] in _ACTIVE]
        slots = max(0, graph["max_parallel"] - len(active))
        locks = set(graph.get("owner_held_locks", ()))
        locks.update(key for row in active for key in row["lock_keys"])
        ready = []
        for row in rows:
            if not slots:
                break
            if row["job_id"] is not None:
                continue
            if any(states.get(dep) != "succeeded" for dep in row["dependencies"]):
                continue
            if locks.intersection(row["lock_keys"]):
                continue
            ready.append(row)
            locks.update(row["lock_keys"])
            slots -= 1
        return tuple(ready)

    def ready(self, context: WorkspaceContext, graph_id: UUID) -> tuple[dict[str, str], ...]:
        """A read-only hint. SQL rechecks every condition on actual job insertion."""
        graph, rows = self._read(context, graph_id)
        return tuple(
            {"node_id": row["node_id"], "operation_id": row["operation_id"]}
            for row in self._ready_rows(graph, rows)
        )

    def launch_ready(
        self,
        context: WorkspaceContext,
        graph_id: UUID,
        *,
        setup_token: str,
        limit: int = 8,
    ) -> tuple[dict[str, Any], ...]:
        """Launch checked eligible leaves via existing signed workbench jobs.

        A peer may win a concurrent launch after this advisory read. The SQL
        gate rejects the stale attempt; caller reconciles by inspecting the
        linked graph. No automatically retried external effect can result.
        """
        if type(limit) is not int or not 1 <= limit <= 64:
            raise InvalidInputError("Invalid atomic launch batch limit")
        graph_id = self._ident(graph_id)
        graph, rows = self._read(context, graph_id)
        selected = self._ready_rows(graph, rows)[:limit]
        launched = []
        for row in selected:
            # Check and launch authorization is verified AGAIN inside the existing
            # workbench transaction. Never mint a job or invoke a handler here.
            result = self.workbench.launch(
                context,
                row["operation_id"],
                row["check_job_id"],
                row["request"],
                f"atomic-{graph_id.hex}-{row['node_id']}",
                setup_token=setup_token,
            )
            linked = self._read(context, graph_id)[1]
            current = next(item for item in linked if item["node_id"] == row["node_id"])
            if str(current["job_id"]) != result["job"]["id"]:
                raise StateMismatchError("Checked job was not atomically bound to its atomic leaf")
            launched.append({"node_id": row["node_id"], "job": result["job"]})
        return tuple(launched)

    def inspect(self, context: WorkspaceContext, graph_id: UUID) -> dict[str, Any]:
        graph, rows = self._read(context, graph_id)
        eligible = {row["node_id"] for row in self._ready_rows(graph, rows)}
        statuses = {row["node_id"]: row["job_state"] for row in rows}
        public = []
        for row in rows:
            state = row["job_state"]
            if state is None:
                if graph["state"] == "interrupted":
                    state = "interrupted"
                elif any(statuses.get(dep) in _FAILED for dep in row["dependencies"]):
                    state = "blocked_dependency"
                elif row["node_id"] in eligible:
                    state = "ready"
                else:
                    state = "waiting"
            public.append(
                {
                    "node_id": row["node_id"],
                    "operation_name": row["operation_name"],
                    "dependencies": list(row["dependencies"]),
                    "job_id": str(row["job_id"]) if row["job_id"] is not None else None,
                    "state": state,
                    "error_code": row["error_code"],
                    "possible_effect": row["dispatched_at"] is not None,
                    "resource_holds": int(row["resource_holds"]),
                }
            )
        job_states = [row["job_state"] for row in rows]
        outcome = (
            "interrupted" if graph["state"] == "interrupted"
            else "succeeded" if all(state == "succeeded" for state in job_states)
            else "needs_reconciliation" if "indeterminate" in job_states
            else "running" if any(state in _ACTIVE for state in job_states)
            else "blocked" if any(state in _FAILED for state in job_states)
            else "ready" if eligible else "waiting"
        )
        return {
            "schema": "potbot-atomic-graph-status/v1",
            "graph_id": str(graph["id"]),
            "state": graph["state"],
            "outcome": outcome,
            "max_parallel": graph["max_parallel"],
            "manifest_hash": graph["manifest_hash"],
            "nodes": public,
        }

    def interrupt(self, context: WorkspaceContext, graph_id: UUID) -> dict[str, Any]:
        """Fence future dispatch before issuing cancellation on linked jobs.

        In-flight work already dispatched can remain uncertain. Another call
        can reconcile/renew cancellation requests after a transient failure.
        """
        graph_id = self._ident(graph_id)
        with self.database.transaction(context) as conn:
            graph = conn.execute(
                "SELECT id,state FROM atomic_graph_runs WHERE workspace_id=%s AND id=%s "
                "AND principal_id=%s FOR UPDATE",
                (context.workspace_id, graph_id, context.principal_id),
            ).fetchone()
            if graph is None:
                raise ScopeDeniedError("Atomic graph is unavailable")
            if graph["state"] == "active":
                conn.execute(
                    "UPDATE atomic_graph_runs SET state='interrupted',"
                    "interrupted_at=clock_timestamp() WHERE workspace_id=%s AND id=%s",
                    (context.workspace_id, graph_id),
                )
            jobs = conn.execute(
                "SELECT node_id,job_id FROM atomic_graph_nodes WHERE workspace_id=%s "
                "AND graph_id=%s AND principal_id=%s AND job_id IS NOT NULL",
                (context.workspace_id, graph_id, context.principal_id),
            ).fetchall()
        outcomes = []
        for row in jobs:
            try:
                job = self.jobs.cancel(
                    context, row["job_id"],
                    f"atomic-stop-{graph_id.hex}-{row['node_id']}",
                )
                outcomes.append({"node_id": row["node_id"], "job_id": str(row["job_id"]),
                                 "state": job["state"]})
            except Exception as exc:
                # Never report cancellation if the underlying job owner could
                # not confirm it. The dispatch fence remains committed.
                outcomes.append({"node_id": row["node_id"], "job_id": str(row["job_id"]),
                                 "state": "cancellation_unconfirmed", "error_type": type(exc).__name__})
        return {"graph_id": str(graph_id), "state": "interrupted", "jobs": outcomes}

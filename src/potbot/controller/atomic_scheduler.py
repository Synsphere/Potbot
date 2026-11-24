"""R7.2 deterministic atomic-leaf *admission and scheduling plans*.

No model output can authorize a Cog. The caller provides installed signatures,
controller-authorized operation bindings and typed inputs; the execution owner
must independently revalidate these snapshots and atomically claim the work in
its PostgreSQL ledger before dispatch. This planner never starts a process,
consumes a budget, or treats an in-memory claim as a durable lease.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from potbot.cogs.contracts import CogSignature, TypedCogValue, check_cog_input
from potbot.controller.atomic_tasks import AtomicOperation, TaskGraph
from potbot.domain.errors import InvalidInputError, StateMismatchError


@dataclass(frozen=True, slots=True)
class RegisteredLeaf:
    """Controller-authorized binding, not a model-created operation."""

    operation: AtomicOperation
    signature: CogSignature
    lock_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.operation, AtomicOperation) or not isinstance(
            self.signature, CogSignature
        ):
            raise InvalidInputError("registered atomic leaf requires a Cog binding")
        if (
            not isinstance(self.lock_keys, tuple)
            or len(self.lock_keys) > 32
            or len(set(self.lock_keys)) != len(self.lock_keys)
            or any(
                not isinstance(key, str)
                or not 1 <= len(key.encode("utf-8")) <= 128
                or "\x00" in key
                for key in self.lock_keys
            )
        ):
            raise InvalidInputError("invalid atomic resource lock keys")


@dataclass(frozen=True, slots=True)
class ProposedLeaf:
    """Model-proposed, typed action. Registration and scope are NOT inferred."""

    task_id: str
    operation_name: str
    value: TypedCogValue

    def __post_init__(self) -> None:
        if (
            not isinstance(self.task_id, str)
            or not self.task_id
            or not isinstance(self.operation_name, str)
            or not self.operation_name
            or not isinstance(self.value, TypedCogValue)
        ):
            raise InvalidInputError("invalid proposed atomic leaf")


@dataclass(frozen=True, slots=True)
class DispatchCandidate:
    """Verified plan for a separate, durable dispatch owner to atomically claim."""

    task_id: str
    operation_name: str
    cog_ref: str
    binding_hash: str
    input_fingerprint: str
    lock_keys: tuple[str, ...]
    dependencies: tuple[str, ...]


class AtomicAdmissionPlanner:
    """Bounded graph admission with deterministic, resource-aware ready sets.

    This layer intentionally has no authorization to dispatch. A PostgreSQL
    owner must transactionally lock the graph and resource keys, recheck lease,
    budget, permissions, exact installed Cog snapshot, and record an invocation
    before handing a candidate to a qualified worker. A crash after a possible
    invocation requires reconciliation, never blind retry.
    """

    def __init__(
        self,
        graph: TaskGraph,
        registry: Mapping[str, RegisteredLeaf],
        *,
        max_parallel: int = 8,
    ) -> None:
        if not isinstance(graph, TaskGraph):
            raise InvalidInputError("atomic planner requires a bounded TaskGraph")
        if type(max_parallel) is not int or not 1 <= max_parallel <= 64:
            raise InvalidInputError("invalid atomic concurrency bound")
        if not isinstance(registry, Mapping) or len(registry) > 256:
            raise InvalidInputError("invalid registered atomic operation catalogue")
        entries = dict(registry)
        if any(
            not isinstance(name, str)
            or not isinstance(binding, RegisteredLeaf)
            or binding.operation.name != name
            for name, binding in entries.items()
        ):
            raise InvalidInputError("operation registry must use exact registered names")
        self.graph = graph
        self.registry = entries
        self.max_parallel = max_parallel
        self._admitted: dict[str, DispatchCandidate] = {}

    def admit(
        self,
        proposals: tuple[ProposedLeaf, ...],
        *,
        approved_nodes: frozenset[str] = frozenset(),
    ) -> None:
        """Validate the ENTIRE batch before changing the graph.

        Admission is not permission to execute. The caller must still supply
        authorization from the durable operation owner when claiming the plan.
        """
        if not isinstance(approved_nodes, frozenset) or any(
            not isinstance(node_id, str) for node_id in approved_nodes
        ):
            raise InvalidInputError("approval identities must be controller-supplied")
        if not isinstance(proposals, tuple) or not 1 <= len(proposals) <= 128:
            raise InvalidInputError("invalid atomic proposal batch")
        pending: dict[str, tuple[ProposedLeaf, RegisteredLeaf, DispatchCandidate]] = {}
        for proposal in proposals:
            if not isinstance(proposal, ProposedLeaf):
                raise InvalidInputError("untyped atomic proposal")
            node = self.graph.nodes.get(proposal.task_id)
            binding = self.registry.get(proposal.operation_name)
            if (
                node is None
                or node.state != "needs_decomposition"
                or binding is None
                or proposal.task_id in pending
                or proposal.task_id in self._admitted
            ):
                raise InvalidInputError("unknown, duplicate or previously admitted leaf")
            check_cog_input(binding.signature, proposal.value)
            from potbot.domain.serialization import canonical_json

            size = len(canonical_json(proposal.value.value).encode("utf-8"))
            if size > binding.operation.max_input_bytes or (
                binding.operation.requires_approval and proposal.task_id not in approved_nodes
            ):
                raise InvalidInputError("atomic leaf exceeds limits or lacks approval")
            candidate = DispatchCandidate(
                task_id=proposal.task_id,
                operation_name=binding.operation.name,
                cog_ref=str(binding.signature.cog_ref),
                binding_hash=binding.signature.binding_hash,
                input_fingerprint=proposal.value.fingerprint,
                lock_keys=binding.lock_keys,
                dependencies=node.depends_on,
            )
            pending[proposal.task_id] = (proposal, binding, candidate)
        # All checks above must complete before the first mutation.
        for task_id, (proposal, binding, candidate) in pending.items():
            self.graph.admit_leaf(
                task_id,
                operation=binding.operation,
                estimated_input_bytes=len(canonical_json(proposal.value.value).encode("utf-8")),
                approved=task_id in approved_nodes,
            )
            self._admitted[task_id] = candidate

    def admitted(self) -> tuple[DispatchCandidate, ...]:
        """Immutable ordered snapshot for the separate checked execution owner.

        This is not a dispatch authorization or a durable worker claim.
        """
        return tuple(self._admitted[key] for key in self.graph.nodes if key in self._admitted)

    def ready(
        self,
        *,
        completed: frozenset[str],
        in_flight: frozenset[str] = frozenset(),
        held_locks: frozenset[str] = frozenset(),
        interrupted: bool = False,
    ) -> tuple[DispatchCandidate, ...]:
        """Produce a stable independent ready set; never dispatch it here."""
        if type(interrupted) is not bool:
            raise InvalidInputError("invalid interrupt flag")
        if any(not isinstance(items, frozenset) for items in (completed, in_flight, held_locks)):
            raise InvalidInputError("ready-set state must be immutable")
        valid_ids = set(self.graph.nodes)
        if not completed <= valid_ids or not in_flight <= valid_ids or completed & in_flight:
            raise StateMismatchError("ready-set state is inconsistent with the graph")
        if interrupted:
            return ()
        reserved = set(held_locks)
        for node_id in in_flight:
            candidate = self._admitted.get(node_id)
            if candidate is None:
                raise StateMismatchError("in-flight task lacks an admitted binding")
            reserved.update(candidate.lock_keys)
        slots = max(0, self.max_parallel - len(in_flight))
        if slots == 0:
            return ()
        result: list[DispatchCandidate] = []
        for task_id in self.graph.executable(set(completed)):
            if task_id in in_flight:
                continue
            candidate = self._admitted.get(task_id)
            if candidate is None:
                raise StateMismatchError("ready leaf has no admitted binding")
            if not reserved.isdisjoint(candidate.lock_keys):
                continue
            result.append(candidate)
            reserved.update(candidate.lock_keys)
            if len(result) >= slots:
                break
        return tuple(result)

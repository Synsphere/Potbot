# R7.2-A: deterministic atomic leaf admission and dispatch planning

This increment bridges the 41-kind R7 taxonomy and bounded `TaskGraph` to exact
Cog signatures without creating a second execution authority.

`AtomicAdmissionPlanner` takes an operator-supplied registry of `RegisteredLeaf`
entries. A proposed leaf is not allowed to invent a Cog signature, input data
type, installed revision, resource lock, approval, or executor. Admission checks
the **entire** proposed batch before mutating the graph, using
operator-supplied approval identities; a proposed leaf cannot self-approve. Every resulting
`DispatchCandidate` retains the exact Cog binding hash, typed input fingerprint,
dependencies and resource locks. Independent ready leaves are returned in stable
graph order while respecting dependency completion, held locks, in-flight work,
interrupt fences and a finite concurrency limit.

A dispatch candidate **is not** an executable permit, a PostgreSQL reservation,
a worker claim, or a verified operation. The existing task/operation owner must
recheck the currently installed signature, the authorizing principal, the
current run lease, permissions, quotas and task budget under a database
transaction, then atomically record the possible dispatch. Concurrent
processes cannot coordinate by sharing this in-memory planner.

## Next closure gates for R7.2-B

- Persist immutable admitted graph revision, node dependencies, input hashes,
  resource keys and operation bindings in the existing workspace-scoped
  PostgreSQL authority with RLS and unique claims.
- Transactionally select independent eligible nodes using row/resource locks;
  recheck R7.1 interrupts/context epochs and task-bound budget/permissions.
- Record dispatch IDs and exact input receipts before any side effect. On
  uncertain outcomes, require reconciliation rather than redispatch.
- Integrate with existing Cog operation workbench and job workers, explicit
  cancellations, human approvals, and scheduler events.
- Verify concurrent workers, stale leases, SIGKILL windows, duplicate plans,
  conflicting locks, budget exhaustion and per-task ownership on real PostgreSQL.
- Collect installed-host and live Cog qualification separately. Syn-related
  changes remain deferred until a release is ready.

No production scheduler qualification is claimed by the R7.2-A tests.

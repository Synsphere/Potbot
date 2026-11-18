# R7.2-B — Durable atomic graph admission and checked job scheduling

**Baseline:** `8a23cceed525ea91d8265e69f63f49efdd2948c7` (`7.2`, after R7.2-A).
**Schema:** migration **49** (`atomic_graph_schema`).
**Scope:** checked native Cog leaves only; no new Syn release work, no silent model execution.

## Architecture and ownership

The R7.2-A `AtomicAdmissionPlanner` remains non-executing. A controller must
bind each proposed leaf to one installed `CogSignature`, typed request, registered
bounded operation, and successful **existing** `operation.check` job. The
`AtomicGraphService.admit` transaction persists an immutable, source-linked DAG,
exact launch requests and signed-check identities. The graph UUID and manifest
hash define idempotent registration; changed replay conflicts rather than
rewriting state. Parent/decomposition records are retained in the structure.

`AtomicGraphService.ready` produces an advisory deterministic batch. The only
launch entry point is `AtomicGraphService.launch_ready`, which delegates to
`OperationWorkbench.launch` with its usual setup token, signed check report,
expiry and installation validation. `JobService.submit` creates the **same**
`cog.execute` job as a manual checked launch. Migration 49's `AFTER INSERT` jobs
trigger **atomically** verifies the graph and associates the new job with its
leaf, inside the same transaction that records the checked operation and
`job.queued` event. Concurrent schedulers can propose the same work, but only
one checked job can commit. The trigger validates:

1. Immutable request/job type, original selected Cog check and single-issue identity.
2. All predecessor leaf jobs have actually **succeeded**, not merely finished.
3. No leaf already has a job; the graph is not interrupted.
4. The graph has spare queued/running/cancelling slots.
5. Exclusive resource locks cannot conflict with any active atomic graph job
   in the same **workspace**, even when its owner differs. The partial unique
   index on `(workspace_id,lock_key) WHERE active` is the global gate.

Resource holds are released by the **existing job** on observed
`succeeded`/`failed`/`cancelled` transitions. An `indeterminate` job keeps its
resource holds active, because an orphaned effect cannot safely be declared
finished. There is deliberately no model-command or blind retry that releases
an unknown effect; this needs independent operator reconciliation. Existing
resource-limits, host qualification, Cog grants, task budgets and settlement
remain with `JobService.mark_dispatched` and the native dispatcher.

`AtomicGraphService.interrupt` first commits a graph-wide dispatch fence and
then requests cancellation through `JobService.cancel`. Migration 49 checks
this fence on the exact `dispatched_at` transition. An effect already dispatched
can still be uncertain and is not described as never started. On interruption,
repeating the call retries any unconfirmed cancellation requests without
clearing the dispatch fence.

`inspect` reports the actual job states, dependency blocks, retained possible
external effects, lock holds and immutable plan hash; it **never** reports an
unobserved successful effect. Database row-level security enforces both
workspace membership and the owning principal on the plan tables.

## Integration example (trusted controller, not model-generated Python)

```python
from uuid import uuid4
from potbot.controller.atomic_graph_runtime import AtomicGraphService, AtomicLaunchBinding

# graph: host-validated TaskGraph; planner: AtomicAdmissionPlanner with
# each leaf already admitted using exact RegisteredLeaf signatures.
# checks: per-leaf successful operation.check jobs from OperationWorkbench.check.
service = AtomicGraphService(database)
identity = uuid4()
service.admit(actor, identity, planner, {
    node_id: AtomicLaunchBinding(operation_id=check.operation_id,
                                 check_job_id=check.job_id,
                                 request=check.exact_request)
    for node_id, check in checks.items()
})

# An operator-owned scheduler tick, authenticated with the same setup token.
service.launch_ready(actor, identity, setup_token=current_operator_token)
# Later ticks read committed job outcomes and release independent successors.
print(service.inspect(actor, identity))
```

This is a **trusted Python integration interface**, not a new public endpoint.
No model controls the setup token, checked operation identifier, or signed
launch. A deployed controller must explicitly configure its scheduler tick
and operator credential handling; R7.2-B does not install a new autonomous
background process or open a public dispatch endpoint.

## Acceptance / limitations

- Unit: typed request and Cog identity mismatch, duplicate checks, request
  mutation, native wall bounds, migration triggers and release fence.
- PostgreSQL: duplicate launch, failed dependency, two simultaneous schedulers,
  workspace-wide exclusive lock, interrupted ready work, uncertain effect holds,
  idempotent graph replay and cross-principal visibility.
- Production: run existing **real** operation workbench check-and-launch,
  installed-host native worker, grants, budgets and stop/expiry suites.
  Direct SQL synthetic check receipts in tests exercise database enforcement
  only; they **do not** qualify a Syn installation or provider.
- Recovery: `indeterminate` effects are retained and never dispatched again.
  Resource unlock after independently proven reconciliation, automatic scheduler
  ticks, UI/API exposure, and specialist model benchmark qualification are
  future independently tested work. Syn upgrades remain deferred until release.

After this migration, the next optimization milestone is **R7.3**, paired
role-specific specialist benchmarks. That work must not treat unqualified local
model weights, synthetic checks or advisory task classes as live deployments.

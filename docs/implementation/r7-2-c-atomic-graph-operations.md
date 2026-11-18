# R7.2-C: bounded atomic graph pump and inspection

This increment connects the already admitted R7.2-A/B graph to an **explicitly
started** polling scheduler and private operation workbench. This is not a
new task executor, Cog registry or automatic model permission grant.

- `potbot atomic-graphs list/inspect` reads workspace/principal-owned records.
- `potbot atomic-graphs tick` starts a single, bounded scheduler pass after
  resolving the operator's existing setup capability from the native vault.
- `potbot atomic-graphs worker` opt-in loops over bounded pages and carries a
  deterministic cursor so a busy or blocked graph does not starve later graphs.
  Stop signals stop **new** submission, not an already dispatched Cog.
- `potbot atomic-graphs interrupt --confirm` commits a new-dispatch fence first,
  and then sends existing JobService cancellation requests. In-flight effects
  may require reconciliation; no result is silently assumed cancelled.
- The authenticated `/v1/ui/atomic-graphs` endpoint lists and inspects the
  same graph state; its confirmed POST `tick`/`interrupt` actions call the same
  owners. The Next.js BFF limits paths, body sizes, workspace identity and
  exposed fields and uses the operator setup token on its server only.
- `/atomic-graphs` presents exact child states, dependencies, linked job IDs,
  possible effects, and held resource locks. Polling is bounded. On uncertain
  response the UI disables automatic resubmission.

No scheduler iteration can launch an unchecked operation: the existing workbench
rechecks the original signed result, active task policy, installed capability,
resource limits and PostgreSQL migration-49 locks at job insertion. Concurrent
agents racing for the same leaf must not create a duplicate external effect.
The owner keeps resource holds for indeterminate jobs; the pump has no hidden
retry or admin reconciliation shortcut.

## Installation and qualification

Apply migration 49 before running. Use an existing restricted workspace principal
and an explicitly configured `api.setup_token` in the native vault. Start the
ordinary Potbot API/worker service with the signed Cog installation configured;
start the optional scheduler in a separate terminal:

```bash
potbot atomic-graphs --workspace-id UUID --principal-id UUID list
potbot atomic-graphs --workspace-id UUID --principal-id UUID worker --interval 2
```

The worker requires the existing checked operations and installed Cog worker;
without them it cannot confer execution eligibility. The GUI is available at
`/atomic-graphs` on the existing frontend, not a new listener.

**Release gates outstanding:** real concurrent workers under PostgreSQL, installed
host/Cog qualification, cancellation and SIGKILL fault injection, and actual
browser acceptance. Offline tests and typed BFF checks alone are not proof of
live qualification. Syn release-dependent integration remains deferred.

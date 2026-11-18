# R7.1 durable temporal steering

**Implementation baseline:** merged main
`2fdce72e5f037c883d67b53838e45ff473eb40be`, reviewed 18 November 2026.

R7.1 connects the existing rule-first steering classifier to the PostgreSQL
reasoning inbox, fenced run, task-budget journal and single reasoning owner.
It implements the current-goal temporal control slice of
[R7](24-atomic-task-routing-and-specialists.md) and U08-T. No new Syn release,
task scheduler, worker queue or independent permission store is introduced.

## Operator behavior

The existing authenticated endpoint remains:

```text
POST /v1/reasoning/{run_id}/steer
{"request_id":"operator-unique-key","prompt":"USER MESSAGE"}

GET /v1/reasoning/{run_id}/steering/{request_id}
GET /v1/reasoning/{run_id}/steering
GET /v1/reasoning/{run_id}
GET /v1/reasoning/{run_id}/events
GET /v1/reasoning/{run_id}/invocations
```

Workspace membership and the existing request-body/authentication rules remain
authoritative. The original approving principal is retained with every new
request; readback does not relabel that source as the current reader.

| Component | Admission and execution |
| --- | --- |
| `interrupt` | The request transaction fences subsequent dispatch immediately. The owner acknowledges it without another model call and ends this reasoning run as `interrupted`. |
| `deferred`, current goal | Captures the exact operation active at intake. Only a confirmed successful predecessor permits graph admission at a checkpoint. If the owner was idle, the retained checkpoint is the predecessor. |
| `deferred`, next goal | Explicit next-task/after-completion requests retain a dependency on the entire current run. They are not reinterpreted as current hypotheses or automatically launched. |
| `background` | Retains literal source-linked context. New material context revises assumptions; repeated identical text retains another source without another material revision. |
| `clarify` | Retains the question and an explicit clarification outcome without dispatch. |

For example, `FYI, only 2GB RAM; stop now; add edge-case handling` yields three
independently identified components. The fact is retained, subsequent dispatch
is fenced, and the deferred instruction is left unexecuted. A mixed message is
never silently reduced to just its highest-priority label.

The classifier handles separate sentences, lines, semicolons and explicit
conjunctions. It protects quoted examples, inline code and code fences, and
records exact UTF-8 source spans and hashes. This is a bounded syntax contract,
not a claim of unrestricted natural-language parsing. Ambiguous free-form
requests remain explicit; a model is not used to decide an urgent stop.

## What an acknowledgement means

The response adds immutable `components`, original `principal_id`, append-only
`component_results`, and an `acknowledgement`. `dispatch_fenced: true` means
future calls in this run cannot pass admission. It does **not** mean a remote
request already sent has stopped. `in_flight_stop_confirmed` remains false
unless a separate executor can establish such evidence; this increment makes
no such claim.

Model transports receive a cancellation callback that also observes the
durable stop flag. A transport that cannot interrupt safely may finish its
already admitted call. Its actual completion and usage are retained before the
run stops. A transport that fails without a terminal receipt remains
indeterminate. Evidence calls use the same admission fence; there is no false
claim that every external evidence adapter supports in-flight cancellation.

Non-streaming inference reports HTTP 409 with `reasoning_interrupted` for this
terminal condition. A stream whose headers were already sent reports that code
in its final error event and emits `[DONE]`; it does not send a fabricated
successful answer.

## Dispatch and context ordering

Migration **48**, `reasoning_temporal_schema`, extends the existing run and inbox
tables. `reasoning_runtime_dispatches` records possible-dispatch boundaries and
their observations. It is an execution receipt table, not a work queue.

Steering intake and each model/evidence admission lock the same run row. A call
admitted before a concurrent stop is an existing possible effect. A call whose
admission follows the accepted stop is rejected. The dispatch also requires the
exact checkpointed node claim, live lease and context epoch. A second active
dispatch is rejected until the prior observation is retained.

Task-bound calls retain the existing budget owner. Its dispatch transaction
also checks the stop flag under the run lock. A confirmed unsent reservation is
released. A possibly sent call without usage keeps its original hold; changing
the steering state never refunds uncertain work or silently retries it.

Background context is included as literal user data in the next compiled prompt,
with source identity, source hash, context epoch and task-understanding revision.
It is explicitly not independently verified evidence. Conflicting source facts
remain separate records. A background update racing prompt compilation forces
a fresh undispatched prompt; only confirmed unsent attempts may be rebuilt.
Four consecutive context races exhaust that bounded admission attempt rather
than permit an old prompt to slip through.

Generated hypotheses retain their proposal revision. Questions produced before
a material context update cannot acquire a current revision merely because
their investigator node was created later. A fresh generator may reassess an
unexecuted duplicate proposal; otherwise stale questions are marked
`context_reassessment_required` and do not dispatch. The critic and synthesizer
receive the retained facts and the actual missing-evidence status.

Background information alone creates no hypothesis, new task or authorization.
The same registered tool authority and provider disclosure policy still govern
every admitted operation.

## Dependencies and recovery

Current-goal deferred components name an immutable dispatch UUID or checkpoint.
The controller and the transactional component-decision owner both enforce that
dependency. A failed, indeterminate or confirmed unstarted predecessor cannot
release deferred execution. An explicit next-goal component names the run and
becomes eligible for a separately admitted goal only after successful run
completion. R7.2 owns that subsequent task scheduling; R7.1 retains the request
and its dependency without claiming to have performed it.

Duplicate submission of the same original body and request ID returns its
retained receipt, including after the run ends. Reusing a key for different text
fails. A database trigger prevents source mutation and replacement of recorded
component outcomes. Fenced owner decisions cannot be published with an expired
or substituted lease. Legacy pre-migration requests remain readable and retain
their existing interpretation.

The existing run-recovery rule remains: interrupted or expired calls are never
automatically replayed. Inspect the run, component outcomes, event history and
budget invocations before creating a newly admitted run. There is no implicit
`resume` of an interrupted process. A pending next-goal component is evidence of
an unperformed request, not an execution receipt.

The existing file-only steering prototype is unchanged. Durable R7.1 behavior
requires the production PostgreSQL inbox and run store configured together, as
the service launcher already does. Apply migration 48 before starting the
updated API and reasoning workers.

## Bounds and qualification

Intake accepts 1–8192 UTF-8 bytes and at most 16 literal components per request.
A run retains at most 128 ordinary requests and at most 128 admitted background
components / 32768 background text bytes. One additional urgent stop can be
admitted after the ordinary request capacity is exhausted. If a mixed stop also
exceeds background capacity, the stop still commits and those new background
components carry explicit blocked outcomes. Existing pending next-goal requests
cannot hide a later stop behind a page boundary.

The dedicated workflow `.github/workflows/reasoning-r7-1.yml` runs deterministic
classification, tree, HTTP and regression checks plus real PostgreSQL
migration/RLS/lifecycle tests. Its fixture model transports and source adapters
are explicitly synthetic; no paid model or live vendor credentials are needed.

Acceptance covers mixed intent and exact Unicode spans; quoted/code examples;
idempotency and source immutability; independent API/owner transactions; expired
leases; stop before model and evidence admission; stop during a call; held versus
released task budgets; background races before dispatch and during hypothesis
generation; next-goal retention; and context-capacity emergency handling.

These tests establish the supported local controller behavior. They do not
qualify live provider cancellation, arbitrary natural-language parsing,
distributed atomic scheduling, installed-host capabilities, Syn activation or
the entire U08/R7 programme. Existing Syn release-dependent work and global
runtime qualification remain separate gates.

# U04-T selected task execution contract

U04-T consumes one retained U04-S operation selection through the existing
OperationWorkbench and durable jobs queue. A selection is never an execution
grant. Check and launch remain separate explicit operator decisions. Registry
and MCP discovery selections retain their existing destination owners.

## Public lifecycle

The schema is `potbot-task-execution/v1`. Every selection-scoped response has
`workspaceId`, `principalId`, `selectionId`, `selectionSha256`, `operationId`
(null for a non-operation selection), and the original `taskBinding`.

`TaskExecutionService.context(context, selection_id)` adds `selection` (the
existing U04-S selection projection), `operation` (the existing workbench
description, or null), `supported`, `current`, and bounded `reasonCodes`.
Reading this endpoint does not submit a job or initialize a native provider.

`check(context, selection_id, selection_sha256, request, acceptance, key,
setup_token=...)` returns the scoped envelope plus `binding` and `job`.
The HTTP request is exactly `{selectionSha256, request, acceptance, confirm}`,
with `confirm: true`. `request` is the existing strict JobSpec (16 KiB maximum).
`acceptance` is the bounded `potbot-task-result-oracle/v1` object with 1–16
literal RFC 6901 assertions (`equals`, `contains`, `length_equals`). The complete
HTTP body is limited to 128 KiB. A successful check remains a worker observation,
not authorization to launch.

`launch(context, selection_id, check_job_id, binding_sha256, key,
setup_token=...)` returns the scoped envelope plus `binding` and `job`. Its HTTP
body is exactly `{checkJobId, bindingSha256, confirm}`, with `confirm: true`.
The server retrieves the originally bound request and acceptance. It cannot
substitute caller-provided input at launch. The existing unique run-per-check
constraint applies. Exact idempotent retries recover committed jobs even when
their original check or current task authority has expired.

`jobs(context, selection_id, limit=25, after=None)` returns `items` containing
`{binding, job}`, `nextCursor` (the existing opaque job keyset cursor, at most
512 characters, or null), and `hasMore`. The maximum page
is 25. `inspect(context, selection_id, job_id)` returns `binding` and `job`.
Check and run job UUIDs are the resume identities; there is no new attempt queue.

`events(context, selection_id, job_id, after=0, limit=100)` adds the existing
job-event page (`items`, `next_cursor`, `has_more`). `cancel(context,
selection_id, job_id, key)` requires ordinary scoped ownership and explicit
confirmation at the HTTP boundary, not an operator setup token. Historical
reads and cancellation remain available after task or selection currentness is
lost. Event payloads and artifact ranges retain existing bounded projections.
Only a cancellation receipt may return `binding: null` when the retained positive
binding cannot be validated. The immutable owner/selection/job relationship is
still required to stop work. This exception cannot be used for a check, launch,
ordinary result verification or a positive acceptance outcome.

## Immutable execution binding

The existing jobs table retains an immutable `task_selection_id` foreign key
and optional `task_execution_binding`; legacy jobs keep both null. Check and
launch signatures include the binding digest. Launch copies the exact binding
from its check. This is provenance on the existing lifecycle, not another state
machine, registry, or scheduler.

`binding` has these exact fields:

| Field | Meaning |
| --- | --- |
| `schema` | `potbot-task-execution-binding/v1` |
| `workspaceId`, `principalId` | Exact submitting owner |
| `selectionId`, `selectionSha256` | Exact retained selection |
| `searchId`, `resultId`, `candidateId` | Existing discovery provenance |
| `operationId`, `ownerSha256` | Exact selected operation and owner projection |
| `consumerSha256` | Exact installed execution, admission and assertion implementation |
| `taskBinding` | Original U04-S task/contract/state/semantic release binding |
| `capabilities` | Selected canonical capability projection, never a grant |
| `request` | Strict original JobSpec |
| `requestSha256` | Canonical JobSpec digest |
| `acceptance` | Original bounded result oracle |
| `acceptanceSha256` | Canonical oracle digest |
| `sha256` | Canonical digest of all binding fields except this field |

`execution_binding(context, job_id)` is an internal verified historical binding
read for the result owner. It verifies the original retained operator signature
without requiring that its short admission expiry is still current. Loss or
rotation of that key prevents positive outcome verification. Ordinary inspect
and cancellation do not require the historical signature to be current.
The binding also pins the installed execution, worker admission and result
assertion implementations. A changed consumer prevents new dispatch and
positive reinterpretation of historical results; exact committed retries
still recover their original check or run.

Both the check worker and the launch worker revalidate the exact selection,
task binding, current owner, and canonical metadata. Native input/output ports
must match the worker's installed binding. The final existing
`JobService.mark_dispatched` admission also revalidates selection currentness
before changing the dispatch marker or reserving native execution budget.

## Result assertions

`result(context, selection_id, job_id)` returns the scoped envelope plus
`binding`, `result` (the existing Workbench result DTO), and `verification`.
`artifact(context, selection_id, job_id, offset=0, length=262144)` retains the
existing verified artifact-range shape under the exact selection/job join.

Verification schema `potbot-task-result-verification/v1` contains `status`
(`pending`, `passed`, `failed`, or `unavailable`), `bindingSha256`,
`acceptanceSha256`, nullable `outputSha256` and `evidenceSha256`, nullable
`resultKind` (`hosted_text` or `typed_cog`), `checks` (index, passed, reasonCode),
and bounded `reasonCodes`. Only complete owned output with verified execution
and accounting receipts is evaluated. A preview, success flag, client verdict,
or partial output cannot establish an assertion pass. The report does not
change global task status or claim independent real-host qualification.

The oracle sees `{"text": "complete output"}` for hosted text, and
`{"typeRef": "declared output type", "value": ...}` for a typed Cog result.
For example, a hosted literal containment check points to `/text`; a Cog
returning `{"checked": true}` uses an equality assertion at `/value/checked`.
Missing pointers and wrong JSON types fail the assertion. No expressions,
regular expressions, network lookups or executable predicates are accepted.

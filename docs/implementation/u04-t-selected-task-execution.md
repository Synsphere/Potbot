# U04-T selected task execution and result verification

**Patch baseline:** `f275dcc62495ef73d1de3ffcd0b22f393be381ac`, the merged U04-S
revision reviewed on 18 November 2026.

U04-T completes the supported task-selection-to-result workflow for the existing
native Cog and hosted text-generation handlers. An exact U04-S selection now
opens a task-bound operation review, queues a worker readiness check, launches
the original checked request once, retains progress and cancellation, and
evaluates the complete retained output against assertions chosen before launch.
The [wire contract](u04-t-task-execution-contract.md) defines the shared
controller, HTTP and browser DTOs.

## Completed workflow and owners

| Step | Behavior | Existing authority |
| --- | --- | --- |
| Discover | Read verified Cog descriptions and canonical ports, or the installed hosted-generation handler's descriptive contract. | OperationCatalogue, SynService and ArtifactStore; U04-S ranking |
| Select | Retain the exact candidate, source observation, task contract, repository state and semantic release. | U04-S search/result/selection |
| Review | Display the selected operation, original task and typed request; specify literal expected-result assertions. | TaskExecutionService over the existing operation workbench |
| Check | Queue an authorized readiness check; verify selection currentness, canonical ports, current worker policy, installation and resources. | OperationChecks and the existing jobs worker |
| Launch | Retrieve the request and assertions from the successful check; one check can create at most one run. | OperationWorkbench, signed authorization and the existing unique database constraint |
| Dispatch | Recheck task and selected source authority at the existing dispatch boundary, then use the installed handler's normal admission. | JobService, native grants or hosted provider policy, BudgetLedger |
| Monitor | Resume by original job identity, replay bounded events and inspect private output. | Existing principal-owned jobs and immutable job events |
| Verify | Read complete result evidence and settled accounting, then evaluate the original assertions. | Existing gateway, artifact and accounting receipts; shared literal assertion evaluator |
| Cancel or recover | Cancel with ordinary owner access; recover a lost submission using its exact original key. | Existing jobs cancellation and command receipts |

There is no additional scheduler, execution queue, tool registry, grant
authority or accounting ledger. The new controller records provenance on
existing check/run jobs. Registry and MCP selections still open their admitted
acquisition or MCP owners; they cannot be converted into generic native jobs.

## Descriptive metadata and exact native ports

The discovery source collector now resolves a Cog's exact retained Syn snapshot
through the existing artifact and semantic owners. It verifies the complete
snapshot artifact, source and canonical catalogue fingerprints, the selected
object's membership and implementation signature. Explicit descriptions take
precedence; an absent description can use that exact Cog type's definition.
Mutable instructions and object names do not become capability descriptions.

The projection supplies declared input and output types and the existing
`cog.execute:<native-reference>` task capability. Different namespaces with
conflicting declarations produce unavailable metadata. Descriptions, declared
ports and a known pure signature are observations; the worker independently
checks its actual installed operation, profile and canonical type identities.
Native execution preserves the installed port spelling after canonical alias
comparison, while the signed user request remains unchanged.

Hosted model entries receive a description of the existing prompt-to-text
handler and its registered text type. This does not rate a model or make a
provider, credential, disclosure policy, paid effect or model quality claim.
Those remain checks of the selected worker and provider policy.

Metadata reads are bounded to 32 distinct attempted snapshots and 64 MiB of
declared snapshot source bytes per collection, with bounded candidate projections.
Failed verified reads consume the allowance and are not retried for later
candidates in the same collection. The
collector uses the already registered workspace artifact root or an explicitly
injected existing ArtifactStore. It does not search alternative storage roots,
probe a worker profile, install a package or make a network request. For a
deployment that stores Syn artifacts in a subdirectory, set the same
`POTBOT_SYN_ARTIFACT_ROOT` in the API, worker and task CLI environments and use
that exact directory with the existing Syn CLI's `--artifact-root`. The override
must be an absolute, existing physical directory within the registered
workspace's artifact area. An unset override uses `workspace/artifacts`;
`workspace/artifacts/objects` is never guessed. Missing bytes or invalid
configuration remain unavailable, and metadata reads never create directories.

## Immutable selected execution binding

Application migration **46** adds optional immutable `task_selection_id` and
`task_execution_binding` fields to existing jobs. A relational foreign key
retains the original owned U04-S selection. Check and launch signatures bind
the complete execution binding digest. Existing jobs keep null selected-task
fields and retain their current lifecycle.

The binding includes the selection, search, result and candidate identities,
operation owner digest, seven-field task binding, selected canonical
capabilities, exact JobSpec, predeclared acceptance specification, and source
fingerprint of the execution and result-verification consumer. No assertion can
be added or changed at launch. A caller sends only the successful check UUID,
the reviewed binding digest and confirmation.

Admission revalidates the selected source, task contract, repository state,
semantic release, implementation consumer and canonical operation declaration.
Native checks also compare both ports with the installed worker profile. The
existing Workbench checks continue to enforce current installation, grants,
provider policy, credentials, disclosure and budget. A check is an observation
and does not reserve execution resources or call a provider.

Changing the task, revoking its selected source, replacing its runtime,
changing the trusted consumer or rotating operator authority can block new
execution. Reading history and cancelling owned work remain possible when
positive admission is unavailable. A successful request replay returns the
original receipt; it never silently rechecks or starts another operation.

## Independent result assertions

The acceptance specification has the following shape:

```json
{
  "schema": "potbot-task-result-oracle/v1",
  "assertions": [
    {"op": "equals", "pointer": "/text", "value": "expected answer"}
  ]
}
```

For hosted generation, the assertion view is `{ "text": "complete retained
text" }`. It is the existing worker's complete redacted output stream. For a
native typed result, the view is `{ "typeRef": "canonical-or-supported-type",
"value": ... }`; assertions can address a field such as `/value/count`.
Pointers follow RFC 6901. The supported operations are exact JSON equality,
literal nonempty string containment, and exact string/list length. Boolean,
integer and floating-point values remain distinct. No code, regular expression,
URL fetch or nested tool invocation is supported.

Each specification contains 1–16 assertions, occupies at most 64 KiB, and has a
16 KiB bound for each expected value. Structural and pointer bounds prevent
unbounded traversal. The same pure evaluator now serves the existing MCP
qualification oracle; its public protocol and validation semantics are retained.

Before evaluating a successful execution, the result consumer verifies:

- The original signed check and launch binding, including the current consumer
  implementation fingerprint; historical verification ignores admission expiry
  but does not ignore a changed or missing signing key.
- The owned job's successful immutable terminal event and original dispatch
  event, with their exact task, operation and selected-model linkage.
- Settled task accounting backed by the existing immutable usage row.
- For hosted generation, the exact retained request/policy, successful gateway
  observation and completion records, and every retained output event.
- For a native Cog, the complete content-verified artifact, exact typed result
  envelope and declared output type.

Complete verification is bounded to 4 MiB and 4,096 output events. Native artifact
results outside that bound can still be downloaded through the existing artifact
owner, but are unavailable for this bounded assertion workflow. A 64 KiB UI
preview is never substituted for the complete result. Missing or corrupt
artifacts, changed coordination rows, incomplete output, unsettled accounting
or an uncertain invocation cannot produce a pass.

| Verification status | Meaning |
| --- | --- |
| `pending` | The execution has not reached a terminal state. |
| `passed` | The complete verified result satisfies every predeclared assertion. |
| `failed` | The execution succeeded, but at least one assertion did not match. |
| `unavailable` | The job did not complete successfully, evidence is incomplete or invalid, or historical admission cannot be verified. |

The report retains the binding, acceptance, output and evidence hashes, along
with value-blind assertion results. It is reproducible from the existing
immutable evidence and does not introduce another mutable result registry.
Passing these assertions does not change the task's global completion state,
qualify a deployed host, or prove requirements that the operator did not express.

## CLI and HTTP

Use the original selection UUID from `potbot task-discovery … select`. The
existing native `api.setup_token` capability is needed for check and launch.
It is read from the operator credential owner; tokens are not command arguments
or printed output. Ordinary history, result reads, downloads and cancellation
do not read that capability.

```sh
potbot task-execution --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  context SELECTION_UUID

potbot task-execution --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  check SELECTION_UUID --selection-sha256 SELECTION_SHA256 \
  --input request.json --acceptance acceptance.json --key check-001 --confirm

potbot task-execution --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  job SELECTION_UUID CHECK_JOB_UUID

potbot task-execution --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  run SELECTION_UUID --check-job CHECK_JOB_UUID \
  --binding-sha256 BINDING_SHA256 --key run-001 --confirm

potbot task-execution --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  jobs SELECTION_UUID --limit 25

potbot task-execution --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  events SELECTION_UUID RUN_JOB_UUID --after 0 --limit 100

potbot task-execution --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  result SELECTION_UUID RUN_JOB_UUID

potbot task-execution --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  download SELECTION_UUID RUN_JOB_UUID --output new-result.json

potbot task-execution --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  cancel SELECTION_UUID RUN_JOB_UUID --key cancel-001 --confirm
```

The placeholders are exact identities and hashes returned by the preceding
commands. Run only after the existing worker has completed an eligible check.
The CLI does not start a second worker. Downloads verify the selected scope,
artifact hash and complete length and refuse to replace an existing file.

HTTP routes are under
`/v1/ui/task-execution/selections/{selectionId}`:

| Method and suffix | Purpose |
| --- | --- |
| `GET /context` | Exact selected task, operation contract and currentness |
| `POST /checks` | Reviewed selection digest, JobSpec, acceptance and confirmation |
| `POST /runs` | Original check UUID, binding digest and confirmation |
| `GET /jobs` | Bounded selected-job history with an opaque continuation cursor |
| `GET /jobs/{jobId}` | Reopen the original owned check or run |
| `GET /jobs/{jobId}/events` | Existing bounded progress replay |
| `GET /jobs/{jobId}/result` | Existing preview and independently checked complete outcome |
| `GET /jobs/{jobId}/artifact` | Verified artifact range under the exact selection/job join |
| `POST /jobs/{jobId}/cancel` | Explicit owned cancellation without setup authority |

Requests use ordinary workspace/principal authentication. Mutations require an
exact allowed Origin and one stable Idempotency-Key; checks and launches also
require the operator setup capability. Duplicate headers, unknown/duplicate
query fields, duplicate JSON keys, nonfinite numbers and unexpected envelope
fields are rejected. Request bodies are capped at 128 KiB and responses at
1,500,000 bytes. Responses are private and noncacheable.

## Browser, deployment and recovery

Selecting a supported operation in Task Discovery opens Operations with its
exact selection UUID. The selected-task workbench reads current context before
review and presents check, explicit run, progress, cancellation, history and
result verification. Workspace, principal, selection and request changes fence
late responses. Pending mutations retain their original body and idempotency
key for deliberate recovery after a lost response or page reload.

Apply the existing administrator migration command through revision 46 before
starting the upgraded API and workers:

```sh
potbot admin migrate
```

Rebuild the frontend and restart the configured API and jobs worker. Continue
using the existing native Cog and hosted provider policies, artifact owner and
server-side `POTBOT_UI_SETUP_TOKEN` configuration. The token is never sent to
browser JavaScript. This patch adds no package dependency or automatic model,
tool, provider or runtime installation.

Create a fresh U04-S shortlist after upgrading before starting selected
execution. The verified metadata projection and discovery consumer fingerprint
change in this release, so earlier shortlists and selections remain readable
historical evidence. They are not silently reauthorized under the new code.
The shared assertion evaluator also changes the MCP qualification consumer
fingerprint. Earlier U04-Q reports remain historical; a workflow requiring
current task qualification must collect fresh evidence through that existing
qualification owner.

## Acceptance and remaining plan gates

The dedicated U04-T workflow runs source-metadata, signed lifecycle, private
ownership, raced-launch, final-dispatch, recovery, complete-output and browser
contract tests. The PostgreSQL HTTP fixture is generated through the actual
controller and worker with a clearly labeled synthetic provider transport;
frontend tests consume those responses independently. Existing operation,
discovery and MCP assertion regression gates remain in place.

Installed native runtime and real vendor/model acceptance remain separate
evidence gates. A fixture proves the controller and result-consumer behavior
it exercises; it does not claim that this development host has qualified Linux
namespaces, a specific provider, or actual model ranking quality. Broader
U04/M4/T04 implementation status remains partial, and the historical U00 audit
and runtime qualification fields are preserved.

The next coherent code milestone is the supported T05 research/evidence path:
extend the existing information-need and research owners from immutable
repository replay to approved external search and fetch, immutable extracted
source artifacts, traceable citations and explicit contradiction handling.
Current external-source preflight descriptors are not execution receipts. That
milestone must connect actual disclosure authorization and budget reservation
through existing transport and execution owners, retain ambiguous-call recovery,
and avoid introducing another scheduler or source registry. Specialist T05.3
connectors and deployed provider profiles keep their own acceptance records.

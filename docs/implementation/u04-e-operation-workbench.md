# U04-E controlled operation workbench

**Patch baseline:** `df13593407a4818a2c709af65c9b78ad6b82535d`, reviewed 17 November 2026.
**Milestone:** a complete operator workflow for the existing native Cog and
hosted generation handlers, following the merged U04-C catalogue.

The Operations page now takes a supported catalogue entry through a task-bound
worker check, an explicit launch, durable monitoring, cancellation and result
inspection/download. The API and CLI expose the same controller. No additional
registry, scheduler, grant authority or budget ledger is introduced.

## Completed workflow

| Step | Behavior and authority |
| --- | --- |
| Select | The existing principal-scoped catalogue retains conservative recorded readiness and partial linked accounting. |
| Configure | A supported invocation contract selects the existing job handler, exact task and native source/input or hosted model/provider/role. |
| Check | An authorized `operation.check` job observes the configured worker's current task, policy, installation, grants and budget. It produces safe reason codes and hashed bindings. |
| Launch | The operator explicitly confirms the exact checked request. One successful check can create at most one run. |
| Revalidate | Before execution, the worker verifies the authorization and current bindings again; the existing handler still performs its normal final dispatch admission. |
| Monitor | Owned job state, progress, bounded event replay and output survive API and browser restarts. A known job ID can be reopened. |
| Result | Hosted output and native artifact metadata are visible to the job owner. Artifact previews and ranged downloads verify content identity. |
| Cancel/recover | Cancellation uses the existing job lifecycle. Lost responses preserve the original mutation key; possible external effects remain indeterminate until reconciled. |

Other catalogue kinds explain that no supported job route is installed. A model
entry selects exactly one hosted model. It does not imply a local model runner,
an arbitrary MCP tool, or a workflow executor. The existing S9–S12 maintenance
review, activation, postflight, release and rollback path remains authoritative.

## Worker eligibility

Native checks validate the current task contract and repository source, exact
profile, registered agent home, native executable, runtime/image pins, operator
health, the installed Context protocol/binding and operator route, S12 directory
holds, installed Syn provenance, node/enrollment binding
and persistent Cog grant generation/expiry. Catalogue `syn://cog/…` identities
are translated by the backend invocation contract to the installed
`syn://thing/…` execution reference; the worker proves that exact reference
against its installed profile. Native runtime `syn://thing/…` observations are
also retained as Cog entries.

Directed REST/GraphQL/gRPC inputs require a provider broker that the generic
native job dispatcher does not supply. The check reports those routes as
unsupported instead of promising an execution that cannot complete. A missing
Context binding/route blocks the check; a changed operator boot invalidates the
recorded runtime identity before launch.

Hosted checks use the configured worker policy for the selected provider/model
and role, disclosure, protocol, request bounds and conservative budget demand.
They check only the selected credential's presence and reject protected values
in the prompt. They do not discover provider inventories or contact a provider.

Checks never execute a Cog process, make an inference call, issue a one-use Syn
authorization, reserve budget or settle usage. Their reports are observations,
not grants. A check job may succeed while reporting `eligible: false`; the
individual checks then explain the blocked or unsupported requirement.

Reports expire after at most 120 seconds in the worker and are bound to the
exact canonical request, task contract, operation and relevant policy/runtime
identities. Changed inputs, a revised task, changed policy, replaced executable,
revoked grant, new S12 hold, lost credential or expired check deny launch or
execution before any external effect. The worker also checks current available
budget; a prior observation never reserves resources for a later run.

## Operator authorization and private ownership

Check and launch require the existing independent `api.setup_token` operator
capability as well as ordinary workspace authentication. The HTTP setup bridge
is available only on an operator loopback listener. The CLI obtains that
capability from the existing native credential store when the operator invokes
check/run; it does not accept a token argument or print the value.

The server signs a purpose-specific, expiring authorization over workspace,
principal, operation, full request and, for launch, the check job identity. The
worker verifies it against the current native capability. Rotation, a locked
credential store, scope mismatch or a modified request prevents execution.
The private MAC and input never appear in public job projections.

Application migration **39** adds `owner_principal_id`, `operation_check_id` and
private `operation_authorization` to existing jobs. It backfills ownership only
when an immutable `submit_api_job` receipt identifies one principal. Legacy
jobs without an unambiguous receipt remain unavailable to application users.
It adds principal row-level security to jobs and their events, immutable check
and launch bindings, a same-owner check foreign key, and a unique constraint
preventing multiple runs from one check. Earlier migration checksums remain
unchanged.

All job inspection, list/count/claim, event replay, cancellation, artifact reads
and hosted invocation lookup respect the owner, including between members of
one workspace. Workers must run under the principal whose queue they serve.
Reads and owned cancellation remain available without the operator setup
capability; losing permission to start new work must not prevent cancellation.

## API

The existing authentication supplies workspace and principal; callers cannot
override them in the request. Responses use `potbot-operation-workbench/v1`,
explicit workspace/principal/operation identities and private no-store caching.

| Method and suffix under `/v1/ui/operations/{id}` | Request/result |
| --- | --- |
| `GET /contract` | Supported handler, input fields and authoritative execution reference |
| `POST /checks` | Existing bounded JobSpec JSON; `Idempotency-Key` and operator setup header required |
| `POST /runs` | `{checkJobId, request, confirm: true}` with the same checked JobSpec and a new stable launch key |
| `GET /jobs/{jobId}` | Owned public check/run state |
| `GET /jobs/{jobId}/events?after=0&limit=100` | Bounded owned replay page and cursor |
| `GET /jobs/{jobId}/result` | Hosted output, artifact metadata and text preview |
| `GET /jobs/{jobId}/artifact?offset=0&length=262144` | Digest-verified artifact range encoded as base64 |
| `POST /jobs/{jobId}/cancel` | `{confirm: true}` with a stable cancellation key |

Mutations require the configured exact Origin. Check/run additionally require
`X-Potbot-Setup-Token`. Duplicate or unknown query parameters, malformed IDs,
oversized/duplicate-key/non-finite JSON and unexpected body fields are rejected.
The generic job submission endpoint cannot create signed checks or bypass the
linked-launch controller.

Input JSON is bounded to 16 KiB after canonicalization. Workbench event pages
contain at most 100 entries and 512 KiB; continuation preserves unread events.
Output and artifact text previews are each limited to 64 KiB and explicitly
mark truncation. Artifact ranges are at most 256 KiB. The browser and CLI
download action allow at most 16 MiB and verify the complete SHA-256 and byte
count before publishing a local result. Larger artifacts remain accessible
through the existing bounded artifact interfaces.

## CLI

Use the existing database configuration and real scope IDs. Obtain the operation
ID through `operations list` and inspect its contract before preparing input.
For example, a hosted request file has the existing job shape:

```json
{
  "job_type": "model.generate",
  "episode_id": "00000000-0000-0000-0000-000000000001",
  "input": {"prompt": "Summarize the supplied task.", "max_output_tokens": 256, "timeout_seconds": 30},
  "selected_models": [{"provider": "openai", "model": "operator-selected-model", "role": "reasoning"}]
}
```

The task, model and provider above are placeholders, not automatic permission or
provider recommendations. A native request instead uses `job_type: cog.execute`,
no selected models, and the existing agent/repository/profile/type/value input
contract with the backend's `executionReference` as `operation_ref`.

```sh
potbot operations --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  contract OPERATION_SHA256
potbot operations --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  check OPERATION_SHA256 --input request.json --key check-001
potbot operations --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  job OPERATION_SHA256 CHECK_JOB_UUID
potbot operations --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  run OPERATION_SHA256 --check-job CHECK_JOB_UUID --input request.json --key run-001 --confirm
potbot operations --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  events OPERATION_SHA256 RUN_JOB_UUID --after 0 --limit 100
potbot operations --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  result OPERATION_SHA256 RUN_JOB_UUID
potbot operations --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  download OPERATION_SHA256 RUN_JOB_UUID --output new-result.json
potbot operations --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  cancel OPERATION_SHA256 RUN_JOB_UUID --key cancel-001 --confirm
```

Run only after the check has finished with `eligible: true`. The independent
configured `jobs worker` processes both checks and existing execution types.
Submitting a check/run does not start another worker. All command help remains
usable without PostgreSQL, web, provider or native-vault dependencies.

If a response is lost, repeat the identical command with the identical key.
Committed retries return the original job identity even after the check expires.
Changing the request or check under that key conflicts. A different launch key
cannot reuse an already consumed check. The download command requires a new
filename and never overwrites an existing file, including a file created while
the download is in progress.

## Browser and deployment

Open **Operations**, select an entry, fill its task and invocation fields, and
choose **Check readiness**. The page presents the worker's checks before making
an explicit run available. It monitors the owned check/run, output and result,
and supports cancellation and reopening a known job identity.

The Next.js server forwards the existing `POTBOT_UI_SETUP_TOKEN` only for check
and launch. Configure it to match the backend/worker's native `api.setup_token`;
it is never sent to browser JavaScript. The backend workspace configuration and
ordinary bearer remain mandatory. The mutation proxy uses fixed routes, bounded
bodies and response projectors rather than accepting an arbitrary upstream URL.

Workspace, principal, selected operation, task and input changes fence late
responses. Polls are abortable. Indeterminate mutation responses retain the
same request body and key for a deliberate retry, and never generate a second
automatic launch. The existing tool-upgrade page also fences late responses
across scope/selection changes.

Pending browser submissions retain their exact input and key in the tab's
session storage under workspace, principal and operation identity. A confirmed
receipt clears that pending request. Reopening it only offers an explicit
retry; it never initiates work during navigation or page load.

Before starting the upgraded API and workers, apply the existing application
migration command, `potbot admin migrate`, through revision 39 using the
configured administrative connection. Restart those processes and rebuild the
frontend. Configure the existing native Cog handler and hosted provider policy
for the routes the operator intends to use; missing handlers/credentials are
reported as unavailable instead of provisioned implicitly.

## Installed-host acceptance runner

`scripts/qualify_operation_workbench.py` provides an opt-in acceptance run against
an already configured loopback API and worker. Commit the applied source first:
the runner records the client commit and hashes, and rejects changed client
source. Supply one exact private request file and a new journal filename in a
private directory owned by the operator.

```sh
python scripts/qualify_operation_workbench.py \
  --api-url http://127.0.0.1:8765 --origin http://127.0.0.1:3100 \
  --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  --operation-id OPERATION_SHA256 --input /private/request.json \
  --key acceptance-001 --report /private/operation-acceptance.jsonl --confirm-run
```

The runner reads the existing native API access and setup capabilities. It
submits exactly one check and at most one explicitly confirmed run, polls only
their owned IDs, checks terminal events and verifies a complete small result.
It never claims another queued job or starts a new worker. Hosted execution
additionally requires `--allow-paid --confirm-prompt-disclosure`; use a request
whose disclosure and cost the operator has approved. These flags are acceptance
runner controls, not additional global application permissions.

The private journal is created before any submission and retains command keys,
known job IDs, scope, source identity, result hashes and safe outcome codes.
It never records prompt/output text or credentials. The runner does not retry
POST requests; an uncertain submission leaves its original key for deliberate
recovery. Successful observation sets `qualifiedSmoke: true` while keeping
`qualifiedForProduction: false` and `workerSourceAttested: false`. Installed
worker source/image qualification still needs its independent evidence.

## Evidence and remaining qualification

The milestone's tests cover domain signatures and expiry, HTTP/CLI authority,
same-workspace privacy, migration/backfill, concurrent idempotent launch,
check/run cancellation, stale task/runtime/grant/policy/budget rejection, hosted
output, and native result integrity. Real PostgreSQL lifecycle tests exercise
the central service and configured worker; native process/transport and hosted
provider boundaries use explicit fixtures. Frontend tests cover forms, public
projections, authenticated fixed-route proxying, retries and scope fences.

The main CI workflow runs a dedicated U04-E acceptance step and retains its
JUnit report and actual public HTTP contract fixture. A subsequent Node check
passes that fresh backend fixture through the frontend response projectors.
Existing Python and frontend checks remain required. Local fixture
and database passes establish implementation behavior; they do not establish
a deployed host, paid provider or rendered-browser qualification result.

### Local patch validation

| Gate | Result on this patch |
| --- | --- |
| Dedicated U04-E acceptance suite | 175 passed, including 33 real PostgreSQL lifecycle cases; no skips |
| Additional PostgreSQL regressions | 51 passed across the existing API, hosted gateway, Cog grants, catalogue, migration/role isolation and artifact range/upload Cogs |
| Existing jobs and S12 regressions | Passed in the 51-test central/worker/jobs/S12 gate before the final dedicated workbench rerun |
| Roadmap inventory regressions | 50 passed; 209 documents and 731 requirements retained; global runtime qualification remains false |
| Complete frontend suite | 140 passed, including actual HTTP/worker response projection and exact-request recovery |
| Python static checks | Ruff/format passed across 960 files; mypy passed across 397 source files |
| Frontend static and production checks | TypeScript and Next.js production build passed |
| Canonical plan consistency | Tool-plan and object-memory plan checkers passed |

The disposable database was PostgreSQL 16.2 with pgvector 0.8.7. Provider HTTP
and native process/Syn execution boundaries were explicit fixtures. This local
environment denies the Unix-domain socket required to launch Chromium, so no
rendered acceptance result is claimed. The new commit still needs its own CI
run after applying the patch; the green merged baseline is independent evidence.

The completed scope is the supported operation workbench. Wider U04/M4/T04,
external package discovery/acquisition, additional workflow/connector handlers,
local-model runtime qualification and ecosystem/concurrency acceptance retain
their existing owners and evidence gates. The roadmap's global
`runtime_qualified` flag remains false.

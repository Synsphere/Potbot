# U04-S — Task-aware capability discovery and exact selection

Source baseline: merged U04-Q main
`d904442b7f08c3b7c6c975092327cc89646ae562`, 18 November 2026.

This milestone implements the supported T04.3 task-to-selection workflow:
read the current task and existing capability owners, retain an explained
shortlist, optionally refine a bounded candidate pool with the existing local
embedding owner, inspect an exact candidate, and retain a revalidated handoff
to its existing review surface. Migration 45 adds private search, immutable
result, selection and signed event records. CLI, authenticated API and browser
use the same controller.

A selected result remains `executable:false`. Discovery does not install a
package, add a task capability, invoke an MCP tool or replace runtime admission.
The installed host/vendor evidence described in [U04-Q](u04-q-installed-mcp-qualification.md)
still needs its own configured operator environment. Controlled retrieval and
native-lifecycle fixtures do not qualify a deployed model or provider. U04/M4/T04
and the historical U00 requirement statuses remain partial.

## Existing owners and private scope

| Concern | Owner and retained evidence |
| --- | --- |
| Task | Existing running episode, exact current contract, repository state and semantic release pin; the seven-field binding is identical to U04-Q. |
| Registry candidates | U04-D signed source and publisher heads, retained snapshot and exact candidate digest. Inspection-only profiles can be relevant without becoming executable. |
| Installed skills | Existing acquired-skill control and immutable package/release/source identities. An exact registry package match may prefer a current installed route; verified acquired-skill manifest descriptions can support operation discovery. |
| Other operations | Existing bounded `OperationCatalogue` projection of Cogs, tools, models, workflows, connectors and skills. Source or historical installation observations do not establish current execution authority. |
| MCP | U04-M current configured tool and exact retained reference, including binding, installation, snapshot and definition identities. Other tools advertised by that server require their own admission. |
| Task evidence | Independently valid U04-Q suite/report for the same task, state, installation, configuration, agent and current MCP reference. |
| Types | Existing sealed canonical SYN catalogue and protected Potbot data aliases; no second type registry or guessed name-to-type mapping. |
| Semantic refinement | Existing named embedding-space/profile registration, local supervised encoder and task budget ledger. |
| Selection | Immutable task-discovery handoff selecting an existing owner; the destination remains responsible for review and execution. |

All search, result and selection records are workspace/principal private.
Migration 45 also restricts the existing command-receipt table's
`task.discovery.*` operation namespace to the owning principal; a second
workspace member cannot recover another principal's discovery receipt through
the general workspace-scoped receipt owner. Unrelated receipt namespaces retain
their existing policy. The
collector reads existing owners without refreshing a registry, reconciling MCP,
creating a task, opening native configuration or invoking a provider. A stale
source remains explicit and cannot enter a current shortlist. An incomplete
scan, a pagination loop or a bound violation fails instead of returning a
complete-looking partial catalogue.

Stable catalogue identities include relevant owner heads and actual currentness.
Display timestamps and recent invocation/accounting-only changes do not make
the source identity drift. A changed task, source, publisher decision, installed
control, reference, report, type catalogue, ranking policy or implementation
consumer does make a previously retained result historical. Historical bytes
remain readable; fresh selection requires a new current search.

## Typed filtering and explained ranking

The candidate corpus is bounded to **256 observations and 1 MiB** of canonical
safe summaries. Public shortlists contain at most **ten results**. Each candidate
has bounded description/class/type/effect/scope/platform fields, exact owner
identity and digest, currentness reasons and task-specific report references.
Raw arguments, case oracles, credentials, full native definitions and native
paths are not ranking inputs.

Constraints have exactly `classes,inputTypes,outputTypes,platform,
permittedEffects,permittedScopes,requireLocal,requireTaskEvidence`. Request
arrays contain at most 64 unique entries; candidate capability arrays contain
at most 32. Unspecified effects/scopes are `null`. An explicit empty allowance
is restrictive. Flags are Booleans. Unknown required ports, effects, scopes,
platform or local execution evidence cannot prove a hard constraint.

The existing `syn://` and `synny://` spellings identify the same class. Native
types normalize to `syn://`; protected `potbot-data-*` aliases normalize to
`synny://`. Unknown query types are rejected against the sealed catalogue.
Declared unknown source types remain unknown. Supplied input `A` is compatible
with accepted input `B` only when `A` is a subtype of `B`; produced output `C`
satisfies requested output `D` only when `C` is a subtype of `D`. A Potbot data
representation is not assigned an invented native superclass.

Hard exclusions run before lexical scoring and before semantic encoding.
Explicit task capability denial, unavailable owner authority, incompatible
types, denied effects/scopes, unavailable platform/locality and required
missing task evidence all exclude a candidate. A nullable required capability
means `taskPermission:unknown`; it is not a permission grant.

The deterministic scorer reuses the reviewed `CLASS_PHRASES` vocabulary.
Feature contributions are retained separately: class fit, unique description
token recall/precision, exact/compatible type fit, adequate installed preference,
exact-task passing evidence and optional positive semantic similarity. Names,
publisher ownership, popularity, registration order and repeating a keyword do
not create a trust or relevance bonus. Installation and report evidence only
prefer an already relevant candidate. A no-match result is retained explicitly.

Task evidence is `none`, `matching_pass`, `matching_failure` or `historical`.
Only `matching_pass` contributes a positive report preference. A report for
another task or changed installation cannot become general quality evidence.
If separate current suites disagree, an exact failure takes precedence over
an exact pass; different suites are not treated as a timestamp-ordered series.
Unknown cost, latency and general quality stay unknown.

General operation observations currently lack canonical descriptions and ports.
They remain explicit unknowns instead of deriving capabilities from names or
mutable context instructions. Verified acquired-skill package descriptions are
the supported description-bearing operation exception. Current MCP descriptions
can support retrieval, but JSON Schema fields are not guessed into canonical
SYN ports or task permissions.

## Optional native semantic refinement

Hybrid searches retain one pending request. They do not run an encoder in the
API or browser. The collector first applies the same hard exclusions, then
chooses at most **sixteen** description-bearing candidates for transient
encoding. Lexical relevance orders that bounded pool; lack of lexical overlap
is not an admission barrier to a semantic paraphrase. An empty description
cannot acquire relevance from a name fallback.

The explicit `refine` command uses the existing pinned
`Qwen/Qwen3-Embedding-0.6B` profile, named representation and offline model
installation. The default profile is 1,024-dimensional normalized cosine with
at most four document texts per batch. One reservation covers the query and
all document batches before model/tokenizer preparation. The total deadline
is 1–120,000 ms, shared by preparation and every batch. Positive cosine scores
are bounded retrieval features; nonpositive scores add zero.

Query and candidate vectors exist only in the supervised call's transient
memory. Private text is not copied into shared `embedding_sources`, source
heads, artifact objects, vector rows or a new retrieval index. Retained native
evidence contains exact query/document/profile hashes, call/token/time usage,
reservation IDs and usage origin. The private search record itself retains its
reviewed query and safe candidate summaries for exact recovery and inspection.
The existing evidence store also seals the exact score digest and an opaque
per-claim binding. Altered scores or an accounting receipt from another claim
with identical inputs cannot be substituted into this search.

Confirmed usage is settled through the existing task ledger. Confirmed unused
capacity is released. Uncertain dispatch/termination leaves usage held and the
search indeterminate. A result after cancellation, authority drift or the total
deadline cannot be published as a current shortlist. Retrying an existing
native claim recovers its durable state and never invokes the encoder again.
The signed claim timestamp also bounds publication lifetime; the client cannot
extend a claim by supplying a smaller elapsed-time value.
An empty eligible pool completes without native inference or a reservation.

## Operator walkthrough

Apply migration 45 with the existing configured `potbot admin migrate`
workflow, using the same schema and administrative owner as prior upgrades.
Restart the application/UI on the updated checkout. The populated 44→45
acceptance gate preserves earlier checksums and exact signed U04-M/U04-Q data.
Use the existing restricted PostgreSQL workspace/principal and independent
native `api.setup_token` credential; task discovery creates none of these.

### 1. Read the current task context

The task must already be running, with its current contract, captured repository
state and semantic release pin. Start with a retained context read:

```bash
potbot task-discovery --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  context --episode EPISODE_UUID --state REPOSITORY_STATE_UUID
```

Retain the returned `contextSha256` and task binding. The response includes
source counts, type/policy/consumer hashes and existing named embedding-space
registrations. A registered space is not proof that its model files or host
runtime are available.

### 2. Create a lexical shortlist

Prepare the exact JSON request, at most 8 KiB. Replace the UUID/hash placeholders
with the reviewed current values; the example does not claim that any particular
capability is installed:

```json
{
  "episodeId": "EPISODE_UUID",
  "stateId": "REPOSITORY_STATE_UUID",
  "contextSha256": "CURRENT_CONTEXT_SHA256",
  "query": "Inspect the repository revision history",
  "constraints": {
    "classes": [],
    "inputTypes": [],
    "outputTypes": [],
    "platform": null,
    "permittedEffects": null,
    "permittedScopes": null,
    "requireLocal": false,
    "requireTaskEvidence": false
  },
  "mode": "lexical",
  "limit": 10,
  "embedding": null,
  "confirm": true
}
```

```bash
potbot task-discovery --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  create --input /operator/task-discovery-request.json --key task-search-1 --confirm

potbot task-discovery --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  search SEARCH_UUID

potbot task-discovery --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  result RESULT_UUID
```

Lexical mode creates an immutable result without inference. Review its feature
contributions, source currentness, unknown capability/evidence fields and hard
exclusions. `requireTaskEvidence:true` restricts the result to a passing report
for this exact current task; it cannot borrow another task's pass.

### 3. Refine an explicitly selected hybrid request

Install and independently smoke-test the existing pinned local model through
the [embedding setup](../embeddings.md) and [Linux sandbox](../linux-sandbox-ci.md)
workflows. Model setup is a separate explicit operation. `task-discovery refine`
does not download weights, access a remote provider or run outside the required
supervisor. The model directory must be an existing physical path within the
workspace's configured `models` area, with the pinned model manifest and files.

If the standard named space is not registered, the existing owner supports:

```bash
potbot embedding register-space --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  --embedding-name general_text
```

Read task context again after any registration. Create a new request with its
current context hash, `mode:"hybrid"`, and this `embedding` object using one
exact returned space/profile option:

```json
{
  "spaceId": "EXISTING_SPACE_UUID",
  "embeddingName": "general_text",
  "profileSha256": "EXACT_REGISTERED_PROFILE_SHA256",
  "maxCandidates": 16,
  "runtimeBudgetMs": 120000
}
```

Keep the other creation fields from step 2. Use a new stable command key for
the newly reviewed request. Then, from the configured native operator host,
use the returned pending search revision and binding hash:

```bash
potbot task-discovery --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  refine SEARCH_UUID --revision SEARCH_REVISION --digest SEARCH_BINDING_SHA256 \
  --model-dir /workspace/models/qwen3-embedding-0.6b \
  --artifacts-root /workspace/artifacts/objects --key task-refine-1 --confirm
```

The model and artifact locations are placeholders for the existing workspace
configuration. The artifact owner is reused for the encoder service; transient
ranking does not publish query or candidate artifacts. The task must have
sufficient calls, tokens, milliseconds and concurrency allowance for the entire
bounded batch plan. A missing model or unsupported profile fails before a fresh
native claim; once dispatch is uncertain, the existing usage owner retains that
uncertainty.

### 4. Inspect and select an exact owner

Read the current search/result and inspect an included candidate SHA:

```bash
potbot task-discovery --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  inspect SEARCH_UUID CANDIDATE_SHA256

potbot task-discovery --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  select SEARCH_UUID --revision CURRENT_SEARCH_REVISION \
  --digest CURRENT_SEARCH_BINDING_SHA256 --candidate CANDIDATE_SHA256 \
  --key task-select-1 --confirm

potbot task-discovery --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  selection SELECTION_UUID
```

Progressive inspection rechecks the exact owner digest and returns at most
128 KiB of detail through its existing read owner. Selection rechecks the task,
catalogue, policy, consumer and chosen owner, then retains a fixed handoff:
operation in `/tools`, registry candidate in `/discover`, or binding in
`/discover/mcp`. The destination still owns installation review, task admission,
qualification and execution. No caller-provided URL or native command is a
handoff target.

### 5. Monitor, cancel and recover exact commands

`searches --episode EPISODE_UUID --limit 25 --after CURSOR_UUID` reads bounded
private history. At most sixteen searches per principal may be pending/running.
Cancel an active search using its current reviewed identity:

```bash
potbot task-discovery --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  cancel SEARCH_UUID --revision CURRENT_REVISION --digest CURRENT_BINDING_SHA256 \
  --key task-cancel-1 --confirm
```

If a response is lost, retry the same command key and exact request bytes, then
read current state. A recovered command receipt does not refresh its reviewed
context. A cancelled or indeterminate native claim is not rerun under a new key.
Inspect existing usage/worker evidence, and create a separately reviewed fresh
search only when appropriate. A setup-key rotation or an invalid signature
blocks positive current authority; cancellation cannot re-sign a tainted search
into an approved result. Retirement advances beyond the latest durable event
head even if an old row was restored, preserving the denial instead of reviving
an earlier positive admission.

## API and browser

The [public contract](u04-s-task-discovery-contract.md) defines the exact DTOs
and `/v1/ui/task-discovery` routes. Mutations require the scoped bearer, exact
approved Origin, separate setup capability, one stable Idempotency-Key and a
bounded exact body with `confirm:true`. Responses are private/no-store. There
is no client-uploaded score, result, embedding vector or provider-endpoint route.

Open `/discover/tasks?workspace=WORKSPACE_UUID&episode=EPISODE_UUID&state=REPOSITORY_STATE_UUID`.
An optional `search=SEARCH_UUID` selects exact retained history. Opening and
refreshing are GET-only. The browser can create a lexical or pending hybrid
request, inspect an included result, cancel, select and recover an exact lost
command through the existing per-tab recovery journal. Bounded monitoring reads
state; native refinement remains the explicit CLI command above. Provider text
is rendered as text, and scoped result destinations select the requested owner.

## Acceptance gates and remaining evidence

The dedicated `.github/workflows/task-discovery-u04-s.yml` gate runs the frozen
40-request discovery regression, new held-out/typed/ranking/source tests,
API/CLI validation and exact command recovery, real PostgreSQL task/source/Q
joins, principal isolation, currentness and budget/native-claim races, populated
44→45 migration, report-projection corruption tests and transient embedding
accounting/privacy tests. The actual HTTP lifecycle export is then consumed by
the Node browser DTO/recovery tests. It retains JUnit and the exported response
corpus. Functional Bubblewrap CI also runs the real supervised deadline case;
it must not convert a host-isolation failure into a passing model qualification.

The original forty golden requests remain unchanged and the acceptance
threshold stays **at least 90% class recall@3**. Tests also require no
permission-denied executable selection, stable ranking, correct subtype
direction, explicit no-result behavior and no ownership/keyword repetition
boost. Fixture vectors are explicitly synthetic. They prove contract,
accounting and lifecycle behavior, not real-model relevance improvement.

| Evidence group | Current validation record |
| --- | --- |
| Typed ranking and existing-owner source tests | 70 passed; includes verified acquired-skill description/control and empty-description semantic exclusion. |
| New and affected unit/API/CLI/owner regressions | 664 passed with no skips or failures, including affected discovery, operation, qualification and embedding suites. |
| Frozen discovery benchmark | 40/40 class recall@3, or 100%, against the unchanged forty-request corpus; zero executable recommendations. `artifacts/task-discovery-ranking-metrics.json` retains the measurement. Runtime/model-quality qualification remains false. |
| Real PostgreSQL task/Q/native-accounting and migration tests | 47 passed, zero failures/skips: 24 task-service/HTTP, four exact MCP/task/Q and migration, six report-projection, and thirteen transient-embedding cases. Local harness: PostgreSQL 16.2 with pgvector 0.8.7. |
| Browser DTO/recovery and actual HTTP export | 307 frontend tests passed with no skips, including the actual PostgreSQL/HTTP export consumer; TypeScript and production build passed. No rendered installed-host or model qualification is inferred. |
| Dedicated hosted database | Existing repository Compose image uses PostgreSQL 17 with pgvector 0.8.7; hosted workflow execution remains external evidence. |
| Source and canonical plan gates | Ruff passed with all 884 files formatted; strict mypy passed across 438 source files; `git diff --check` passed. The canonical 48-stage tool and 32-stage memory plan checks passed without changing historical qualification statuses. |
| Real supervised Bubblewrap deadline | One test explicitly deselected on the local host because its known NETLINK/network-namespace restriction prevents required isolation setup. This is not a skip or passing runtime qualification. The dedicated workflow retains the case as required after functional Bubblewrap preflight. |
| Offline Qwen relevance quality, deployed MCP hosts and vendor cases | Not qualified by these fixtures; explicit installed-model/host/task evidence remains required. |

The next evidence work is to run the admitted workflow with the chosen offline
model and existing installed profiles, compare frozen lexical and hybrid
retrieval on held-out task requests, and retain exact pass/failure/interruption
reports for the selected hosts/vendors. Broader operation description/port
adoption, native vendor interoperability and end-to-end verified task completion
remain separate gates; this milestone creates no second scheduler or registry.

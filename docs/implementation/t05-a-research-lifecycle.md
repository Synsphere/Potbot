# T05-A external research lifecycle

T05-A makes one bounded external research action runnable through the existing
`ResearchStore`, `ResearchRunStore`, task budget ledger, artifact store and
evidence/event owners. It does not create a second queue, a mutable provider
registry or an automatic research scheduler. The HTTP/browser contract is in
[t05-a-research-contract.md](t05-a-research-contract.md).

## Scope and visibility

An action names one workspace, task contract, open information-need revision,
repository snapshot and semantic release digest. Its external selector is
`documentation / query_source / external-research`; `target_version` is the
exact operator policy fingerprint. Query text is bounded to 500 characters.
Action admission is inert. A separate confirmed run admission binds the query,
approved provider destinations, exact fetch hosts and resource ceilings to a
durable `controller_decision` event and one task reservation.

Research queries, raw responses, extracted documents and comparison evidence
retain the existing **workspace-shared** semantics. The response scope names
the current reader. The immutable authorization names the original approving
principal. Client command keys are scoped to the authenticated principal, while
one existing research action still has at most one run across the workspace.
Setup authority is required for new admission; cancellation needs only ordinary
authenticated workspace access.

## One queue, separate execution modes

Migration 47 adds `execution_kind` and immutable external authorization fields
to `research_runs`. Historical rows default to `local`. The local worker filters
for local rows, and cannot complete or retire an external attempt through its
unbudgeted local methods. The external worker selects only external rows.

| State | Meaning and permitted continuation |
| --- | --- |
| `queued` | Exact admission and budget are retained; no send has begun. A current worker can claim it once. |
| `running` | One worker owns the generation and lease. Each send independently rechecks current authority. |
| `succeeded` | All scheduled provider outcomes and the bounded extraction batch have an authenticated completion receipt. Individual sources can still be unavailable or extraction can be incomplete. |
| `blocked` | A confirmed negative outcome stopped work. Known transport usage is settled, or a never-started reservation is released. |
| `indeterminate` | A dispatched request or local processing attempt cannot be fully accounted for. The reservation stays held where usage is uncertain; no automatic replay occurs. |

Cancellation of queued work retires it immediately. Cancellation of running
work sets a durable flag; the current response can still be retained and
accounted for, while the next send and final positive publication are denied.
An expired worker is quarantined. A new worker never starts the old action
again merely because its previous lease expired.

## Exact dispatch and retained responses

`ExternalResearchPolicy` extends the existing source-policy owner. It chooses
supported provider adapters, separately approves disclosure to each provider,
specifies which confirmed failure statuses permit continuation, and lists exact
fetch hosts whose terms the operator has approved. Credentials stay in the
existing credential broker. Provider endpoints cannot be supplied by a model
or browser request.

The native worker compares configured providers in order. Every search request
uses the fixed adapter method, URL and canonical body. Results retain both the
provider-specific result set and URL-deduplicated lineage. Two search engines
returning one publisher do not become two independent sources.

Document retrieval is limited to the first bounded merged results. A URL must
match the configured exact host. A complete, retained robots response must
permit the URL; its crawl interval and the configured host interval both apply.
Each robots and document fetch is an explicitly admitted request. There are no
hidden follow-up fetches, redirects, automatic retries, browser execution or
remote parsing calls.

The transport uses the existing `PinnedHttpsTransport` and `ConnectPolicy`
authority for exact-route checks, public DNS/address validation, numeric dial
and TLS verification. The final callback runs after credentials and the exact
request are prepared, immediately before the send. It commits a signed request
receipt only after checking the task, need, semantic pin, lease, cancellation,
policy, consumer fingerprint, exact request, source rate interval and existing
reservation. Long configured rate waits renew the same lease while repeating
currentness and total-deadline checks; they do not create another attempt.

The native CLI supplies a live deployment-policy reader, including for an
explicit `--policy` file. The worker reloads it before claiming work and before
each send, and checks it before processing and again before publishing the
result. Changing or removing that policy retires the old authority; a process
that already loaded an earlier file cannot continue sending or publish a
positive result under it. Expired-worker quarantine processes at most 25 rows
per invocation.

The response is a separate signed immutable receipt. A successful bounded
response publishes its complete raw bytes through the existing artifact store.
Partial, redirected, oversized or uncertain responses never publish bytes as
complete source evidence. A response may be recorded after task authority
changes during the call, so actual usage is not lost; that observation does not
authorize another send.

## Accounting and processing failure

One reservation covers the complete attempt before any native work starts:

| Dimension | Reservation and actual usage |
| --- | --- |
| `calls` | The configured request ceiling; actual confirmed sends include searches, robots requests and document fetches. |
| `tool_ms` | Request timeouts plus configured rate intervals, and 5,000 ms per possible document for optional PDF processing. Actual transport and retained supervised PDF durations are recorded. |
| `bytes_read` | Request ceiling multiplied by the maximum source bytes. Confirmed received bytes are retained, including over-limit observations. |
| `money_microunits` | Request ceiling multiplied by the largest configured per-call price ceiling. Confirmed provider sends use that configured conservative charge, not an invented provider invoice. |
| `concurrency` | One held slot, released by the existing ledger when accounting is final. |

Actual measured overruns are not erased. The existing ledger can exhaust the
task allowance. Usage is labelled `measured`, `conservative_estimate`, or
explicitly `synthetic` for injected test transports; a synthetic transport cannot
present itself as a production measurement.

Before source processing, the worker commits a `processing_started` occurrence
to the existing execution-event owner. That marker survives a publication
rollback or worker loss. Unknown work after this point retains an indeterminate
hold instead of settling only the already known network calls. Completion and
the task usage settlement then commit together with the output artifact and
signed receipt. Positive publication cannot be repeated for the same run.

## Extraction and citation evidence

The controller rebuilds normalized search results and extracted documents from
the retained raw responses. It does not accept a caller-supplied batch or source
text as evidence. The output contains complete source/extraction artifact IDs
and hashes, requested/resolved URLs, retrieval time, extractor identity, source
status and explicit reason codes. Publication times remain labelled as source
reported or provider reported; a retrieval timestamp does not become a
publication timestamp.

HTML, UTF-8 text and JSON use deterministic bounded parsers. HTML scripts and
embedded objects are data and are not executed. JSON text locations retain
JSON Pointers. Citations use byte offsets into the retained UTF-8 extraction,
with exact quote hashes and segment locations.

PDF extraction is optional and explicit. `POTBOT_RESEARCH_PDF_PROFILE` selects
an exact executable digest and the existing supervised Bubblewrap process
owner. The profile fingerprint is included in admission. Missing or failed
isolation produces an unavailable source, with no host-parser fallback. The
first PDF transform retains actual profile/executable/isolation identities,
elapsed time and process-stop evidence under the signed completion receipt.

Ordinary result/source/citation reads never run a native PDF subprocess.
Historical PDF audit verifies its exact raw/extraction artifacts, authenticated
completion and Cog receipt, current parser/profile/executable pin, and retained
process-stop/isolation evidence. Pure parsers are independently re-executed
over the retained bytes. These are distinct verification mechanisms; neither
performs another external fetch.

## Typed transformation and monitoring

The installed controller-scoped signature is:

| Identity | Value |
| --- | --- |
| Cog | `syn://cog/potbot-research-external` |
| Input | `synny://type/potbot-research-action` |
| Output | `synny://type/potbot-research-external-batch` |
| Handler | `potbot.cogs.external_research`, implementation revision 1 |

The existing `TypedCogValue` and `CogReceipt` owners bind the one input and one
output, task contract, repository state, invocation ID, implementation binding
and upstream authorization. The declared installed signature is not a deployed
SYN release qualification: `synActivation` remains `not_qualified` in these
receipts. An imported/activated release or live-host qualification cannot be
invented from fixture success.

The existing `ExecutionEventService` records the actual root start, request
child starts and outcomes, processing start, completion or negative retirement.
Each child references its causal event and run. Output occurrences name the
real artifact and hash. Measured durations and bytes are reported; unavailable
CPU and memory measurements stay null. Events share the existing commit-ordered
workspace cursor and retain causal identity across page/reconnect reads.

## Verification boundaries

The real PostgreSQL acceptance suite uses genuine restricted workspace roles
and the production admission, transport normalization, raw artifact, extraction,
budget, signed-receipt and audit paths. Its HTTP transport is an explicitly
synthetic fixture. It does not claim live Brave/Tavily service qualification or
working native PDF isolation on a host whose namespace probe is unavailable.

Search completion does not resolve an information need, mark a task verified,
prove a claim true, establish publisher independence, determine a licence or
grant another operation. Cited comparisons remain separate explicit evidence
publication through the existing artifact and evidence owners. Specialist
scientific/archive providers and broader extraction formats remain separate
implementation-plan work beyond this supported external research profile.

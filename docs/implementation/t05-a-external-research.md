# T05-A external research and cited evidence

**Patch baseline:** `9c0305301a4cf74a2972c89bb567ba0157c0772c`, the merged
U04-T revision reviewed on 18 November 2026.

T05-A supplies a complete configured research workflow: select a task and
information need, review an exact query and its disclosure, run the existing
research worker, inspect retained sources, derive exact UTF-8 citations and
publish a reproducible source agreement or conflict report. The browser at
`/research`, `potbot research` CLI and authenticated API use the same owners.

The [HTTP contract](t05-a-research-contract.md) specifies the public DTOs. The
[lifecycle design](t05-a-research-lifecycle.md) describes admission, per-request
authorization, accounting, typed transformation and independent audit.

## Implemented workflow

| Step | Delivered behavior |
| --- | --- |
| Review | Read the exact running task, repository state, semantic release and open need revision. Create a need through its existing owner when required. |
| Admit | Retain the reviewed query as an inert research action. Policy changes require renewed review. |
| Disclose and queue | Explicitly approve disclosure to the configured providers and reserve one bounded attempt through the existing task ledger. |
| Search | Use fixed Brave and Tavily adapters, retain independent normalized result sets and merge duplicate URLs with complete provider lineage. Only configured confirmed failures permit fallback. |
| Fetch | Follow result-derived URLs on exact approved hosts after a retained robots decision. Respect configured rate intervals, terms approval, byte ceilings and deadlines. |
| Extract | Retain raw source bytes and versioned HTML, UTF-8 text or JSON extraction. An explicitly pinned isolated PDF profile can add authored PDF text and page locations. |
| Cite | Derive a quote from the complete verified extraction using exact UTF-8 byte offsets and a quote hash. Keep source, text, receipt and location identities. |
| Compare | Retain operator-selected literal values and their citations. Conflicts, stale or missing required publication dates and unavailable evidence remain unresolved or unsupported. |
| Recover | Reopen the original run and progress events, cancel with ordinary workspace authority, or recover a committed mutation using its original key and exact body. |

Research retains the existing **workspace-shared visibility**. Queries, source
artifacts and comparison reports are visible to other authorized workspace
members. The response scope identifies the current reader; signed admission
retains the original approving principal. Browser requests and recovery records
are fenced to their authenticated workspace and principal.

## Deployment configuration

Apply application migration **47** with the existing migration command before
starting the updated API and worker. It adds an execution kind and immutable
external authorization to `research_runs`; existing rows remain local research.
The local worker excludes external rows, and the external worker only claims
explicitly admitted external rows. There is no new scheduler or queue.

The API, worker and CLI must share the same database, registered workspace
artifact root, native `api.setup_token` authority and research policy. Ordinary
reads and cancellation do not require the setup capability. The workbench uses
the existing `workspace/artifacts` directory; it neither creates a guessed root
nor introduces principal-private artifact ownership.

Set `POTBOT_RESEARCH_POLICY` to an existing physical absolute JSON file. The
CLI also accepts `--policy PATH`. A policy has this shape:

```json
{
  "schema": "potbot-external-research-policy/v1",
  "allow_query_disclosure": true,
  "providers": [
    {"provider": "brave", "cost_microunits_per_call": 3, "minimum_interval_ms": 1000},
    {"provider": "tavily", "cost_microunits_per_call": 5, "minimum_interval_ms": 1000}
  ],
  "fetch_hosts": [
    {"host": "docs.example.com", "terms_approved": true, "minimum_interval_ms": 1000}
  ],
  "fallback_statuses": ["rate_limited", "quota_exhausted", "unavailable"],
  "max_results": 8,
  "max_documents": 4,
  "max_requests": 12,
  "max_source_bytes": 262144,
  "timeout_seconds": 15
}
```

The example host is a placeholder and the monetary values are illustrative
configured ceilings, not verified provider prices. Replace them with the
operator's approved hosts, rate limits and account-specific ceilings. Use only
providers to which this task's query may be disclosed. The policy fingerprint
is part of action and run admission; changing the file does not amend an
already authorized request.

Store provider credentials under the existing credential names
`research.brave` and `research.tavily`. The transport also supports the explicit
environment fallbacks `POTBOT_BRAVE_SEARCH_API_KEY` and
`POTBOT_TAVILY_API_KEY`. Credential values are added only in the transport; they
do not appear in policies, browser descriptors, request receipts or artifacts.
Provider endpoints are fixed by the adapters. A model or browser cannot supply
a new provider endpoint or an arbitrary source URL.

The existing task budget must contain sufficient `calls`, `tool_ms`,
`bytes_read`, `money_microunits` and `concurrency` capacity. Searches, robots
requests and document requests all count. Optional PDF work is also reserved
and measured. The worker retains observed overruns instead of reducing them to
the reservation. Unknown usage after a send or processing start keeps an
indeterminate hold and is never automatically replayed.

### Optional isolated PDF profile

Install and qualify the existing process-isolation prerequisites and a trusted
`pdftotext` executable. Set `POTBOT_RESEARCH_PDF_PROFILE` to an existing physical
absolute profile file with the exact physical executable path and SHA-256:

```json
{
  "schema": "potbot-isolated-pdftotext/v1",
  "executable": "/absolute/physical/path/to/pdftotext",
  "executable_sha256": "EXACT_64_CHARACTER_SHA256"
}
```

Use the same profile for admission, the worker and later audit. The executable
is copied into a disposable sandbox and receives bounded source bytes on stdin,
with no network and no arbitrary source-file access. The existing
`ProcessSupervisor` and Bubblewrap owner enforce process, time, memory and
output bounds. Missing or failed isolation returns an unavailable extraction;
there is no unconfined parser fallback. Scanned PDFs without authored text
remain unsupported because OCR is outside this increment.

Ordinary result, source, citation and comparison reads never invoke the PDF
parser. PDF readback checks the authenticated transformation and exact retained
artifacts, current configured executable/profile identities and retained
process-stop evidence. Pure text/HTML/JSON transformations can be replayed
deterministically from retained raw bytes.

The mandatory real PDF acceptance test failed closed on the development host
with `pdf_isolation_unavailable`. The patch's CI workflow installs the required
prerequisites and runs that test without a skip. A supported host must pass it
before its PDF profile is qualified.

## Operator workflow

In `/research`, select the running task and repository state, then choose or
create an information need. Review the displayed query and provider disclosure
before queueing. The progress view shows the retained execution, source outcomes
and accounting origin. A run can succeed while an individual source is
unsupported or unavailable; only complete verified extractions can supply
citations.

Open a source, select a quote and verify it before adding it to a comparison.
Offsets are UTF-8 bytes, so multi-byte characters cannot be split or mistaken
for JavaScript character offsets. The client hashes the exact selected text;
the server independently derives it from the authenticated source artifact.

The CLI uses the same steps. Identity and digest placeholders below are values
returned by the preceding commands:

```sh
potbot research --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  context --episode TASK_UUID --state STATE_UUID

potbot research --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  need-create --episode TASK_UUID --state STATE_UUID --input need.json \
  --key need-001 --confirm

potbot research --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  context --episode TASK_UUID --state STATE_UUID --need NEED_UUID

potbot research --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  admit --episode TASK_UUID --state STATE_UUID --need NEED_UUID \
  --context-sha256 CONTEXT_SHA256 --input query.json --key action-001 --confirm

potbot research --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  run --action ACTION_UUID --policy-sha256 POLICY_SHA256 \
  --key research-001 --disclosure-confirmed --confirm

potbot research --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  worker-once --worker-id research-worker-001

potbot research --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  events RUN_UUID --after 0 --limit 100

potbot research --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  result RUN_UUID

potbot research --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  source RUN_UUID SOURCE_SHA256

potbot research --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  cite --input citation.json

potbot research --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  compare --episode TASK_UUID --need NEED_UUID --context-sha256 CONTEXT_SHA256 \
  --input claims.json --key comparison-001 --confirm

potbot research --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  comparison EVIDENCE_UUID
```

`need.json` uses the existing `NeedSpec` object exposed by the context response.
`query.json` is the exact reviewed `queryTemplate` object from that response,
with any supported operator edits. `worker-once` claims one already admitted
run; browser reads do not launch a worker. Existing service supervision can
invoke that entry point under the same configured workspace and operator
authority.

A citation request contains exactly these fields:

```json
{
  "runId": "RUN_UUID",
  "sourceId": "SOURCE_SHA256",
  "startByte": 10,
  "endByte": 16,
  "quoteSha256": "SHA256_OF_EXACT_SELECTED_UTF8_BYTES"
}
```

The range is start-inclusive and end-exclusive. A quote is 1–16,384 bytes,
must end on UTF-8 boundaries and cannot contain unretained text. The server
supplies the quote, source hashes, locations, dates and exact research scope.
Search snippets are provider lineage and cannot stand in for fetched-source
citations.

`claims.json` contains exactly `{"claims": [...]}`. Each of 1–16 distinct claims
has `key`, `label` and a nonempty `citations` array of the above requests, with
at most 32 citations overall. The label describes the operator's comparison;
it does not become a factual assertion that the engine semantically verifies.
The compared values are the exact quoted strings.

| Comparison state | Meaning |
| --- | --- |
| `supported` | All requested citations were independently verified, meet any declared publication freshness and contain one literal value. |
| `unresolved` | Available values conflict, required publication time is unavailable/stale, or only part of the requested evidence could be verified. |
| `unsupported` | No requested citation can supply usable evidence for this claim and exact need scope. |

Publication timestamps are explicitly source-reported. Date-only or timezone-
free text remains in the raw artifact without inventing a publication instant.
A recent retrieval does not make an old source current. Multiple search engines
returning the same URL retain their separate lineage but do not establish
publisher independence. `license` remains `unknown` when it is unknown.

Reports retain `qualification: verified_cited_source_agreement_only` and
`needResolved: false`. They do not resolve a need, complete a task or prove that
a quoted publisher claim is true. Historical reports are reproduced at their
retained evaluation time. A new need revision cannot adopt old citations as
current-scope support, while a valid historical report remains readable.
Changed signing authority, consumer code or damaged evidence prevents positive
historical verification.

## Recovery and qualification

After a lost write response, retry the **exact original body and key**. A
committed receipt is recovered before consulting changed task or policy state;
the facade cannot silently reserve or dispatch a second attempt. A new key
means a newly reviewed command. Browser recovery records retain only the
reviewed command, scoped identity and key, never a setup token. Every HTTP POST
also carries `X-Potbot-Expected-Principal` from that reviewed scope. The API
rejects a different authenticated principal before any owner call, so backend
credential rotation cannot execute an old tab's recovery as another actor.
Cancellation remains available with ordinary workspace access even if new
admission is blocked.

The automated HTTP fixture uses the production task/need, database, policy,
budget, transport adapters, artifact, extraction, citation and comparison
owners. Only its HTTP transport and provider credentials are synthetic. Its
actual responses are exported for the strict browser contract consumer. The
separate mandatory PDF/process gate checks a real authored document through
the installed isolation owner.

### Verified development results

| Gate | Observed result |
| --- | --- |
| Combined research and affected-owner Python regression | **601 passed**, zero failures/skips: 511 unit cases and 90 real PostgreSQL cases. |
| New database acceptance within that run | **34** external lifecycle cases and **19** cited-evidence/HTTP cases, including current-policy revocation, immutable recovery and actual HTTP fixture export. |
| Complete frontend suite | **369 passed**, zero failures/skips; the 35 research cases consume the real exported HTTP responses with an explicitly synthetic provider transport. |
| Browser TypeScript and production build | Passed. |
| Project Ruff and formatting | Passed; all 1,110 Python files already formatted. |
| Strict project mypy | Passed across 461 source files. |
| Plan consistency | Tool plan: 48 stages; memory plan: 32 stages. Both passed with their negative probes. These checks do not establish runtime qualification. |
| Actual isolated authored-PDF test on this host | **Failed closed** with `pdf_isolation_unavailable`; no skip and no unconfined fallback. The capable-host CI gate remains mandatory. |

The merged U04-T baseline's final observed CI snapshot had 19 successful
workflows and one failed main verification workflow. The failure was a Ruff
formatting gate in `domain/result_assertions.py`; later verification steps did
not run, and dependent evidence uploads consequently lacked their outputs.
This patch includes the one-line signature-format correction, with an unchanged
Python AST, and the local project formatting gate now passes. No remote rerun
or post-merge green CI result is claimed for this uncommitted patch.

This increment implements the supported T05.1/T05.2/T05.4 path, with typed Cog
input/output receipts and causal monitoring. `synActivation` remains
`not_qualified` until an exact deployed SYN release is imported and qualified.
Live-provider credentials/egress and native PDF host qualification require
their own acceptance evidence. The full G07 document/OCR/archive roundtrip
gates and T05.3 scientific/archival connectors remain open, so the canonical
T05 statuses and global runtime qualification are unchanged.

The next coherent implementation batch is **T05.3 specialist retrieval** using
the same admitted research lifecycle: exact query contracts, DOI and author/
title matching, correction/retraction metadata, independent source lineage and
explicit unsupported endpoint behavior. Qualification of the configured live
provider and isolated PDF profiles remains a deployment gate alongside that
implementation work.

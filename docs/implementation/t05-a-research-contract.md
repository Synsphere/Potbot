# T05-A research workbench contract

This is the shared controller, HTTP and browser contract for **T05-A: external
research and cited evidence**. Research actions, queries and retained artifacts
are shared with authenticated members of their workspace. The current response
principal identifies the reader, not necessarily the immutable submitting actor.

## Operator boundary

All workbench responses have `schema: "potbot-research-workbench/v1"` and
`scope: {workspaceId, principalId}`. Bodies are bounded to 131,072 bytes and
responses to 1,500,000 bytes. Responses are private/no-store. Mutations require
an exact allowed Origin, one stable Idempotency-Key and explicit confirmation.
Need creation, action admission, run admission and comparison publication also
require the existing operator setup capability. Cancellation uses ordinary
workspace access. The capability is never returned to browser JavaScript.
Every POST, including cancellation and citation reads, also requires one
`X-Potbot-Expected-Principal` containing the canonical principal UUID from the
reviewed context or original command journal. The BFF forwards it unchanged;
the API checks it against the authenticated actor before any owner call.
A rotated backend bearer cannot execute an old tab's command under another
principal's idempotency namespace. GETs can supply the same optional fence.

The fixed HTTP root is `/v1/ui/research`; the browser BFF is `/api/ui/research`.

| Method and path | Request | Result fields beyond the common envelope |
| --- | --- | --- |
| GET `/context` | `episodeId`, `repositoryStateId`, optional `needId` | `task`, `needs`, `need`, `policy`, `providerDescriptors`, `contextSha256`, `queryTemplate`, `current`, `reasonCodes`, `visibility` |
| POST `/needs` | `{episodeId,repositoryStateId,spec,confirm:true}` | `need` |
| POST `/actions` | `{episodeId,repositoryStateId,needId,contextSha256,query,confirm:true}` | `action`, `actionIdentity`, `reused` |
| POST `/runs` | `{actionId,policySha256,disclosureConfirmed:true,confirm:true}` | `execution` |
| GET `/runs` | optional `episodeId`, `limit` (1–25), opaque `after` | `items` of execution envelopes, `nextCursor` |
| GET `/runs/{runId}` | none | `execution` |
| GET `/runs/{runId}/events` | integer `after` (default 0), `limit` (1–100) | `execution`, `items`, integer/null `nextCursor` |
| GET `/runs/{runId}/result` | none | `execution`, `batch` or null, `verification`, `reasonCodes` |
| GET `/runs/{runId}/sources/{sourceId}` | none | `execution`, `source`, `extraction` |
| POST `/runs/{runId}/cancel` | `{confirm:true}` | `execution` |
| POST `/citations` | one CitationSpec | `citation` |
| POST `/comparisons` | `{episodeId,needId,contextSha256,claims,confirm:true}` | `evidenceId`, `report` |
| GET `/comparisons/{evidenceId}` | none | `evidenceId`, `report` |

Citation POST is an explicit read: it requires Origin and the expected-principal
fence but no setup capability,
confirmation, idempotency header or command journal. It reads retained bytes
and makes no external request. Run-list cursors are at most 512 characters and
reuse the existing creation-time/UUID keyset. Event cursors are integers.
Browser mount and resume perform reads only. A lost mutation retains its exact
original request and key for deliberate recovery.

## Context and action

`task` is `{episodeId,contractRevisionId,repositoryStateId,
semanticReleaseSha256,status}`. `needs` contains at most 25 summaries
`{id,revisionId,contentSha256,status,question}`. The selected `need` is null or
`{id,revisionId,contentSha256,status,spec}`; `spec` is the existing NeedSpec wire
record, including `schema_version:1`. Need creation takes that same record.

`policy` is null when unavailable, otherwise `{sha256,content}` where `content`
is the safe `ExternalResearchPolicy.to_dict()` projection. It contains configured
provider IDs/cost ceilings/rate intervals, allowed fetch hosts and terms flags,
fallback statuses, disclosure permission and request/result/byte/time limits.
`providerDescriptors` lists fixed adapter method/endpoint/revision information;
no credentials are returned. `visibility` is always `workspace`.

### T05.3 specialist provider extension

The same policy and disclosure lifecycle supports at most nine distinct
configured providers: `brave`, `tavily`, `crossref`, `openalex`, `arxiv`, `pubmed`,
`semantic_scholar`, `wikidata` and `internet_archive`. This does not add a browser
endpoint for arbitrary search or SPARQL. The operator still reviews one literal
`search_text` and the fixed provider list before authorizing a run.

Provider descriptors retain exactly `{id,method,endpoint,adapterRevision,mode,
scope}`. Specialist descriptors use `mode:"specialist_retrieval"` and the same
`scope:"explicit_query_disclosure"`. `method:"POST"` describes the admitted
internal query envelope. The pinned adapter generates its documented wire
method, path and parameters; credentials never appear in these descriptors.
PubMed can retain a second request of `kind:"metadata"`, with provider `pubmed`
and its same fixed logical search endpoint, after an admitted search supplies
the exact bounded identifiers. This request is separately authorized, accounted
and retained. It cannot accept a caller-supplied metadata URL.

Each specialist normalized item has the ordinary search fields plus an exact
`specialist` record:

```text
schema: "potbot-specialist-metadata/v1"
recordId: string (at most 512 bytes)
doi: normalized DOI or null
authors: at most 64 names, each at most 512 bytes
publicationDate, eventDate, updatedDate: MetadataDate or null
sourceRole: "primary" | "secondary" | "unknown"
recordKind: "publisher_registry" | "repository" | "bibliographic_index"
            | "knowledge_graph" | "archive_catalogue"
retraction: {state: "reported_retracted" | "not_reported" | "unknown",
             notices: at most 20 Notice records}
```

`MetadataDate` is exactly `{value,precision,provenance}`. Value and provenance
are at most 128 bytes; precision is `year`, `month`, `day`, `instant` or
`reported`. The first four retain their actual temporal precision; `reported`
retains a bounded unparsed source string. Partial dates never become fabricated
UTC instants for freshness. Ordinary `publishedAt` remains an actual UTC instant
or null. Retrieval time is separate from all three metadata dates.

`Notice` is exactly `{kind,relation,targetDoi,date}`. Kind is `retraction`,
`correction`, `update`, `expression_of_concern` or `withdrawal`; relation is
`updates`, `updated_by` or `record_flag`; target DOI and MetadataDate may be
null. A notice's direction is retained. `not_reported` means only that this
provider record does not report a retraction; it is not a clean-status verdict.

Merged results retain their existing five fields and every complete normalized
lineage item. Exact URLs can join as before. Distinct specialist URLs can join
by matching non-null DOI, or by a conservative normalized title and complete
author-set match without conflicting non-null DOIs. Every pair in a group must
match; a DOI-less intermediary cannot bridge two conflicting DOIs. The first
observed URL, title and snippet remain the displayed representative. Separate
indexes reporting the same work do not prove independent publisher evidence.
The browser displays the original provider metadata for each lineage item and
keeps authored source citation requirements unchanged.

`contextSha256` pins the current reader/workspace, exact task and selected need,
and policy fingerprint. Action admission rechecks it. `queryTemplate` is null
without a current need and policy; otherwise it is an editable existing
ResearchQuery wire object derived from the need's actual decision and evidence
criteria. The admitted external selector is exactly:

```json
{
  "category": "documentation",
  "operation": "query_source",
  "locator": "external-research",
  "target_version": "POLICY_SHA256"
}
```

`max_results` and `max_source_bytes` equal the policy ceilings. The remaining
ResearchQuery fields retain literal search text, required identifiers, expected
observation, decision effect, evidence requirement, stopping condition, fallback
description and freshness seconds. A fallback description cannot authorize a
new provider or destination. `action` is the existing ResearchAction snake-case
wire record. Admission is inert; the separate reviewed run mutation authorizes
the configured disclosure and resource reservation.

## Execution and extracted sources

`execution` is the ResearchRunStore envelope
`potbot-external-research-run/v1`. Its `scope` names the current reader. `run`
retains IDs, task/need revision, state, revision, timestamps, safe error code,
cancellation flag, authorization/policy/consumer hashes, disclosure decision,
reservation, output/receipt hashes, request count, usage origin and workspace
visibility. `action` retains the original action, `authorization` its safe
signed-admission projection, and `receipt` is null until available.

Result `verification` is `pending`, `verified`, or `unavailable`. A verified
`batch` is `potbot-external-research-batch/v1`, with exact action/run/policy/
consumer identities, retained request receipts, normalized search results and
lineage, document records, settled usage, typed transformation provenance,
completeness and safe reason codes. Search snippets are discovery observations;
citations require a fetched document.

A document's `id` and `sourceId` are the same SHA-256 identity. It retains
`requestedUrl`, `resolvedUrl`, `retrievedAt`, `rawArtifactId`, `rawSha256`,
`extractionArtifactId`, `extractionSha256`, `status` and `reasonCodes`.

An extraction has schema `potbot-extracted-source/v1` and these exact fields:
`status`, `sourceSha256`, `mediaType`, `extractorId`, `extractorSha256`, `text`,
`textSha256`, `segments`, `metadata`, `complete`, `license`, `reasonCodes`,
`execution`. Execution is null for pure extraction or the actual PDF supervisor
receipt `{profileSha256,executableSha256,isolationSha256,elapsedMs,processesStopped}`.
Statuses are `extracted`, `unsupported`, `invalid`, `over_limit` or
`extractor_unavailable`. Text is canonical UTF-8 with LF; its SHA-256 covers
those exact bytes. A segment is `{startByte,endByte,kind,locator,page}`, with
kind `text`, `html_element`, `json_pointer` or `pdf_page`; page is null or a
positive page number. Metadata is `{title,publishedAt,publicationProvenance}`;
publication fields are null or explicitly `source_reported`. Retrieval time
does not establish publication time. License is explicitly `unknown`.

## Citations and comparison

CitationSpec has exactly `{runId,sourceId,startByte,endByte,quoteSha256}`.
Offsets are **UTF-8 byte offsets, start inclusive and end exclusive**. Both
boundaries must be codepoint boundaries. Quotes contain 1–16,384 bytes, and
`quoteSha256` covers the exact selected UTF-8 bytes. Browser string indices are
UTF-16 and must never be used as these offsets without conversion.

A verified citation has schema `potbot-research-citation/v1`, the five spec
fields, `rawSha256`, `textSha256`, `receiptSha256`, `quote`, `requestedUrl`,
`resolvedUrl`, `retrievedAt`, `publishedAt`, `publicationProvenance`, `license`,
`lineStart`, `lineEnd`, `segments`, `researchScope`, `freshnessSeconds`, and
`sha256` over the full record excluding `sha256`. Its scope is the original
ResearchScope. The citation owner verifies the run receipt and immutable raw
and extraction artifacts before deriving the quote. Invalid evidence raises a
bounded error; caller text is never treated as the retained quote.

Comparison input contains 1–16 distinct claims, each exactly
`{key,label,citations}`. Key is a literal identifier, label is a bounded
operator-authored description, and citations are 1–16 CitationSpecs. A total of
32 citations is allowed. The selected literal quote is the value being compared;
the operator is responsible for assigning comparable excerpts to a claim key.
No semantic entailment or model-produced verdict is inferred from a label.

The report has schema `potbot-research-comparison/v1`, `workspaceId`, `episodeId`,
`needId`, `needRevisionId`, `contextSha256`, `evaluatedAt`, `claims`, `status`,
`qualification:"verified_cited_source_agreement_only"`, `needResolved:false`,
`sha256`. Each claim has `key`, `label`, `status` (`supported`, `unresolved`,
`unsupported`), verified `citations`, `values` (distinct exact quotes),
`reasonCodes`, and `unavailableCitationCount`. Unequal cited values remain
unresolved. Missing, corrupt, stale or required-but-undated evidence cannot
support a claim. When freshness is requested, its age is evaluated from a
source-reported publication time; retrieval time is kept separate. The report
states the evaluation time and remains historical evidence after revisions.

Reports are retained through the existing ArtifactStore and evidence-event
owner, with no separate claim registry or need-completion transition. A readback
revalidates their hashes, source chains and deterministic comparison at the
recorded evaluation time. Labels and content render inertly in the browser.

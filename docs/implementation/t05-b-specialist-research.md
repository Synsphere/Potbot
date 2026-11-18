# T05-B specialist scientific and archival research

Baseline: merged Potbot `2fdce72e5f037c883d67b53838e45ff473eb40be`, reviewed on
18 November 2026. This increment completes the remaining supported T05.3
implementation and proceeds to R7.1 temporal steering. New Syn integration is
deferred until Syn is ready for a release; existing authority remains enforced.

## Delivered behavior

Seven fixed adapters extend the same research policy, durable queue, per-send
admission, task budget, artifacts, monitoring and independent audit. No new
scheduler, registry, credential store or network authority is introduced.

| Provider | Supported query | Retained metadata |
| --- | --- | --- |
| Crossref | Bibliographic text or exact DOI | Publisher registry identity, authors, publication/event/update dates, correction and retraction relationships |
| OpenAlex | Text or DOI filter | Work identity, authors, publication/update dates, provider-reported retraction flag |
| arXiv | Literal text/DOI search or explicit `arxiv:` identifier | Versioned repository identity, authors, first-submission and version dates, declared DOI |
| PubMed | Literal text or DOI search | Separately requested summaries, exact PMID/DOI identities, authors, publication text and notice/publication types |
| Semantic Scholar | Text or DOI lookup | Paper identity, DOI, authors, publication date/year; missing retraction data stays unknown |
| Wikidata | Exact English label, Q identifier or DOI | Fixed generated SPARQL, entity identity, publication and event claims with unknown source date precision |
| Internet Archive | Literal catalogue search | Item identity, creators, work/event date separately from catalogue availability date |

The fixed endpoint and adapter source references are documented in
`src/potbot/research/specialist.py`. Arbitrary SPARQL, endpoint overrides,
pagination, source-script execution and implicit follow-up HTTP calls are not
part of these profiles. Unsupported service access, error responses and changed
wire contracts cannot become positive evidence.

The public research request retains an inert, canonical POST adapter envelope.
The pinned adapter derives the actual read-only GET route before send; provider
response receipts name the exact admitted envelope and consumer implementation.
This distinction is explicit in the [HTTP contract](t05-a-research-contract.md).
Credentials enter only fixed headers and never query strings or persisted
request bodies. The real pinned HTTPS route and Authorization-header checks are
covered with a synthetic final socket backend in addition to adapter fixtures.

## PubMed follow-up ownership

ESearch returns identifiers. A successful nonempty response therefore derives
one `metadata` request for ESummary. The research owner reconstructs its exact
IDs from the retained search bytes before dispatch and again during final
audit. It rejects substituted, omitted, duplicated or additional identifiers,
out-of-order requests and duplicate dispatch. Empty search results require no
child. Metadata failures follow only the already approved fallback policy.

Both requests have separate admission, request/response artifacts, causal
monitoring, rate enforcement and measured usage. Admission reserves two possible
calls for PubMed. A timeout or unknown send retains the existing indeterminate
hold and is never retried automatically.

## Identity, dates and evidence

Results first group exact canonical URLs, then match work identities using an
equal normalized DOI or conservative normalized title plus complete author
list. Conflicting known DOIs do not merge through a DOI-less intermediary. A
cross-URL group requires pairwise compatibility among its complete lineages.
Each provider's URL, rank, text, dates, record identity and retraction/correction
claims remain separately inspectable.

Metadata retains source role (`primary`, `secondary`, or `unknown`) and record
kind. These labels describe the metadata source. A registry and an index of the
same paper do not become two independent confirmations of the paper's findings.

Publication, event, update and retrieval dates remain separate. Year/month/day
precision is retained without inventing an instant. Wikidata's truthy projection
does not expose the original Wikibase date precision, so its dates remain
reported strings. Archive catalogue availability does not make an old work
newly published. Only an explicitly timezone-bearing publication instant feeds
the existing `publishedAt` field.

Missing retraction information remains unknown. A correction notice's `updates`
relationship points to the affected work; it does not mark the notice itself as
retracted. Conflicting provider flags remain visible. Metadata and snippets
cannot replace exact fetched-source citations, resolve needs or complete tasks.

## Configuration and limits

Use the existing `POTBOT_RESEARCH_POLICY` file and `/research` workbench or
`potbot research` CLI. Select any subset of the nine supported providers,
including the previous Brave/Tavily profiles. The following policy reviews only
scientific metadata and performs no document fetches:

```json
{
  "schema": "potbot-external-research-policy/v1",
  "allow_query_disclosure": true,
  "providers": [
    {"provider": "crossref", "cost_microunits_per_call": 0, "minimum_interval_ms": 1000},
    {"provider": "openalex", "cost_microunits_per_call": 0, "minimum_interval_ms": 1000},
    {"provider": "pubmed", "cost_microunits_per_call": 0, "minimum_interval_ms": 400}
  ],
  "fetch_hosts": [],
  "fallback_statuses": ["rate_limited", "quota_exhausted", "unavailable"],
  "max_results": 8,
  "max_documents": 0,
  "max_requests": 4,
  "max_source_bytes": 262144,
  "timeout_seconds": 15
}
```

The costs above are illustrative operator-configured ceilings, not a pricing
claim. Account access and policy must be reviewed before use. Optional credentials
use `research.openalex` / `POTBOT_OPENALEX_API_KEY` and
`research.semantic_scholar` / `POTBOT_SEMANTIC_SCHOLAR_API_KEY`. Current fixed
profiles use OpenAlex's Bearer header and Semantic Scholar's `x-api-key` header.
The other new profiles do not read credentials.

Configured intervals cannot lower the adapter floors: arXiv 3,000 ms, PubMed
400 ms, and 1,000 ms for the other specialist providers. These intervals and
PubMed's possible extra call are included in the displayed and native budget
reservation. Existing workspace-level rate serialization is retained.
Operator-wide/IP-wide sharing across workspaces, installations and other clients
must be qualified separately; per-workspace intervals do not prove fleet-wide
service compliance or arXiv's single-connection requirement.

Each specialist batch is limited to 40,000 normalized UTF-8 bytes, 20 records and
262,144 raw bytes. Actual routes are bounded to 2,048 bytes and validated before
admission. The global provider count is nine, total request limit 32 and document
limit eight. Rejected ranks remain explicit and complete raw bytes remain in
the original artifact. The existing public response bound is unchanged.

## Verification and rollout

`tests/integration/test_research_specialist.py` exercises actual PostgreSQL,
HTTP, budget, artifact and audit owners with explicitly synthetic provider
responses. Its HTTP export is consumed by the strict browser projectors. It
demonstrates one paper matched across Crossref, OpenAlex and PubMed while keeping
all three URLs, partial dates and conflicting notices. Negative cases cover
metadata substitution, repeated sends, rate waits, scope expansion and fallback.

The T05 workflow retains mandatory real isolation/PDF acceptance and now includes
the specialist lifecycle and fresh browser contract. Local fixtures do not
qualify live accounts, provider relevance or a deployment host. The merged
baseline's dedicated research CI passed its real PDF/isolation step; its later
dependency-installation failure is a separate issue addressed in this patch.

This research increment needs no additional database migration beyond T05-A's
47. The accompanying R7.1 steering extension requires migration 48. Retained
research attempts bind their exact consumer digest: stop/drain active workers
before upgrading, then review new actions under the new implementation. Do not
repin or replay a historical uncertain attempt to make it appear current.

Rollback uses the existing installed-version/release process after workers stop.
Keep immutable history and any held usage. Broader G07 document/OCR/archive
formats, live vendor/task quality and deployed Syn activation retain their
separate gates; global runtime qualification stays false. Continue with R7.2
atomic task admission and scheduling after this batch.

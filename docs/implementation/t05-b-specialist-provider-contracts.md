# T05-B specialist provider contracts

This reference describes the T05.3 adapter layer added after merged T05-A
`2fdce72e5f037c883d67b53838e45ff473eb40be`. It uses the existing external research
admission, request ledger, budget, evidence, source artifacts and operator
workbench. A configured query remains an explicit disclosure to each selected
provider. Metadata never grants permission to fetch a returned source.

## Supported read profiles

| Provider | Logical endpoint | Generated read operation | Identity and metadata |
| --- | --- | --- | --- |
| Crossref | `https://api.crossref.org/works` | Bibliographic query; explicit DOI selects `/works/{doi}` | Publisher-deposited registry metadata, authors, partial publication dates, event start, deposit time and correction/retraction relationships. |
| OpenAlex | `https://api.openalex.org/works` | Search; DOI filter | Secondary bibliographic index, authors, publication date and explicitly reported retraction flag. |
| arXiv | `https://export.arxiv.org/api/query` | Literal `all:` phrase; `arxiv:ID` selects a version-preserving ID lookup | Primary repository metadata, original submission and current version dates, authors and optional DOI. |
| PubMed | `https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi` | Literal query or DOI article-ID lookup; separately admitted ESummary child | Secondary bibliographic metadata, exact search IDs, authors, DOI and structured publication types, including retracted publications and notices. |
| Semantic Scholar | `https://api.semanticscholar.org/graph/v1/paper/search` | Search; explicit DOI selects the paper endpoint | Secondary bibliographic metadata, authors, DOI and publication date/year. Missing retraction information stays unknown. |
| Wikidata | `https://query.wikidata.org/sparql` | Generated, bounded SPARQL for an exact English label, `Q` identifier or DOI | Secondary knowledge-graph record; publication (`P577`) and event (`P585`) dates remain separate. |
| Internet Archive | `https://archive.org/advancedsearch.php` | Literal catalogue phrase with fixed returned fields | Catalogue item identity and creator; historical work/event date remains separate from the catalogue public date. |

The logical method remains `POST` because the admitted request contains an inert,
canonical JSON query envelope. The pinned transport derives one exact `GET` wire
request. It never forwards the envelope to the provider, accepts caller-selected
endpoints, runs arbitrary SPARQL, follows redirects or performs hidden pagination.
`specialist_search_body` validates both the textual query and the generated
2048-character ASCII URL before admission. A long percent-encoded query can
exceed the URL bound even when it fits the textual bound.

DOIs support bare `10.…` strings, the `doi:` prefix and exact `doi.org` or
`dx.doi.org` resolver URLs. Case normalization retains suffix punctuation.
Arbitrary URLs, resolver query strings, fragments and credentials are rejected.
DOI lookup is direct for Crossref, OpenAlex, PubMed, Semantic Scholar and
Wikidata. For arXiv and Internet Archive, a DOI is a literal search phrase;
finding that phrase does not establish an exact identifier match.

OpenAlex permits a basic keyless request and an optional configured bearer token.
Semantic Scholar accepts an optional configured `x-api-key`. Keys enter headers
only and do not enter the query envelope, URL, result metadata or artifacts.
An access denial, rate limit, unavailable endpoint or changed response schema
remains an explicit unsuccessful outcome; no alternate undocumented API is used.

## PubMed request boundary

ESearch returns identifiers rather than complete bibliographic records. The
controller retains its exact raw response, derives a bounded summary envelope
from those IDs, and admits that second request with its own cost, request and
rate accounting. Empty ESearch results need no child request. Nonempty ESearch
results cannot masquerade as a complete normalized metadata batch.

Summary verification requires exactly the IDs in the retained search response,
in the same order, with matching per-record IDs and no duplicate, omitted or
injected rows. The adapter itself performs no network I/O. An unavailable child
request does not fabricate titles, citations or publication metadata from an ID.

## Metadata and identity grouping

Each normalized specialist result preserves the existing web-result fields and
adds `specialist`, with schema `potbot-specialist-metadata/v1`:

- Provider record identity, optional normalized DOI and a bounded author list.
- Separate publication, event and update dates, each with literal value,
  precision and field provenance. Partial dates never become invented midnight
  instants. Wikidata's truthy date projection lacks source precision and remains
  `reported`; an Archive public date never makes a historical item current.
- Metadata source role (`primary`, `secondary` or `unknown`) and record kind.
  A primary metadata record is not an independently verified scientific claim.
- Provider-reported retraction status and directional correction/retraction
  notices. Crossref `update-to` identifies the work affected by a notice; it does
  not mark that notice itself as retracted. An absent flag remains `unknown`;
  an explicit false flag is `not_reported`.

Exact URL buckets are formed first, preserving all provider lineage including
conflicting metadata. Distinct URL buckets can then join only when every
cross-bucket pair agrees on an explicit DOI, or has the same normalized title
and complete author set without conflicting DOIs. The latter requires a title
of at least 12 characters and at least one author. Normalization is Unicode
NFKC, lower case and whitespace-separated letters/numbers; it is not fuzzy name
resolution. A DOI-less record cannot bridge two already conflicting works.
Web-only results support URL equivalence, not inferred bibliographic identity.

The existing merged-result schema is unchanged: the first canonical URL supplies
its source ID and display fields, and `lineage` retains every contributing
record. Two APIs indexing the same work do not establish independent publishers,
nor do search metadata or abstracts replace fetched-document citation spans.

## Bounds and qualification

Each query returns at most 20 selected records; a raw response is at most
262,144 bytes; each normalized specialist batch is at most 40,000 UTF-8 bytes.
Excess or invalid records receive explicit rejected ranks instead of silent
metadata truncation. ArXiv Atom parsing denies DTDs and entity declarations and
bounds XML shape; JSON parsers reject duplicate keys and nonfinite numbers.

The provider policy must respect the adapter's conservative interval floor:
arXiv 3,000 ms, PubMed 400 ms and other specialist providers 1,000 ms. These
floors do not promise available quota. ArXiv's published limit applies across
all operator-controlled machines and requires one connection at a time; a
deployment must coordinate that aggregate rate as well as its local ledger.

Automated adapter fixtures exercise all seven wire profiles, construction of
the real pinned HTTPS route, metadata parsing, malformed responses, DOI and
author/title matching, correction direction, source dates and bounded output.
These tests do not establish live provider availability, account entitlement,
installed-host PDF isolation or deployed Syn activation. Those remain separate
qualification evidence. Syn release work is deferred as requested by the user;
the adapter layer does not create a substitute Syn runtime or claim a release.

## Primary API references

These references were inspected on 18 November 2026. Their contracts and account
limits can change; installed profiles require current provider acceptance.

- [Crossref REST API](https://github.com/CrossRef/rest-api-doc) and
  [response format](https://github.com/CrossRef/rest-api-doc/blob/master/api_format.md).
- [OpenAlex authentication](https://help.openalex.org/api/authentication/) and
  [work attributes](https://help.openalex.org/data/works/attributes/).
- [arXiv API manual](https://info.arxiv.org/help/api/user-manual.html) and
  [API terms/rate limits](https://info.arxiv.org/help/api/tou.html).
- [NCBI E-utilities parameters and pipelines](https://www.ncbi.nlm.nih.gov/sites/books/NBK25499/).
- [Semantic Scholar Academic Graph API](https://api.semanticscholar.org/api-docs).
- [Wikidata Query Service manual](https://www.mediawiki.org/wiki/Wikidata_Query_Service/User_Manual).
- [Internet Archive advanced-search interface](https://archive.org/advancedsearch.php)
  and [developer portal](https://archive.org/developers/).

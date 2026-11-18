# U04-D registry source and ranking contracts

This document describes the bounded source-observation layer. Source and publisher
admission, retained snapshots, review receipts and lifecycle handoff belong to
the discovery service. Skill installation remains owned by U04-A; executable
acquisition, qualification and release remain owned by U04-X and S9–S12.

## Supported source interfaces

Primary documentation checked on 18 November 2026:

| Adapter | Documented interface | Acquisition boundary |
| --- | --- | --- |
| `curated` | Explicit local `potbot-curated-registry/v1` JSON file | Exact public GitHub skill commit/path, existing approved tool recipe plus exact repository/commit, or inspection only |
| `clawhub` | [Official HTTP API](https://docs.openclaw.ai/clawhub/http-api): public `/api/v1/search`, `/skills`, exact skill versions and download | Owner-qualified identity and complete path/size/SHA-256 inventory are required before skill bytes are accepted |
| `mcp_registry` | [Official API](https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/api/official-registry-api.md), [generic API](https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/api/generic-registry-api.md): `/v0.1/servers`, exact versions, opaque cursors and status metadata | Registry server/package declarations are inspection-only; they do not install or configure an MCP server |
| `skills_sh` | [Documented API](https://www.skills.sh/docs/api): `/api/v1/skills`, search and exact observed file snapshots | Dedicated Vercel OIDC credential required; absent credentials report `authentication_required`. No fallback to a different CLI endpoint |

ClawHub downloads may contain a hosted ZIP or a `public-github` JSON handoff.
For a handoff, Potbot validates the repository, exact commit and subdirectory,
constructs the existing fixed-host GitHub fetch, and checks the selected complete
file inventory. It never follows the handoff's `archiveUrl`. A Git-backed listing
without a supported exact version and complete file hashes remains unavailable
for this acquisition route. Additional unlisted archive files fail verification.

The MCP registry may return a deprecated latest version. Potbot retains its
status; it neither selects a different version implicitly nor treats lexical
version ordering as semantic precedence. The declared namespace is registry
identity evidence, not an independently verified publisher signature. Package
arguments and environment declarations stay untrusted metadata.

The skills.sh file snapshot has no demonstrated immutable Git revision. Potbot
uses an independently computed digest of the exact source identity and complete
file inventory as `snapshot:<sha256>`. Its `responseSha256` is the canonical hash
of those stable response fields, excluding popularity counters and JSON object
ordering. The separately retained `claimedContentHash` is a registry assertion,
not a claim that Potbot knows the registry's hashing algorithm. Re-fetching a
changed file, file set, source identity or claimed hash fails the selected review.

## Python interfaces

`RegistryCandidate.from_dict(value)` and `RegistryPage.from_dict(value)` validate
their complete schemas; `.to_dict()` returns detached JSON-compatible values.
Candidate nested objects are stored as immutable canonical JSON. `.sha256`
identifies the entire normalized candidate, including its provenance and
unsupported conditions. This digest is not an authorization signature.

`RegistrySources` accepts an already parsed tuple of curated candidates, an
optional injected HTTP transport, an optional dedicated skills.sh token callback,
a 1–86,400 second TTL, and an injectable UTC clock. No source configuration or
candidate contains credentials. Its methods are:

```python
search(adapter, query, cursor=None, limit=25) -> RegistryPage
inspect(adapter, canonical_id, version=None) -> RegistryCandidate
resolve(adapter, canonical_id, version) -> RegistryCandidate
fetch_skill(exact_candidate) -> SkillPackage
```

`inspect` may discover the current exact version. `resolve` requires the selected
exact version and rejects moving selectors such as `latest`, `main` and `HEAD`.
`fetch_skill` returns a verified private data package; it performs no installation,
activation, dependency execution or credential injection. A curated executable
recipe target is handled by the existing acquisition service, never by this
method. Git source receipts preserve the existing U04-A format.

The injectable transport signature is
`(url, *, headers, max_bytes) -> RegistryHTTPResponse(status, headers, body)`.
The default transport permits only the three fixed registry HTTPS hosts, disables
ambient proxies and redirects, and forwards a dedicated credential only to
`skills.sh`. No API bearer or setup capability is forwarded. HTTP failure bodies
are discarded; returned failure codes and retry delays are bounded.

## Bounds and identity rules

| Boundary | Limit or behavior |
| --- | --- |
| Metadata response | 2 MiB, duplicate JSON keys rejected, bounded depth and item count |
| Normalized candidate | 128 KiB; source text is display data |
| Registry page | 1–100 candidates; 2 MiB normalized byte limit |
| Complete curated file | 2 MiB; 1–256 exact unique entries |
| Source query / cursor | 256 / 2,048 Unicode characters |
| Network request | 10 second socket timeout, 20 second total read allowance, no redirects |
| Skill ZIP | Existing 4 MiB compressed bound |
| Skill contents | Existing 128 files, 1 MiB per file, 8 MiB total |
| Requirements | 128 clauses and 64 KiB canonical JSON; only canonical binary/OS OR groups |
| Ranking collection | 4,096 validated candidates, matching the service's bounded source collection |
| Ranking query | 500 characters and at most 64 distinct meaningful terms |

Malformed or oversized responses fail closed. A repeated cursor is rejected;
the durable refresh owner also bounds total pages and rejects cursor cycles.
A short ClawHub page with a next cursor is not end-of-results. Each exact
publisher/slug or namespace/server/version identity is distinct. Similar names
do not merge candidates. Successful pages retain ETag and retrieval/expiry
metadata; failed pages contain no stale candidates. Conditional collection-wide
revalidation is not inferred from one page's ETag.

ClawHub exact-release hashes exclude mutable download counters and moving latest
tags. They retain selected publisher metadata, exact version/file hashes and
reported moderation, so changes to those review-relevant fields invalidate the
candidate. Discovery snapshots separately retain collection content identities.

## Curated manifest format and truthful example

`examples/registry/curated-capabilities.json` contains 20 inspection profiles
covering the plan's capability classes. Each retains the reviewed source commit,
relevant existing file hashes and `runtimeQualified: false`. These are declarations
for inspection; none ships an executable distribution or becomes trusted merely
because it appears in the repository. Android, Apple, remote and hardware profiles
explicitly retain `planned_unavailable`, with no claimed supported runtime.

A curated file has `schema: potbot-curated-registry/v1` and `entries`. Entries use
the candidate wire fields without the four derived fields `schema`, `adapter`,
`manifestSha256`, and `sourceSha256`. The parser derives those fields and binds
the original complete curated file's SHA-256. Source creation separately admits
that file digest. A candidate distribution is one of:

- `inspect_only` with no further fields.
- `github_skill` with canonical `repository`, exact 40-character `commit`, and
  explicit relative `path` (empty for repository root).
- `tool_recipe` with an existing `recipeId`, canonical `repository`, and exact
  `commit`. The recipe owner independently verifies all bindings before handoff.

Registry-specific distributions are created only by their source adapters and
cannot be supplied through a curated file. Arbitrary fetch URLs, shell commands,
permission grants and auto-activation fields are not supported distribution
contracts.

## Explainable task selection

`rank_candidates(candidates, query, *, constraints=None, installed_sha256=(),
denied_sha256=(), limit=10)` produces an inspection shortlist plus explicit
exclusions. Supported constraints are `classes`, `inputTypes`, `outputTypes`,
`platform`, `permittedEffects`, `permittedScopes`, and `requireLocal`.

Hard exclusions run before scoring: current owner policy denial, deleted or
deprecated source state, explicit incompatible classes/Types/platforms,
disallowed or unknown effects, denied scopes and unproved local runtime.
The reviewed retrieval classes do not replace the existing SYN Type or Cog
registry. Every returned item has `executable: false`; task admission and actual
operation dispatch remain separate authorities.

Scores combine reviewed literal task aliases, declared canonical class fit and
bounded unique-term relevance in descriptions. Repeating words adds no score.
Candidate names, publisher identity, popularity and registration order add no
score. Current installed-route preference must be supplied by the existing owner
and applies only to an adequate match. A remote manifest's `installed`, `safe`
or other self-description cannot create this preference. Equal scores use a
content digest only as a stable display tie-breaker.

`discovery_installed.installed_skill_preference` supplies this preference from
at most 256 of the current principal's active, enabled acquired skills. It
verifies the existing control MAC and immutable decision, then matches exact
Git repository/commit/subdirectory or the complete resolved registry inventory
and retained provenance. It does not read resource bytes or infer native-tool
installation from recipe existence. Verified control revision, package and
source bindings participate in result cursors, so disable, update, revocation
or key rotation withdraws a formerly preferred route. This bounded preference
does not redefine which skills are visible through the existing Context owner.

`tests/fixtures/discovery/golden-requests.json` contains 40 reviewed requests.
Tests cover expected inspection classes, ownership and ordering neutrality,
keyword repetition, denied effects/scopes, optional platform absence and the
no-suitable-candidate outcome. Cost, latency, reliability and runtime qualification
are explicitly unknown; no numeric estimates are fabricated. These fixture
results do not qualify third-party packages, live network contracts, hardware,
or the entire T04 implementation stage.

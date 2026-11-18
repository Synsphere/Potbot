# U04-S task-aware capability discovery contract

Baseline: `d904442b7f08c3b7c6c975092327cc89646ae562` (merged U04-Q).
All envelopes contain `schema: potbot-task-discovery/v1`, `workspaceId`,
`principalId` and the named result. Lists contain `items,nextCursor` with
canonical UUID cursors and limits 1–25. IDs are canonical UUIDs unless explicitly
described as SHA-256. Hashes are 64 lowercase hexadecimal characters.

Reads contact only existing retained owners. Native inference is an explicit
CLI operation. Mutations require the existing scoped bearer, exact allowed
Origin, separate setup capability, one stable Idempotency-Key beginning with an
alphanumeric character, and an exact bounded JSON body with `confirm:true`.
Responses are private/no-store. Original command receipts are recoverable with
identical request bytes; they do not refresh the current task or candidate set.

## Routes

- `GET /v1/ui/task-discovery/context?episodeId=UUID&stateId=UUID` → `context`.
- `GET /searches?episodeId=UUID&limit=25&after=UUID` → list of searches.
- `GET /searches/{id}` → `search`.
- `GET /results/{id}` → `result`.
- `GET /selections/{id}` → `selection`.
- `GET /searches/{id}/candidates/{candidateId}` → `candidate,detail`.
- `POST /searches` → `search`; creates a lexical result or one pending hybrid request.
- `POST /searches/{id}/cancel` → `search`.
- `POST /searches/{id}/select` → `selection`; revalidates existing owners and retains a handoff.

There is no browser inference, model download, index publication, provider
configuration, tool execution, automatic installation or uploaded-score route.

## Task and request

`taskBinding` has exactly the U04-Q fields: `episodeId,contractRevisionId,
contractSha256,repositoryStateId,repositorySha256,semanticReleaseId,
semanticReleaseSha256`. It selects an existing running task and exact state.

Constraints have exactly `classes,inputTypes,outputTypes,platform,
permittedEffects,permittedScopes,requireLocal,requireTaskEvidence`.
Request arrays are bounded to 64 unique strings, sorted during normalization;
candidate capability arrays are bounded to 32. Class/type arrays default to
empty; platform defaults to null. Effects/scopes
are null for unspecified or arrays for an explicit allowance, including empty.
The two requirements are Boolean and default false. User constraints cannot
broaden the existing task contract. Canonical Syn/Synny aliases are equivalent.
Supplied inputs must be subtypes of accepted inputs; produced outputs must be
subtypes of requested outputs. Unknown required evidence cannot prove a match.

The context object contains exactly:
`taskBinding,taskStatus,taskCapabilities,catalogueSha256,typesSha256,
policySha256,consumerSha256,contextSha256,counts,embeddingSpaces`.
`taskStatus` is `running`. Counts contain `registry,operations,mcp`.
Each embedding-space option contains `spaceId,embeddingName,profileSha256,
model,revision,dimensions,maxInputTokens,maxBatchSize`; this is a retained
registration, not proof of local model availability or quality.

The search creation body is exactly:
`{episodeId,stateId,contextSha256,query,constraints,mode,limit,embedding,confirm}`.
Query is nonempty plain text of at most 500 characters; limit is 1–10.
Mode is `lexical|hybrid`. Embedding is null for lexical, otherwise exactly
`{spaceId,embeddingName,profileSha256,maxCandidates,runtimeBudgetMs}`.
Candidate allowance is 1–16; total native runtime is 1–120,000 milliseconds.
The embedding space/name/profile must match a retained registered view.

## Search

Exact fields:
`id,revision,bindingSha256,status,taskBinding,contextSha256,catalogueSha256,
typesSha256,policySha256,consumerSha256,query,constraints,mode,limit,embedding,
candidateCount,resultId,resultSha256,selectedCandidateId,selectionId,reasonCode,
approvalValid,authorityCurrent,reasonCodes,actions,createdAt,updatedAt`.

Status is `requested|running|completed|selected|failed|cancelled|indeterminate`.
Revisions are positive safe integers. Result/selection IDs, result hash and
selected candidate SHA are nullable. Candidate count is 0–256. Actions are
drawn from `cancel|select`; selection is available only for a currently valid
completed nonempty shortlist. Native claim tokens never appear in this DTO.

Cancel accepts exactly `{revision,digest,confirm:true}`. Select accepts exactly
`{revision,digest,candidateId,confirm:true}`, where candidateId is the SHA-256
identity of an included result. A selection grants no execution permission.

## Candidates and ranking

Candidate identity is a SHA-256 of exact existing-owner identity/observation.
The safe candidate fields are:
`id,kind,name,description,ownerId,bindingId,ownerSha256,capabilities,
requiredTaskCapability,installed,authorityCurrent,reasonCodes,taskEvidence`.
Kind is `registry|operation|mcp`. Name/description are bounded to 256/4,096
characters. Owner ID is a UUID or SHA-256 according to its existing owner;
bindingId is a UUID only for MCP, otherwise null.

Capabilities contain exactly `classes,inputTypes,outputTypes,effects,scopes,
platforms`, each a bounded array. Required task capability is nullable text.
Installed/current are Boolean observations, never grants.

Task evidence has exactly `status,suiteId,reportId,reportSha256,caseCount,
passedCases`. Status is `none|matching_pass|matching_failure|historical`.
Only an independently valid current report for the identical task/state,
installation and current reference can be matching evidence. Historical or
other-task results cannot confer a positive task-success preference.

Ranked items extend the candidate with `score,features,typeFit,taskPermission,
reasons,executable`. Score and all feature contributions are nonnegative safe
integers. Features contain exactly `classes,lexical,types,installed,evidence,
semantic`. Type fit is `not_requested|exact|compatible|unknown|incompatible`.
Task permission is `permitted|denied|unknown`. Executable is always false.
Excluded rows contain exactly `id,reasonCodes`, retaining hard-filter reasons.

The corpus is bounded to 256 candidates and 1 MiB of canonical summaries. Hard
exclusions happen before ranking or semantic refinement. Names, publisher
ownership, repeated keywords and registration order supply no trust bonus.
Unknown cost, latency and general quality remain unknown. A positive cosine
score is a retrieval feature; it cannot override a hard exclusion.

## Result

Exact fields:
`id,searchId,sha256,approvalValid,authorityCurrent,reasonCodes,taskBinding,
catalogueSha256,typesSha256,policySha256,consumerSha256,query,constraints,mode,
items,excluded,terms,inferredClasses,noSuitableCandidate,semantic,recordedAt`.

Items are the ordered 0–10 ranked candidates. Exclusions are bounded by corpus
size; terms/classes are bounded string arrays. No-suitable is true exactly when
items is empty. Results are immutable; source/task/policy changes make them
historical and block a fresh selection without rewriting the original result.

Semantic is null for lexical results and hybrid requests with no hard-eligible
candidate (which complete without inference), or exactly the native summary
`{spaceId,embeddingName,profileSha256,querySha256,documentsSha256,candidateCount,
calls,inputTokens,elapsedMs,reservationIds,usageOrigin}`. No vectors or raw
embedding documents are public. The existing local embedding owner batches
within its registered profile, charges the existing task budget and retains
uncertain compute outcomes. No private text is copied into shared sources,
artifacts or a new vector index. Native retries do not rerun an existing claim.

## Selection and progressive inspection

Selection contains exactly `id,searchId,resultId,candidateId,sha256,
approvalValid,authorityCurrent,reasonCodes,taskBinding,owner,handoff,recordedAt`.
Owner contains exactly `kind,ownerId,bindingId,ownerSha256`.
Handoff contains exactly `kind,identity,bindingId`; kind is
`operation|registry|mcp`, and it opens the corresponding existing review owner.
No URL, command, capability, credential or runtime path is accepted from the
client as a handoff. Current task and source identities are rechecked first.

Progressive inspection returns the retained ranked `candidate` and a bounded
`detail` object from its existing owner. It runs no provider operation and
loads no unrelated full definitions. Provider-authored strings remain text.

## Browser

`/discover/tasks?workspace=UUID&episode=UUID&state=UUID&search=UUID` supports
task and exact search deep links. Opening or refreshing the page is GET-only.
Use the existing per-tab exact-command recovery journal and bounded monitoring.
Result destinations are fixed scoped links: operation in `/tools`, candidate
in `/discover`, or binding in `/discover/mcp`. Existing destination pages must
select the requested identity rather than silently showing a generic list.

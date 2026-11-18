# U04-D discovery API contract

All responses use `schema: potbot-discovery/v1`, `workspaceId` and
`principalId`. Lists return `items` and `nextCursor`. Every mutation requires
the ordinary authenticated scope, exact allowed Origin, independent setup
capability, stable Idempotency-Key and `confirm: true`. Replays return the
original committed response. GETs read bounded retained observations only.

## Source and publisher records

`GET /v1/ui/discovery/sources` returns sources with `id`, `name`, `adapter`,
`revision`, `state` (`active`, `disabled`, `revoked`), `configSha256`,
`bindingSha256`, `approvalValid`, `snapshotId`, `snapshotSha256`, `fetchedAt`,
`expiresAt`, `query`, `lastErrorCode`, `createdAt`, `updatedAt`, and `actions`.
Nullable snapshot fields are null before a successful refresh. If a retained
snapshot no longer verifies, its UUID remains visible while hash and freshness
metadata are withheld. Source creation
and bounded network refresh are explicit host CLI commands. Source controls
are `POST /sources/{id}/{enable|disable|revoke}` with `revision`, `digest`,
and `confirm` only.

`GET /v1/ui/discovery/publishers?sourceId=UUID` returns `id`, `sourceId`,
`name`, `revision`, `state` (`unreviewed`, `approved`, `blocked`, `revoked`),
`bindingSha256`, `approvalValid`, `evidence`, `updatedAt`, and `actions`.
Publisher controls are `POST /publishers/{id}/{approve|block|revoke}` with
the same exact revision/digest/confirm fields. A publisher admission is an
operator decision about that source namespace; registry claims are not an
independent publisher signature or package safety finding.
Revocation is terminal for both source and publisher records. Invalid signatures
or setup-key rotation allow containment by revoke only; a new source admission is
required before further acquisition review.

## Candidates

`GET /v1/ui/discovery/candidates` accepts `q`, `kind`, `sourceId`, `after`,
and `limit` (1–50). Candidate `after`/`nextCursor` is an opaque base64url
string of 1–512 characters, bound by the server to the complete query, retained
source heads, publisher revisions and decision bodies, current authority and
expiry flags, and verified installed-skill control bindings. Preserve it exactly;
restart the search after a 409. Source,
publisher and check lists retain UUID cursors. `GET /candidates/{id}` returns `candidate`.

Candidate search also accepts `platform`, `permittedEffects`, `permittedScopes`,
`classes`, `inputTypes`, `outputTypes`, and `requireLocal=true|false`. CSV lists
contain at most 32 unique values of at most 256 Unicode codepoints each; each
filter is at most 4096 UTF-8 bytes and the complete encoded query at most 8192
bytes. Effects/classes/platforms use the domain enums; Types use canonical SYN
or Synny Type references. Omitted permission filters are unconstrained; an
explicit empty `permittedEffects` or `permittedScopes` permits none. Search
text is at most 500 Unicode codepoints. Every query change creates a new
browser search generation; hard constraints are retained through pagination.

A candidate contains `id`, `sourceId`, `snapshotId`, `candidateSha256`,
`observation`, `sourceState`, `publisherId`, `publisherState`,
`sourceApprovalValid`, `publisherApprovalValid`, `stale`, `canReview`,
`reasonCodes`, `executable: false`, `score` (integer or null), and
`scoreReasons` (bounded strings). `observation` is the strict
`potbot-registry-candidate/v1` record from `domain/discovery.py`. It includes
exact provenance, capabilities, dependencies, distribution and unsupported
fields; it contains no execution authority. A refreshed source snapshot
invalidates references to the previous candidate set.

## Exact review and measured prerequisites

`POST /candidates/{id}/checks` accepts exactly `snapshotId`,
`candidateSha256`, `agentId`, `confirm`. It resolves the selected exact
version under current source/publisher admission. For portable skills it
retains the verified private package through the existing U04-A owner so
the requirement list comes from the actual package. No installation or
activation occurs. Tool candidates must match an existing approved recipe.
An admitted ClawHub or skills.sh summary may offer `canReview: true` with
`exact_resolution_required` while still unresolved or inspection-only. The POST
explicitly inspects data to resolve the current exact snapshot or version; a known
version is preserved. The browser follows the server's action rather than deriving
review authority from a version string or distribution kind.

The response contains `check` with `id`, `candidateId`, `sourceId`,
`snapshotId`, `requestedCandidateSha256`, `candidateSha256`, `revision`, `bindingSha256`, `agentId`,
`status`, `candidate`, `requirements`, `packageId`, `recipeId`,
`measurement`, `measurementCurrent`, `target`, `reasonCodes`, `actions`,
`createdAt`, and `updatedAt`. Package/recipe/measurement/target may be null.
Statuses are `awaiting_measurement`, `measuring`, `measured`, `failed`,
`handed_off`, and `cancelled`.

`requestedCandidateSha256` retains the exact registry observation digest from
the original POST. `candidateSha256` and the embedded candidate describe the
resolved observation and inspected package requirements used by measurement.

`GET /checks?after=UUID&limit=25` lists the principal's retained check records
with `items` and `nextCursor` (limit 1–50). `GET /checks/{id}` reads the same
individual check envelope. `/discover?workspace=UUID&check=UUID` reopens an
exact retained review only after matching the explicit workspace scope.
A configured host runs
`potbot discovery measure CHECK_UUID` for one explicit review. It uses the
existing supervisor and workspace lease; this is not a second job scheduler.
The fixed probe measures prerequisite presence inside the selected sandbox.
It executes neither the package nor a requested dependency. Checks are bound
to candidate, source admission, agent, runtime, probe and requirements, and
expire within 120 seconds. Missing or unsupported requirements remain explicit.
This report is prerequisite evidence, never task or invocation permission.
Repeating a host measurement with the same idempotency key returns the retained
durable check before runtime setup. An incomplete attempt is never redispatched
under that key.

`POST /checks/{id}/{handoff|cancel}` accepts exactly `revision`, `digest`,
`confirm`. Handoff requires a current completed measurement and current
source/publisher/snapshot bindings. It may retain missing prerequisites for
the next acquisition review; it does not claim the package can run. It returns
the same `check` envelope with `target: {kind, id}`: `skill_package` selects
the already retained U04-A package, while `tool_acquisition` creates exactly
one existing U04-X source request. No manifest, command or permission is
generated by registry metadata. Source/publisher revocation fences new
discovery handoffs; installed instances retain their existing explicit
disable/revoke/release controls.

The frontend links to `/skills?workspace=UUID&package=UUID` or
`/setup/tools?workspace=UUID&operation=UUID`. These select an existing record;
they never automatically import, build, install or activate it.

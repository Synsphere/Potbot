# U04-Q MCP task qualification contract

Baseline: `2ef29e3141a581bec4fd6d7daf5d98f828d1d9df`.
All envelopes have `schema: potbot-mcp-qualification/v1`, `workspaceId`,
`principalId`, and the named result. List envelopes have `items,nextCursor`;
the cursor is a canonical UUID and limit is 1–50. Reads are retained projections.
They may verify the native setup key but acquire no provider bearer or process.

Mutations require ordinary scoped authentication, exact allowed Origin, a
separate native setup capability, one stable Idempotency-Key, and exactly
`{revision,digest,confirm:true}`. Body limit is 8,192 bytes. Responses use the
existing private/no-store and redacted error contract. Repeated commands recover
the original receipt; current state is read separately.

## Routes

- `GET /v1/ui/mcp-qualification/suites?bindingId=UUID&limit=25&after=UUID`.
- `GET /suites/{id}` returns `suite`.
- `GET /attempts?suiteId=UUID&limit=25&after=UUID`.
- `GET /attempts/{id}` returns `attempt`.
- `GET /reports/{id}` returns `report`.
- `POST /suites/{id}/{enable|disable|revoke}` returns `suite`.
- `POST /suites/{id}/qualify` returns `attempt`; it requests one native run.
- `POST /attempts/{id}/cancel` returns `attempt`.

Private suite admission, native execution and receipt reconciliation are CLI
operations. There is no browser route for importing arguments, starting native
processes, submitting reports or replaying tool calls.

## Task binding

`taskBinding` has exactly `episodeId,contractRevisionId,contractSha256,
repositoryStateId,repositorySha256,semanticReleaseId,semanticReleaseSha256`.
The IDs and hashes refer to existing task, repository and semantic owners.

## Suite

`id,bindingId,name,description,agentId,toolName,transport,revision,state,
bindingSha256,approvalValid,suiteSha256,suiteFileSha256,configurationSha256,
installationSha256,consumerSha256,taskBinding,cases,activeAttemptId,
lastAttemptId,lastReportId,authorityCurrent,reasonCodes,actions,createdAt,updatedAt`.

State is `enabled|disabled|revoked`; revoked is terminal. Valid enabled idle
suites can `qualify`; enabled suites can `disable`, disabled suites can `enable`,
and any non-revoked suite can `revoke`. Invalid signatures offer only revocation.
Name is at most 96 characters, description 2,048, and tool name 128. Transport is
`stdio|streamable_http`. Revisions are positive safe integers; all hashes are
64 lowercase hexadecimal characters. Attempt/report IDs are nullable UUIDs.

There are 1–8 ordered cases, each exactly
`{id,title,toolName,caseSha256,argumentsSha256,oracleSha256,assertionCount}`.
Case IDs match `[a-z][a-z0-9_-]{0,63}` and are unique; titles are at most 128
characters. Assertion count is 1–16. Raw arguments, expected values, credentials,
native paths and worker claims are never public fields.

`authorityCurrent` checks current signed suite, discovery reference, task and
consumer source identities. It does not replace native runtime/profile readback
at execution time and grants no permission.

## Attempt

`id,suiteId,bindingId,agentId,revision,bindingSha256,status,
requestedSuiteSha256,suiteSha256,configurationSha256,caseCount,completedCases,
passedCases,failedCases,cases,reportId,reportSha256,reasonCode,actions,
recoveryAvailable,createdAt,updatedAt`.

Status is `requested|running|recovering|succeeded|failed|cancelled|indeterminate`.
`actions` is empty or contains `cancel`. Native recovery is advertised separately
by `recoveryAvailable`; the browser never dispatches it. Report ID/hash are null
until a retained report exists. Counts are bounded by the admitted case count.

Each case projection has exactly
`id,title,caseSha256,status,actionId,candidateId,reservationId,runId,
resultArtifactId,resultSha256,receiptSha256,oracleSha256,checks,elapsedMs,reasonCode`.
Status is `pending|prepared|passed|failed|not_run|indeterminate`.
Identity/digest/result fields are null before their evidence exists; oracle hash
is always retained. `checks` is null or an ordered list of
`{index,passed,reasonCode}` with 1–16 entries and zero-based contiguous indexes.
No check exposes actual or expected values. `elapsedMs` is null or a nonnegative
safe integer. Cancellation fences future work; it does not assert that a process
has already stopped. Invalid suite/attempt signatures or terminal retirement of
invalid admission cannot project positive case evidence: all cases become
`indeterminate` with null execution/result fields and `admission_invalid`,
counts are zero, and report references/recovery are withheld. Active records
remain cancellable using their exact current revision/digest; otherwise the
attempt status is `indeterminate`. The private journal is retained unchanged.

## Report

`id,suiteId,attemptId,bindingId,sha256,approvalValid,complete,passed,
authorityCurrent,reasonCodes,evidence,cases,recordedAt`.

Cases have the same safe projection as the attempt. `complete` requires a
verified stopped result for every case; `passed` additionally requires every
declared assertion to pass. A partial recovery can retain an incomplete report.
Historical reports remain inspectable after their authority becomes stale.
Recovery of an older attempt preserves the latest requested-attempt identity;
its recovered report cannot supersede evidence from a newer attempt.

`evidence` has exactly:
`schema: potbot-mcp-qualification-report/v1`, `workspaceId,principalId,
attemptId,agentId,suiteSha256,suiteFileSha256,consumerSha256,
configurationSha256,installationSha256,definitionSha256,runtimeSha256,
imageSha256,qualificationSha256,agentHomeRevision,storageSha256,
referenceSha256,taskBinding,observedAt,completedAt,casesSha256`.
Agent home revision is a positive safe integer. Times are UTC ISO strings in
observation order. `casesSha256` binds the ordered retained native case records.
Evidence comes from existing gateway runs, artifacts and stopped receipts; the
web API accepts no uploaded success report.

## Browser entry

`/discover/mcp/qualification?workspace=UUID&suite=UUID&binding=UUID` supports a
suite deep link and optional binding filter. Preserve scope, exact command bytes
and idempotency keys in the existing per-tab recovery journal. Use bounded
GET-only monitoring and read current state after a terminal or recovered command.
Display provider-authored strings as text. Definitions are observations and
these reports are scoped task evidence, never an installation or execution grant.

# U04-M live MCP discovery contract

Base: `ed2a6af914076834be88f1059bc5d60d5f678efe`.
All envelopes have `schema: potbot-mcp-discovery/v1`, `workspaceId`,
`principalId`, and their named result. Lists use `items` and nullable
`nextCursor` (canonical UUID, limit 1–50). Every mutation requires ordinary
scope authentication, exact allowed Origin, independent native setup capability,
stable Idempotency-Key and `confirm: true`. Bodies are bounded to 8,192 bytes.
GETs return retained observations without provider calls, OAuth bearer acquisition
or process work. The local native setup capability may be read to verify current
record signatures.

## Routes

- `GET /v1/ui/mcp-discovery/bindings` and `/bindings/{id}`.
- `GET /bindings/{id}/entries?kind=tool|resource|resource_template|prompt&q=...`:
  retained definitions, query ≤256 characters, normal pagination. A cursor absent
  from the current snapshot is a 409 requiring a fresh page.
- `GET /bindings/{id}/entries/{entryId}/reference`: outer scope envelope with
  `reference`; requires a current signed enabled binding and fresh exact snapshot.
- `GET /attempts?bindingId=UUID` and `/attempts/{id}`.
- `POST /bindings/{id}/{enable|disable|revoke|reconcile}` with exactly
  `{revision,digest,confirm:true}`. Reconcile records an explicit durable attempt.
- `POST /attempts/{id}/cancel` with the same exact decision body.

Admission/configuration and execution of one reconciliation attempt are host
CLI operations. The HTTP constructor starts no poller, process or network call.
Controls return `binding`; reconcile and cancellation return `attempt`. Replays
return the original committed command receipt, followed by an explicit current
read. Host execution retries recover the current attempt without redispatch.

## Binding

`id,name,agentId,revision,state,bindingSha256,approvalValid,installationSha256,
configurationSha256,definitionSha256,toolName,toolFingerprint,transport,
ttlSeconds,watchMs,snapshotId,snapshotSha256,fetchedAt,expiresAt,stale,
lastErrorCode,activeAttemptId,lastAttemptId,counts,catalogInfo,actions,
createdAt,updatedAt`.

State is `enabled|disabled|revoked`; revoked is terminal. Invalid signatures
permit only terminal revocation. Native configuration is pinned by digest;
endpoint, argv, native paths, bearer credentials and private worker claims are
never public fields. Transport is `stdio|streamable_http`. TTL is 60–3,600 seconds,
with a shorter peer cache hint respected. Watch is 0 or 250–5000 milliseconds.

Counts has exactly `tools,resources,resourceTemplates,prompts` integer counts.
Nullable `catalogInfo` has exactly `protocolVersion,serverInfo,capabilities,
collections,notificationSupport,cacheTtlMs`. The collector's
`potbot-mcp-live-catalog/v1` schema defines these fields. `serverInfo` is a
bounded object or null; capabilities are bounded peer claims. Collections has
the four count keys, each `observed|not_advertised`. Notification support has
exactly `tools,resources,prompts` booleans. Cache TTL is null or a nonnegative
safe integer. Snapshots and dates are null before a successful observation.

## Entry

`id,bindingId,snapshotId,snapshotSha256,kind,identity,name,description,
definition,definitionSha256,configuredTool,referenceAvailable,reasonCodes,
executable:false`.

Kinds are `tool|resource|resource_template|prompt`. Definitions and URI/template
strings are untrusted display data. The UI never fetches them or opens them as
an executable instruction. `configuredTool` means exact equality with the
existing operator-selected tool contract, not a grant. Definitions ≤128 KiB,
name ≤128 characters, description ≤4,096, identity ≤2,048, metadata depth ≤32 and
nodes ≤8,192. Public pages are ≤1,572,864 bytes. Complete retained catalogues ≤2 MiB,
256 entries per collection and 1,024 total. No partial advertised collection is
published as a complete snapshot.

## Attempt

`id,bindingId,agentId,revision,bindingSha256,status,requestedBindingSha256,
installationSha256,configurationSha256,snapshotId,snapshotSha256,changes,
reasonCode,actions,createdAt,updatedAt`.

Statuses are `requested|running|succeeded|failed|cancelled|indeterminate`.
Changes is null or exactly `added,removed,changed,unchanged` integer counts.
Safe fixed reason codes contain no provider bodies or native exceptions.
Cancellation fences future exchanges and completion; it is not proof that
an already running process stopped. The native lifetime owner supplies that proof.

## Reference and execution boundary

A reference has exactly `schema: potbot-mcp-reference/v1`, `workspaceId`,
`principalId`, `bindingId`, `bindingRevision`, `bindingSha256`,
`installationSha256`, `snapshotId`, `snapshotSha256`, `entryId`, `kind`,
`identity`, `definitionSha256`, and `expiresAt`.

References preserve exact observation identity for planner readback. Existing
linked `InstalledMcpTool` invocations additionally require `mcp_reference` in
the canonical frozen action input alongside `installation_hash` and `arguments`.
The gateway revalidates current signatures, state, snapshot, expiry, exact
installed-profile hash and approved tool equality before admission and dispatch.
It retains the reference hash in
the existing intent/receipt evidence. Ordinary task, capability, budget,
credential, Syn and process admission still apply. A reference does not authorize
`tools/call`, `resources/read`, `prompts/get`, credential use or a new installation.

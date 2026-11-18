# U04-M — Live MCP discovery and reconciliation

Implementation date: **18 November 2026**. Patch baseline:
`ed2a6af914076834be88f1059bc5d60d5f678efe` (merged U04-D).

This milestone connects an already installed, admitted MCP server to complete
interface observations, durable reconciliation, exact planner references and
the existing execution gateway. It supplies the live-discovery part of T04.2
after U04-D's registry/source review. The broader U04/M4/T04 programme remains
partial: installed-host, vendor interoperability and verified task outcomes
still require their independent evidence.

## Delivered behaviour

| Area | Behaviour |
| --- | --- |
| Interface collection | Enumerate every advertised tools, resources, resource templates and prompts collection with bounded pagination and protocol negotiation. Retain exact definitions and hashes. |
| Durable ownership | Migration 43 adds principal/workspace-scoped bindings, attempts, immutable snapshots and append-only control events. Native setup authority signs exact revisions and current event heads. |
| Reconciliation | One explicit request, one worker claim and one atomic complete-catalogue replacement. Record additions, changes, removals and unchanged definitions. |
| Currentness | Invalidate references when a new attempt begins, authority changes, a binding is disabled/revoked, TTL expires or collection fails. Retain the last snapshot for inspection. |
| Native execution | Reuse the registered Agent home, installed runtime/image, release gates, Syn route, supervised process and lease ledger. Require confirmed process-stop evidence before publishing. |
| Existing gateway | A linked installation requires the exact current reference and admitted profile hash in addition to its existing task, capability, policy, budget, credential and process authority. |
| Operator surfaces | `potbot mcp-discovery`, the scoped `/v1/ui/mcp-discovery` API and `/discover/mcp` expose review, request, monitoring, cancellation and signed enable/disable/revoke controls. |

The public [DTO and route contract](u04-m-mcp-discovery-contract.md) is shared
with the frontend through an actual HTTP acceptance export. Public reads return
retained records; they do not launch a process, acquire a bearer or contact a
server. Resource URIs, templates, descriptions and prompt definitions remain
untrusted display data.

## Protocol and observation bounds

The collector preserves the transport's modern `2026-07-28` and supported
legacy `2025-11-25`, `2025-06-18`, `2025-03-26` negotiation. Legacy fallback is
limited to an explicit method-not-found response. An ambiguous transport error
does not trigger a second attempt.

Each advertised collection must finish successfully. A missing capability is
recorded as `not_advertised`; a malformed page, repeated cursor, duplicate
identity, interrupted collection or incomplete advertised list rejects the
whole candidate. Limits are 256 entries per collection, 1,024 overall, 128 KiB
per definition and 2 MiB for the complete canonical catalogue. Names, identities,
JSON depth and node count are bounded independently.

The configured TTL is 60–3,600 seconds. A shorter peer cache hint wins; zero
means retain the observation without issuing a fresh reference. TTL starts at
observation start, so slow collection does not extend its usable lifetime.

In-band change notifications during collection invalidate the candidate.
Modern HTTP may also use an explicit 250–5,000 ms metadata watch through the
approved broker and live Syn route. Unsupported legacy subscriptions fail
explicitly. Default watch is zero. A change during that bounded window fails
the attempt and leaves the prior snapshot stale; there is no automatic recrawl.
No idle background listener or polling daemon is installed. These are bounded
point-in-time observations, not a server-wide transactional version guarantee.

The metadata-only sandbox profile accepts discovery/list/subscription methods.
It denies `tools/call`, `resources/read` and `prompts/get`, and cannot mint task
capabilities. Existing HTTP invocation confirmation/watch profiles retain their
own admission rules; metadata watch does not rewrite executable arguments.

## Native owner and configuration

Prepare one operator-owned JSON file outside every worker-visible mount. The
reader requires an absolute physical path, a regular file, private owner/mode,
stable bytes, no symbolic links and a maximum size of 512 KiB. It accepts exactly:

| Field | Existing owner data |
| --- | --- |
| `schema` | `potbot-mcp-installed-profile/v1` |
| `agentId` | The already registered Agent UUID. |
| `definition` | Every field of the existing `ToolDefinition`; tuple fields use JSON arrays. |
| `tool` | The exact approved `McpToolInfo` wire definition. |
| `capability` | Existing `ToolCapabilityDescriptor.to_dict()` result. |
| `implementation` | Existing `ToolImplementationDescriptor.to_dict()` result. |
| `installation` | Existing `ToolInstallationDescriptor.to_dict()` result. |
| `executableSha256` | The admitted executable digest. |
| `oauthRegistration` | `null` for stdio, or the absolute private path to the existing OAuth registration. |

This file describes an existing installation. It supplies no new task grants,
runtime registration, server installation or credential contents. The
configuration hash binds its exact bytes and, for HTTP, the actual registration
identity and bytes. Reformatting the profile therefore requires a new admission.
The file has no binding ID; native execution registration supplies the separately
selected binding UUID using `load_discovery_installation`.

Reconciliation attaches to the configured native operator using the existing
runtime manifest, `POTBOT_OPERATOR_REQUIRED=1`, scoped launcher and
`POTBOT_NATIVE_COG_CONFIG` version 2 or 3 `context_rpc` configuration. Runtime,
image, qualification, Agent home, entrypoint and route identities are checked
through the existing owners. Private profile paths are revalidated outside
worker mounts before taking a worker claim. Profile/registration bytes are read
back before exchanges and publication; edits fence the running attempt without
replacing its frozen installed owner.

Native observation has a 120-second deadline. Authority is checked inside the
lease, before and after exchanges and before publication. The observation binds
the actual isolation and process-stop receipt, runtime/image/qualification
digests and Agent home revision. A missing or uncertain stopped outcome remains
indeterminate and preserves its lease/lifetime evidence for explicit recovery.
Cancellation or timeout waits for the existing shutdown owner to finish. Its
EOF/TERM/KILL and stream-drain grace can extend cleanup beyond 120 seconds;
such a late observation cannot become a successful published snapshot. Cleanup
has one shared task, so cancellation during shutdown cannot abandon the child
or replace its retained stop receipt.

## Operator walkthrough

Use the existing configured PostgreSQL scope and native `api.setup_token`
credential. Replace uppercase UUIDs, revisions, digests and paths below with
the exact values returned by the preceding review. Do not place the setup token
or bearer credentials in the command line or the profile.

Admit the existing installation for observation:

```bash
potbot mcp-discovery --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  admit --profile /operator/mcp-profile.json --name "Reviewed MCP server" \
  --ttl 300 --watch-ms 0 --key mcp-admission-1 --confirm

potbot mcp-discovery --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  binding BINDING_UUID
```

Request one reconciliation using the reviewed binding revision and digest:

```bash
potbot mcp-discovery --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  request BINDING_UUID --revision BINDING_REVISION --digest BINDING_SHA256 \
  --key mcp-request-1 --confirm
```

Run that exact attempt from the configured native worker environment, using
the **attempt's** revision/digest:

```bash
potbot mcp-discovery --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  reconcile ATTEMPT_UUID --revision ATTEMPT_REVISION --digest ATTEMPT_SHA256 \
  --profile /operator/mcp-profile.json --manifest /operator/runtime.json \
  --workspace-name WORKSPACE_NAME --key mcp-observation-1 --confirm

potbot mcp-discovery --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  entries BINDING_UUID --kind tool --limit 25

potbot mcp-discovery --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  reference BINDING_UUID ENTRY_UUID
```

The browser at `/discover/mcp?workspace=WORKSPACE_UUID&binding=BINDING_UUID`
offers the same retained review and explicit request/control lifecycle. A request
waits for the native CLI; opening the page does not launch it. The UI monitors
status with bounded GET-only reads, keeps exact lost-response commands per tab
and reads current state after recovering the original command receipt.

## Failure, cancellation and replay

| Outcome | Retained state and next action |
| --- | --- |
| Lost request/control response | Retry the identical key and body to recover the original receipt, then read current state. |
| Lost native execution response | Retry the identical execution key and body. Recover the existing running/terminal attempt before loading native configuration; never dispatch it twice. |
| Invalid page, timeout or detected catalogue change | Publish no partial snapshot. Inspect the fixed failure reason, review the binding again and explicitly request a new attempt. |
| Cancelled or disabled while running | Fence future exchanges and completion. The native lifetime owner still has to establish that processes stopped. |
| Unknown stop outcome | Keep the attempt/lifetime indeterminate and use the existing lease recovery owner. Do not remove candidate evidence or replay the process blindly. |
| Setup-key rotation or changed current event head | Historical metadata stays readable, but old positive admission and references fail. Admit/review under current authority. |
| Re-enabled binding | Previous data remains stale until a new successful reconciliation. Revocation is terminal. |

There are at most 16 non-revoked bindings per principal. Revoked history remains
readable and does not consume an active slot. PostgreSQL row locks and current
signed event heads fence concurrent admission, controls, claims and late workers.

## Invocation linkage

Register an existing installation with `load_discovery_installation(context,
profile_path, binding_id, credentials)` to retain both the binding UUID and
configuration hash. The linked frozen action input is exactly
`{installation_hash, arguments, mcp_reference}`. An unlinked legacy installation
keeps its previous `{installation_hash, arguments}` contract and hashes.

The gateway compares the reference with the current signed binding, snapshot,
expiry, exact definition and installed owner/profile. It repeats those checks
before admission, during intent pinning and at actual native/HTTP exchange
boundaries, including cancellation/confirmation waits. Existing receipts pin
the reference hash. Replaying a verified completed receipt preserves the
original result even after later revocation because it performs no new dispatch.
No discovered resource or prompt is converted into an executable tool.

## Migration, validation and remaining gates

Run the normal `potbot admin migrate` workflow with the configured database before
using these routes. Migration 43 follows migration 42 and preserves all previous
tables and installed-owner records. It adds restricted principal/workspace RLS,
membership/record foreign keys, immutable snapshot/events and bounded payloads.

The dedicated `mcp-discovery-u04-m.yml` gate covers protocol pagination and
notifications, metadata-only sandbox admission, private profile/native lifetime,
durable PostgreSQL transitions and migration, exact gateway references, API/CLI
boundaries, and frontend consumption of actual HTTP responses. It also retains
the existing gateway regression profiles. All 48 canonical tool stages, all
32 memory stages and the roadmap's historical qualification boundaries remain.

This patch also repairs two observed failures on merged U04-D: contract exporters
create their destination parents before writing, and the complete unit/git gate
has a finite ten-minute deadline after its 4,484-test selection hit the prior
four-minute limit at 85%. No assertions or required checks are removed.

Protocol tests exercise actual framed stdio fixtures and controlled HTTP peers.
Native/controller acceptance uses explicitly marked runtime and stopped-process
fixtures together with production host/service code, the real lease ledger and
real PostgreSQL. These do not establish installed Bubblewrap, Syn, Xpra or a
third-party provider's production qualification. The next evidence milestone is
the installed-host and vendor/task acceptance matrix over exact approved
profiles, including interruption, revocation, schema change and verified task
outcomes. Broader semantic retrieval and ecosystem interoperability remain open.

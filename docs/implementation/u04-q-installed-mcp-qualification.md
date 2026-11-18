# U04-Q — Installed MCP task qualification and recovery

Source baseline: merged main
`2ef29e3141a581bec4fd6d7daf5d98f828d1d9df` (U04-M), 18 November 2026.

This milestone implements the complete supported task qualification lifecycle:
admit an independently reviewed private case suite, request one execution, run
it through the existing MCP gateway, verify the actual retained results, and
publish signed evidence with explicit recovery after interruption. CLI, API and
browser use one principal-scoped PostgreSQL owner. Migration 44 is additive.

The implementation is ready to collect evidence on a configured installation.
The controlled peer and native-process fixtures in acceptance tests do not
qualify a deployed provider or its isolation installation. U04/M4/T04 remains
partial until the selected installed hosts and vendor/task profiles have their
own reviewed evidence. Historical U00 requirement statuses remain unchanged.

## Existing authority and evidence owners

| Concern | Owner reused by qualification |
| --- | --- |
| Tool identity and interface | Installed MCP profile plus an exact current U04-M discovery reference. |
| Task permissions | Existing running episode, current contract, repository state and semantic release pin. Qualification creates no task or additional grant. |
| Native isolation | Registered Agent home, runtime/image qualification, operator launcher, shared-home leases, sandbox, Syn route and existing credential broker. |
| Execution | Existing action/input artifacts, candidate workspace and lease, budget reservation, `ToolGateway.execute_mcp` and completion records. |
| Results | Actual stdout artifact, verified stopped receipt, invocation fingerprint, repository fingerprint and independent bounded assertions. |
| Operator decisions | Scoped signed suite/attempt records, immutable event heads, exact revision/digest checks and durable idempotency receipts. |
| Recovery | Existing gateway reconciliation and retained action receipts. No recovery path invokes the tool again. |

The native owner takes the existing exclusive Agent-home preparation guard
while establishing the candidate owner and materializing each candidate. It
releases that guard before the gateway takes its ordinary execution lease.
Qualification therefore shares the established workspace exclusion rules.

Every prepared case records its action, input artifact, candidate lease, budget
reservation, private storage identity and original runtime/image/qualification/
Agent-home/source/configuration/reference pins before dispatch. Finished
evidence must preserve those identities. Recovery under changed native pins
cannot describe the historical invocation as qualification of the new runtime.

## Private case format and independent result checks

The suite is an operator-owned JSON file, outside every worker-visible mount.
The reader requires an absolute physical path, a regular file owned by the
operator, private permissions, no symbolic links, stable bytes and at most
512 KiB. It admits exactly one to eight uniquely named ordered cases, all for
the single approved read-only tool in the linked installed profile. Arguments
must satisfy that installed tool's approved schema.

This illustrative suite expects a separately established record to exist in a
read-only search service. Replace its tool name, arguments and expected values
with the exact approved profile and an independently checked task fixture:

```json
{
  "schema": "potbot-mcp-qualification-suite/v1",
  "name": "Known record lookup",
  "description": "Verify the operator-maintained qualification record.",
  "cases": [
    {
      "id": "known-record",
      "title": "Find the exact reviewed record",
      "toolName": "search",
      "arguments": {"query": "qualification-record-001"},
      "oracle": {
        "schema": "potbot-mcp-result-oracle/v1",
        "assertions": [
          {"op": "length_equals", "pointer": "/structuredContent/items", "value": 1},
          {"op": "equals", "pointer": "/structuredContent/items/0/id", "value": "qualification-record-001"},
          {"op": "contains", "pointer": "/content/0/text", "value": "Qualification record"}
        ]
      }
    }
  ]
}
```

Each case has a lowercase ID matching `[a-z][a-z0-9_-]{0,63}`, a title of at most
128 characters, arguments of at most 32 KiB and one to sixteen assertions.
Suite name and description limits are 96 and 2,048 characters. Oracle documents
are bounded to 64 KiB, each expected value to 16 KiB, and JSON pointers to
1,024 UTF-8 bytes and sixteen segments. The evaluator implements only typed
`equals`, nonempty literal `contains` and nonnegative `length_equals` checks.
It does not evaluate code, regular expressions, external URLs or artifact links.

Pointers address the normalized MCP result, including `/content` and
`/structuredContent`. Equality distinguishes booleans, integers and floating
point values. A missing pointer, wrong type, wrong value or missing literal
produces a fixed negative check. Malformed native output, an unsuccessful
observation, altered invocation/type identity or a normalized-result hash
mismatch cannot become a successful assertion result.

The evaluator first verifies the exact gateway output envelope and reconstructs
its normalized MCP result. Native recovery also compares the actual artifact
bytes with the retained stopped receipt and rechecks the frozen input bytes.
Neither a peer's success flag nor its claimed hash substitutes for expected
task outcomes. Choose independent fixture values or pinned source content; a
server-provided checksum of its own response is not an independent oracle.

The [vendor case review](u04-q-mcp-vendor-cases.md) records source-pinned
DanceFlow, WordPress and IBM compatibility findings and what still needs actual
installation or task evidence. It does not provide fabricated working provider
credentials, deployment receipts or unsupported authentication profiles.

Both the semantic suite hash and exact file-byte hash are retained. Reformatting
the admitted file changes its execution identity. Raw arguments, expected
values, credential material and native paths are absent from public DTOs.

## Native prerequisites and operator walkthrough

Apply migration 44 through the existing configured `potbot admin migrate`
workflow before using the new API or CLI, then restart the application/UI on
the updated checkout. The populated 43→44 acceptance test preserves previous
migration checksums and the existing signed discovery records. Use the same
configured schema and administrative database owner as previous upgrades.

Use the configured PostgreSQL scope and existing native `api.setup_token`
credential. The setup capability is separate from the ordinary workspace
bearer. Existing U04-M profile/runtime/Syn/credential prerequisites still apply;
the CLI creates none of them. The selected discovery reference must be fresh,
and the existing task must already authorize the tool and have sufficient
budget. A task contract change or catalogue refresh requires a new admission.

Select the existing private artifact backend that contains the admitted
repository state's snapshot and objects. An empty newly created artifact root
cannot materialize that task's candidate; this workflow does not migrate
artifacts. Create a separate private receipt directory outside all worker
mounts. Both directories must be operator-owned with private permissions. Retain
these exact directories for recovery; directory inode, ownership and permissions
are pinned. Neither may be under an Agent home, shared tools, models, skills or
runtime rootfs. The paths below are placeholders for your operator storage:

```bash
install -d -m 700 /operator/potbot-qualification/receipts
chmod 600 /operator/mcp-profile.json /operator/mcp-cases.json
```

Admit the reviewed suite against the existing task and repository state:

```bash
potbot mcp-qualification --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  admit --binding BINDING_UUID --episode EPISODE_UUID --state REPOSITORY_STATE_UUID \
  --profile /operator/mcp-profile.json --suite /operator/mcp-cases.json \
  --key qualification-admit-1 --confirm

potbot mcp-qualification --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  suite SUITE_UUID
```

Request one attempt using the suite's exact reviewed revision and binding hash:

```bash
potbot mcp-qualification --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  request SUITE_UUID --revision SUITE_REVISION --digest SUITE_BINDING_SHA256 \
  --key qualification-request-1 --confirm
```

Run the requested attempt using the **attempt's** returned revision and binding
hash, from the configured native operator environment:

```bash
potbot mcp-qualification --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  run ATTEMPT_UUID --revision ATTEMPT_REVISION --digest ATTEMPT_BINDING_SHA256 \
  --profile /operator/mcp-profile.json --suite /operator/mcp-cases.json \
  --manifest /operator/runtime.json --workspace-name WORKSPACE_NAME \
  --artifacts-root /operator/potbot-artifacts \
  --receipts-root /operator/potbot-qualification/receipts \
  --key qualification-run-1 --confirm

potbot mcp-qualification --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  attempt ATTEMPT_UUID

potbot mcp-qualification --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  report REPORT_UUID
```

`suites --binding BINDING_UUID` and `attempts --suite SUITE_UUID` provide bounded
history with `--limit` and `--after`. There are at most 32 non-revoked suites per
principal. Historical reports and revoked suites remain readable.

The native attempt has a 600-second deadline. Existing shutdown/drain grace may
extend cleanup, but late evidence cannot become a successful current report.
Authority is rechecked before dispatch, at the actual MCP exchange boundary,
during cancellation polling, and before evidence publication. These callbacks
use ordinary read-only database queries because the gateway can already own the
task's row lock on a different connection.

## Cancellation, interruption and exact recovery

Cancellation, disabling or revoking a suite fences the worker and future tool
calls. It does not assert that an in-flight process has stopped. The native
owner and existing gateway receipt remain responsible for that proof.

| Observed condition | Supported action and evidence |
| --- | --- |
| Lost request/control response | Retry exactly the same command key and body, then read current state. The original command receipt is retained. |
| Lost native run/recovery response | Retry the same key and reviewed body. The CLI returns the current attempt before opening native configuration and never acquires another execution claim. |
| Prepared action with no gateway run | Explicit recovery verifies absence under the existing task lock, releases the reservation/lease through their owners, and records `not_run`. It does not run the remaining cases. |
| Stopped receipt exists after interrupted publication | Recovery revalidates the retained action, input, actual output artifact and receipt, reruns the independent assertions, and retains the corresponding report without another tool call. |
| Missing or uncertain stopped receipt | Preserve `indeterminate` evidence and existing lease ownership. A fresh invocation is not a recovery action. |
| Changed profile, suite, source, runtime, Agent home or private storage | Reject qualification under the changed identity. Use the existing execution/lifetime owner for historical cleanup and explicitly admit a newly reviewed suite before new work. |
| Changed task contract, cancelled task or stale/revoked catalogue | Fresh execution is denied. Receipt-only recovery can retain historical evidence under its original native pins; the report does not become current authority. |
| Invalid signature or setup-key rotation | Reads remain available. Positive execution/recovery admission is denied. Terminal retirement cannot re-sign an untrusted journal into recoverable authority. |

Inspect the current attempt. When `recoveryAvailable` is true and its native
inputs are unchanged, run a new explicitly reviewed recovery command:

```bash
potbot mcp-qualification --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  recover ATTEMPT_UUID --revision CURRENT_REVISION --digest CURRENT_BINDING_SHA256 \
  --profile /operator/mcp-profile.json --suite /operator/mcp-cases.json \
  --manifest /operator/runtime.json --workspace-name WORKSPACE_NAME \
  --artifacts-root /operator/potbot-artifacts \
  --receipts-root /operator/potbot-qualification/receipts \
  --key qualification-recover-1 --confirm
```

A process death during preparation, before the durable prepared event exists,
can leave an undispatched reservation or candidate in the existing owners.
Their lease/budget reconciliation owns that cleanup. This command handles only
durably prepared cases and does not guess which unjournaled resources belonged
to a partially prepared case. Once preparation is retained, any unknown dispatch
outcome requires actual stopped-receipt evidence.

## Reports, API and browser review

The browser entry is
`/discover/mcp/qualification?workspace=WORKSPACE_UUID&suite=SUITE_UUID` with an
optional `binding=BINDING_UUID` filter. It shows task/suite identities, current
authority, ordered case evidence, bounded history and fixed failure reasons.
It supports qualification requests, enable/disable/revoke and cancellation,
exact lost-response command recovery, and bounded GET-only monitoring. Native
execution and receipt reconciliation remain explicit CLI operations.

The [public contract](u04-q-mcp-qualification-contract.md) defines the fixed
`/v1/ui/mcp-qualification` routes and DTOs. The BFF has an allowlisted route
grammar. Mutations require one authenticated scope, exact approved Origin,
independent setup capability, one stable key and exactly the reviewed
`{revision,digest,confirm:true}` body. There is no browser upload of raw cases,
credentials, native commands, provider reports or arbitrary endpoints.

A report is complete only when every case has a verified stopped result. It
passes only when every declared assertion passes. Partial recovery can retain
an incomplete negative report. A newer failed or active attempt makes an older
report historical; receipt recovery of that older attempt cannot make its pass
current again. Invalid signatures or retired invalid admission also suppress
positive attempt projections: case results become indeterminate, pass counts
are zero, and report references are withheld while cancellation remains
available for active records. The private journal is preserved for inspection
through its existing owners. A passing scoped report
does not broaden a task contract, install a tool, change a runtime qualification
or authorize another provider/profile.

## Validation and remaining installed evidence

The dedicated `mcp-qualification-u04-q.yml` workflow exercises the strict suite
and oracle, native preparation/execution/recovery, authenticated controls,
populated migration 43→44, principal isolation, signed event heads, command
races, cancellation/revocation at the actual exchange, changed original pins,
gateway/result/receipt corruption and actual HTTP responses consumed by the
frontend. It retains JUnit and real API contract exports. Existing discovery
and gateway regressions remain required.

Acceptance uses real PostgreSQL, production task/candidate/budget/gateway/
artifact/receipt/controller code and an explicitly marked MCP process/peer.
The native tests also exercise the real shared-home SQLite guard. They do not
supply a deployed provider, operator credentials, real Bubblewrap/Syn runtime
qualification or third-party service availability.

The next evidence milestone is to run these explicit commands on each selected
installed host and vendor/task profile, retain its exact passing report plus
negative/interruption cases, and review compatibility gaps against the pinned
vendor matrix. Broader semantic discovery and ecosystem interoperability remain
open work; the 48 canonical tool stages and 32 object-memory stages are retained.

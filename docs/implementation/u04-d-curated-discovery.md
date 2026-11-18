# U04-D curated discovery and measured prerequisites

**Patch baseline:** `a701eb68523767ddaf685a7cc6e991a896f21b66`, reviewed
18 November 2026, after the U04-X executable acquisition merge.

This milestone completes the supported discovery path from a bounded source
snapshot to an exact acquisition review. `/discover`, `potbot discovery`, and
the authenticated discovery API share source and publisher decisions, task
ranking, retained candidate reviews, measured sandbox prerequisites, and exact
handoffs into [U04-A](u04-a-portable-skill-acquisition.md) and
[U04-X](u04-x-executable-tool-acquisition.md).

Migration 42 retains private discovery evidence and signed decisions. The
existing Context skill registry still owns portable packages and installation;
the existing S9–S12 owners still own executable acquisition and release. The
new discovery records do not grant task execution or replace those owners.

After applying the patch, use the existing administrator migration command
with the configured native administrator DSN, then restart the API and UI:

```bash
potbot admin --schema potbot migrate
```

Use the deployment's actual schema if it is not `potbot`. The populated
version-41 migration test verifies that earlier migration checksums and
existing executable recipes survive the upgrade to version 42.

## Completed workflow

| Phase | Result and authority |
| --- | --- |
| Admit a source | The native CLI records an independently signed source configuration; curated sources bind the exact manifest bytes. Endpoints are fixed by the selected adapter. |
| Refresh metadata | One explicit host command retrieves a complete bounded snapshot, retaining source identity, hashes and freshness. A failed refresh records a safe failure code and makes the previous snapshot stale. |
| Review publishers | The operator approves, blocks or terminally revokes a source namespace with its exact revision and digest. Registry assertions remain identified as claims. |
| Search by task | Deterministic, explained ranking uses declared capability classes, semantic Types and literal task terms after explicit platform, effects, scopes and local-runtime filters. |
| Prefer an existing skill | An exact matching active acquired skill receives a preference only after its current package and signed control are verified through the existing skill owner. |
| Inspect a selection | An explicit confirmed review resolves an exact version or file snapshot, verifies actual skill bytes and retains a private package. A tool selection must match an already approved executable recipe. |
| Measure prerequisites | One explicit host command uses the existing registered Agent, supervisor and workspace lease to inspect the selected sandbox with a fixed, independently pinned interpreter. |
| Hand off the exact review | A current signed review and fresh measurement select the retained U04-A package or create one U04-X source request in the same PostgreSQL transaction. |
| Continue acquisition | Deep links select the exact package or executable operation in the existing workbench. Installation, activation, build approval and release retain their existing explicit decisions. |

The API performs no source refresh or native process dispatch. Ordinary GETs
read retained metadata only. Preparing a confirmed review may retrieve bounded
package data; it does not run a package, installer, dependency or hook.

## Source coverage and ranking

The [source contract](u04-d-registry-sources.md) records the primary API
references, identities, schemas, network bounds and fixture evidence.

- **Curated manifests:** exact public GitHub skill commits and paths, exact
  approved tool recipes and commits, or explicit inspection profiles.
- **ClawHub:** public metadata and owner-qualified exact releases, with a
  complete file inventory required for portable skill acquisition. ZIP and
  supported public GitHub handoffs are verified against that inventory.
- **skills.sh:** documented API access through the dedicated native
  `discovery.skills_sh.oidc` credential slot. Missing credentials report
  `authentication_required`. Complete file snapshots have computed identities;
  they are not represented as immutable Git commits.
- **Official MCP Registry:** versioned server metadata, namespace identity,
  pagination and lifecycle status. These remain inspection candidates pending
  admission and live transport reconciliation.

ClawHub and skills.sh search summaries can be selected for explicit data
inspection. A known selected version must remain exact. A versionless summary
is resolved during that inspection, after which the retained review identifies
the actual version or complete file snapshot. The original search digest is
kept as `requestedCandidateSha256`; measurement and handoff bind the resolved
`candidateSha256`. An already resolved candidate cannot silently change its
reviewed metadata, publisher or file inventory.

Ranking does not convert a natural-language privacy statement into permission.
The API, CLI constraints JSON and UI advanced filters carry explicit hard
constraints. Omitted effects/scopes mean unconstrained discovery; explicit
empty lists permit none. Unknown costs and runtime compatibility remain
unknown. Popularity, publisher ownership and vendor names confer no trust bonus.
All returned discovery candidates have `executable: false`.

Identical observations from multiple sources remain independently admitted.
A blocked source cannot suppress an identical candidate from another admitted
source, and duplicate content cannot crash the ranking collection. Result
cursors bind the query, filters, source and publisher heads, freshness, current
signature validity and verified installed-skill controls. A changed binding
requires restarting pagination instead of mixing old and new results.

The included `examples/registry/curated-capabilities.json` contains 20
inspection profiles and `tests/fixtures/discovery/golden-requests.json` contains
40 task cases. These exercise broad capability classes, explicit denials,
ranking explanations and no-suitable-candidate cases. Optional host profiles
are marked unavailable. They are not 20 installed or qualified integrations,
nor does the golden set establish task execution success.

## Operator walkthrough

Use the existing configured PostgreSQL scope and native `api.setup_token`
credential. Replace uppercase identifiers below with actual UUIDs, revisions
and digests returned by the preceding review. Do not put setup credentials in
the command line or source manifest.

Admit and refresh the bundled inspection catalogue:

```bash
potbot discovery --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  add-source --adapter curated --name "Reviewed capability profiles" \
  --manifest examples/registry/curated-capabilities.json --ttl 900 \
  --key source-admission-1 --confirm

potbot discovery --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  refresh SOURCE_UUID --manifest examples/registry/curated-capabilities.json \
  --key source-refresh-1 --confirm

potbot discovery --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  publishers --source SOURCE_UUID

potbot discovery --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  publisher-control PUBLISHER_UUID approve --revision REVISION \
  --digest PUBLISHER_BINDING_SHA256 --key publisher-admission-1 --confirm

potbot discovery --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  search "extract tables from documents" --source SOURCE_UUID
```

The bundled profiles are inspection only. To prepare an actual acquisition,
admit a supported exact skill source or a curated tool entry tied to a real
approved recipe, then select that candidate. To use ClawHub or skills.sh,
`add-source` selects that adapter and omits `--manifest`; `refresh` supplies
a source search query of at most 256 characters. Retained task ranking accepts
at most 500 characters. No arbitrary registry URL is accepted.

Create the selected exact review for an already registered Agent:

```bash
potbot discovery --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  prepare CANDIDATE_UUID --snapshot SNAPSHOT_UUID \
  --digest CANDIDATE_SHA256 --agent AGENT_UUID --key exact-review-1 --confirm
```

Inspect its package, provenance, actual requirements and unsupported fields
in `/discover?workspace=WORKSPACE_UUID&check=CHECK_UUID` or with
`potbot discovery ... check CHECK_UUID`. Then run one measurement from the
existing installed scoped worker environment:

```bash
potbot discovery --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  measure CHECK_UUID --revision REVISION --digest CHECK_BINDING_SHA256 \
  --manifest /operator/runtime.json --workspace-name WORKSPACE_NAME \
  --probe-python /usr/bin/python3 --probe-python-sha256 APPROVED_INTERPRETER_SHA256 \
  --key prerequisite-measurement-1 --confirm
```

This requires the existing healthy operator attachment,
`POTBOT_OPERATOR_REQUIRED=1`, the configured
`POTBOT_OPERATOR_SCOPED_LAUNCHER`, current signed image/runtime bindings,
registered Agent home and S12 release policy. The interpreter hash must come
from the operator's independent approved image inventory. A manifest file or
an environment variable alone does not establish host qualification.

Read the new check revision and digest, then hand off within the measurement's
freshness window:

```bash
potbot discovery --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  handoff CHECK_UUID --revision REVISION --digest CHECK_BINDING_SHA256 \
  --key acquisition-handoff-1 --confirm
```

A `skill_package` target opens `/skills?workspace=WORKSPACE_UUID&package=PACKAGE_UUID`.
A `tool_acquisition` target opens
`/setup/tools?workspace=WORKSPACE_UUID&operation=OPERATION_UUID`.
Both select the retained record for the existing acquisition review. A
measurement with blocked or unsupported requirements may be handed off for
that review; it never claims the package is runnable or approves dependency
installation.

## What the prerequisite measurement proves

The fixed probe runs with Python isolation (`-I -S`), a fixed guest PATH and
no inherited host environment. It checks the declared binary names, supported
operating-system alternatives and requested environment-name presence. It
does not print environment values or run a requested binary. Canonical
`any_binary` and operating-system groups use OR semantics; the remaining
clauses are combined with AND semantics. Configuration, vendor aliases and
other unsupported compatibility requirements remain explicit unsupported
results.

Binary presence must refer to an allowed image location or an exact admitted
shared-tool entrypoint. Symlinks into mutable Agent homes, build trees,
configuration, models or other unapproved mounts cannot satisfy a prerequisite.
Shared tools require current runtime pins and existing release admission.
The report binds workspace, principal, Agent home revision, candidate, source
admission, requirements, runtime, image, qualification, probe, interpreter and
observed result. Its freshness window is at most 120 seconds.

The signed check is revalidated inside the existing lease before dispatch and
after readback. A successful report requires a confirmed process stop. An
uncertain stop preserves the existing indeterminate lease and records an
indeterminate outcome; cancellation does not assert that a native process has
stopped. Package execution, live credential eligibility and useful task
completion remain independent qualifications.

## Persistence, revocation and recovery

All five migration-42 tables enforce workspace and principal scope. Snapshots
and events are append only. Source, publisher and check MACs use the current
independent native setup capability and bind their exact state; the immutable
event head also prevents revision rollback. Mutable database rows and unsigned
events cannot be turned into a new approval through disable/enable or
block/approve controls.

Source and publisher revocation are terminal. After key rotation or invalid
signature evidence, the supported control is terminal revocation followed by
a fresh source admission and review. Up to 16 nonrevoked sources are admitted
per principal; disabled sources retain a slot, while terminal revocation frees
one. Retired source/check history remains available through bounded lists.
Source/publisher controls fence new
discovery reviews, measurements and handoffs. Existing installed instances
retain their established disable, revoke and release controls.

Mutation recovery retains exact idempotency key, request body and scope.
Ordinary commands return their original committed receipt; clients then read
the current record. Repeating an exact host measurement command returns the
current retained check before native runtime configuration. An atomic fresh
claim check also prevents a concurrent retry from dispatching or failing
another worker's attempt. An incomplete measurement is inspected and explicitly
cancelled; it is not silently rerun. A fresh review provides a new attempt.

The browser stores bounded scope-bound recovery journals without setup secrets.
It validates the exact original response binding, polls for bounded periods,
and requires workspace agreement before opening acquisition deep links.
Record selection never submits an acquisition or activation command.

## Verification and remaining gates

The dedicated `.github/workflows/discovery-u04-d.yml` gate runs source,
ranking, installed preference, prerequisite, CLI and HTTP tests; real PostgreSQL
discovery and existing acquisition-owner regressions; and Node projections
against actual exported HTTP responses. It retains JUnit and HTTP evidence.
The existing full Python, frontend and roadmap gates remain in place.

Local verification completed on 18 November 2026:

| Gate | Result |
| --- | --- |
| New source, ranking, installed preference, prerequisite, HTTP and CLI unit tests | 239 passed |
| Real PostgreSQL discovery plus existing skill, executable acquisition and catalogue regressions | 87 passed, including 37 discovery cases; no skips |
| Full frontend Node suite | 224 passed; no failures or skips |
| Frontend TypeScript and production build | Passed; discovery page and BFF included |
| Actual HTTP response projection | 19 lifecycle envelopes verified by the frontend contract tests |
| Ruff and formatting | Passed across 841 Python files |
| Mypy | Passed across 420 source files |
| Locked dependencies | Existing 77-package lock verified; no dependency changes |
| Canonical plans | 48 tool stages and 32 memory stages passed their consistency gates |

Registry responses and native process outcomes in these tests are explicit fixtures.
Real PostgreSQL transactions, RLS, migrations, signed decisions, retained
packages and existing acquisition-owner handoffs are exercised together.
Fixture receipts do not establish live registry access, a deployed native
credential store, Bubblewrap/Syn/WSL2 isolation, rendered browser acceptance or
real third-party package task success.

The next major implementation step is **admitted live MCP reconciliation**:
enumerate the actual tools/resources/prompts of an enabled installation through
the existing MCP transport owners, bind schemas and supported protocol to its
exact revision and principal, and invalidate stale references after expiry,
removal or change. Then qualify selected vendor/task profiles on the actual
installed host. Broad T04, U04/M4 and skill-ecosystem requirements remain partial;
the historical U00 audit identities and `runtime_qualified: false` are preserved.

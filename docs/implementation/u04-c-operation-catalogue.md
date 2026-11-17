# U04-C operation catalogue and linked accounting

**Patch baseline:** `6db5cd385fad5ba085be8f1affb4e0f9ebafec9a`, reviewed 17 November 2026.
**Scope:** the read-only catalogue portion of U04-C/RD18, with U05/U11/U13
integration. This follows the merged S12 release gate. It does not complete
U04, T04, M4, connector qualification or the wider acquisition programme.

**Follow-on:** this document retains the merged catalogue's acceptance boundary.
[U04-E](u04-e-operation-workbench.md) adds the complete authorized workbench,
current-worker eligibility and principal-owned job/result lifecycle on migration
39. Its execution routes are separate from the read-only catalogue GET routes.

The catalogue answers three questions from existing records: which operations
are visible to this workspace principal, what evidence or hold is recorded, and
which recent invocations and ledger entries can be linked to that operation.
It creates no registry, scheduler, budget ledger or execution authority.

## Entry points

Open **Operations** from the workspace menu or the model installations panel.
The `/tools` page preserves the selected workspace, supports text/kind/state
filters and bounded paging, and opens a detailed evidence view for one entry.
Release records link to the existing `/setup/tools` review page. All activation,
approval, postflight and rollback decisions remain on the existing S9–S12 path.

The authenticated API exposes two GET routes:

```text
GET /v1/ui/operations?q=profile&kind=cog&limit=25
GET /v1/ui/operations/{catalogue_operation_id}
```

Only `q`, `kind`, `state`, `after` and `limit` are accepted on the list route;
the detail route accepts no query parameters. Workspace and principal are
derived from authentication. The browser proxy chooses only an authorized
configured workspace and validates the returned public schema before rendering.
Responses use `Cache-Control: private, no-store`.

The CLI uses the same service and JSON contract:

```sh
uv run --locked potbot operations \
  --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  list --kind cog --query profile --limit 25

uv run --locked potbot operations \
  --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  inspect CATALOGUE_OPERATION_SHA256
```

The UUIDs and SHA-256 identity above are placeholders. The CLI uses the existing
PostgreSQL connection configuration and workspace membership checks. It does
not start a worker, discover models or obtain a native credential. `potbot
operations --help` remains available without database or web extras.

## Existing authorities and visibility

`OperationCatalogue` reads one PostgreSQL statement snapshot inside a read-only
transaction. Application migration 38 remains the latest required schema;
this increment adds no database migration or durable table.

| Record owner | Catalogue observation | Visibility boundary |
| --- | --- | --- |
| Workspace runtime context | Recorded tool, Cog, workflow, connector, model or gateway reference and fingerprint | Current private context heads owned by the authenticated principal |
| Tool context history | Shared or owned source references | Existing context-object visibility, not arbitrary workspace-private payloads |
| S11/S12 upgrade records | Approval/activation phase, release generation, binding/evidence hashes and pending update | Authenticated principal's existing upgrade records |
| Model catalogue and deployments | Model identity, deployment fingerprint and recorded health/qualification | Existing workspace-visible metadata; no endpoint configuration, secrets or discovery calls |
| Published skills | Published source identity and content hash | Shared or owned context object; skill text remains untrusted data |
| Sealed Syn snapshots | Imported Cog reference and source hash | Workspace-visible sealed source metadata; signatures do not imply current authorization |

Commands, source bodies, prompts, tool arguments, credentials, configuration
paths and provider responses are not part of the public projection. Source and
evidence hashes identify retained records; the catalogue does not verify a
current installation by reading its filesystem or calling its provider.

Global credential setup and secret-presence state are not available to an
ordinary workspace bearer. Dependency resolution and credential presence need
their own authorized source before they can be added. Missing observations
remain unknown rather than being presented as satisfied prerequisites.

## Readiness contract

The public schema is `potbot-operation-catalogue/v1`. An entry has a stable
workspace/kind/reference identity, its contributing source identities and
revisions, nullable version/fingerprint, reason codes, optional S12 release
record, recent invocations and linked accounting.

| State | Meaning in this projection |
| --- | --- |
| `source_only` | Only source, configuration or historical release metadata is established |
| `installed_unqualified` | A runtime context observation is retained, but the current host and dispatch permission have not been revalidated |
| `policy_disabled` | An existing activation/repin/postflight transition records a hold |
| `degraded` | Recorded deployment health or postflight evidence reports a failure |
| `indeterminate` | The retained release outcome is unresolved |
| `qualified` | Reserved by the public vocabulary; this database-only implementation does not emit it |

`updateAvailable` is a separate nullable facet. A retained pending upgrade can
establish `true`; the absence of such a record does not establish that no
upstream update exists. An indeterminate invocation is also recorded separately
from release readiness, so an uncertain historical run is not mistaken for a
new installation decision.

The detail view explains each reason and shows release evidence separately.
Even `releaseState: released`, a stored model qualification or an imported
signed Cog does not establish current permission to execute. Normal dispatch
still evaluates the exact installed source, runtime pins, task, node/enrollment,
capabilities, grants, expiry, live Syn state and relevant S12 evidence.

The service never calls model discovery, a secret resolver, a host probe or the
lease-expiry writer. There are no catalogue execute/install/approve endpoints.

## Invocation ownership and accounting

Invocation history is limited to at most five recent linked records per entry.
Cog events must have the authenticated emitting principal. Submitted native
Cog and hosted jobs must have that principal's immutable `submit_api_job`
command result in the same workspace. Reasoning invocations must join a
reasoning run owned by the principal. Workspace membership alone is not enough
to reveal another principal's jobs, prompts or accounting.

History joins include the operation family and reference. A tool and a model
with the same display name cannot inherit each other's history or costs. The
catalogue reads existing reservations and usage; it never settles a reservation
or creates a new charge. Repeated links to the same reservation are counted
once within the entry's projection.

The accounting object explicitly declares:

```json
{
  "scope": "linked_recent_invocations",
  "complete": false,
  "reservationIds": [],
  "settledCount": 0,
  "indeterminateCount": 0,
  "totals": []
}
```

Totals preserve each ledger unit and its `measured`, `provider_reported`,
`synthetic` or `conservative_estimate` origin. Values are decimal strings so
the browser does not round large integer amounts. Concurrency reservations
are excluded from usage totals. Unavailable accounting is not converted to a
zero cost, and these bounded observations are not lifetime operation totals.

## Snapshot, bounds and refresh

The service bounds source observations to 2,048, page size to 100, search text
to 160 characters and each entry's retained source/reason lists. An oversized
catalogue fails explicitly instead of silently claiming a complete truncated
inventory. These are response/projection bounds; the underlying indexed tables
remain the authority and must be sized and monitored normally.

The snapshot hash covers the full visible projected catalogue. Filtering does
not change that hash, allowing a filtered list and its detail response to be
compared. Cursors bind the workspace, principal, normalized filters, snapshot
and last entry. A changed snapshot or a cursor reused with another scope or
filter returns a conflict; malformed cursors are invalid input. No read-time
clock is included in the hash.

The frontend aborts superseded requests and fences late results when the
workspace, filter, selection or page changes. A conflict or changed principal
or snapshot clears the old selection and paging state and asks for an explicit
refresh. Raw upstream errors and unexpected fields do not enter the page.

## Verification and remaining gates

Unit tests cover conservative readiness, source identity, update/release
projection, namespace collisions, bounded inputs, cursor consistency and
usage provenance/deduplication. API/CLI tests cover authentication, fixed
workspace scope, invalid queries, sanitized failures and the read-only command
surface. Real PostgreSQL tests exercise restricted roles, membership/context
visibility, owned history and accounting, and the database read-only boundary.
Frontend tests cover the public projector and proxy contract.

The main CI workflow has a dedicated catalogue acceptance step and retains
`operation-catalogue-u04-c.xml`. The normal frontend test, typecheck and build
gates apply. A local pass and the already merged S12 CI results do not constitute
CI evidence for the final commit containing this increment.

### Local patch validation

| Check | Result on this increment |
| --- | --- |
| Focused Python projection/API/CLI/context/release regressions | 139 passed, including seven new catalogue PostgreSQL tests and 16 existing context/S12 PostgreSQL regressions |
| Roadmap inventory regression suite | 50 passed |
| Full frontend test suite | 111 passed, no skipped tests |
| Python static checks | Ruff and formatting passed across 943 files; mypy passed across 391 source files |
| Frontend static/build checks | TypeScript and Next.js 16.4.5 production build passed |
| Python-to-TypeScript public contract check | Actual list, filtered list, cursor continuation and detail projections accepted without changing their public fields |
| Roadmap and canonical plan consistency | 208 sources and 731 requirements reconciled; tool and object-memory plan checkers passed; runtime qualification remains false |

The database checks used real disposable PostgreSQL 16.2 with pgvector 0.8.7.
Hosted-provider responses in the tests are explicit mocks. Local Chromium
could not open a page because this environment denies its Unix-domain socket;
no rendered acceptance pass for the new page is claimed. Final-commit CI and
an actual operator-host UI check remain follow-up qualification.

The U04-E follow-on implements authorized native installation/grant checks,
hosted policy/credential checks and owned job execution for the supported
handlers. Remaining programme work includes acquisition discovery/staging beyond
the existing S9–S12 links, current-host qualification, broader non-job invocation
owner coverage, connector behavior and actual concurrency qualification. Continue
through the existing gateway, context, grant and ledger owners. Do not promote
the whole U04/M4/T04 programme from this catalogue increment.

# U04-X executable tool acquisition and qualification

**Patch baseline:** `7fd1c9349c3ae7feb8d52e23701d1d26a4a01bc4`, reviewed 17 November 2026.
**Milestone:** the complete executable acquisition path following the merged
[portable skill lifecycle](u04-a-portable-skill-acquisition.md): exact source,
operator recipe, build review, sandbox build, independent staged tests, first
installation or upgrade, independent runtime repin, installed postflight,
rollback and recovery.

`/setup/tools`, `potbot tool-acquisition` and the authenticated HTTP API now
share a durable lifecycle. Migration 41 extends the existing S11/S12 approval
record. S10 still owns sandbox build/staging, S9 still owns tests and filesystem
promotion, and S12 still owns executable release. The change adds no replacement
installed-tool registry, job scheduler, Cog grant or model execution authority.

## Completed workflow

| Phase | Durable result | Execution authority |
| --- | --- | --- |
| Publish recipe | Private immutable recipe, fixed GitHub repository, enrolled Agent and operator signature | Host CLI and independent native setup capability |
| Request source | Exact 40-character commit under the selected recipe; stable command identity | Authenticated principal plus setup capability |
| Acquire source | Verified Git objects, exact tracked bytes, bounded inventory and immutable private receipt | One-shot host source worker using an independently pinned Git executable |
| Review | Actual predecessor or verified absence, source/recipe hashes, rendered command, runtime pins and dependency lock hashes | Read-only review through API, CLI and UI |
| Approve build | Exact revision and review digest signed independently of the workspace database | Explicit operator decision |
| Build and stage | One approved rootfs command in the monitored sandbox, confirmed stop and descriptor-safe transfer receipt | Existing S10 writer lease and S7 monitor |
| Qualify candidate | Exact Git source/artifacts, smoke and unit tests, cleanup proof and immutable S9 evidence | Existing independent S9 test owner |
| Approve promotion | Exact measured test evidence and revision | Separate operator decision; build approval does not activate |
| Promote | Atomic initial placement or upgrade exchange, exact registration and a retained denial hold | Existing S11 worker and S9 filesystem owner |
| Repin | Operator applies reviewed executable hashes, restarts with the reviewed manifest, then requests host readback | Existing S12 authority; no automatic manifest editing |
| Postflight | Original approved test suite against installed bytes, with retained signed release evidence | Existing S12 authority and monitored test executor |
| Roll back | Verified predecessor restored, or first installation removed to retained private staging | Existing S9 owner; restored executables remain held |
| Withdraw | Actual absence plus removal of retired runtime pins independently verified | S12 records `withdrawn`; no synthetic passing postflight |
| Reconcile | Exact observed outcome or terminal abandonment, preserving evidence and denial holds | Newly signed action, existing native leases and independent readback |

The source worker and build worker are explicit maintenance commands. The API
constructor starts no worker. A browser action records a decision or host
request; it cannot invoke Git, a process supervisor or filesystem promotion.

## Source and recipe contract

Sources are canonical public `https://github.com/OWNER/REPOSITORY.git` URLs at an
exact lowercase 40-character Git commit. Moving refs, alternate transports,
credentials in URLs, redirects, submodules, source symlinks, special files and
unsafe resource names are refused. The fixed recipe selects the repository;
the browser supplies only its published recipe ID and the exact commit.

Source fetches use an absolute host Git executable whose SHA-256 the operator
has independently approved. The child receives a narrow environment, disabled
credential helpers, no inherited proxy/token/configuration, and an empty private
home so libcurl cannot reuse an operator's `.netrc`. Automatic Git maintenance
is disabled. The subprocess group is terminated on both completion and failure,
including when the Git leader exited before a pipe-holding helper.

The source policy bounds committed files to 8,192, expanded source to 128 MiB,
each source file to 16 MiB, and retained Git/source storage to 384 MiB and
32,768 entries. Git commands have a 120-second deadline and bounded output;
storage growth is monitored throughout retrieval. These are application
admission limits, not an operating-system disk quota. The private cache must be
on the qualified controller's controlled storage.

Every receipt binds repository, commit, Git tree, source inventory, file count,
expanded byte count and the retained storage digest. Readback verifies the
receipt, Git origin/HEAD and actual tracked bytes independently of the mutable
Git index. A committed request reuses its existing immutable receipt. An
incomplete attempt is not overwritten or silently resumed.

Recipes are bounded, private, regular operator files. They contain:

- A fixed tool slug, public repository, name and description.
- Exact build argv and timeout, with only `{build}`, `{checkout}`, `{tool}` and
  `{agent_home}` path substitutions. The executable remains an absolute rootfs
  program.
- Declared source cleanup paths and dependency lockfile paths.
- Explicit reusable artifact paths; qualification requires at least one
  measured executable under `static/` or `environment/`.
- Two to sixteen distinct tests, including smoke and unit tests, using existing
  S9 command and time limits.

The CLI retains a private copy named by the complete recipe digest before
publishing its signed record. Each worker independently loads that copy and
compares it to the published recipe. Source review binds the fully rendered
command and actual registered Agent directory, not an unresolved template.
Recipe commands appear in the owner's build review; credentials must use the
existing credential owners, not command-line secrets.

A recipe may deliberately run source code inside the approved sandbox. This
does not authorize repository hooks or install hints to execute on the host.
The builder has no automatic dependency or model download path. Dependencies
must already exist in the qualified image or approved storage; a missing
dependency is a failed build, not implicit permission to install one.

## Isolation, source integrity and delayed-worker fencing

Source materialization, ownership handling, exact no-follow inventory checks
and process dispatch occur inside the same existing S10 writer lease. The
destination is the enrolled home's `builds/OPERATION_UUID_HEX` directory and
must not already exist. Host ownership comes from the actual qualified physical
home; guest UID metadata is never treated as a host UID map.

Build approval is checked again inside the writer lease before dispatch. S9
revalidates the same claim under its own lease before tests and journal changes.
Activation, rollback and recovery also validate current claims inside their
native leases. S12 checks both before work and inside its existing PostgreSQL
transaction before committing release evidence. These checks prevent a delayed
worker from acting after a signed recovery has fenced its old claim.

The staged `.git` directory is untrusted builder output. Host verification now
rejects executable Git configuration, includes, filters, hooks, alternate
object stores, replacement refs and other redirected control files before
starting Git. Actual tracked bytes and modes are compared to the pinned HEAD;
`assume-unchanged` and `skip-worktree` flags cannot hide modified source.
Incomplete S10 incoming directories remain hidden from ordinary guest mounts.

The existing host restrictions still apply: a qualified Linux/WSL2 runtime,
verified image/rootfs, registered Agent homes, healthy S7 monitor, mediated
filesystem access and maintenance-window coordination. This code does not
claim to defeat a privileged unbrokered host writer or create a distributed
lease protocol.

## First installation and rollback

An absent predecessor is represented as `oldCommit: null` and `kind: install`.
The host independently checks the registry, filesystem and retained release
hold; a damaged or unregistered tree is not treated as absence. No zero hash,
empty predecessor repository or fabricated install receipt is introduced.

S9 places the first tested candidate atomically into an absent destination and
records its exact definition. Upgrades retain the existing atomic exchange and
same-publisher checks. Both operations create a denial hold before admission
can observe changed executables.

Rolling back an upgrade restores its real predecessor and requires fresh
runtime repin/postflight. Rolling back a first installation returns the exact
installed tree to hidden retained staging and removes only its matching
registration. The operator removes the retired executable pins. S12 then
verifies absence and records `withdrawn`; the denial marker remains so another
unregistered tree at the old pathname cannot inherit admission.

The unified operation catalogue reports withdrawal as unavailable and excludes
pretest acquisition records. It continues to distinguish recorded release
evidence from current runtime/grant admission.

## Recovery and cancellation

Every public mutation requires an exact revision, appropriate evidence digest,
explicit confirmation, independent setup capability and stable idempotency key.
Retries return the original committed public receipt even if the worker has
advanced the operation. The browser retains the exact request body/key in its
scoped recovery journal and reloads current state before offering another action.

Queued source/build requests can be cancelled. Rejecting or cancelling a tested
candidate requires a subsequent host reconciliation to retire its native S9
`ready` transaction. Abandonment takes the existing writer lease, verifies the
original installation/candidate where present, records `aborted`, and keeps the
hidden bytes. A new attempt uses a new operation ID.

A signed recovery can fence a stale sourcing/building or host-action claim.
It does not assert that a live child has stopped. An active or indeterminate
native lease still blocks recovery until the operator supplies the existing
independent reconciliation evidence. A stale worker denied before dispatch
does not create a fabricated uncertain lease.

For a lost host acknowledgment, the worker reads the actual S9 journal and S12
binding. A completed repin or release is read back without rerunning tests.
Failed/indeterminate postflight evidence can be acknowledged unchanged under
the native hold; it is never reset into a new test attempt. Interrupted
postflight offers rollback. Partial filesystem transitions use the existing
S9 reconciliation rules and produce a new private pin proposal when needed.

Setup-key rotation invalidates pending execution authority. A fresh current-key
decision may authorize terminal abandonment of an unactivated candidate using
its exact retained private recipe. That `abandon_only` claim cannot build,
promote or restore the old execution signature. Activated records with invalid
historical release authority remain held for explicit native recovery.

## Operator setup and use

Apply all pending application migrations with the existing admin credential:

```bash
uv run --locked potbot admin migrate
```

Provision private controller directories for recipes, source receipts, S10
receipts and runtime pin proposals outside both the workspace home and shared
tool mounts. Use the existing native credential profile for the API and host
worker. `api.setup_token` remains independent of the ordinary workspace bearer.
The Next server retains `POTBOT_UI_SETUP_TOKEN`; it is not hydrated into browser
JavaScript or placed in URLs.

Publish a reviewed private recipe for an already enrolled Agent:

```bash
uv run --locked potbot tool-acquisition \
  --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  publish-recipe /srv/potbot/controller/recipe.json \
  --manifest /srv/potbot/controller/manifest.json \
  --workspace-name WORKSPACE_NAME --agent-id AGENT_UUID \
  --recipe-dir /srv/potbot/controller/recipes \
  --key publish-reviewed-recipe-1 --confirm
```

Request its exact source using the returned recipe UUID:

```bash
uv run --locked potbot tool-acquisition \
  --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  request RECIPE_UUID --commit FULL_40_CHARACTER_COMMIT \
  --key acquire-reviewed-source-1 --confirm
```

On the qualified host, quiesce the normal singleton S7 operator before running
maintenance. The following command shape advances one exact operation. Replace
`source` with `build`, `promote` or `action` only after the corresponding durable
approval/request exists:

```bash
uv run --locked potbot tool-acquisition \
  --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  run-once --phase source --operation-id OPERATION_UUID \
  --manifest /srv/potbot/controller/manifest.json \
  --workspace-name WORKSPACE_NAME --agent-id AGENT_UUID \
  --recipe-dir /srv/potbot/controller/recipes \
  --source-dir /srv/potbot/controller/sources \
  --evidence-dir /srv/potbot/controller/stages \
  --proposal-dir /srv/potbot/controller/pins \
  --git /usr/bin/git --git-sha256 INDEPENDENTLY_REVIEWED_GIT_SHA256
```

`inspect` returns the current review/evidence/binding digest and revision. The
CLI controls `approve-build`, `approve`, `reject`, `rollback`, `reconcile`,
`repin`, `postflight` and `cancel` all take an operation UUID, `--revision`,
`--digest`, `--key` and `--confirm`. Use `review.sha256` for build approval,
`evidenceSha256` for tested approval/rejection, and `bindingSha256` for the other
controls. `recipes`, `list` and `events` provide bounded pagination.

After promotion or rollback, inspect the private pin proposal, independently
review/apply the executable hashes or removals, and restart with the approved
manifest. Then request `repin` and run the `action` phase. Request and run
`postflight` only when S12 reports `repinned`. Verified first-install withdrawal
finishes at pin-removal readback without executing an absent tool.

The UI follows the same sequence and exposes only actions supported by the
current server record. Existing legacy tested approvals remain available in
the same workbench. A new released executable still needs its existing Cog,
dispatcher, credential and resource grants for ordinary task execution.

## Verification and evidence limits

The completed local gates for this patch are:

| Gate | Result |
| --- | --- |
| Focused acquisition/runtime/API/CLI and check-runner unit regressions | 309 passed, plus 8 subtests |
| PostgreSQL acquisition pipeline, first-install and S12 lifecycle | 58 passed |
| Merged skill acquisition, operation catalogue and workbench PostgreSQL regressions | 27 passed |
| Complete frontend test suite | 193 passed, including 23 acquisition tests |
| Frontend TypeScript and production build | Passed |
| Python typing | Passed across 410 source files |
| Ruff lint and formatting | Passed; 823 Python files formatted |
| Canonical tool and memory plan checks | Passed; 48 and 32 stages respectively |
| Locked dependency resolution | Passed; no dependency or lockfile changes |

The dedicated U04-X gate covers real PostgreSQL migration/RLS, signed commands,
HTTP projections, actual local Git acquisition, descriptor-safe copy, native
lease journals and atomic filesystem placement. Named process fixtures exercise
build/test/postflight success, failure and stop/fencing outcomes. They are not
native sandbox qualification receipts.

The frontend contract gate consumes the actual HTTP fixture exported by the
PostgreSQL pipeline. Unit tests cover hostile source paths/control files, Git
helper lifetime, claim fencing, bounded requests and optional-dependency-free
CLI help. Frontend tests cover strict projections, route guards, scope changes,
exact request recovery and lifecycle actions. Typecheck and production build
remain separate from rendered-browser acceptance.

The merged baseline's broad CI PostgreSQL lane timed out after 900 seconds at
89% of its selected tests without a reported assertion failure. Its finite
budget is now 1,800 seconds and the enclosing verification job has 45 minutes.
The full broad gate is retained; the new dedicated U04-X gate also retains its
JUnit and real HTTP contract fixture.

This patch does not claim live GitHub retrieval, installed native-vault
custody, Bubblewrap/Syn/WSL2 identity qualification, third-party package task
success, rendered-browser acceptance or the new patch's future GitHub CI run.
Those must be measured on the selected real operator host. The roadmap retains
`runtime_qualified: false` and the wider U04/M4/T04 requirements remain partial.

## Remaining programme

The next major acquisition breadth is curated registry discovery and publisher
trust, including documented ClawHub/skills.sh/MCP adapters, task-led selection
and dependency eligibility measured against actual agent/runtime capabilities.
It should use these source, review, build and release owners. Portable skill
filesystem views, per-agent allowlists/turn snapshots, vendor substitutions and
real task-success qualification remain separate requirements. No install hint,
registry popularity signal or successful package parse should bypass this
executable approval and release lifecycle.

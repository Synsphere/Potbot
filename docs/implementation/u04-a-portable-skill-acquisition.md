# U04-A portable skill acquisition and lifecycle

**Patch baseline:** `c10b9d24de033b8ee93785d2b37e703b9d5e6f75`, reviewed 17 November 2026.
**Milestone:** a complete data-package workflow following the merged U04-E
operation workbench: acquire, inspect, review, install, activate, read resources,
update, roll back, disable and revoke.

The `/skills` workshop, `potbot skills` CLI and authenticated HTTP API use the
same PostgreSQL service. Imported releases join the existing Context skill
registry and unified operation catalogue. Actual guest discovery and resource
loading continue through genuine Context sessions. Installation creates no
execution grant, interpreter process, credential injection or dependency job.

## Completed workflow

| Step | Result |
| --- | --- |
| Acquire | A bounded ZIP, CLI local directory, or public GitHub repository at an exact commit becomes a private immutable package with original files and source provenance. |
| Inspect | Review original `SKILL.md`, resource inventory, full-file hashes, license declaration, declared dependencies, vendor fields and explicit compatibility losses. |
| Plan | A deterministic review binds the target skill, package, source, predecessor and current control revision. Updates show added, changed and removed paths. |
| Install | A separately authorized, explicitly confirmed transaction admits exactly that plan into the existing skill registry. The selected release starts disabled. |
| Activate | A second explicit control enables the reviewed current release for future authenticated discovery and reads. A valid current approval is required. |
| Read | Guests explicitly request a skill ID and full package digest; supporting files use bounded, verified Context resource reads. |
| Update | Acquire another immutable package and review its diff. Installation advances the same skill and disables the new release until explicit activation. |
| Roll back | Select a retained approved release with an exact current revision. Rollback restores its original provenance and leaves it disabled. |
| Disable | Stop future guest discovery, loads and resource reads while retaining approved bytes and history. |
| Revoke | Permanently close the skill identity. Future installation, activation and rollback are denied; owner review history remains available. |

The lifecycle is synchronous bounded data work. It does not introduce another
job queue, scheduler, agent session authority or installed-skill registry.
The existing S9–S12 tool owners remain responsible for executable dependencies,
maintenance leases, runtime pins and postflight release.

## Sources and portable format

`context/skill_packages.py` implements three explicit entry points:

- **ZIP upload:** an archive containing root `SKILL.md`, one enclosing folder,
  or an explicitly selected subdirectory. The API accepts bytes rather than a
  host path. The exact compressed archive hash and selected path are retained.
- **Local directory through the CLI:** bounded regular files are read through
  no-follow descriptors and converted into a ZIP. There is no remote host-path
  input. Links, devices, changing files and excessive inventories are rejected.
- **Public GitHub:** a canonical `https://github.com/owner/repository` URL, a full
  lowercase 40-character commit, and an optional exact subdirectory. The adapter
  fetches only the fixed GitHub archive host. It disables ambient proxies,
  redirects and credentials; no Git subprocess or checkout hook runs.

Branches, tags, arbitrary URLs, private repositories, registry searches and
authentication-assisted downloads are outside this source profile. A GitHub
archive is bounded as a whole before subdirectory selection, so a large
repository may require an explicitly prepared ZIP containing the skill.

The parser supports UTF-8 `SKILL.md` with YAML frontmatter, multiline descriptions
and bounded JSON-compatible metadata. Imported names use 1–64 lowercase ASCII
alphanumeric characters separated by single hyphens. The original text and
resource bytes are retained verbatim, including CRLF line endings. Parsed
metadata normalizes numbers for stable PostgreSQL JSONB identity; original
spelling remains in `SKILL.md`. Duplicate mapping keys, aliases, object tags,
non-finite or unsafe large numbers, excessive nesting and invalid Unicode/NUL
metadata are rejected before persistence.

The format is informed by the [Agent Skills specification](https://agentskills.io/specification)
and [OpenClaw skill metadata](https://docs.openclaw.ai/tools/skills). Dependencies
such as binaries, alternative binaries, environment variables, configuration,
operating systems and declared tools become **unverified requirements**.
OpenClaw installers, credential injection, proprietary dispatch fields and
unknown behavior remain visible in the compatibility report and never become
Potbot permissions. An `allowed-tools` declaration is source data.

Bundled scripts can be inspected and downloaded. They are never run by import,
installation, activation or resource reads. A package's `AGENTS.md`, `MODEL.md`
or `MEMORY.md` remains an ordinary untrusted resource. Potbot independently
seeds and manages the skill's live Context triple.

| Bound | Allowance |
| --- | --- |
| Compressed ZIP | 4 MiB |
| Expanded selected package | 8 MiB |
| Regular files | 128 |
| Individual resource, including `SKILL.md` | 1 MiB |
| Frontmatter | 32 KiB |
| Canonical package manifest | 128 KiB |
| Resource response | 64 KiB of decoded bytes |
| HTTP ZIP JSON envelope | 6 MiB |
| Other HTTP mutation envelopes | 64 KiB |
| Metadata page | At most 50 entries and 1.5 MiB, with a continuation cursor |

The importer rejects traversal, absolute paths, drive names, backslashes,
noncanonical Unicode paths, device names, case collisions, duplicate ZIP
entries, file/directory collisions, links, encrypted entries and unsupported
compression. It reads bounded entries in memory and never extracts an archive.
Source integrity is recorded as `content-integrity-only`; neither a commit hash
nor a package digest establishes publisher trust or execution safety.

## Identity, approvals and migration 40

The release digest hashes the canonical manifest of **every file**. A resource
update therefore produces a new release even if `SKILL.md` is unchanged.
`content_sha256` separately identifies the original instruction text. Source
provenance has its own digest, so identical package bytes can be reviewed from
a different source without redefining an immutable release.

Migration **40** extends the existing `workspace_context_skills`, versions and
releases. Imported records use an explicit `acquired` origin and the principal
namespace `imported-<principal-hex>.<name>`. They do not claim an agent author
or a Context session. Historical authored rows retain their agent identities,
text-based release hashes and mandatory genuine active-session authoring rules.
Guest `skills.save`, `skills.publish` and `skills.archive` cannot edit imported
records.

Private package and file tables retain exact source/manifest bytes, and immutable
control records retain every install, update, activation, rollback, disable and
revocation. Current package identity is separate from the release's original
package identity, allowing an explicit source-only change to have its own audit
event. The current head uses a monotonic control revision and compare-and-swap.

Install and control actions require ordinary workspace authentication plus the
independent native `api.setup_token` capability. The server signs the exact
workspace, owner, skill, selected package/release/source, revision, state and
enabled flag. Runtime loading verifies against the current native capability.
Changing raw database fields does not produce a valid approval. Activation and
rollback cannot convert a forged head or unsigned retained release into an
approved package.

Token rotation or an unavailable vault blocks imported runtime reads. Recover
by opening a fresh review plan for the retained package, explicitly installing
it with the current capability and then activating it. Disabling a damaged
head cannot manufacture a new activation authority. Terminal revocation remains
available to contain a damaged or rotated record.

Prior migration checksums are preserved. Before running the upgraded API and
workers, apply the existing `potbot admin migrate` command through revision 40
with the configured administrative connection, then restart those processes.
The dedicated migration test upgrades populated revision-39 authored records
and verifies their original text, context and authoring behavior.

## Context loading and revocation

Existing `skills.list` and `skills.load` include enabled acquired skills whose
current approval verifies. `skills.load` requires an exact current release
digest and returns original text, `content_sha256`, the package manifest,
provenance and a separate live Context triple. Content remains classified as
`untrusted-skill-data`; it is not automatically injected into instructions.

The new guest method is:

```json
{
  "method": "skills.read",
  "skill_id": "CANONICAL_SKILL_UUID",
  "release_sha256": "EXACT_PACKAGE_SHA256",
  "resource_path": "references/guide.md",
  "offset": 0,
  "max_bytes": 65536
}
```

The existing Context RPC envelope supplies schema and request ID, and its real
session supplies workspace/principal/agent authority. Every read rechecks
current state, approval and whole-package integrity before returning a base64
range with full resource and range hashes. The guest client verifies response
identity, offset, size, digest and forward progress.

Full instruction loads retain the existing 2 MiB transport frame limit. If the
complete escaped response and live Context cannot fit, the request returns a
bounded nonfatal error before consuming the session request count. The guest
can continue with `skills.read` ranges for `SKILL.md`; no successful load claims
that a truncated preview is the complete instruction text.

Imported bytes are **not** written into the persistent `_agents` tree mounted
by ordinary workers. That existing tree retains historical authored snapshots;
placing imported resources there would leave an old filesystem path readable
after revocation. Imported resources instead remain behind authenticated RPC.
This profile therefore does not promise filesystem `{baseDir}` compatibility
or automatic execution of relative script paths.

Update and rollback never substitute a different release for an explicitly
requested digest. Subsequent reads of an old digest fail once the current head
changes. Disable and revoke prevent future guest reads. Already returned bytes
cannot be recalled from a running process. The owner can inspect retained
history and resource bytes through operator review interfaces, including after
revocation; that review access is separate from guest runtime availability.

## HTTP and browser workflow

All responses use `potbot-skill-acquisition/v1` with explicit `workspaceId` and
`principalId` and private no-store caching. The browser uses corresponding fixed
`/api/ui/skills` BFF routes. Its existing server-only `POTBOT_UI_SETUP_TOKEN`
must match the backend/runtime's native `api.setup_token`; tokens never enter
browser JavaScript or package-source requests. Mutating endpoints are enabled
only through the operator loopback setup bridge.

| Method and suffix under `/v1/ui/skills` | Purpose |
| --- | --- |
| `GET /`, `GET /packages` | Installed library and private acquisition pages |
| `POST /imports/zip` | `{archiveBase64, subdirectory}` |
| `POST /imports/github` | `{repository, commit, path}` |
| `GET /packages/{id}` | Manifest, source and a 64 KiB instruction preview with explicit truncation |
| `GET /packages/{id}/plan?skillId=…` | Exact first-install or update review |
| `POST /packages/{id}/install` | `{planSha256, expectedRevision, skillId, confirm:true}` |
| `GET /{id}` | Current record, recorded enabled state and independently checked approval validity |
| `GET /{id}/history?limit=25&after=…` | Paginated immutable control decisions and their retained releases |
| `POST /{id}/activate`, `/disable`, `/revoke` | `{expectedRevision, releaseSha256:null, confirm:true}` |
| `POST /{id}/rollback` | `{expectedRevision, releaseSha256, confirm:true}` |
| `GET /packages/{id}/resources?path=…&offset=0&length=65536` | Private package inspection |
| `GET /{id}/resources?releaseSha256=…&path=…&offset=0&length=65536` | Exact retained release/resource inspection |

Every POST requires a stable `Idempotency-Key`, ordinary authentication, an exact
allowed Origin and setup capability. Duplicate headers/fields/queries, unexpected
selectors and oversized requests fail before mutation. Workspace peers cannot
inspect another principal's private acquisitions or control their skills.
Published availability is checked separately from raw recorded state.

The workshop exposes the complete review and lifecycle, manifest/resource
inspection, full-digest downloads and paginated history. Text and metadata are
escaped. No skill Run control is introduced. The existing Operations page
continues to distinguish these data packages from admitted execution routes.

Before submitting a browser mutation, the exact body/key is retained in a
workspace/principal-scoped IndexedDB recovery journal. An uncertain response
offers an explicit retry with that same request. If durable browser storage is
unavailable, the UI explains that recovery is limited to the current tab.
Scope and selection changes fence late responses. No source acquisition or
control action starts automatically during navigation.

## CLI

Install the `skills` extra for standalone CLI parsing; the `web` extra includes
the same YAML dependency. Help does not load PostgreSQL, YAML, network clients
or native credentials. All commands use the existing database/scope options.

```sh
potbot skills --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  import-zip ./review-documents.zip --key acquire-001
potbot skills --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  import-directory ./review-documents --key acquire-local-001
potbot skills --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  import-github https://github.com/OWNER/REPOSITORY \
  --commit FULL_40_CHARACTER_COMMIT --path skills/review-documents --key acquire-git-001
potbot skills --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  plan PACKAGE_UUID
potbot skills --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  install PACKAGE_UUID --plan PLAN_SHA256 --revision 0 --key install-001 --confirm
potbot skills --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  activate SKILL_UUID --revision 1 --key activate-001 --confirm
potbot skills --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  download SKILL_UUID --release RELEASE_SHA256 --path references/guide.md --output ./guide.md
potbot skills --workspace-id WORKSPACE_UUID --principal-id PRINCIPAL_UUID \
  history SKILL_UUID --limit 25
```

Use `plan NEW_PACKAGE_UUID --skill SKILL_UUID` for an update; install with the
returned exact digest and current revision. `rollback`, `disable` and `revoke`
require `--revision`, `--key` and `--confirm`. Rollback also requires `--release`.
Inspect returned revisions rather than assuming the example's values.

Repeat an uncertain command with its identical key and arguments. Committed
Git retries return the retained receipt before fetching upstream again. A
different request under that key conflicts. Downloads verify every range and
the complete digest before exclusively creating a new destination file.

## Acceptance and remaining programme

The dedicated gate covers parser attacks and budgets, private package/RLS
boundaries, current-key signatures, raw-SQL forgery, exact review/CAS, concurrent
commands, lost-response replay, source-only changes, resource-only changes,
rollback, terminal revocation, numeric JSONB round trips and the populated
revision-39 migration. Real PostgreSQL tests exercise HTTP and genuine Context
sessions, including a framed worker subprocess. The actual HTTP fixture is
checked by the TypeScript projectors; browser bridge, recovery and resource
download tests run separately.

Local acceptance on this patch completed these gates:

| Gate | Result |
| --- | --- |
| Dedicated acquisition, HTTP, CLI, Context and migration gate | 253 tests passed, including 31 real PostgreSQL cases |
| Existing operation catalogue/workbench and Context database regressions | 54 PostgreSQL tests passed |
| Existing API, CLI, catalogue and worker-dispatch regressions | 106 tests passed |
| Complete frontend contract/interaction suite | 168 tests passed, including 28 acquisition cases |
| Rich HTTP fixture from the real PostgreSQL lifecycle | Accepted by the frontend projectors |
| TypeScript and Next.js production build | Passed |
| Strict Python type checking | Passed across 402 source files |
| Python lint and formatting | Passed; 805 files checked for formatting |
| CI-pinned uv 0.12.23 lock compatibility | Passed |

An additional existing installed-Syn socket regression could not run in this
execution environment: creating its AF_UNIX socket raised `EPERM` before
product code. Its qualification gate remains unchanged. The successful framed
Context worker test uses subprocess pipes and real PostgreSQL; it does not
stand in for the installed Syn/Bubblewrap socket boundary.

These are implementation and local/database/contract acceptance claims. Live
GitHub retrieval, a deployed native credential store, Bubblewrap launch and
rendered browser interaction retain their installed-host gates. No source test,
simulated transport, archive hash or build marks the wider runtime qualified.

The next acquisition programme is executable and registry breadth: documented
ClawHub/skills.sh/MCP adapters, publisher trust envelopes, task-led search,
sandbox-measured dependency eligibility and separately approved tool installation
through S9–S12. Filesystem skill views, per-agent allowlists, turn snapshots,
native vendor substitutions and task-success qualification also remain under
the wider skill integration plan. U04/M4/T04 stay partial; this milestone closes
the complete bounded portable-data lifecycle, not those independent programmes.

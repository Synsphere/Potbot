# Potbot Orb Workspace — UI-1/UI-2

Next.js frontend for Potbot, designed to live under `potbot/frontend/`. This first
increment implements an orbital workspace selector and a floating/dockable window
shell, with an on-demand Three.js graph **demo preview**. It deliberately does
**not** claim that the backend's workspace discovery, chat sessions, PTY, browser,
preferences or download/installation telemetry contracts exist yet.

## Run

Node 22+ is recommended. From the repository's `frontend/` directory:

```bash
npm install  # generates package-lock.json; commit it after validating the build
npm run dev
```

Open `http://127.0.0.1:3100/?demo=1` for an **explicitly synthetic** four-workspace
preview. The root route `/` never invents backend resources: until UI-backend
integration is configured it displays a setup notice.

The first build environment used for this patch has no external npm registry
access, so neither `npm install`, `npm run build`, nor an interactive browser E2E
run could be completed here. The pure layout/orbit/graph tests run without npm
packages on Node 22:

```bash
npm test
node --experimental-strip-types tests/bench-graph.mjs
```

These checks are not substitutes for a browser build. Once dependencies are
installed, run `npm run typecheck` and `npm run build`, then test the interface
with Playwright on Chromium, Firefox and WebKit.

## Controls

- Hover the top orbital ring, or click **Workspaces** to reveal all workspace
  orbs. **Alt + Left/Right** switches and recentres the active orb. The other
  orbs move above the top edge after selection.
- The chat is the **permanent central surface**. Other panes float by default.
  Drag a Dockview pane header or tab to reposition/dock; each pane also has
  explicit dock-left/right/above/below and float controls.
- The **menu cannot maximize**. The graph can maximize using its `⛶` control
  even while it is floating. Escape restores the graph to its prior pane.
- Close the graph to animate into an independent **utility orb** near the
  upper-right; click that orb to restore the floating pane. It does not join the
  workspace ring.
- The **terminal** button inside the composer creates a floating pane, initially
  anchored near the composer. It remains a placeholder until a workspace-scoped
  authenticated PTY service is provided by the backend.
- The menu opens **Preferences**, **Downloads** and **Model installations** in
  independent dockable panes. All progress indicators accurately report that
  backend telemetry is not connected; no fake downloads/installations appear.
- Window layouts are persisted in **browser localStorage per workspace** with a
  versioned schema. Switching workspaces unmounts its layout and restores the
  newly selected workspace's layout.

## Performance choices

The orbital selector uses CSS transforms and opacity. Motion handles spring
interpolation and shared-layout animation without creating a WebGL context for
navigation. Dockview does all the heavy docking, saving/restoring, floating and
resizing. The graph preview uses **one InstancedMesh** for all nodes plus a
single line-segment geometry for edges and does not run a perpetual animation
loop. It renders only on interaction, resize, visibility and data changes.

The graph caps device pixel ratio to 1.5, nodes to 1500, and edges to 4500.
When hidden or replaced with its utility orb, its WebGL resources are disposed.
A keyboard-focusable list/fallback should be expanded in UI-3 for production
accessibility. The included CPU diagnostic measures layout projection only,
not actual GPU frame times or VRAM usage.

See `../docs/ui/performance-research.md` and
`../docs/ui/frontend-backend-contract.md` for design trade-offs and missing
backend services.

## Safety boundaries

The preview never accepts provider keys in browser code. No Next.js route is
currently permitted to proxy Potbot's privileged bearer credentials. When
connecting to a real backend, implement user authentication, workspace-scoped
sessions, CSRF protection for mutations, and a server-side credential boundary
before enabling remote requests. Do not expose Potbot's service API token using
`NEXT_PUBLIC_` environment variables.

## UI-3 — live operator bridge, streaming chat and reasoning checkpoint graph

UI-3 adds server-only, single-operator authentication and a limited bridge to
**existing** Potbot API endpoints. Live mode deliberately exposes **one**
configured workspace. The current backend does not yet have authenticated
multi-workspace discovery or durable chat sessions. Demo workspaces remain
available exclusively at `/?demo=1` on a development build.

1. Copy `.env.example` to `.env.local` and replace **every** placeholder with
   actual, private operator values. `POTBOT_UI_PASSWORD` is the local UI login;
   `POTBOT_UI_COOKIE_SECRET` should be independently random (32+ characters).
2. `POTBOT_UI_PUBLIC_ORIGIN` must match exactly how you open Next.js (for
   example `http://127.0.0.1:3100`). A non-loopback public origin requires
   HTTPS. Cookie sessions last eight hours, are HttpOnly and SameSite=Strict.
3. `POTBOT_UI_BACKEND_URL` is restricted to an explicit HTTP **loopback** `/`
   origin (for example `http://127.0.0.1:8000`); forward a remote backend via a
   trusted tunnel or reverse proxy instead of weakening the server's URL policy.
4. `POTBOT_UI_BACKEND_TOKEN` must be a **workspace-scoped** backend credential;
   `POTBOT_UI_WORKSPACE_ID` must be its actual workspace. The existing backend
   does not provide an identity-introspection endpoint, so configuration must
   be checked by the operator. Never put this token into `NEXT_PUBLIC_*`.
5. Start Potbot's service and then `npm install && npm run dev` in `frontend/`.
   Open `/`, authenticate with the operator password, and submit a message.

The browser calls authenticated same-origin Next.js `/api/ui/*` routes, never
Potbot directly. Next forwards the normal OpenAI-compatible chat stream and
safe run ID, then polls the existing `/v1/reasoning/{run_id}` snapshot and
paginated events. The graph is a **sanitized projection** containing only node
labels, kinds, state, edges and revision; raw results, prompt hashes and claim
tokens are not forwarded. Sequence gaps reset the cursor and replay from the
current checkpoint. Polling is throttled while the graph is minimized or
when the run is terminal. The default graph is still 3D WebGL with an accessible
node-list fallback.

Chat context is maintained in this **browser tab only**; refreshing the page
or signing out clears it. This is not a persisted chat-session service. A lost
connection after dispatch has an **uncertain** outcome and is **never**
automatically resubmitted. The existing steering endpoint is not automatically
used for new chat messages, since the UI currently lacks a durable session and
explicit steering-intent workflow.

### Security and deployment boundaries

This is a **single-operator** bridge, not multi-user authentication. It rejects
cross-origin mutations, validates the cookie HMAC and lifetime, enforces a
small in-process login-attempt limit, constrains request sizes and workspace
IDs, disables upstream redirects, and never returns server secrets to the
browser. Deploy behind TLS and an **external rate limiter**; the in-process
limiter is not sufficient for multi-process or Internet-facing authentication.
Do not expose this developer-facing bridge publicly until an account-aware,
workspace-authorized identity provider and end-to-end security review are added.

### Tests

```bash
npm test                          # offline, built-in Node 22 tests
npm run typecheck                 # requires npm dependencies installed
npm run build                     # requires npm dependencies installed
```

A mocked integration suite covers login, origin enforcement, credential
isolation, workspace bounds, chat streaming, safe graph projection and cursor
handling. A real Potbot service is needed for end-to-end browser acceptance.

## UI-4 — graph workbench, event timeline, rendering telemetry

The floating and fullscreen graph now share a searchable inspector. Filter
nodes by recorded state or kind, search by public node label or identifier,
see ancestor context retained for search hits, and select any node using a
keyboard-accessible, paginated list. A **List** toggle unmounts the WebGL canvas
without losing selection or filters. Graph search, view mode and the diagnostics
preference are stored locally per workspace; account-level sync still requires
an authorized backend preferences service.

The live event timeline displays the actual sequence and type fields returned
by `/v1/reasoning/{run_id}/events`. The Next.js BFF **never forwards raw event
payloads**, task secrets or claim tokens. Its 240-entry bounded history detects
cursor gaps, reports incomplete source history and resumes from the most recent
available cursor rather than looping through missing events. Potbot currently
does not supply event timestamps here, so the UI does not fabricate them.

**Perf** opens a diagnostic overlay showing instanced-node and line counts,
scene-build wall time, synchronous render CPU time, capped pixel ratio, draw
count and rough graph-buffer estimate. These figures are **not GPU timings,
frame rate or measured peak VRAM**. The Three.js canvas draws on demand and
selection changes reuse the existing scene, changing instance attributes and
camera position rather than rebuilding the geometry. WebGL context loss falls
back to an accessible node list. `npm run benchmark:graph` provides a separate
CPU-only 1,500-node query and timeline diagnostic.

The Preferences window now keeps the local compact setting active even after
the window is closed, offers accessible graph list mode and graph diagnostics,
and links directly to the dockable Downloads and Model Installations windows.
Those two windows still explicitly report **backend telemetry unavailable**.
No made-up transfer progress or installation status is displayed.

### Validation

```bash
npm test
npm run benchmark:graph
npm install --no-audit --no-fund
npm run typecheck
npm run build
```

The UI-4 patch also removes the `tabAnimation` Dockview prop, which was rejected
by the pinned `dockview-react` TypeScript contract in the preceding CI run.
The full dependency typecheck, Next production build, and a real-browser WebGL
benchmark still require npm installation in the target/CI environment.

## Native Xpra-only application windows (UI-5)

Potbot uses **one Xpra-owned X11 display per workspace** and the pinned
upstream Xpra HTML5 protocol/codec engine with *no visible Xpra desktop
chrome*. Each application window is an independently draggable/dockable
Dockview canvas. The operator's keyboard, pointer, focus and resize commands
are sent back via the same Xpra binary WebSocket protocol, not via XTEST.
No independent Xvfb server or PNG screenshot polling path exists.

Prepare the exact pinned HTML5 client before running Next.js:

```bash
python frontend/scripts/prepare-xpra-html5.py \
  --upstream external/xpra-html5 --output frontend/public/xpra-html5
```

The private backend relay and the Next.js server require the Xpra token and
public WebSocket URL described in
[`docs/ui/xpra-seamless-integration.md`](../docs/ui/xpra-seamless-integration.md).
Real sandboxed XBill/Dockview acceptance remains opt-in until qualified.

## UI-6 — efficient Xpra damage surfaces and per-window diagnostics

The Xpra HTML5 decoder still owns the native protocol, codecs and X11 input.
Potbot now copies only coalesced changed rectangles from each decoded *window*
canvas into Dockview. Scrolling, resizing, unknown packets and damage exceeding
70% of the window use a full-window baseline instead. Each frame includes the
Xpra window generation and a monotonic sequence, preventing late updates from
repainting a recycled X11 window identifier.

Minimizing an application pane suspends `ImageBitmap` copies without killing its
X process; reopening requests a complete frame before incremental updates.
Window input uses Xpra's native key/pointer/focus methods. Resize is debounced,
and wheel events use a non-passive listener to avoid scrolling the page.

Each pane displays an approximate rendered FPS, count of incremental patches and
cumulative decoded pixel-copy volume. An unsupported bitmap decoder reports an
error with a manual retry control; it does **not** spin indefinitely. These
measurements are not compressed stream bandwidth, measured GPU memory or
end-to-end remote interaction latency.

```bash
npm test
npm run benchmark:xpra
npm run typecheck
npm run build
```

The CPU-only synthetic damage benchmark shows theoretical pixel-copy savings,
not actual browser/GPU FPS. Live acceptance still needs the Xpra/XBill and
Playwright-equipped host described in the Xpra integration guide. This patch
also fixes two Xpra CI failures: a TypeScript control union and cleanup of
orphaned sockets after an owned Xpra process exits.

### UI-7 — Workspace Xpra application manager

Click **Manage Xpra application windows** in the utility rail or open
**Xpra applications** from the menu. Search by title or X window ID, filter
Visible / Minimized, focus an existing window or restore one that was hidden.
Each pane remains dockable and resizable; the **Minimize** action never kills
the application. If the Xpra session is disconnected, the panel offers a manual
**Reconnect Xpra** control after automatic retries are exhausted.

Window footers report approximate decoded-copy volume, observed frame rate,
P95 client-side frame gap, and input-to-next-frame **proxy** latency. Do not
interpret these as measured GPU FPS, bandwidth or verified input acknowledgements.

```bash
cd frontend
npm test
npm run benchmark:windows    # CPU only: 500 synthetic application windows
npm run benchmark:xpra       # CPU only: synthetic damage rectangles
npm run typecheck
npm run build
```

The opt-in Linux acceptance workflow adds a second application (`xterm`) next
to XBill, verifies both become separate Dockview windows, sends input to each,
then minimizes/restores xterm while its sandbox process continues to run.
This requires a provisioned Linux Xpra host and is not simulated as passing.

## UI-8 — Real workspace activity and model catalogue

The **Workspace activity** window and preferences pages now read actual
workspace-scoped Potbot API data through authenticated, server-side Next.js
routes. Neither a fake progress bar nor raw job inputs/provider metadata are
forwarded to the browser. A job status is not a download status:

- `GET /api/ui/activity?after=N` reads the workspace's `/v1/jobs` and
  `/v1/jobs/events`, returning up to 50 job summaries and 100 sequence-ordered
  event types; payloads, prompts, lease tokens and service keys are discarded.
- `GET /api/ui/models` reads `/v1/models` and projects only advisory model
  identities, roles and qualification/configuration flags. **This is not an
  installed-model inventory.**
- Activity has a dockable window, search, state filter, manual refresh and
  8-second live polling while the tab is visible. Panels remain workspace-scoped.
- The Downloads pane continues to identify transfer telemetry as unavailable:
  the backend has a CLI downloader with `DownloadProgress`, but no authorized
  transfer-status HTTP API. The Model Installations pane displays real catalogue
  entries, but never invents install completion or current disk consumption.
- Terminal and Browser panes can open Xpra application windows that the
  workspace has **already launched**; backend PTY/browser launch and session
  reattachment are separate pending capabilities.

This intentionally leaves the missing API work visible in the UI. Backend
contracts proposed in `docs/ui/frontend-backend-contract.md` identify the next
server-side milestone without conflating planned endpoints with implemented ones.

## UI-10: multiple workspace connections and saved conversations

The operator shell can now connect to up to 12 individually authenticated
Potbot workspace services using the server-only `POTBOT_UI_WORKSPACES_JSON`.
The selected workspace orb switches the backend credential and independent
PostgreSQL-backed chat history. The chat header offers a conversation picker,
New Conversation and explicit pending-turn reconciliation. Backend migration 27
must be applied before enabling these routes. This is **not** a way to access
another workspace using a bearer token from a different workspace.

See [the UI-10 implementation and security contract](../docs/ui/ui10-durable-workspace-chat.md)
for configuration, endpoint details, persistence/recovery limits and the next
backend milestones. Single-workspace environment variables remain supported.

### UI-11 — Qualified Xpra application sessions

The Xpra application manager now exposes an operator-pinned Launch menu and
running-process sessions. Bash/xterm and browser panels may start approved
recipes. Closing a Dockview window never terminates the application; an
explicit Stop action does. Application sessions are scoped to the workspace and
persist across Next.js reconnections while the Xpra operator remains running.
Launches stay **disabled** without a pinned rootfs, live Xpra socket, qualified
Linux Bubblewrap/AF_UNIX IPC and operator-private `--apps-policy`.
See `docs/ui/ui11-xpra-application-sessions.md`.

For multiple concurrent Xpra workspaces, set the server-only `POTBOT_UI_XPRA_BRIDGES_JSON`
array with separate scoped bridge tokens and endpoints. Legacy single-workspace
`POTBOT_UI_XPRA_BRIDGE_*` settings are still accepted.

## U04-A — Portable skill acquisition workshop

Open **Skill import and release workshop** from the workspace menu, or visit
`/skills?workspace=<authorized-workspace-id>`. The workshop accepts a ZIP of up
to 4 MiB or a public GitHub repository pinned to an exact 40-character commit
and package directory. It provides private source review, declared requirements
and unsupported-behavior findings, complete verified resource downloads, exact
installation plans, explicit activation, updates, retained release history,
rollback, disable and permanent revoke. Install, update and rollback leave the
skill disabled. Invalid approval requires a freshly reviewed update before
activation; revoke remains available to the owner.

Deploy the backend's skill acquisition migration and HTTP routes alongside this
frontend. Reads use the existing operator session and configured workspace
backend bearer. Every mutation also requires the **server-only**
`POTBOT_UI_SETUP_TOKEN` to match the backend's current setup capability. Keep it
out of `NEXT_PUBLIC_*` variables and browser configuration. The BFF selects an
allowlisted loopback backend, checks the exact origin, and forwards only fixed,
bounded skill routes. Imports and history are private to the importing
principal; shared readers see currently enabled, valid approved packages and
cannot change another owner's controls.

Activation makes approved instructions and resources available as **Context
data**. The workshop has no Run action, does not execute packaged scripts or
hooks, does not install dependencies, and makes no execution-readiness claim.
Executable tool maintenance remains in `/setup/tools`.

Before submitting, the browser retains the exact action body and idempotency
key in IndexedDB, scoped to the tab, workspace and principal. Uncertain results
offer an explicit **Retry exact request** action; reload never automatically
resubmits. A failed authorization check during recovery retains the original
receipt because an earlier attempt may have committed. If IndexedDB is
unavailable, full, or already holds another tab's unresolved request, recovery
falls back to memory and the UI warns that it is limited to the open tab. Keep
that tab open until the outcome is confirmed. A duplicated tab cannot overwrite
the other tab's durable pending receipt.

`npm test`, `npm run typecheck` and `npm run build` cover the frontend gates.
The skill contract test consumes a retained real HTTP/PostgreSQL lifecycle
fixture; set `POTBOT_SKILL_CONTRACT_OUTPUT` to the fresh integration-test export
to check the current backend wire responses directly.

## U04-X — Executable source acquisition and release

Open **Executable tool acquisition and upgrades** from the workspace menu, or
visit `/setup/tools?workspace=<authorized-workspace-id>`. The acquisition
workbench uses an immutable operator recipe and an exact Git commit. Its source
review shows the verified inventory, predecessor or verified absence, dependency
lock hashes, sandbox identity and exact build arguments. Separate approvals
authorize the sandbox build and promotion of the tested candidate. The same
durable operation then tracks host pin verification, installed postflight,
release, predecessor rollback or verified withdrawal of a first installation.
The **Existing tested approvals** tab retains the earlier S11 review surface.

Deploy the acquisition migration and HTTP API with this frontend. Publish a
recipe from the enrolled host and operate the acquisition worker using the
existing S10/S9/S11/S12 owners and their maintenance prerequisites. The browser
cannot supply build commands, host paths, repository overrides or executable
pins. Applying reviewed pins, restarting the runtime and reconciling interrupted
workspace leases remain independent host actions; their browser controls request
readback through the host worker. A built or promoted candidate stays held until
the release owner records passing installed postflight. Released tools continue
to require normal execution policy and task-specific eligibility checks.

Reads are private to the configured workspace bearer principal. Every mutation
also requires the server-only `POTBOT_UI_SETUP_TOKEN`, the authenticated operator
session and exact origin. The BFF forwards fixed routes, limits mutation JSON to
8 KiB and bounds public metadata to 2 MiB. Private claims, signatures, raw process
output and host storage locations do not reach the browser.

The browser retains the exact request and idempotency key in the same IndexedDB
recovery journal used by skills, under a separate tool/workspace/principal/tab
scope. Recovery requires an explicit **Retry exact request**; rejected recovery
authorization does not discard an earlier uncertain outcome. If durable storage
is unavailable or a copied tab already owns the slot, the page warns that only
the open tab retains the request. Keep that tab open until the receipt is
confirmed. Monitoring reads the selected operation every five seconds, pauses
in hidden tabs, and stops after 60 reads until explicitly resumed. Control
history is cursor-paged and keeps at most 200 loaded events in the page.

The frontend gates remain `npm test`, `npm run typecheck` and `npm run build`.
Set `POTBOT_TOOL_ACQUISITION_CONTRACT_OUTPUT` to the actual HTTP/PostgreSQL
pipeline export to check the current backend responses against
`tests/tool-acquisitions-contract.test.mjs`. Its sandbox process outcomes are
explicit integration fixtures; real sandbox qualification retains its existing
host acceptance gates.

## U04-S — Task-aware typed and hybrid discovery

Open **Task-aware capability shortlist** from the workspace menu, or visit
`/discover/tasks?workspace=WORKSPACE_UUID`. Optional `episode=TASK_UUID`,
`state=REPOSITORY_STATE_UUID` and `search=SEARCH_UUID` selectors identify exact
existing records. Loading or refreshing the page performs only retained reads.

Enter an existing running task and repository state, then **Read current task
context**. The workbench displays the exact task contract, repository state,
semantic release, task capabilities, catalogue/type/policy/consumer hashes,
source counts and registered embedding profiles. Creating a search requires the
reviewed context digest; source or task drift requires another context review.

Lexical and typed matching is the default. Optional constraints cover capability
classes, registered Syn/Synny input/output types, platform, permitted effects and
canonical scopes, local evidence and matching task success evidence. Unspecified
permissions differ from explicitly empty allowances. Constraints narrow the
existing task contract. Supplied inputs must fit accepted inputs; produced
outputs must fit required outputs. Unknown required evidence cannot prove a fit.

**Review exact search request** displays the complete bounded request before
confirmation. Lexical requests retain a completed shortlist. Hybrid requests
retain a pending command for the configured native operator to refine explicitly
with `potbot task-discovery … refine`. The browser never performs inference,
model download, provider configuration, index publication or tool execution.
A registered embedding profile is not proof of locally available model files.
The native owner retains privacy, budget, runtime and accounting checks. An empty
hard-eligible pool can complete without inference and has no semantic evidence.

The shortlist displays hard constraints before scores, current owner reasons,
installation observations, task capability requirements, exact qualification
suite/report evidence, feature contributions and explanations. Excluded
candidate identities retain their hard-filter reasons. Cost, latency and general
quality remain unknown. A semantic score is a retrieval contribution; every
candidate has `executable:false`. **Inspect current owner detail** lazily reads
only the matching existing owner and renders provider content as plain text/JSON.
An owner or task conflict requires refreshing the search before another decision.

**Review selection** retains an exact handoff after current identities are
rechecked. Selection grants no execution permission. Fixed scoped destinations
select the actual operation in `/tools?workspace=…&operation=SHA256`, registry
candidate in `/discover?workspace=…&candidate=UUID`, or MCP binding in
`/discover/mcp?workspace=…&binding=UUID`. Matching qualification suites link to
`/discover/mcp/qualification`. Historical evidence remains inspectable; a stale
selection does not offer an active handoff. Existing acquisition, controlled
checks, native qualification and execution owners remain authoritative.

The fixed `/api/ui/task-discovery` BFF exposes context, bounded search history,
exact searches/results/selections and progressive candidate reads, plus only
create, cancel and select commands. It validates UUID/hash identities, duplicate
JSON keys, nested DTO shapes, array/number bounds and exact receipt joins. Requests
are capped at 8 KiB, public responses at 1.5 MiB and owner detail at 128 KiB.
Mutations require the operator session, exact Origin, workspace bearer, the
server-only setup capability, explicit confirmation and a stable idempotency key.
Responses are private/no-store, and unsupported native/score-upload routes fail
before contacting the backend.

Commands are journaled before dispatch in the existing per-tab, workspace and
principal recovery store. An uncertain command blocks replacement. **Retry exact
command** recovers its original receipt with the same request bytes and key;
opening the page never retries a command automatically. Verified receipts are
followed by separate current reads. In-flight reads are aborted on task,
selection or scope changes. Optional progress monitoring pauses in hidden tabs,
after 30 reads or when reviewing a command; it only reads retained progress.

Run `npm test`, `npm run typecheck` and `npm run build`. Task discovery tests cover
strict DTOs, normalized constraints, fixed routes, authority/identity mismatches,
recovery, bounded streams and exact destination links. The actual HTTP/PostgreSQL
lifecycle export is consumed through `POTBOT_TASK_DISCOVERY_CONTRACT_OUTPUT`, with
`tests/fixtures/task-discovery-http.json` retained for offline regression. Its
synthetic embedding adapter tests lifecycle and accounting; it does not qualify
a real embedding model or external provider.

## U04-Q — Installed MCP task qualification

Open **MCP task qualification** from the workspace menu or the admitted MCP
catalogue, or visit `/discover/mcp/qualification?workspace=WORKSPACE_UUID`.
An optional `suite=SUITE_UUID` opens one retained suite, and
`binding=BINDING_UUID` filters suites to an existing discovery binding. Deep
links require one explicit, matching authorized workspace. The page also opens
an exact suite UUID and pages retained suites and attempts, keeping at most
200 records in each list.

The configured host admits one private, bounded task suite for an existing
installed MCP tool. The browser displays its name, ordered safe case summaries,
assertion counts, argument and oracle digests, exact task contract, repository
state, semantic release, installation and configuration identities. Arguments,
expected values, native paths, provider credentials and process commands remain
private. Provider-authored descriptions are rendered as text.

**Request one qualification attempt** records one signed attempt for that exact suite
revision and digest. The configured host explicitly executes its retained ID
through the existing budgeted gateway. Page loads and monitoring issue GET
requests only. Automatic status reads pause in hidden tabs and after thirty
checks; the operator can pause or resume them. The UI shows prepared cases,
verified stopped results, per-assertion booleans and safe failure reason codes,
with their gateway run, artifact and receipt identities. It does not show raw
actual or expected values. Cancellation fences future work without claiming
that an already running native process has stopped.

Reports distinguish passed, failed and incomplete cases, retained signature
validity and current stored authority. A historical report remains inspectable
after a newer attempt, task drift, disabled suite or invalidated source.
Currentness requires an exact match to the suite's latest attempt and report,
ordered case identities and task/source bindings. Native runtime and profile
readback remain mandatory at execution time. A passing report is scoped task
evidence; it supplies no installation or execution permission. Native receipt
recovery is shown as an informational host action with no browser dispatch.

The fixed `/api/ui/mcp-qualification` BFF exposes retained suite, attempt and
report reads, plus reviewed qualify, enable, disable, revoke and cancel
decisions. It forwards no admission, native execution, recovery or report upload
route. Mutations require the authenticated operator, exact allowed origin,
scoped backend bearer and independent server-only `POTBOT_UI_SETUP_TOKEN`.
Requests are bounded to 8 KiB and public responses to 1.5 MiB. Strict projections
validate case evidence, scope, pagination, original receipts and exact native
report identities before anything reaches the page.

Each decision is stored before dispatch in a qualification-specific per-tab,
workspace-and-principal journal. Reloading recovers the original path, JSON
bytes and key without submitting it. **Retry the identical request** explicitly
recovers its original receipt, then reads current state. An uncertain request
locks subsequent writes, and a rejected recovery does not erase its earlier
uncertain outcome. Scope changes hide private records, and request generations
abort late responses after selection or filter changes. If durable storage is
unavailable, preserve the displayed request while the tab retains its temporary
copy.

Run `npm test`, `npm run typecheck` and `npm run build`. The qualification DTO,
route and recovery suites cover fixed routes, authority, evidence coherence,
native-field rejection and exact request recovery. The contract test consumes
real HTTP/PostgreSQL lifecycle output through
`POTBOT_MCP_QUALIFICATION_CONTRACT_OUTPUT`, with a checked-in export for offline
checks. Its native process and provider outcomes come from the integration
harness; it does not qualify an external vendor or a rendered browser session.

## U04-M — Admitted MCP catalogues and reconciliation

Open **Admitted MCP catalogues and reconciliation** from the workspace menu, or
visit `/discover/mcp?workspace=WORKSPACE_UUID`. An optional `binding=BINDING_UUID`
opens one retained binding only when the URL contains one explicit, authorized
workspace selection. The capability discovery and operation workbench pages also
link to this view.

The configured host admits a binding to an existing installed MCP descriptor,
configuration and native owner. The browser lists those admitted bindings and
their retained tools, resources, resource templates and prompts. It shows exact
installation, configuration, tool, definition and snapshot digests alongside
protocol version, server claims, collection support, freshness, attempt history
and added/removed/changed/unchanged counts. Peer definitions, URLs, URI templates,
icons and schemas remain text data. Inspecting a resource does not read it;
inspecting a prompt does not expand it; discovering a tool does not grant
permission to invoke it.

**Request exact reconciliation** records one durable attempt. The configured host
must explicitly run its retained attempt ID; page loads, GET requests and status
polling never dispatch host work. Automatic status reads are bounded to thirty
checks, pause when the tab is hidden, and can be paused or resumed explicitly.
Enable, disable, terminal revoke and attempt cancellation all use the reviewed
revision and digest. Cancellation fences publication without claiming that the
native process has stopped. A replacement source or installation requires a new
host admission; this page has no endpoint, command or installation editor.

The **Inspect exact reference** action reads a scope-bound reference to one exact
retained definition. Copy stays available only while the enabled signed binding,
installation, revision, snapshot, entry and expiry remain current. The client
rechecks expiry immediately before copying. References grant no execution,
resource-read, prompt-expansion or subscription authority. Disabling, revoking,
schema drift, a newer snapshot, stale evidence or signature invalidation prevents
use of the previous reference.

The fixed `/api/ui/mcp-discovery` BFF forwards only the retained read routes and
reviewed decision routes under `/v1/ui/mcp-discovery`. Deploy the matching API and
migration with this frontend. Reads use ordinary authenticated workspace scope;
mutations also require same-origin checks and the server-only
`POTBOT_UI_SETUP_TOKEN`. Responses are bounded and projected through strict DTOs;
native endpoints, commands, paths, tokens and signatures are never forwarded as
binding fields. Embedded peer metadata is displayed as untrusted plain JSON.

Each mutation is retained before dispatch in a separate per-tab,
workspace-and-principal recovery journal. Reloading recovers the original path,
JSON bytes and idempotency key without submitting it. An explicit identical retry
recovers the original receipt, after which the UI rereads current state. An
uncertain request cannot be replaced by a new decision. Scope denial or a changed
workspace/principal removes the mounted private views; request generations fence
late responses, filter changes and snapshot pagination.

`mcp-discovery.test.mjs`, `mcp-discovery-routes.test.mjs` and
`mcp-discovery-recovery.test.mjs` cover strict projections, fixed BFF routing,
scoping, mutations, bounded metadata and exact recovery/currentness behavior.
`mcp-discovery-contract.test.mjs` consumes real HTTP/PostgreSQL lifecycle output
through `POTBOT_MCP_DISCOVERY_CONTRACT_OUTPUT`, with a checked-in wire fixture for
offline frontend checks. These checks do not qualify an external MCP provider or
a rendered browser session.

## U04-D — Capability discovery and exact acquisition review

Open **Capability discovery and source review** from the workspace menu, or
visit `/discover?workspace=<authorized-workspace-id>`. Search retained registry
observations by task, package kind, source and platform, with explicit local,
effect, scope, capability-class and canonical Type constraints. Empty permitted
effect or scope lists permit none; omitted constraints leave that dimension
unconstrained. Ranked pagination preserves the complete query and its opaque
source-bound cursor. Changes to source heads require restarting that search.

The **Sources and publishers** tab shows exact admission bindings, snapshot
freshness and namespace claims. A source or publisher decision applies to future
discovery reviews and handoffs. Source creation and bounded remote refresh remain
operator CLI actions; registry claims do not establish publisher signatures,
package safety or executable readiness. Source or publisher revocation does not
silently alter packages already controlled by the existing acquisition owners.

Select a candidate, review its provenance and declarations, and supply an exact
enrolled agent UUID. **Create exact review** resolves the retained selection and
inspects the private portable package, where applicable. A configured host then
runs `potbot discovery measure CHECK_UUID` with its normal operator/native
configuration. This explicitly selected measurement uses the existing supervisor
and workspace lease. It does not start a second worker queue, execute the package
or invoke its dependency programs. Reports bind the selected agent, runtime,
source and actual package requirements; they expire within 120 seconds. Missing
and unsupported prerequisites stay visible and may continue into explicit
acquisition review when the server offers that action.

**My exact reviews** reopens retained checks by list or UUID. Monitoring reads a
selected pending review every five seconds, pauses in hidden tabs and stops after
60 reads until resumed. Expired or invalidated evidence requires a fresh review
and measurement. Explicit handoff selects the same private package at
`/skills?workspace=UUID&package=UUID`, or the existing executable source request
at `/setup/tools?workspace=UUID&operation=UUID`. Links only select records under a
matching explicit workspace. Discovery never imports on page load, installs,
builds, activates or runs a package. Registry skill source bindings remain visible
through the existing skill package, plan, release and history projections.

Deploy the discovery migration/API with this frontend. Reads use the authenticated
operator session and allowlisted workspace backend bearer. All mutations require
the server-only `POTBOT_UI_SETUP_TOKEN` and exact origin. The BFF forwards fixed
routes, limits mutation JSON to 8 KiB and public responses to 1.5 MiB, and strips
private signatures, host locations and credentials. Exact request bodies and keys
use a separate discovery/tab/workspace/principal IndexedDB journal. Recovery never
automatically replays; an authorization rejection on retry retains the earlier
uncertain receipt. If durable storage is unavailable or a duplicated tab owns the
slot, recovery is limited to the open tab. Keep that tab open until confirmed.

Run `npm test`, `npm run typecheck` and `npm run build`. Set
`POTBOT_DISCOVERY_CONTRACT_OUTPUT` to the actual HTTP/PostgreSQL export to verify
the current backend responses with `tests/discovery-contract.test.mjs`. The saved
fixture includes explicit runtime/provider test outcomes; live sandbox and remote
provider qualification retain their separate host gates.

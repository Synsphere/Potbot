/** Public executable acquisition review data. Runtime owners retain all effects. */
import { UUID, SHA256 } from "./operation-workbench.ts";
export { UUID, SHA256 };
export const TOOL_SCHEMA = "potbot-tool-acquisitions/v1" as const;
export const COMMIT = /^[a-f0-9]{40}$/;
export const TOOL_ACTIONS = ["approve-build", "approve", "reject", "rollback", "reconcile", "repin", "postflight", "cancel"] as const;
export const TOOL_PHASES = ["source_queued", "sourcing", "review", "build_queued", "building", "failed", "indeterminate", "cancelled", "awaiting_approval", "approved", "executing", "awaiting_repin", "repinned", "testing", "active", "rolled_back", "withdrawn", "rejected"] as const;
export const RELEASE_STATES = ["none", "repinned", "testing", "failed", "indeterminate", "released", "withdrawn"] as const;
export type ToolAction = typeof TOOL_ACTIONS[number];
export type ToolPhase = typeof TOOL_PHASES[number];
export type ToolScope = { schema: typeof TOOL_SCHEMA; workspaceId: string; principalId: string };
export type ToolRecipe = { id: string; name: string; description: string; slug: string; repository: string; recipeSha256: string; agentId: string; ownerPrincipalId: string; createdAt: string;
  build: { commandSha256: string; timeoutMs: number; network: "denied" }; tests: { name: string; kind: string; timeoutMs: number }[];
  cleanupPaths: string[]; lockfilePaths: string[]; artifactPaths: string[]; approvalValid: boolean };
export type ToolSource = { schema: "potbot-tool-source/v1"; operation_id: string; repository: string; commit: string; git_tree: string; tree_sha256: string;
  files: number; bytes: number; source_sha256: string; storage_sha256: string };
export type ToolReview = { sha256: string; kind: "install" | "upgrade"; oldCommit: string | null; newCommit: string; sourceSha256: string; recipeSha256: string;
  commandSha256: string; imageDigest: string; qualificationSha256: string; buildRoot: string; buildArgv: string[]; lockfiles: [string, string][];
  network: "denied"; automaticActivation: false; requiresPostflight: true };
export type ToolOperation = { id: string; ownerPrincipalId: string; owned: true; agentId: string; slug: string; kind: "install" | "upgrade" | null;
  repository: string; oldCommit: string | null; newCommit: string; revision: number; phase: ToolPhase; recipe: ToolRecipe; source: ToolSource | null; review: ToolReview | null;
  build: { commandSha256: string; isolationIdentity: string; treeSha256: string; files: number; bytes: number } | null;
  tests: { name: string; kind: string; elapsedMs: number; passed: boolean }[]; evidenceSha256: string | null; suiteSha256: string;
  releaseState: typeof RELEASE_STATES[number]; releaseGeneration: number; releaseBindingSha256: string | null; releaseEvidenceSha256: string | null;
  installedState: "unknown" | "absent" | "held" | "released"; hostAction: { action: "rollback" | "reconcile" | "repin" | "postflight"; status: "queued" | "running" | "failed" | "indeterminate" } | null;
  allowedActions: ToolAction[]; bindingSha256: string; errorCode: string | null; createdAt: string; updatedAt: string };
export type ToolEvent = { revision: number; action: string; priorState: string; nextState: string; evidenceSha256: string; recordedAt: string };
export type RecipeList = ToolScope & { items: ToolRecipe[]; nextCursor: string | null };
export type AcquisitionList = ToolScope & { items: ToolOperation[]; nextCursor: string | null };
export type AcquisitionEnvelope = ToolScope & { operation: ToolOperation };
export type AcquisitionEvents = ToolScope & { operationId: string; items: ToolEvent[]; nextCursor: number; hasMore: boolean };
const CODE = /^[a-z][a-z0-9_]{0,63}$/, SLUG = /^[a-z][a-z0-9-]{1,62}$/, TEST = /^[a-z][a-z0-9_.-]{1,63}$/;
const TEST_KINDS = ["smoke", "unit", "integration", "regression"];
const HOST_ACTIONS = ["rollback", "reconcile", "repin", "postflight"];
export function toolObject(value: unknown): Record<string, unknown> | null { return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null; }
function count(value: unknown, max = Number.MAX_SAFE_INTEGER, min = 0): value is number { return Number.isSafeInteger(value) && Number(value) >= min && Number(value) <= max; }
function text(value: unknown, max: number, min = 1): value is string { return typeof value === "string" && Array.from(value).length >= min && Array.from(value).length <= max && !value.includes("\u0000") && new TextDecoder().decode(new TextEncoder().encode(value)) === value; }
function id(value: unknown, pattern = UUID): value is string { return typeof value === "string" && pattern.test(value); }
function digest(value: unknown): value is string { return id(value, SHA256); }
function nullableDigest(value: unknown): value is string | null { return value === null || digest(value); }
function stamp(value: unknown): value is string { return text(value, 64) && /^\d{4}-\d\d-\d\dT/.test(value) && /(?:Z|\+00:00)$/.test(value) && Number.isFinite(Date.parse(value)); }
function relative(value: unknown): value is string { return text(value, 180) && !value.startsWith("/") && !value.includes("\\") && value.split("/").every(part => part !== "" && part !== "." && part !== ".." && !part.startsWith(".git")); }
export function toolRepository(value: unknown): value is string { return text(value, 512) && /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\.git$/.test(value); }
function paths(value: unknown, max: number, min = 0): string[] | null { return Array.isArray(value) && value.length >= min && value.length <= max && value.every(relative) && new Set(value).size === value.length ? value as string[] : null; }
function scope(value: unknown, workspace: string, principal?: string): ToolScope | null {
  const row = toolObject(value); return row?.schema === TOOL_SCHEMA && row.workspaceId === workspace && id(workspace) && id(row.principalId) && (!principal || row.principalId === principal) ?
    { schema: TOOL_SCHEMA, workspaceId: workspace, principalId: row.principalId } : null;
}
export function projectToolRecipe(value: unknown, principal: string): ToolRecipe | null {
  const row = toolObject(value), build = row && toolObject(row.build), cleanup = row && paths(row.cleanupPaths, 64, 1), locks = row && paths(row.lockfilePaths, 24), artifacts = row && paths(row.artifactPaths, 48, 1);
  if (!row || !build || !cleanup || !locks || !artifacts || !id(row.id) || row.ownerPrincipalId !== principal || !id(principal) || !id(row.agentId) ||
      !text(row.name, 120) || !text(row.description, 4096) || !id(row.slug, SLUG) || !toolRepository(row.repository) || !digest(row.recipeSha256) || !stamp(row.createdAt) || typeof row.approvalValid !== "boolean" ||
      !digest(build.commandSha256) || !count(build.timeoutMs, 900_000, 100) || build.network !== "denied" ||
      !Array.isArray(row.tests) || row.tests.length < 2 || row.tests.length > 16 || !artifacts.every(path => /^(?:static|environment|downloads)\//.test(path))) return null;
  const tests: ToolRecipe["tests"] = [];
  for (const raw of row.tests) {
    const test = toolObject(raw); if (!test || !id(test.name, TEST) || !TEST_KINDS.includes(String(test.kind)) || !count(test.timeoutMs, 180_000, 100) || tests.some(item => item.name === test.name)) return null;
    tests.push({ name: test.name, kind: test.kind as string, timeoutMs: test.timeoutMs });
  }
  if (!tests.some(item => item.kind === "smoke") || !tests.some(item => item.kind === "unit") || tests.reduce((sum, item) => sum + item.timeoutMs, 0) > 1_200_000) return null;
  return { id: row.id, name: row.name, description: row.description, slug: row.slug, repository: row.repository, recipeSha256: row.recipeSha256, agentId: row.agentId,
    ownerPrincipalId: principal, createdAt: row.createdAt, build: { commandSha256: build.commandSha256, timeoutMs: build.timeoutMs, network: "denied" }, tests,
    cleanupPaths: cleanup, lockfilePaths: locks, artifactPaths: artifacts, approvalValid: row.approvalValid };
}
function sourceView(value: unknown, operation: string, repository: string, commit: string): ToolSource | null {
  const row = toolObject(value);
  if (!row || row.schema !== "potbot-tool-source/v1" || row.operation_id !== operation || row.repository !== repository || row.commit !== commit || !id(row.git_tree, COMMIT) ||
      !digest(row.tree_sha256) || !digest(row.source_sha256) || !digest(row.storage_sha256) || !count(row.files, 8192) || !count(row.bytes, 128 * 1024 ** 2)) return null;
  return { schema: "potbot-tool-source/v1", operation_id: operation, repository, commit, git_tree: row.git_tree, tree_sha256: row.tree_sha256,
    files: row.files, bytes: row.bytes, source_sha256: row.source_sha256, storage_sha256: row.storage_sha256 };
}
function reviewView(value: unknown, operation: string, recipe: ToolRecipe, source: ToolSource, oldCommit: string | null): ToolReview | null {
  const row = toolObject(value);
  if (!row || !digest(row.sha256) || row.kind !== (oldCommit === null ? "install" : "upgrade") || row.oldCommit !== oldCommit || row.newCommit !== source.commit ||
      row.sourceSha256 !== source.source_sha256 || row.recipeSha256 !== recipe.recipeSha256 || !digest(row.commandSha256) || !digest(row.imageDigest) || !digest(row.qualificationSha256) ||
      !text(row.buildRoot, 512) || !new RegExp("^/home/[A-Za-z0-9_.-]+/builds/" + operation.replaceAll("-", "") + "$").test(row.buildRoot) ||
      !Array.isArray(row.buildArgv) || row.buildArgv.length < 1 || row.buildArgv.length > 64 ||
      !row.buildArgv.every(arg => text(arg, 4096) && new TextEncoder().encode(arg).length <= 4096 && !/[\r\n]/.test(arg)) ||
      !row.buildArgv[0].startsWith("/") || /^\/(?:home|usr\/local\/tools|var\/ai)\//.test(row.buildArgv[0]) ||
      !Array.isArray(row.lockfiles) || row.lockfiles.length !== recipe.lockfilePaths.length || row.network !== "denied" || row.automaticActivation !== false || row.requiresPostflight !== true) return null;
  const lockfiles: [string, string][] = [];
  for (const pair of row.lockfiles) {
    if (!Array.isArray(pair) || pair.length !== 2 || !relative(pair[0]) || !recipe.lockfilePaths.includes(pair[0]) || !digest(pair[1]) || lockfiles.some(prior => prior[0] === pair[0])) return null;
    lockfiles.push([pair[0], pair[1]]);
  }
  return { sha256: row.sha256, kind: row.kind as "install" | "upgrade", oldCommit, newCommit: source.commit, sourceSha256: source.source_sha256, recipeSha256: recipe.recipeSha256,
    commandSha256: row.commandSha256, imageDigest: row.imageDigest, qualificationSha256: row.qualificationSha256, buildRoot: row.buildRoot, buildArgv: row.buildArgv as string[], lockfiles,
    network: "denied", automaticActivation: false, requiresPostflight: true };
}
export function projectToolOperation(value: unknown, principal: string): ToolOperation | null {
  const row = toolObject(value), recipe = row && projectToolRecipe(row.recipe, principal);
  if (!row || !recipe || !id(row.id) || row.ownerPrincipalId !== principal || row.owned !== true || row.agentId !== recipe.agentId || row.slug !== recipe.slug || row.repository !== recipe.repository ||
      ![null, "install", "upgrade"].includes(row.kind as string | null) || !(row.oldCommit === null || id(row.oldCommit, COMMIT)) || !id(row.newCommit, COMMIT) || row.oldCommit === row.newCommit ||
      (row.kind === "install" && row.oldCommit !== null) || (row.kind === "upgrade" && row.oldCommit === null) || (row.kind === null && row.oldCommit !== null) ||
      !count(row.revision) || !TOOL_PHASES.includes(row.phase as ToolPhase) || !nullableDigest(row.evidenceSha256) || !digest(row.suiteSha256) ||
      !RELEASE_STATES.includes(row.releaseState as typeof RELEASE_STATES[number]) || !count(row.releaseGeneration) || !nullableDigest(row.releaseBindingSha256) || !nullableDigest(row.releaseEvidenceSha256) ||
      !["unknown", "absent", "held", "released"].includes(String(row.installedState)) || !Array.isArray(row.allowedActions) || row.allowedActions.length > TOOL_ACTIONS.length ||
      !row.allowedActions.every(item => TOOL_ACTIONS.includes(item)) || new Set(row.allowedActions).size !== row.allowedActions.length ||
      !digest(row.bindingSha256) || !(row.errorCode === null || id(row.errorCode, CODE)) || !stamp(row.createdAt) || !stamp(row.updatedAt)) return null;
  const source = row.source === null ? null : sourceView(row.source, row.id, recipe.repository, row.newCommit);
  if (row.source !== null && !source) return null;
  const review = row.review === null ? null : source && reviewView(row.review, row.id, recipe, source, row.oldCommit);
  if ((row.review !== null && !review) || (review && review.kind !== row.kind)) return null;
  let build: ToolOperation["build"] = null;
  if (row.build !== null) {
    const raw = toolObject(row.build);
    if (!raw || !review || raw.commandSha256 !== review.commandSha256 || !digest(raw.isolationIdentity) || !digest(raw.treeSha256) || !count(raw.files, 50_000) || !count(raw.bytes, 16 * 1024 ** 3)) return null;
    build = { commandSha256: review.commandSha256, isolationIdentity: raw.isolationIdentity, treeSha256: raw.treeSha256, files: raw.files, bytes: raw.bytes };
  }
  if (!Array.isArray(row.tests) || row.tests.length > 16) return null;
  const tests: ToolOperation["tests"] = [];
  for (const raw of row.tests) {
    const test = toolObject(raw);
    if (!test || !id(test.name, TEST) || !TEST_KINDS.includes(String(test.kind)) || !count(test.elapsedMs, 1_800_000) || typeof test.passed !== "boolean" ||
        !recipe.tests.some(item => item.name === test.name && item.kind === test.kind) || tests.some(item => item.name === test.name)) return null;
    tests.push({ name: test.name, kind: test.kind as string, elapsedMs: test.elapsedMs, passed: test.passed });
  }
  let hostAction: ToolOperation["hostAction"] = null;
  if (row.hostAction !== null) {
    const raw = toolObject(row.hostAction);
    if (!raw || !HOST_ACTIONS.includes(String(raw.action)) || !["queued", "running", "failed", "indeterminate"].includes(String(raw.status))) return null;
    hostAction = { action: raw.action as NonNullable<ToolOperation["hostAction"]>["action"], status: raw.status as NonNullable<ToolOperation["hostAction"]>["status"] };
  }
  if ((row.releaseState === "released" || row.releaseState === "withdrawn") && (row.releaseGeneration < 1 || !row.releaseBindingSha256 || !row.releaseEvidenceSha256)) return null;
  if (row.installedState === "released" && row.releaseState !== "released") return null;
  if (row.releaseState === "withdrawn" && (row.kind !== "install" || row.oldCommit !== null || row.phase !== "withdrawn" || row.installedState !== "absent")) return null;
  return { id: row.id, ownerPrincipalId: principal, owned: true, agentId: recipe.agentId, slug: recipe.slug, kind: row.kind as ToolOperation["kind"], repository: recipe.repository,
    oldCommit: row.oldCommit, newCommit: row.newCommit, revision: row.revision, phase: row.phase as ToolPhase, recipe, source, review, build, tests, evidenceSha256: row.evidenceSha256, suiteSha256: row.suiteSha256,
    releaseState: row.releaseState as ToolOperation["releaseState"], releaseGeneration: row.releaseGeneration, releaseBindingSha256: row.releaseBindingSha256, releaseEvidenceSha256: row.releaseEvidenceSha256,
    installedState: row.installedState as ToolOperation["installedState"], hostAction, allowedActions: row.allowedActions as ToolAction[], bindingSha256: row.bindingSha256, errorCode: row.errorCode,
    createdAt: row.createdAt, updatedAt: row.updatedAt };
}
export function projectAcquisition(value: unknown, workspace: string, operation?: string, principal?: string): AcquisitionEnvelope | null {
  const row = toolObject(value), identity = scope(value, workspace, principal), result = row && identity && projectToolOperation(row.operation, identity.principalId);
  return identity && result && (!operation || result.id === operation) ? { ...identity, operation: result } : null;
}
export function projectAcquisitionList(value: unknown, workspace: string, principal?: string): AcquisitionList | null {
  const row = toolObject(value), identity = scope(value, workspace, principal);
  if (!row || !identity || !Array.isArray(row.items) || row.items.length > 50 || !(row.nextCursor === null || id(row.nextCursor))) return null;
  const items: ToolOperation[] = [];
  for (const raw of row.items) { const item = projectToolOperation(raw, identity.principalId); if (!item || items.some(prior => prior.id === item.id)) return null; items.push(item); }
  return { ...identity, items, nextCursor: row.nextCursor };
}
export function projectRecipeList(value: unknown, workspace: string, principal?: string): RecipeList | null {
  const row = toolObject(value), identity = scope(value, workspace, principal);
  if (!row || !identity || !Array.isArray(row.items) || row.items.length > 50 || !(row.nextCursor === null || id(row.nextCursor))) return null;
  const items: ToolRecipe[] = [];
  for (const raw of row.items) { const item = projectToolRecipe(raw, identity.principalId); if (!item || items.some(prior => prior.id === item.id)) return null; items.push(item); }
  return { ...identity, items, nextCursor: row.nextCursor };
}
export function projectAcquisitionEvents(value: unknown, workspace: string, after = 0, principal?: string, operation?: string): AcquisitionEvents | null {
  const row = toolObject(value), identity = scope(value, workspace, principal);
  if (!row || !identity || !id(row.operationId) || (operation && row.operationId !== operation) || !count(after) || !Array.isArray(row.items) || row.items.length > 50 || !count(row.nextCursor) || typeof row.hasMore !== "boolean") return null;
  const items: ToolEvent[] = []; let revision = after;
  for (const raw of row.items) {
    const item = toolObject(raw);
    if (!item || !count(item.revision) || item.revision <= revision || !id(item.action, CODE) || !id(item.priorState, CODE) || !id(item.nextState, CODE) || !digest(item.evidenceSha256) || !stamp(item.recordedAt)) return null;
    revision = item.revision; items.push({ revision, action: item.action, priorState: item.priorState, nextState: item.nextState, evidenceSha256: item.evidenceSha256, recordedAt: item.recordedAt });
  }
  return row.nextCursor === revision && (!row.hasMore || items.length > 0) ? { ...identity, operationId: row.operationId, items, nextCursor: revision, hasMore: row.hasMore } : null;
}
export function validateToolMutation(route: string[], value: unknown): Record<string, unknown> | null {
  const row = toolObject(value); if (!row || row.confirm !== true) return null;
  const keys = Object.keys(row).sort().join(",");
  if (route.length === 0) return keys === "commit,confirm,recipeId" && id(row.recipeId) && id(row.commit, COMMIT) ? { recipeId: row.recipeId, commit: row.commit, confirm: true } : null;
  if (route.length !== 2 || !id(route[0]) || !TOOL_ACTIONS.includes(route[1] as ToolAction) || !count(row.revision)) return null;
  const binding = route[1] === "approve-build" ? "reviewSha256" : route[1] === "approve" || route[1] === "reject" ? "evidenceSha256" : "bindingSha256";
  return keys === ["confirm", "revision", binding].sort().join(",") && digest(row[binding]) ? { revision: row.revision, [binding]: row[binding], confirm: true } : null;
}
export function toolActionBody(operation: ToolOperation, action: ToolAction): Record<string, unknown> | null {
  if (!operation.allowedActions.includes(action)) return null;
  const binding = action === "approve-build" ? "reviewSha256" : action === "approve" || action === "reject" ? "evidenceSha256" : "bindingSha256";
  const digest = binding === "reviewSha256" ? operation.review?.sha256 : binding === "evidenceSha256" ? operation.evidenceSha256 : operation.bindingSha256;
  return validateToolMutation([operation.id, action], { revision: operation.revision, [binding]: digest, confirm: true });
}
export function projectToolMutation(value: unknown, workspace: string, route: string[], body: unknown, principal?: string): AcquisitionEnvelope | null {
  const request = validateToolMutation(route, body), result = request && projectAcquisition(value, workspace, route[0], principal); if (!request || !result) return null;
  if (!route.length) return result.operation.recipe.id === request.recipeId && result.operation.newCommit === request.commit && result.operation.revision === 1 && result.operation.phase === "source_queued" ? result : null;
  if (result.operation.revision !== Number(request.revision) + 1) return null;
  if (route[1] === "approve-build" && result.operation.review?.sha256 !== request.reviewSha256) return null;
  if (["approve", "reject"].includes(route[1]) && result.operation.evidenceSha256 !== request.evidenceSha256) return null;
  const next = { "approve-build": "build_queued", approve: "approved", reject: "rejected" } as const;
  if (route[1] in next && result.operation.phase !== next[route[1] as keyof typeof next]) return null;
  // S11 retains a tested cancellation as rejection; pretest work is cancelled.
  if (route[1] === "cancel" && !["cancelled", "rejected"].includes(result.operation.phase)) return null;
  if (HOST_ACTIONS.includes(route[1]) && (result.operation.hostAction?.action !== route[1] || result.operation.hostAction.status !== "queued")) return null;
  return result;
}

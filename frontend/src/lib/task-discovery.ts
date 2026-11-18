/** Task discovery retains ranking evidence, never execution grants. */
import { UUID, SHA256 } from "./operation-workbench.ts";
import { CAPABILITIES, EFFECTS, PLATFORMS } from "./discovery.ts";
import { parseMcpJSON } from "./mcp-discovery.ts";
import { projectQualificationTaskBinding, type QualificationTaskBinding } from "./mcp-qualification.ts";
export { UUID, SHA256, parseMcpJSON };
export const TASK_DISCOVERY_SCHEMA = "potbot-task-discovery/v1" as const;
export const MAX_TASK_DISCOVERY_REQUEST = 8192, MAX_TASK_DISCOVERY_RESPONSE = 1_572_864;
export const SEARCH_STATES = ["requested", "running", "completed", "selected", "failed", "cancelled", "indeterminate"] as const;
export type TaskDiscoveryScope = { schema: typeof TASK_DISCOVERY_SCHEMA; workspaceId: string; principalId: string };
export type TaskDiscoveryConstraints = { classes: string[]; inputTypes: string[]; outputTypes: string[]; platform: string | null; permittedEffects: string[] | null; permittedScopes: string[] | null; requireLocal: boolean; requireTaskEvidence: boolean };
export type TaskEmbeddingChoice = { spaceId: string; embeddingName: string; profileSha256: string; maxCandidates: number; runtimeBudgetMs: number };
export type TaskEmbeddingSpace = { spaceId: string; embeddingName: string; profileSha256: string; model: string; revision: string; dimensions: number; maxInputTokens: number; maxBatchSize: number };
export type TaskDiscoveryContext = { taskBinding: QualificationTaskBinding; taskStatus: "running"; taskCapabilities: string[]; catalogueSha256: string; typesSha256: string; policySha256: string; consumerSha256: string; contextSha256: string; counts: { registry: number; operations: number; mcp: number }; embeddingSpaces: TaskEmbeddingSpace[] };
export type TaskDiscoverySearch = { id: string; revision: number; bindingSha256: string; status: typeof SEARCH_STATES[number]; taskBinding: QualificationTaskBinding; contextSha256: string; catalogueSha256: string; typesSha256: string; policySha256: string; consumerSha256: string; query: string; constraints: TaskDiscoveryConstraints; mode: "lexical" | "hybrid"; limit: number; embedding: TaskEmbeddingChoice | null; candidateCount: number; resultId: string | null; resultSha256: string | null; selectedCandidateId: string | null; selectionId: string | null; reasonCode: string | null; approvalValid: boolean; authorityCurrent: boolean; reasonCodes: string[]; actions: ("cancel" | "select")[]; createdAt: string; updatedAt: string };
export type TaskDiscoveryEvidence = { status: "none" | "matching_pass" | "matching_failure" | "historical"; suiteId: string | null; reportId: string | null; reportSha256: string | null; caseCount: number; passedCases: number };
export type TaskDiscoveryCandidate = { id: string; kind: "registry" | "operation" | "mcp"; name: string; description: string; ownerId: string; bindingId: string | null; ownerSha256: string; capabilities: { classes: string[]; inputTypes: string[]; outputTypes: string[]; effects: string[]; scopes: string[]; platforms: string[] }; requiredTaskCapability: string | null; installed: boolean; authorityCurrent: boolean; reasonCodes: string[]; taskEvidence: TaskDiscoveryEvidence };
export type RankedTaskCandidate = TaskDiscoveryCandidate & { score: number; features: { classes: number; lexical: number; types: number; installed: number; evidence: number; semantic: number }; typeFit: "not_requested" | "exact" | "compatible" | "unknown" | "incompatible"; taskPermission: "permitted" | "denied" | "unknown"; reasons: string[]; executable: false };
export type TaskDiscoverySemantic = { spaceId: string; embeddingName: string; profileSha256: string; querySha256: string; documentsSha256: string; candidateCount: number; calls: number; inputTokens: number; elapsedMs: number; reservationIds: string[]; usageOrigin: "synthetic" | "measured" | "provider_reported" };
export type TaskDiscoveryResult = { id: string; searchId: string; sha256: string; approvalValid: boolean; authorityCurrent: boolean; reasonCodes: string[]; taskBinding: QualificationTaskBinding; catalogueSha256: string; typesSha256: string; policySha256: string; consumerSha256: string; query: string; constraints: TaskDiscoveryConstraints; mode: "lexical" | "hybrid"; items: RankedTaskCandidate[]; excluded: { id: string; reasonCodes: string[] }[]; terms: string[]; inferredClasses: string[]; noSuitableCandidate: boolean; semantic: TaskDiscoverySemantic | null; recordedAt: string };
export type TaskDiscoverySelection = { id: string; searchId: string; resultId: string; candidateId: string; sha256: string; approvalValid: boolean; authorityCurrent: boolean; reasonCodes: string[]; taskBinding: QualificationTaskBinding; owner: { kind: TaskDiscoveryCandidate["kind"]; ownerId: string; bindingId: string | null; ownerSha256: string }; handoff: { kind: TaskDiscoveryCandidate["kind"]; identity: string; bindingId: string | null }; recordedAt: string };
export type TaskDiscoveryPage = TaskDiscoveryScope & { items: TaskDiscoverySearch[]; nextCursor: string | null };
export type TaskDiscoveryCreate = { episodeId: string; stateId: string; contextSha256: string; query: string; constraints: TaskDiscoveryConstraints; mode: "lexical" | "hybrid"; limit: number; embedding: TaskEmbeddingChoice | null; confirm: true };
export type TaskDiscoveryEnvelope = TaskDiscoveryScope & ({ context: TaskDiscoveryContext } | { search: TaskDiscoverySearch } | { result: TaskDiscoveryResult } | { selection: TaskDiscoverySelection } | { candidate: RankedTaskCandidate; detail: Record<string, unknown> });
export const EMPTY_TASK_CONSTRAINTS: TaskDiscoveryConstraints = { classes: [], inputTypes: [], outputTypes: [], platform: null, permittedEffects: null, permittedScopes: null, requireLocal: false, requireTaskEvidence: false };
export function taskObject(value: unknown): Record<string, unknown> | null { return value !== null && typeof value === "object" && !Array.isArray(value) && [Object.prototype, null].includes(Object.getPrototypeOf(value)) ? value as Record<string, unknown> : null; }
const id = (value: unknown): value is string => typeof value === "string" && UUID.test(value);
const sha = (value: unknown): value is string => typeof value === "string" && SHA256.test(value);
const integer = (value: unknown, min = 0, max = Number.MAX_SAFE_INTEGER): value is number => Number.isSafeInteger(value) && Number(value) >= min && Number(value) <= max;
const text = (value: unknown, max: number, empty = false, multiline = false): value is string => typeof value === "string" && value.length <= max && (empty || value.trim().length > 0) && !(multiline ? /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/ : /[\u0000-\u001f\u007f]/).test(value) && new TextDecoder().decode(new TextEncoder().encode(value)) === value;
const keys = (row: Record<string, unknown>, fields: readonly string[]) => Object.keys(row).sort().join(",") === [...fields].sort().join(",");
const stamp = (value: unknown): value is string => text(value, 64) && /^\d{4}-\d\d-\d\dT/.test(value) && /(?:Z|[+-]\d\d:\d\d)$/.test(value) && Number.isFinite(Date.parse(value));
const member = <T extends string>(value: unknown, options: readonly T[]): value is T => typeof value === "string" && options.includes(value as T);
const strings = (value: unknown, max = 32, size = 256): value is string[] => Array.isArray(value) && value.length <= max && value.every(item => text(item, size)) && new Set(value).size === value.length;
const codes = (value: unknown, maximum = 64): value is string[] => strings(value, maximum, 96) && value.every(item => /^[a-z][a-z0-9_]{0,95}$/.test(item));
const typeRefs = (value: unknown, maximum = 32): value is string[] => strings(value, maximum) && value.every(item => /^(?:syn|synny):\/\/type\/[a-z0-9]+(?:[-_][a-z0-9]+)*$/.test(item));
const pair = (identity: unknown, digest: unknown) => identity === null && digest === null || id(identity) && sha(digest);
const authority = (row: Record<string, unknown>) => typeof row.approvalValid === "boolean" && typeof row.authorityCurrent === "boolean" && codes(row.reasonCodes) && row.authorityCurrent === (row.reasonCodes.length === 0) && (!row.authorityCurrent || row.approvalValid);
function copy<T>(row: Record<string, unknown>, fields: readonly string[]): T { return Object.fromEntries(fields.map(key => [key, structuredClone(row[key])])) as T; }
function scope(value: unknown, workspace: string, principal?: string): TaskDiscoveryScope | null { const row = taskObject(value); return row?.schema === TASK_DISCOVERY_SCHEMA && row.workspaceId === workspace && id(workspace) && id(row.principalId) && (!principal || row.principalId === principal) ? { schema: TASK_DISCOVERY_SCHEMA, workspaceId: workspace, principalId: row.principalId } : null; }
function boundedJSON(value: unknown, maximum = 131072): value is Record<string, unknown> {
  let nodes = 0; const visit = (item: unknown, depth: number): boolean => { if (++nodes > 8192 || depth > 24) return false; if (item === null || typeof item === "boolean") return true; if (typeof item === "number") return Number.isFinite(item); if (typeof item === "string") return !item.includes("\0") && new TextDecoder().decode(new TextEncoder().encode(item)) === item; if (Array.isArray(item)) return item.every(child => visit(child, depth + 1)); const row = taskObject(item); return !!row && Object.entries(row).every(([key, child]) => visit(key, depth + 1) && visit(child, depth + 1)); };
  try { return !!taskObject(value) && visit(value, 0) && new TextEncoder().encode(JSON.stringify(value)).length <= maximum; } catch { return false; }
}
const CONSTRAINT_FIELDS = ["classes", "inputTypes", "outputTypes", "platform", "permittedEffects", "permittedScopes", "requireLocal", "requireTaskEvidence"];
export function projectTaskConstraints(value: unknown): TaskDiscoveryConstraints | null {
  const r = taskObject(value); if (!r || !keys(r, CONSTRAINT_FIELDS) || !strings(r.classes) || !r.classes.every(item => CAPABILITIES.includes(item)) || !typeRefs(r.inputTypes, 64) || !typeRefs(r.outputTypes, 64) || !(r.platform === null || member(r.platform, PLATFORMS)) || !(r.permittedEffects === null || strings(r.permittedEffects) && r.permittedEffects.every(item => EFFECTS.includes(item))) || !(r.permittedScopes === null || strings(r.permittedScopes, 64)) || typeof r.requireLocal !== "boolean" || typeof r.requireTaskEvidence !== "boolean") return null;
  return copy(r, CONSTRAINT_FIELDS);
}
const EMBEDDING_FIELDS = ["spaceId", "embeddingName", "profileSha256", "maxCandidates", "runtimeBudgetMs"];
export function projectTaskEmbedding(value: unknown): TaskEmbeddingChoice | null { const r = taskObject(value); return r && keys(r, EMBEDDING_FIELDS) && id(r.spaceId) && text(r.embeddingName, 64) && sha(r.profileSha256) && integer(r.maxCandidates, 1, 16) && integer(r.runtimeBudgetMs, 1, 120000) ? copy(r, EMBEDDING_FIELDS) : null; }
const SPACE_FIELDS = ["spaceId", "embeddingName", "profileSha256", "model", "revision", "dimensions", "maxInputTokens", "maxBatchSize"];
function embeddingSpace(value: unknown): TaskEmbeddingSpace | null { const r = taskObject(value); return r && keys(r, SPACE_FIELDS) && id(r.spaceId) && text(r.embeddingName, 64) && sha(r.profileSha256) && text(r.model, 512) && typeof r.revision === "string" && /^(?:[a-f0-9]{40}|[a-f0-9]{64})$/.test(r.revision) && r.dimensions === 1024 && integer(r.maxInputTokens, 1, 32768) && integer(r.maxBatchSize, 1, 64) ? copy(r, SPACE_FIELDS) : null; }
const CONTEXT_FIELDS = ["taskBinding", "taskStatus", "taskCapabilities", "catalogueSha256", "typesSha256", "policySha256", "consumerSha256", "contextSha256", "counts", "embeddingSpaces"];
export function projectTaskDiscoveryContext(value: unknown): TaskDiscoveryContext | null {
  const r = taskObject(value); if (!r || !keys(r, CONTEXT_FIELDS) || !projectQualificationTaskBinding(r.taskBinding) || r.taskStatus !== "running" || !strings(r.taskCapabilities, 128) || ![r.catalogueSha256, r.typesSha256, r.policySha256, r.consumerSha256, r.contextSha256].every(sha)) return null;
  const counts = taskObject(r.counts); if (!counts || !keys(counts, ["registry", "operations", "mcp"]) || !Object.values(counts).every(value => integer(value, 0, 256)) || (counts.registry as number) + (counts.operations as number) + (counts.mcp as number) > 256 || !Array.isArray(r.embeddingSpaces) || r.embeddingSpaces.length > 128) return null;
  const spaces = r.embeddingSpaces.map(embeddingSpace); if (spaces.some(item => item === null) || new Set(spaces.map(item => item!.spaceId + ":" + item!.embeddingName)).size !== spaces.length) return null;
  return copy(r, CONTEXT_FIELDS);
}
const EVIDENCE_FIELDS = ["status", "suiteId", "reportId", "reportSha256", "caseCount", "passedCases"];
export function projectTaskEvidence(value: unknown): TaskDiscoveryEvidence | null {
  const r = taskObject(value); if (!r || !keys(r, EVIDENCE_FIELDS) || !member(r.status, ["none", "matching_pass", "matching_failure", "historical"]) || !integer(r.caseCount, 0, 8) || !integer(r.passedCases, 0, r.caseCount)) return null;
  if (r.status === "none" ? r.suiteId !== null || r.reportId !== null || r.reportSha256 !== null || r.caseCount !== 0 || r.passedCases !== 0 : !id(r.suiteId) || !id(r.reportId) || !sha(r.reportSha256)) return null;
  if (r.status === "matching_pass" && (r.caseCount === 0 || r.caseCount !== r.passedCases)) return null;
  return copy(r, EVIDENCE_FIELDS);
}
const CANDIDATE_FIELDS = ["id", "kind", "name", "description", "ownerId", "bindingId", "ownerSha256", "capabilities", "requiredTaskCapability", "installed", "authorityCurrent", "reasonCodes", "taskEvidence"];
const RANK_FIELDS = ["score", "features", "typeFit", "taskPermission", "reasons", "executable"];
export function projectTaskCandidate(value: unknown, ranked = false): TaskDiscoveryCandidate | RankedTaskCandidate | null {
  const r = taskObject(value), cap = taskObject(r?.capabilities), fields = [...CANDIDATE_FIELDS, ...(ranked ? RANK_FIELDS : [])];
  if (!r || !keys(r, fields) || !sha(r.id) || !member(r.kind, ["registry", "operation", "mcp"]) || !text(r.name, 256) || !text(r.description, 4096, true, true) || !(r.kind === "operation" ? sha(r.ownerId) : id(r.ownerId)) || !(r.kind === "mcp" ? id(r.bindingId) : r.bindingId === null) || !sha(r.ownerSha256) || !(r.requiredTaskCapability === null || text(r.requiredTaskCapability, 256)) || typeof r.installed !== "boolean" || typeof r.authorityCurrent !== "boolean" || !codes(r.reasonCodes) || !projectTaskEvidence(r.taskEvidence) || !cap || !keys(cap, ["classes", "inputTypes", "outputTypes", "effects", "scopes", "platforms"]) || !Object.values(cap).every(value => strings(value)) || !(cap.classes as string[]).every(value => CAPABILITIES.includes(value)) || !typeRefs(cap.inputTypes) || !typeRefs(cap.outputTypes) || !(cap.effects as string[]).every(value => EFFECTS.includes(value)) || !(cap.platforms as string[]).every(value => PLATFORMS.includes(value))) return null;
  if (ranked) { const f = taskObject(r.features); if (!integer(r.score) || !f || !keys(f, ["classes", "lexical", "types", "installed", "evidence", "semantic"]) || !Object.values(f).every(value => integer(value)) || !member(r.typeFit, ["not_requested", "exact", "compatible", "unknown", "incompatible"]) || !member(r.taskPermission, ["permitted", "denied", "unknown"]) || !codes(r.reasons, 128) || r.executable !== false) return null; }
  return copy(r, fields);
}
const SEARCH_FIELDS = ["id", "revision", "bindingSha256", "status", "taskBinding", "contextSha256", "catalogueSha256", "typesSha256", "policySha256", "consumerSha256", "query", "constraints", "mode", "limit", "embedding", "candidateCount", "resultId", "resultSha256", "selectedCandidateId", "selectionId", "reasonCode", "approvalValid", "authorityCurrent", "reasonCodes", "actions", "createdAt", "updatedAt"];
export function projectTaskSearch(value: unknown): TaskDiscoverySearch | null {
  const r = taskObject(value); if (!r || !keys(r, SEARCH_FIELDS) || !id(r.id) || !integer(r.revision, 1, Number.MAX_SAFE_INTEGER - 1) || !sha(r.bindingSha256) || !member(r.status, SEARCH_STATES) || !projectQualificationTaskBinding(r.taskBinding) || !projectTaskConstraints(r.constraints) || ![r.contextSha256, r.catalogueSha256, r.typesSha256, r.policySha256, r.consumerSha256].every(sha) || !text(r.query, 500) || !member(r.mode, ["lexical", "hybrid"]) || !integer(r.limit, 1, 10) || !(r.mode === "lexical" ? r.embedding === null : projectTaskEmbedding(r.embedding)) || !integer(r.candidateCount, 0, 256) || !pair(r.resultId, r.resultSha256) || !(r.selectedCandidateId === null || sha(r.selectedCandidateId)) || !(r.selectionId === null || id(r.selectionId)) || (r.selectedCandidateId === null) !== (r.selectionId === null) || !(r.reasonCode === null || codes([r.reasonCode])) || !authority(r) || !strings(r.actions, 2, 16) || !r.actions.every(value => value === "cancel" || value === "select") || !stamp(r.createdAt) || !stamp(r.updatedAt) || Date.parse(r.updatedAt) < Date.parse(r.createdAt)) return null;
  if (r.actions.includes("select") && (!r.approvalValid || !r.authorityCurrent || r.status !== "completed" || r.resultId === null) || r.actions.includes("cancel") && !["requested", "running"].includes(r.status) || r.status === "selected" && r.selectionId === null) return null;
  return copy(r, SEARCH_FIELDS);
}
const SEMANTIC_FIELDS = ["spaceId", "embeddingName", "profileSha256", "querySha256", "documentsSha256", "candidateCount", "calls", "inputTokens", "elapsedMs", "reservationIds", "usageOrigin"];
function projectSemantic(value: unknown): TaskDiscoverySemantic | null {
  const r = taskObject(value); if (!r || !keys(r, SEMANTIC_FIELDS) || !id(r.spaceId) || !text(r.embeddingName, 64) || ![r.profileSha256, r.querySha256, r.documentsSha256].every(sha) || !integer(r.candidateCount, 1, 16) || !integer(r.calls, 2, 17) || !integer(r.inputTokens, 1, 17 * 32768) || !integer(r.elapsedMs) || !Array.isArray(r.reservationIds) || r.reservationIds.length !== 1 || !r.reservationIds.every(id) || new Set(r.reservationIds).size !== r.reservationIds.length || !member(r.usageOrigin, ["synthetic", "measured", "provider_reported"])) return null;
  return copy(r, SEMANTIC_FIELDS);
}
const RESULT_FIELDS = ["id", "searchId", "sha256", "approvalValid", "authorityCurrent", "reasonCodes", "taskBinding", "catalogueSha256", "typesSha256", "policySha256", "consumerSha256", "query", "constraints", "mode", "items", "excluded", "terms", "inferredClasses", "noSuitableCandidate", "semantic", "recordedAt"];
export function projectTaskResult(value: unknown): TaskDiscoveryResult | null {
  const r = taskObject(value); if (!r || !keys(r, RESULT_FIELDS) || !id(r.id) || !id(r.searchId) || !sha(r.sha256) || !authority(r) || !projectQualificationTaskBinding(r.taskBinding) || !projectTaskConstraints(r.constraints) || ![r.catalogueSha256, r.typesSha256, r.policySha256, r.consumerSha256].every(sha) || !text(r.query, 500) || !member(r.mode, ["lexical", "hybrid"]) || !Array.isArray(r.items) || r.items.length > 10 || !Array.isArray(r.excluded) || r.excluded.length > 256 || !strings(r.terms, 128, 256) || !strings(r.inferredClasses, 32) || !r.inferredClasses.every(value => CAPABILITIES.includes(value)) || typeof r.noSuitableCandidate !== "boolean" || r.noSuitableCandidate !== (r.items.length === 0) || !(r.mode === "lexical" ? r.semantic === null : projectSemantic(r.semantic) || r.semantic === null && r.items.length === 0) || !stamp(r.recordedAt)) return null;
  const items = r.items.map(value => projectTaskCandidate(value, true)); if (items.some(item => !item) || new Set(items.map(item => item!.id)).size !== items.length) return null;
  const excluded: { id: string; reasonCodes: string[] }[] = []; for (const raw of r.excluded) { const row = taskObject(raw); if (!row || !keys(row, ["id", "reasonCodes"]) || !sha(row.id) || !codes(row.reasonCodes) || row.reasonCodes.length === 0 || excluded.some(item => item.id === row.id) || items.some(item => item!.id === row.id)) return null; excluded.push({ id: row.id, reasonCodes: [...row.reasonCodes] }); }
  if (items.length + excluded.length > 256) return null;
  return copy(r, RESULT_FIELDS);
}
const SELECTION_FIELDS = ["id", "searchId", "resultId", "candidateId", "sha256", "approvalValid", "authorityCurrent", "reasonCodes", "taskBinding", "owner", "handoff", "recordedAt"];
export function projectTaskSelection(value: unknown): TaskDiscoverySelection | null {
  const r = taskObject(value), owner = taskObject(r?.owner), handoff = taskObject(r?.handoff); if (!r || !keys(r, SELECTION_FIELDS) || ![r.id, r.searchId, r.resultId].every(id) || !sha(r.candidateId) || !sha(r.sha256) || !authority(r) || !projectQualificationTaskBinding(r.taskBinding) || !owner || !keys(owner, ["kind", "ownerId", "bindingId", "ownerSha256"]) || !member(owner.kind, ["registry", "operation", "mcp"]) || !(owner.kind === "operation" ? sha(owner.ownerId) : id(owner.ownerId)) || !(owner.kind === "mcp" ? id(owner.bindingId) : owner.bindingId === null) || !sha(owner.ownerSha256) || !handoff || !keys(handoff, ["kind", "identity", "bindingId"]) || handoff.kind !== owner.kind || handoff.identity !== owner.ownerId || handoff.bindingId !== owner.bindingId || !stamp(r.recordedAt)) return null;
  return copy(r, SELECTION_FIELDS);
}
export function projectTaskEnvelope(value: unknown, workspace: string, kind: "context" | "search" | "result" | "selection" | "candidate", identity?: string, principal?: string): TaskDiscoveryEnvelope | null {
  const r = taskObject(value), s = scope(value, workspace, principal); if (!r || !s) return null;
  if (kind === "context") { const context = projectTaskDiscoveryContext(r.context); return context ? { ...s, context } : null; }
  if (kind === "search") { const search = projectTaskSearch(r.search); return search && (!identity || search.id === identity) ? { ...s, search } : null; }
  if (kind === "result") { const result = projectTaskResult(r.result); return result && (!identity || result.id === identity) ? { ...s, result } : null; }
  if (kind === "selection") { const selection = projectTaskSelection(r.selection); return selection && (!identity || selection.id === identity) ? { ...s, selection } : null; }
  const candidate = projectTaskCandidate(r.candidate, true); return candidate && (!identity || candidate.id === identity) && boundedJSON(r.detail) ? { ...s, candidate: candidate as RankedTaskCandidate, detail: structuredClone(r.detail) } : null;
}
export function projectTaskSearches(value: unknown, workspace: string, principal?: string, episode?: string): TaskDiscoveryPage | null {
  const r = taskObject(value), s = scope(value, workspace, principal); if (!r || !s || !Array.isArray(r.items) || r.items.length > 25 || !(r.nextCursor === null || id(r.nextCursor))) return null;
  const items = r.items.map(projectTaskSearch); if (items.some(item => !item || episode && item.taskBinding.episodeId !== episode) || new Set(items.map(item => item!.id)).size !== items.length || r.nextCursor !== null && (items.length === 0 || items.at(-1)!.id !== r.nextCursor)) return null;
  return { ...s, items: items as TaskDiscoverySearch[], nextCursor: r.nextCursor };
}
export function validateTaskMutation(path: string[], value: unknown): Record<string, unknown> | null {
  const r = taskObject(value); if (!r || r.confirm !== true) return null;
  if (path.length === 1 && path[0] === "searches") {
    const constraints = projectTaskConstraints(r.constraints); if (!keys(r, ["episodeId", "stateId", "contextSha256", "query", "constraints", "mode", "limit", "embedding", "confirm"]) || !id(r.episodeId) || !id(r.stateId) || !sha(r.contextSha256) || !text(r.query, 500) || !constraints || !member(r.mode, ["lexical", "hybrid"]) || !integer(r.limit, 1, 10) || !(r.mode === "lexical" ? r.embedding === null : projectTaskEmbedding(r.embedding))) return null;
    return { episodeId: r.episodeId, stateId: r.stateId, contextSha256: r.contextSha256, query: r.query, constraints, mode: r.mode, limit: r.limit, embedding: r.embedding === null ? null : projectTaskEmbedding(r.embedding), confirm: true };
  }
  if (path.length !== 3 || path[0] !== "searches" || !id(path[1]) || !member(path[2], ["cancel", "select"]) || !keys(r, ["revision", "digest", "confirm", ...(path[2] === "select" ? ["candidateId"] : [])]) || !integer(r.revision, 1, Number.MAX_SAFE_INTEGER - 1) || !sha(r.digest) || path[2] === "select" && !sha(r.candidateId)) return null;
  return { revision: r.revision, digest: r.digest, ...(path[2] === "select" ? { candidateId: r.candidateId } : {}), confirm: true };
}
export function projectTaskMutation(value: unknown, workspace: string, path: string[], body: Record<string, unknown>, principal?: string): TaskDiscoveryEnvelope | null {
  const kind = path[2] === "select" ? "selection" : "search", envelope = projectTaskEnvelope(value, workspace, kind, kind === "search" && path.length === 3 ? path[1] : undefined, principal); if (!envelope) return null;
  if ("selection" in envelope) return envelope.selection.searchId === path[1] && envelope.selection.candidateId === body.candidateId ? envelope : null;
  if (!("search" in envelope)) return null; const s = envelope.search;
  if (path.length === 1) return s.taskBinding.episodeId === body.episodeId && s.taskBinding.repositoryStateId === body.stateId && s.contextSha256 === body.contextSha256 && s.query === body.query && s.mode === body.mode && s.limit === body.limit && sameTaskConstraints(s.constraints, body.constraints as TaskDiscoveryConstraints) && sameTaskValue(s.embedding, body.embedding) ? envelope : null;
  return s.revision === Number(body.revision) + 1 && s.status === "cancelled" ? envelope : null;
}

/** JSONB object ordering is not an identity; arrays retain their declared order. */
export function sameTaskValue(left: unknown, right: unknown): boolean {
  const stable = (value: unknown): unknown => Array.isArray(value) ? value.map(stable) : taskObject(value) ? Object.fromEntries(Object.entries(value as Record<string, unknown>).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).map(([key, item]) => [key, stable(item)])) : value;
  return JSON.stringify(stable(left)) === JSON.stringify(stable(right));
}
export function sameTaskConstraints(left: TaskDiscoveryConstraints, right: TaskDiscoveryConstraints): boolean {
  const normalize = (value: TaskDiscoveryConstraints) => {
    const canonical = (ref: string) => { const slug = ref.split("/type/")[1]; return `${slug?.startsWith("potbot-data-") ? "synny" : "syn"}://type/${slug}`; };
    return { ...value, classes: [...value.classes].sort(), inputTypes: value.inputTypes.map(canonical).sort(), outputTypes: value.outputTypes.map(canonical).sort(), permittedEffects: value.permittedEffects === null ? null : [...value.permittedEffects].sort(), permittedScopes: value.permittedScopes === null ? null : [...value.permittedScopes].sort() };
  };
  return sameTaskValue(normalize(left), normalize(right));
}

/** Explicit task-discovery commands and safe links to existing review owners. */
import { UUID, SHA256, TASK_DISCOVERY_SCHEMA, MAX_TASK_DISCOVERY_REQUEST, taskObject, sameTaskValue, sameTaskConstraints, parseMcpJSON, validateTaskMutation, type TaskDiscoverySearch, type TaskDiscoveryResult, type TaskDiscoverySelection, type RankedTaskCandidate, type TaskDiscoveryContext } from "./task-discovery.ts";
import { workshopJournalScope, type WorkshopAttempt } from "./skill-workshop-client.ts";
export const TASK_DISCOVERY_ENDPOINT = "/api/ui/task-discovery";
export function taskDiscoveryScopeInvalidated(status: number, value: unknown, workspace: string, principal: string): boolean { const r = taskObject(value); return status === 401 || status === 403 || r?.schema === TASK_DISCOVERY_SCHEMA && typeof r.workspaceId === "string" && UUID.test(r.workspaceId) && typeof r.principalId === "string" && UUID.test(r.principalId) && (r.workspaceId !== workspace || r.principalId !== principal); }
export function taskDiscoveryJournalScope(workspace: string, principal: string): string { return "task-discovery:" + workshopJournalScope(workspace, principal); }
export function taskDiscoveryAttemptRoute(path: string): string[] | null { return path.startsWith(TASK_DISCOVERY_ENDPOINT + "/") ? path.slice(TASK_DISCOVERY_ENDPOINT.length + 1).split("/") : null; }
export function newTaskDiscoveryAttempt(path: string, body: unknown): WorkshopAttempt { return { path, body: JSON.stringify(body), key: "task-discovery:" + crypto.randomUUID() }; }
export function validateTaskDiscoveryAttempt(value: unknown): WorkshopAttempt | null {
  const r = taskObject(value); if (!r || Object.keys(r).sort().join(",") !== "body,key,path" || typeof r.path !== "string" || typeof r.body !== "string" || new TextEncoder().encode(r.body).length > MAX_TASK_DISCOVERY_REQUEST || typeof r.key !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(r.key)) return null;
  const path = taskDiscoveryAttemptRoute(r.path); if (!path) return null;
  try { return validateTaskMutation(path, parseMcpJSON(r.body)) ? { path: r.path, body: r.body, key: r.key } : null; } catch { return null; }
}
export function taskDiscoverySelection(query: string, workspace: string): { episode: string | null; state: string | null; search: string | null } {
  const p = new URLSearchParams(query); if (!UUID.test(workspace) || p.getAll("workspace").length !== 1 || p.get("workspace") !== workspace) return { episode: null, state: null, search: null };
  const selected = (name: string) => p.getAll(name).length === 1 && UUID.test(p.get(name)!) ? p.get(name) : null;
  return { episode: selected("episode"), state: selected("state"), search: selected("search") };
}
export function taskDiscoveryNeedsMonitoring(search: TaskDiscoverySearch): boolean { return search.status === "requested" || search.status === "running"; }
export function taskResultMatches(search: TaskDiscoverySearch, result: TaskDiscoveryResult): boolean {
  return search.id === result.searchId && search.resultId === result.id && search.resultSha256 === result.sha256 && search.query === result.query && search.mode === result.mode && ["catalogueSha256", "typesSha256", "policySha256", "consumerSha256"].every(key => search[key as keyof TaskDiscoverySearch] === result[key as keyof TaskDiscoveryResult]) && sameTaskValue(search.taskBinding, result.taskBinding) && sameTaskConstraints(search.constraints, result.constraints) && result.items.length <= search.limit;
}
export function taskCanSelect(search: TaskDiscoverySearch, result: TaskDiscoveryResult, candidate: RankedTaskCandidate): boolean {
  return taskResultMatches(search, result) && search.approvalValid && search.authorityCurrent && search.status === "completed" && search.actions.includes("select") && result.approvalValid && result.authorityCurrent && candidate.authorityCurrent && candidate.executable === false && candidate.taskPermission !== "denied" && candidate.typeFit !== "incompatible" && result.items.some(item => item.id === candidate.id);
}
export function taskSelectionMatches(search: TaskDiscoverySearch, result: TaskDiscoveryResult, selection: TaskDiscoverySelection): boolean {
  const candidate = result.items.find(item => item.id === selection.candidateId); return taskResultMatches(search, result) && selection.searchId === search.id && selection.resultId === result.id && search.selectionId === selection.id && search.selectedCandidateId === selection.candidateId && sameTaskValue(selection.taskBinding, search.taskBinding) && !!candidate && selection.owner.kind === candidate.kind && selection.owner.ownerId === candidate.ownerId && selection.owner.bindingId === candidate.bindingId && selection.owner.ownerSha256 === candidate.ownerSha256;
}
export function taskContextMatches(context: TaskDiscoveryContext, episode: string, state: string): boolean { return context.taskBinding.episodeId === episode && context.taskBinding.repositoryStateId === state; }
export function taskOwnerHref(workspace: string, owner: { kind: "registry" | "operation" | "mcp"; ownerId: string; bindingId: string | null }): string | null {
  if (!UUID.test(workspace)) return null; const p = new URLSearchParams({ workspace });
  if (owner.kind === "operation" && SHA256.test(owner.ownerId) && owner.bindingId === null) { p.set("operation", owner.ownerId); return "/tools?" + p; }
  if (owner.kind === "registry" && UUID.test(owner.ownerId) && owner.bindingId === null) { p.set("candidate", owner.ownerId); return "/discover?" + p; }
  if (owner.kind === "mcp" && UUID.test(owner.ownerId) && owner.bindingId !== null && UUID.test(owner.bindingId)) { p.set("binding", owner.bindingId); return "/discover/mcp?" + p; }
  return null;
}
export function taskHandoffHref(workspace: string, selection: TaskDiscoverySelection): string | null {
  if (!selection.approvalValid || !selection.authorityCurrent) return null;
  const href = taskOwnerHref(workspace, { kind: selection.handoff.kind, ownerId: selection.handoff.identity, bindingId: selection.handoff.bindingId });
  return selection.handoff.kind === "operation" ? href && UUID.test(selection.id) ? `${href}&selection=${selection.id}` : null : href;
}
export function exactOwnerSelection(query: string, workspace: string, kind: "operation" | "candidate"): string | null { const p = new URLSearchParams(query), value = p.get(kind); return UUID.test(workspace) && p.getAll("workspace").length === 1 && p.get("workspace") === workspace && p.getAll(kind).length === 1 && value !== null && (kind === "operation" ? SHA256 : UUID).test(value) ? value : null; }

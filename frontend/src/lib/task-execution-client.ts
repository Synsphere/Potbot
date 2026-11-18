/** Explicit selected-operation review, original-command recovery and scoped reads. */
import { UUID, SHA256, TASK_EXECUTION_SCHEMA, MAX_TASK_EXECUTION_REQUEST, sameTaskValue, parseMcpJSON, validateTaskExecutionMutation, projectTaskExecutionArtifact, type TaskExecutionScope, type TaskExecutionContext, type TaskExecutionBinding, type TaskExecutionJob } from "./task-execution.ts";
import { taskObject } from "./task-discovery.ts";
import { checkUsable, artifactBytes, type Artifact } from "./operation-workbench.ts";
import { MAX_ARTIFACT_DOWNLOAD_BYTES } from "./operation-workbench-download.ts";
import { workshopJournalScope, type WorkshopAttempt } from "./skill-workshop-client.ts";
export const TASK_EXECUTION_ENDPOINT = "/api/ui/task-execution", TASK_EXECUTION_MAX_POLLS = 30;
export function taskExecutionBase(selection: string): string { if (!UUID.test(selection)) throw new Error("Select an exact retained discovery selection."); return `${TASK_EXECUTION_ENDPOINT}/selections/${selection}`; }
export function taskExecutionScopeInvalidated(status: number, value: unknown, workspace: string, principal: string): boolean { const row = taskObject(value); return status === 401 || status === 403 || row?.schema === TASK_EXECUTION_SCHEMA && typeof row.workspaceId === "string" && UUID.test(row.workspaceId) && typeof row.principalId === "string" && UUID.test(row.principalId) && (row.workspaceId !== workspace || row.principalId !== principal); }
export function taskExecutionScopeMatches(left: TaskExecutionScope, right: TaskExecutionScope): boolean { return left.workspaceId === right.workspaceId && left.principalId === right.principalId && left.selectionId === right.selectionId && left.selectionSha256 === right.selectionSha256 && left.operationId === right.operationId && sameTaskValue(left.taskBinding, right.taskBinding); }
export function taskExecutionBindingMatchesContext(binding: TaskExecutionBinding, context: TaskExecutionContext): boolean {
  return binding.workspaceId === context.workspaceId && binding.principalId === context.principalId && binding.selectionId === context.selectionId && binding.selectionSha256 === context.selectionSha256 && binding.operationId === context.operationId && sameTaskValue(binding.taskBinding, context.taskBinding) && binding.searchId === context.selection.searchId && binding.resultId === context.selection.resultId && binding.candidateId === context.selection.candidateId && binding.ownerSha256 === context.selection.owner.ownerSha256 && context.selection.owner.kind === "operation" && context.selection.owner.ownerId === binding.operationId;
}
export function taskExecutionCanLaunch(context: TaskExecutionContext, entry: TaskExecutionJob, now = Date.now()): boolean { return context.current && context.supported && context.selection.approvalValid && context.selection.authorityCurrent && !!context.operation?.supported && taskExecutionScopeMatches(context, entry) && taskExecutionBindingMatchesContext(entry.binding, context) && entry.job.job_type === "operation.check" && checkUsable(entry.job, entry.operationId!, now); }
export function taskExecutionNeedsMonitoring(entry: TaskExecutionJob): boolean { return ["queued", "running", "cancelling"].includes(entry.job.state); }
export function taskExecutionJournalScope(workspace: string, principal: string, selection: string): string { if (!UUID.test(selection)) throw new Error("Invalid selection journal scope"); return "task-execution:" + selection + ":" + workshopJournalScope(workspace, principal); }
export function taskExecutionAttemptRoute(path: string): string[] | null { return path.startsWith(TASK_EXECUTION_ENDPOINT + "/") ? path.slice(TASK_EXECUTION_ENDPOINT.length + 1).split("/") : null; }
export function newTaskExecutionAttempt(path: string, body: unknown): WorkshopAttempt { return { path, body: JSON.stringify(body), key: "task-execution:" + crypto.randomUUID() }; }
export function validateTaskExecutionAttempt(value: unknown, selection?: string): WorkshopAttempt | null {
  const row = taskObject(value); if (!row || Object.keys(row).sort().join(",") !== "body,key,path" || typeof row.path !== "string" || typeof row.body !== "string" || new TextEncoder().encode(row.body).length > MAX_TASK_EXECUTION_REQUEST || typeof row.key !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(row.key)) return null;
  const route = taskExecutionAttemptRoute(row.path); if (!route || selection && route[1] !== selection) return null;
  try { return validateTaskExecutionMutation(route, parseMcpJSON(row.body)) ? { path: row.path, body: row.body, key: row.key } : null; } catch { return null; }
}
export function selectedExecutionQuery(query: string, workspace: string, operation: string | null): { selection: string | null; job: string | null; invalid: boolean } {
  const params = new URLSearchParams(query), selected = params.get("selection"), job = params.get("job");
  if (!params.has("selection") && !params.has("job")) return { selection: null, job: null, invalid: false };
  if (!UUID.test(workspace) || params.getAll("workspace").length !== 1 || params.get("workspace") !== workspace || !operation || !SHA256.test(operation) || params.getAll("operation").length !== 1 || params.get("operation") !== operation || params.getAll("selection").length !== 1 || !selected || !UUID.test(selected) || params.has("job") && (params.getAll("job").length !== 1 || !job || !UUID.test(job))) return { selection: null, job: null, invalid: true };
  return { selection: selected, job, invalid: false };
}

export async function readTaskExecutionArtifact(options: { scope: TaskExecutionScope; bindingSha256: string; jobId: string; artifact: Artifact; signal: AbortSignal; current: () => boolean; read?: typeof fetch }): Promise<Uint8Array<ArrayBuffer>> {
  const { scope, bindingSha256, jobId, artifact, signal, current, read = fetch } = options;
  if (!scope.operationId || !SHA256.test(bindingSha256) || !UUID.test(jobId) || !Number.isSafeInteger(artifact.bytes) || artifact.bytes < 0 || artifact.bytes > MAX_ARTIFACT_DOWNLOAD_BYTES) throw new Error("The owned artifact exceeds its browser download boundary.");
  const active = () => { if (signal.aborted || !current()) throw new DOMException("Selected artifact read cancelled", "AbortError"); };
  active(); const bytes = new Uint8Array(artifact.bytes); let offset = 0;
  for (let index = 0; index <= 64; index++) {
    const response = await read(`${taskExecutionBase(scope.selectionId)}/jobs/${jobId}/artifact?offset=${offset}&length=262144`, { credentials: "same-origin", cache: "no-store", signal, headers: { "X-Potbot-Workspace": scope.workspaceId } }); active();
    if (!response.ok) throw new Error("The selected job’s owned artifact is unavailable.");
    const part = projectTaskExecutionArtifact(await response.json(), scope.workspaceId, scope.selectionId, jobId, offset, 262144, scope.principalId, scope.operationId); active();
    if (!part || !taskExecutionScopeMatches(scope, part) || part.binding.sha256 !== bindingSha256 || !sameTaskValue(part.artifact, artifact)) throw new Error("The selected job or artifact identity changed during the read.");
    const chunk = artifactBytes(part.dataBase64); bytes.set(chunk, offset); offset += chunk.length;
    if (part.nextOffset === null) break; if (index === 64) throw new Error("The selected artifact exceeded its bounded read window.");
  }
  if (offset !== artifact.bytes) throw new Error("The selected artifact is incomplete.");
  const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), byte => byte.toString(16).padStart(2, "0")).join(""); active();
  if (digest !== artifact.sha256) throw new Error("The selected artifact failed SHA-256 verification."); return bytes;
}

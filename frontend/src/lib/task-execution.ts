/** Selected discovery provenance narrows the existing checked job lifecycle. */
import { WORKBENCH_SCHEMA, UUID, SHA256, projectContract, projectJob, projectEvents, projectResult, projectArtifact, validateJobSpec, type OperationContract, type JobSpec, type WorkbenchJob, type WorkbenchEvent, type ResultEnvelope, type Artifact } from "./operation-workbench.ts";
import { CAPABILITIES, EFFECTS, PLATFORMS } from "./discovery.ts";
import { projectQualificationTaskBinding, type QualificationTaskBinding } from "./mcp-qualification.ts";
import { projectTaskSelection, taskObject, sameTaskValue, parseMcpJSON, type TaskDiscoverySelection, type TaskDiscoveryCandidate } from "./task-discovery.ts";
export { UUID, SHA256, parseMcpJSON, sameTaskValue };
export const TASK_EXECUTION_SCHEMA = "potbot-task-execution/v1" as const;
export const TASK_BINDING_SCHEMA = "potbot-task-execution-binding/v1" as const;
export const TASK_ORACLE_SCHEMA = "potbot-task-result-oracle/v1" as const;
export const TASK_VERIFICATION_SCHEMA = "potbot-task-result-verification/v1" as const;
export const MAX_TASK_EXECUTION_REQUEST = 131072, MAX_TASK_EXECUTION_RESPONSE = 1500000;
export type TaskExecutionScope = { schema: typeof TASK_EXECUTION_SCHEMA; workspaceId: string; principalId: string; selectionId: string; selectionSha256: string; operationId: string | null; taskBinding: QualificationTaskBinding };
export type TaskResultAssertion = { op: "equals" | "contains" | "length_equals"; pointer: string; value: unknown };
export type TaskResultOracle = { schema: typeof TASK_ORACLE_SCHEMA; assertions: TaskResultAssertion[] };
export type TaskExecutionContext = TaskExecutionScope & { selection: TaskDiscoverySelection; operation: OperationContract | null; supported: boolean; current: boolean; reasonCodes: string[] };
export type TaskExecutionBinding = { schema: typeof TASK_BINDING_SCHEMA; workspaceId: string; principalId: string; selectionId: string; selectionSha256: string; searchId: string; resultId: string; candidateId: string; operationId: string; ownerSha256: string; taskBinding: QualificationTaskBinding; capabilities: TaskDiscoveryCandidate["capabilities"]; request: JobSpec; requestSha256: string; consumerSha256: string; acceptance: TaskResultOracle; acceptanceSha256: string; sha256: string };
export type TaskExecutionJob = TaskExecutionScope & { binding: TaskExecutionBinding; job: WorkbenchJob };
/** A negative cancellation receipt may survive loss of the positive binding. */
export type TaskExecutionCancellation = TaskExecutionScope & { binding: TaskExecutionBinding | null; job: WorkbenchJob };
export type TaskExecutionJobs = TaskExecutionScope & { items: { binding: TaskExecutionBinding; job: WorkbenchJob }[]; nextCursor: string | null; hasMore: boolean };
export type TaskExecutionEvents = TaskExecutionJob & { items: WorkbenchEvent[]; next_cursor: number; has_more: boolean };
export type TaskResultVerification = { schema: typeof TASK_VERIFICATION_SCHEMA; status: "pending" | "passed" | "failed" | "unavailable"; bindingSha256: string; acceptanceSha256: string; outputSha256: string | null; evidenceSha256: string | null; resultKind: "hosted_text" | "typed_cog" | null; checks: { index: number; passed: boolean; reasonCode: string | null }[]; reasonCodes: string[] };
export type TaskExecutionResult = TaskExecutionScope & { binding: TaskExecutionBinding; result: ResultEnvelope; verification: TaskResultVerification };
export type TaskExecutionArtifact = TaskExecutionJob & { artifact: Artifact; offset: number; dataBase64: string; nextOffset: number | null };
export type TaskExecutionEnvelope = TaskExecutionContext | TaskExecutionJob | TaskExecutionCancellation | TaskExecutionJobs | TaskExecutionEvents | TaskExecutionResult | TaskExecutionArtifact;

const isId = (value: unknown): value is string => typeof value === "string" && UUID.test(value);
const isSha = (value: unknown): value is string => typeof value === "string" && SHA256.test(value);
const integer = (value: unknown, min = 0, max = Number.MAX_SAFE_INTEGER): value is number => Number.isSafeInteger(value) && Number(value) >= min && Number(value) <= max;
const keys = (row: Record<string, unknown>, expected: readonly string[]) => Object.keys(row).sort().join(",") === [...expected].sort().join(",");
const strings = (value: unknown, maximum = 32, size = 256): value is string[] => Array.isArray(value) && value.length <= maximum && value.every(item => typeof item === "string" && item.length <= size && !!item.trim() && !/[\u0000-\u001f\u007f]/.test(item) && new TextDecoder().decode(new TextEncoder().encode(item)) === item) && new Set(value).size === value.length;
const codes = (value: unknown): value is string[] => strings(value, 64, 96) && value.every(item => /^[a-z][a-z0-9_]{0,95}$/.test(item));
const cursor = (value: unknown): value is string => typeof value === "string" && value.length > 0 && value.length <= 512 && !/[\u0000-\u0020\u007f]/.test(value);

/** Complete finite literal JSON; this is validation, never expression evaluation. */
export function boundedTaskLiteral(value: unknown, maximum: number): boolean {
  let nodes = 0;
  const visit = (item: unknown, depth: number): boolean => {
    if (++nodes > 8192 || depth > 32) return false;
    if (item === null || typeof item === "boolean") return true;
    if (typeof item === "number") return Number.isFinite(item) && (!Number.isInteger(item) || Number.isSafeInteger(item));
    if (typeof item === "string") return new TextDecoder().decode(new TextEncoder().encode(item)) === item;
    if (Array.isArray(item)) return item.every(child => visit(child, depth + 1));
    const row = taskObject(item); return !!row && Object.entries(row).every(([key, child]) => Array.from(key).length <= 512 && new TextDecoder().decode(new TextEncoder().encode(key)) === key && visit(child, depth + 1));
  };
  try {
    if (!visit(value, 0)) return false;
    // Match the existing Python canonical JSON byte allowance for small floats.
    let adjustment = 0;
    const encoded = JSON.stringify(value, (_key, item: unknown) => { if (typeof item === "number" && !Number.isInteger(item) && Math.abs(item) < 0.0001) adjustment += item.toExponential().replace(/e([+-])(\d)$/, "e$10$2").length - JSON.stringify(item).length; return item; });
    return new TextEncoder().encode(encoded).length + adjustment <= maximum;
  } catch { return false; }
}
export function validTaskPointer(value: unknown): value is string { return typeof value === "string" && new TextEncoder().encode(value).length <= 1024 && new TextDecoder().decode(new TextEncoder().encode(value)) === value && !/[\u0000-\u001f\u007f]/.test(value) && (value === "" || value.startsWith("/")) && !/~(?![01])/.test(value) && (value === "" ? 0 : value.split("/").length - 1) <= 16; }
export function projectTaskOracle(value: unknown): TaskResultOracle | null {
  const row = taskObject(value); if (!row || !keys(row, ["schema", "assertions"]) || row.schema !== TASK_ORACLE_SCHEMA || !Array.isArray(row.assertions) || row.assertions.length < 1 || row.assertions.length > 16 || !boundedTaskLiteral(value, 65536)) return null;
  const assertions: TaskResultAssertion[] = [];
  for (const raw of row.assertions) {
    const item = taskObject(raw); if (!item || !keys(item, ["op", "pointer", "value"]) || !["equals", "contains", "length_equals"].includes(String(item.op)) || !validTaskPointer(item.pointer) || !boundedTaskLiteral(item.value, 16384) || item.op === "contains" && (typeof item.value !== "string" || item.value.length === 0) || item.op === "length_equals" && !integer(item.value)) return null;
    assertions.push({ op: item.op as TaskResultAssertion["op"], pointer: item.pointer, value: structuredClone(item.value) });
  }
  return { schema: TASK_ORACLE_SCHEMA, assertions };
}
function capabilities(value: unknown): TaskDiscoveryCandidate["capabilities"] | null {
  const row = taskObject(value), fields = ["classes", "inputTypes", "outputTypes", "effects", "scopes", "platforms"];
  if (!row || !keys(row, fields) || !Object.values(row).every(value => strings(value)) || !(row.classes as string[]).every(value => CAPABILITIES.includes(value)) || !(row.effects as string[]).every(value => EFFECTS.includes(value)) || !(row.platforms as string[]).every(value => PLATFORMS.includes(value)) || ![...(row.inputTypes as string[]), ...(row.outputTypes as string[])].every(value => /^(?:syn|synny):\/\/type\/[a-z0-9]+(?:[-_][a-z0-9]+)*$/.test(value))) return null;
  return structuredClone(row) as TaskDiscoveryCandidate["capabilities"];
}
export function projectTaskExecutionScope(value: unknown, workspace: string, selection: string, principal?: string, operation?: string): TaskExecutionScope | null {
  const row = taskObject(value), taskBinding = projectQualificationTaskBinding(row?.taskBinding);
  if (!row || row.schema !== TASK_EXECUTION_SCHEMA || row.workspaceId !== workspace || !isId(workspace) || row.selectionId !== selection || !isId(selection) || !isId(row.principalId) || principal && row.principalId !== principal || !isSha(row.selectionSha256) || !(row.operationId === null || isSha(row.operationId)) || operation && row.operationId !== operation || !taskBinding) return null;
  return { schema: TASK_EXECUTION_SCHEMA, workspaceId: workspace, principalId: row.principalId, selectionId: selection, selectionSha256: row.selectionSha256, operationId: row.operationId as string | null, taskBinding };
}
export function projectTaskExecutionContext(value: unknown, workspace: string, selection: string, principal?: string, operationId?: string): TaskExecutionContext | null {
  const row = taskObject(value), scope = projectTaskExecutionScope(value, workspace, selection, principal, operationId); if (!row || !scope) return null;
  const selected = projectTaskSelection(row.selection), operation = row.operation === null ? null : scope.operationId && projectContract(row.operation, workspace, scope.operationId, scope.principalId);
  if (!selected || selected.id !== scope.selectionId || selected.sha256 !== scope.selectionSha256 || !sameTaskValue(selected.taskBinding, scope.taskBinding) || !(row.operation === null || operation) || typeof row.supported !== "boolean" || typeof row.current !== "boolean" || !codes(row.reasonCodes) || row.current && (!selected.approvalValid || !selected.authorityCurrent) || (selected.owner.kind === "operation" ? selected.owner.ownerId !== scope.operationId : scope.operationId !== null || operation !== null) || row.supported && (!operation || !operation.supported || scope.operationId === null)) return null;
  return { ...scope, selection: selected, operation: operation || null, supported: row.supported, current: row.current, reasonCodes: [...row.reasonCodes] };
}
const BINDING_FIELDS = ["schema", "workspaceId", "principalId", "selectionId", "selectionSha256", "searchId", "resultId", "candidateId", "operationId", "ownerSha256", "taskBinding", "capabilities", "request", "requestSha256", "consumerSha256", "acceptance", "acceptanceSha256", "sha256"];
export function projectTaskExecutionBinding(value: unknown, scope: TaskExecutionScope): TaskExecutionBinding | null {
  const row = taskObject(value); if (!row || !keys(row, BINDING_FIELDS) || row.schema !== TASK_BINDING_SCHEMA || row.workspaceId !== scope.workspaceId || row.principalId !== scope.principalId || row.selectionId !== scope.selectionId || row.selectionSha256 !== scope.selectionSha256 || row.operationId !== scope.operationId || !isSha(row.operationId) || !isId(row.searchId) || !isId(row.resultId) || ![row.candidateId, row.ownerSha256, row.requestSha256, row.consumerSha256, row.acceptanceSha256, row.sha256].every(isSha) || !projectQualificationTaskBinding(row.taskBinding) || !sameTaskValue(row.taskBinding, scope.taskBinding)) return null;
  const request = validateJobSpec(row.request), acceptance = projectTaskOracle(row.acceptance), cap = capabilities(row.capabilities);
  if (!request || !acceptance || !cap || request.episode_id !== scope.taskBinding.episodeId || request.job_type === "cog.execute" && request.input.repository_state_id !== scope.taskBinding.repositoryStateId) return null;
  return { ...structuredClone(row), request, acceptance, capabilities: cap } as TaskExecutionBinding;
}
function boundJob(value: unknown, scope: TaskExecutionScope, binding: TaskExecutionBinding, expectedId?: string): WorkbenchJob | null {
  if (!scope.operationId) return null;
  const job = projectJob(value, { schema: WORKBENCH_SCHEMA, workspaceId: scope.workspaceId, principalId: scope.principalId, operationId: scope.operationId }, expectedId);
  if (!job || job.episode_id !== binding.taskBinding.episodeId || job.contract_revision_id !== binding.taskBinding.contractRevisionId || (job.job_type === "operation.check" ? job.operation_check_id !== null || job.selected_models.length !== 0 : job.job_type !== binding.request.job_type || !job.operation_check_id || !sameTaskValue(job.selected_models, binding.request.selected_models))) return null;
  const check = job.result?.operation_check;
  if (check && (check.bindings.requestSha256 !== binding.requestSha256 || check.bindings.contractRevisionId !== binding.taskBinding.contractRevisionId || (check.eligible || check.bindings.taskExecutionSha256 !== undefined) && check.bindings.taskExecutionSha256 !== binding.sha256)) return null;
  return job;
}
export function projectTaskExecutionJob(value: unknown, workspace: string, selection: string, jobId?: string, principal?: string, operation?: string): TaskExecutionJob | null {
  const row = taskObject(value), scope = projectTaskExecutionScope(value, workspace, selection, principal, operation); if (!row || !scope) return null;
  const binding = projectTaskExecutionBinding(row.binding, scope), job = binding && boundJob(row.job, scope, binding, jobId); return binding && job ? { ...scope, binding, job } : null;
}
export function projectTaskExecutionJobs(value: unknown, workspace: string, selection: string, principal?: string, operation?: string): TaskExecutionJobs | null {
  const row = taskObject(value), scope = projectTaskExecutionScope(value, workspace, selection, principal, operation);
  if (!row || !scope || !Array.isArray(row.items) || row.items.length > 25 || !(row.nextCursor === null || cursor(row.nextCursor)) || typeof row.hasMore !== "boolean" || row.hasMore !== (row.nextCursor !== null) || row.hasMore && row.items.length === 0) return null;
  const items: TaskExecutionJobs["items"] = [];
  for (const raw of row.items) { const item = taskObject(raw); if (!item || !keys(item, ["binding", "job"])) return null; const binding = projectTaskExecutionBinding(item.binding, scope), job = binding && boundJob(item.job, scope, binding); if (!binding || !job || items.some(item => item.job.id === job.id)) return null; items.push({ binding, job }); }
  return { ...scope, items, nextCursor: row.nextCursor as string | null, hasMore: row.hasMore };
}
export function projectTaskExecutionEvents(value: unknown, workspace: string, selection: string, jobId: string, after: number, principal?: string, operation?: string): TaskExecutionEvents | null {
  const row = taskObject(value), entry = projectTaskExecutionJob(value, workspace, selection, jobId, principal, operation); if (!row || !entry?.operationId) return null;
  const events = projectEvents({ ...row, schema: WORKBENCH_SCHEMA }, workspace, entry.operationId, jobId, after, entry.principalId); return events ? { ...entry, items: events.items, next_cursor: events.next_cursor, has_more: events.has_more } : null;
}
const VERIFICATION_FIELDS = ["schema", "status", "bindingSha256", "acceptanceSha256", "outputSha256", "evidenceSha256", "resultKind", "checks", "reasonCodes"];
export function projectTaskVerification(value: unknown, binding: TaskExecutionBinding, job: WorkbenchJob): TaskResultVerification | null {
  const row = taskObject(value); if (!row || !keys(row, VERIFICATION_FIELDS) || row.schema !== TASK_VERIFICATION_SCHEMA || !["pending", "passed", "failed", "unavailable"].includes(String(row.status)) || row.bindingSha256 !== binding.sha256 || row.acceptanceSha256 !== binding.acceptanceSha256 || !(row.outputSha256 === null || isSha(row.outputSha256)) || !(row.evidenceSha256 === null || isSha(row.evidenceSha256)) || ![null, "hosted_text", "typed_cog"].includes(row.resultKind as string | null) || !Array.isArray(row.checks) || row.checks.length > binding.acceptance.assertions.length || !codes(row.reasonCodes)) return null;
  const checks: TaskResultVerification["checks"] = [];
  for (const raw of row.checks) { const check = taskObject(raw); if (!check || !keys(check, ["index", "passed", "reasonCode"]) || check.index !== checks.length || typeof check.passed !== "boolean" || !(check.reasonCode === null || codes([check.reasonCode])) || check.passed !== (check.reasonCode === null)) return null; checks.push({ index: check.index as number, passed: check.passed, reasonCode: check.reasonCode as string | null }); }
  if (["passed", "failed"].includes(String(row.status)) && (job.state !== "succeeded" || job.job_type !== binding.request.job_type || checks.length !== binding.acceptance.assertions.length || !isSha(row.outputSha256) || !isSha(row.evidenceSha256) || row.resultKind !== (binding.request.job_type === "model.generate" ? "hosted_text" : "typed_cog") || (row.status === "passed") !== checks.every(check => check.passed))) return null;
  if (["pending", "unavailable"].includes(String(row.status)) && checks.length !== 0) return null;
  return { ...structuredClone(row), checks } as TaskResultVerification;
}
export function projectTaskExecutionResult(value: unknown, workspace: string, selection: string, jobId: string, principal?: string, operation?: string): TaskExecutionResult | null {
  const row = taskObject(value), scope = projectTaskExecutionScope(value, workspace, selection, principal, operation); if (!row || !scope?.operationId) return null;
  const binding = projectTaskExecutionBinding(row.binding, scope), result = projectResult(row.result, workspace, scope.operationId, jobId, scope.principalId);
  if (!binding || !result || !boundJob(result.job, scope, binding, jobId)) return null;
  const verification = projectTaskVerification(row.verification, binding, result.job); return verification ? { ...scope, binding, result, verification } : null;
}
export function projectTaskExecutionArtifact(value: unknown, workspace: string, selection: string, jobId: string, offset: number, length: number, principal?: string, operation?: string): TaskExecutionArtifact | null {
  const row = taskObject(value), entry = projectTaskExecutionJob(value, workspace, selection, jobId, principal, operation); if (!row || !entry?.operationId) return null;
  const part = projectArtifact({ ...row, schema: WORKBENCH_SCHEMA }, workspace, entry.operationId, offset, length, entry.principalId); return part ? { ...entry, artifact: part.artifact, offset: part.offset, dataBase64: part.dataBase64, nextOffset: part.nextOffset } : null;
}
export function validateTaskExecutionMutation(path: string[], value: unknown): Record<string, unknown> | null {
  const row = taskObject(value); if (!row || path[0] !== "selections" || !isId(path[1]) || row.confirm !== true) return null;
  if (path.length === 3 && path[2] === "checks") { const request = validateJobSpec(row.request), acceptance = projectTaskOracle(row.acceptance); return keys(row, ["selectionSha256", "request", "acceptance", "confirm"]) && isSha(row.selectionSha256) && request && acceptance ? { selectionSha256: row.selectionSha256, request, acceptance, confirm: true } : null; }
  if (path.length === 3 && path[2] === "runs") return keys(row, ["checkJobId", "bindingSha256", "confirm"]) && isId(row.checkJobId) && isSha(row.bindingSha256) ? { checkJobId: row.checkJobId, bindingSha256: row.bindingSha256, confirm: true } : null;
  return path.length === 5 && path[2] === "jobs" && isId(path[3]) && path[4] === "cancel" && keys(row, ["confirm"]) ? { confirm: true } : null;
}
export function projectTaskExecutionMutation(value: unknown, workspace: string, path: string[], body: Record<string, unknown>, principal?: string, operation?: string): TaskExecutionJob | TaskExecutionCancellation | null {
  if (path.length === 5 && path[2] === "jobs" && path[4] === "cancel" && taskObject(value)?.binding === null) {
    const row = taskObject(value), scope = projectTaskExecutionScope(value, workspace, path[1], principal, operation);
    if (!row || !scope?.operationId) return null;
    const job = projectJob(row.job, { ...scope, schema: WORKBENCH_SCHEMA, operationId: scope.operationId }, path[3]);
    return job && job.episode_id === scope.taskBinding.episodeId && job.contract_revision_id === scope.taskBinding.contractRevisionId ? { ...scope, binding: null, job } : null;
  }
  const reply = projectTaskExecutionJob(value, workspace, path[1], path[4] === "cancel" ? path[3] : undefined, principal, operation); if (!reply) return null;
  if (path[2] === "checks") return reply.job.job_type === "operation.check" && reply.selectionSha256 === body.selectionSha256 && sameTaskValue(reply.binding.request, body.request) && sameTaskValue(reply.binding.acceptance, body.acceptance) ? reply : null;
  if (path[2] === "runs") return reply.job.job_type === reply.binding.request.job_type && reply.job.operation_check_id === body.checkJobId && reply.binding.sha256 === body.bindingSha256 ? reply : null;
  return reply;
}

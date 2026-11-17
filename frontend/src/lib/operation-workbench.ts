/** Public execution workbench contracts. All execution authority stays on the worker. */
export const WORKBENCH_SCHEMA = "potbot-operation-workbench/v1" as const;
export const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
export const SHA256 = /^[a-f0-9]{64}$/;
const CODE = /^[a-zA-Z][a-zA-Z0-9_.:-]{0,95}$/;
const MODEL = /^[A-Za-z0-9][A-Za-z0-9_./:@+\-]{0,249}$/;
export const PROVIDERS = ["openai", "anthropic", "gemini", "xai", "evolink"] as const;
export const JOB_STATES = ["queued", "running", "cancelling", "succeeded", "failed", "cancelled", "indeterminate"] as const;
export type JobState = typeof JOB_STATES[number];
export type JobType = "cog.execute" | "model.generate" | "operation.check";
export type WorkbenchScope = { schema: typeof WORKBENCH_SCHEMA; workspaceId: string; principalId: string; operationId: string };
export type OperationContract = WorkbenchScope & {
  kind: string; reference: string; executionReference: string | null; name: string; supported: boolean;
  jobType: "cog.execute" | "model.generate" | null; inputFields: string[]; reasonCodes: string[]; maxInputBytes: number;
};
export type ModelSelection = { provider: string; model: string; role: "reasoning" | "coding" };
export type JobSpec = { job_type: "cog.execute" | "model.generate"; episode_id: string; input: Record<string, unknown>; selected_models: ModelSelection[] };
export type CheckReport = {
  schema: "potbot-operation-check/v1"; operationId: string; eligible: boolean;
  checks: { code: string; status: "passed" | "blocked" | "unsupported" }[];
  bindings: { requestSha256: string; contractRevisionId: string; policySha256?: string; profileSha256?: string;
    installationSha256?: string; grantGeneration?: number; grantSha256?: string; repositorySha256?: string;
    releaseSha256?: string; nativeSha256?: string; runtimeSha256?: string };
  checkedAt: string; expiresAt: string; bindingSha256: string;
};
export type JobMetadata = {
  operation_check?: CheckReport; artifact_id?: string; invocation_id?: string; invocation_ids?: string[]; operation_id?: string;
  usage?: Record<string, number>; provider?: string; model?: string; finish_reason?: string; accounting?: string; completed_models?: number;
};
export type WorkbenchJob = {
  id: string; episode_id: string; contract_revision_id: string; job_type: JobType; state: JobState;
  owner_principal_id: string; operation_check_id: string | null; update_token: number;
  selected_models: ModelSelection[]; progress: { stage?: string; completed?: number; total?: number };
  cancellation_requested: boolean; result: JobMetadata | null; error_code: string | null; output_bytes: number;
  created_at: string; updated_at: string; started_at: string | null; finished_at: string | null;
  dispatched_at: string | null; heartbeat_at: string | null; lease_until: string | null;
  event_cursor?: number;
};
export type JobEnvelope = WorkbenchScope & { job: WorkbenchJob };
export type WorkbenchEvent = {
  sequence: number; id: string; job_id: string; event_type: string; recorded_at: string;
  data: { text?: string; provider?: string; model?: string; role?: string; stage?: string };
};
export type EventEnvelope = WorkbenchScope & { items: WorkbenchEvent[]; next_cursor: number; has_more: boolean };
export type Artifact = { id: string; sha256: string; bytes: number; mediaType: string };
export type ResultEnvelope = JobEnvelope & { output: string; truncated: boolean; artifact: Artifact | null; artifactText: string | null; artifactTruncated: boolean };
export type ArtifactEnvelope = WorkbenchScope & { artifact: Artifact; offset: number; dataBase64: string; nextOffset: number | null };

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}
function number(value: unknown, max = Number.MAX_SAFE_INTEGER): value is number { return Number.isSafeInteger(value) && Number(value) >= 0 && Number(value) <= max; }
function identifier(value: unknown, pattern = CODE): value is string { return typeof value === "string" && pattern.test(value); }
function time(value: unknown): value is string { return typeof value === "string" && value.length <= 64 && /^\d{4}-\d\d-\d\dT/.test(value) && Number.isFinite(Date.parse(value)); }
function nullableTime(value: unknown): value is string | null { return value === null || time(value); }
function boundedText(value: unknown, max: number): value is string { return typeof value === "string" && value.length <= max && !value.includes("\u0000"); }
function strings(value: unknown, max: number, pattern = CODE): value is string[] {
  return Array.isArray(value) && value.length <= max && value.every(item => identifier(item, pattern)) && new Set(value).size === value.length;
}
function finiteJSON(value: unknown, depth = 0): boolean {
  if (depth > 32) return false;
  if (value === null || typeof value === "boolean") return true;
  if (typeof value === "string") return !value.includes("\u0000") && new TextDecoder().decode(new TextEncoder().encode(value)) === value;
  if (typeof value === "number") return Number.isFinite(value) && (!Number.isInteger(value) || Number.isSafeInteger(value));
  if (Array.isArray(value)) return value.every(item => finiteJSON(item, depth + 1));
  const row = object(value); return row !== null && Object.entries(row).every(([key, item]) => finiteJSON(key, depth + 1) && finiteJSON(item, depth + 1));
}
function scope(value: unknown, workspace: string, operation: string, principal?: string): WorkbenchScope | null {
  const row = object(value);
  if (!row || row.schema !== WORKBENCH_SCHEMA || row.workspaceId !== workspace || row.operationId !== operation ||
      !UUID.test(workspace) || !SHA256.test(operation) || !identifier(row.principalId, UUID) || (principal && principal !== row.principalId)) return null;
  return { schema: WORKBENCH_SCHEMA, workspaceId: workspace, operationId: operation, principalId: row.principalId };
}
function selection(value: unknown): ModelSelection | null {
  const row = object(value);
  if (!row || !identifier(row.provider) || !identifier(row.model, MODEL) || !["reasoning", "coding"].includes(String(row.role))) return null;
  return { provider: row.provider, model: row.model, role: row.role as ModelSelection["role"] };
}

export function projectContract(value: unknown, workspace: string, operation: string, principal?: string): OperationContract | null {
  const row = object(value), selected = scope(value, workspace, operation, principal);
  if (!row || !selected || !["tool", "cog", "workflow", "connector", "model", "skill", "gateway"].includes(String(row.kind)) ||
      !boundedText(row.reference, 512) || !row.reference || !boundedText(row.name, 256) || !row.name ||
      !(row.executionReference === null || (boundedText(row.executionReference, 512) && row.executionReference.length > 0)) ||
      typeof row.supported !== "boolean" || ![null, "cog.execute", "model.generate"].includes(row.jobType as string | null) ||
      !strings(row.inputFields, 32) || !strings(row.reasonCodes, 32) || !number(row.maxInputBytes, 16_384) || row.maxInputBytes < 1 ||
      (row.supported && (row.jobType === null || (row.jobType === "cog.execute" && row.executionReference === null)))) return null;
  return { ...selected, kind: row.kind as string, reference: row.reference, executionReference: row.executionReference, name: row.name, supported: row.supported,
    jobType: row.jobType as OperationContract["jobType"], inputFields: row.inputFields, reasonCodes: row.reasonCodes, maxInputBytes: row.maxInputBytes };
}

export function projectCheck(value: unknown, operation: string): CheckReport | null {
  const row = object(value), raw = row && object(row.bindings);
  if (!row || !raw || row.schema !== "potbot-operation-check/v1" || row.operationId !== operation || typeof row.eligible !== "boolean" ||
      !Array.isArray(row.checks) || row.checks.length < 1 || row.checks.length > 32 || !time(row.checkedAt) || !time(row.expiresAt) ||
      !/(?:Z|\+00:00)$/.test(row.checkedAt) || !/(?:Z|\+00:00)$/.test(row.expiresAt) ||
      Date.parse(row.expiresAt) <= Date.parse(row.checkedAt) || Date.parse(row.expiresAt) - Date.parse(row.checkedAt) > 300_000 || !identifier(row.bindingSha256, SHA256) ||
      !identifier(raw.requestSha256, SHA256) || !identifier(raw.contractRevisionId, UUID)) return null;
  const checks: CheckReport["checks"] = [];
  for (const value of row.checks) {
    const check = object(value);
    if (!check || !identifier(check.code, /^[a-z][a-z0-9_]{0,63}$/) || !["passed", "blocked", "unsupported"].includes(String(check.status))) return null;
    checks.push({ code: check.code, status: check.status as CheckReport["checks"][number]["status"] });
  }
  if (new Set(checks.map(item => item.code)).size !== checks.length || row.eligible !== checks.every(check => check.status === "passed")) return null;
  const bindings: CheckReport["bindings"] = { requestSha256: raw.requestSha256, contractRevisionId: raw.contractRevisionId };
  for (const name of ["policySha256", "profileSha256", "installationSha256", "grantSha256", "repositorySha256", "releaseSha256", "nativeSha256", "runtimeSha256"] as const) {
    if (raw[name] !== undefined) { if (!identifier(raw[name], SHA256)) return null; bindings[name] = raw[name]; }
  }
  if (raw.grantGeneration !== undefined) { if (!number(raw.grantGeneration) || raw.grantGeneration < 1) return null; bindings.grantGeneration = raw.grantGeneration; }
  return { schema: "potbot-operation-check/v1", operationId: operation, eligible: row.eligible, checks, bindings,
    checkedAt: row.checkedAt, expiresAt: row.expiresAt, bindingSha256: row.bindingSha256 };
}

function metadata(value: unknown, operation: string): JobMetadata | null {
  const row = object(value); if (!row) return null;
  const result: JobMetadata = {};
  if (row.operation_check !== undefined) { const check = projectCheck(row.operation_check, operation); if (!check) return null; result.operation_check = check; }
  for (const name of ["artifact_id", "invocation_id", "operation_id"] as const) {
    if (row[name] !== undefined) { if (!identifier(row[name], UUID)) return null; result[name] = row[name]; }
  }
  if (row.invocation_ids !== undefined) { if (!strings(row.invocation_ids, 16, UUID)) return null; result.invocation_ids = row.invocation_ids; }
  for (const name of ["provider", "finish_reason"] as const) {
    if (row[name] !== undefined) { if (!identifier(row[name])) return null; result[name] = row[name]; }
  }
  if (row.model !== undefined) { if (!identifier(row.model, MODEL)) return null; result.model = row.model; }
  if (row.accounting !== undefined) {
    if (!["conservative_estimate", "provider_reported", "measured", "mixed", "pending_reconciliation"].includes(String(row.accounting))) return null;
    result.accounting = row.accounting as string;
  }
  if (row.completed_models !== undefined) { if (!number(row.completed_models, 16)) return null; result.completed_models = row.completed_models; }
  if (row.usage !== undefined) {
    const usage = object(row.usage); if (!usage || Object.keys(usage).some(key => !["input_tokens", "output_tokens", "tokens", "calls", "inference_ms", "money_microunits"].includes(key)) ||
      Object.values(usage).some(value => !number(value))) return null;
    result.usage = usage as Record<string, number>;
  }
  return result;
}

export function projectJob(value: unknown, selected: WorkbenchScope, expectedId?: string): WorkbenchJob | null {
  const row = object(value);
  if (!row || !identifier(row.id, UUID) || (expectedId && row.id !== expectedId) || !identifier(row.episode_id, UUID) ||
      !identifier(row.contract_revision_id, UUID) || !["cog.execute", "model.generate", "operation.check"].includes(String(row.job_type)) ||
      !JOB_STATES.includes(row.state as JobState) || row.owner_principal_id !== selected.principalId ||
      !(row.operation_check_id === null || identifier(row.operation_check_id, UUID)) || !number(row.update_token) ||
      (row.job_type === "operation.check") !== (row.operation_check_id === null) ||
      !Array.isArray(row.selected_models) || row.selected_models.length > 16 || typeof row.cancellation_requested !== "boolean" ||
      !(row.error_code === null || identifier(row.error_code)) || !number(row.output_bytes, 4 * 1024 * 1024) ||
      !time(row.created_at) || !time(row.updated_at) || !nullableTime(row.started_at) || !nullableTime(row.finished_at) ||
      !nullableTime(row.dispatched_at) || !nullableTime(row.heartbeat_at) || !nullableTime(row.lease_until)) return null;
  const selections: ModelSelection[] = [];
  for (const value of row.selected_models) { const model = selection(value); if (!model) return null; selections.push(model); }
  const rawProgress = object(row.progress); if (!rawProgress) return null;
  const progress: WorkbenchJob["progress"] = {};
  if (rawProgress.stage !== undefined) { if (!identifier(rawProgress.stage)) return null; progress.stage = rawProgress.stage; }
  if (rawProgress.completed !== undefined || rawProgress.total !== undefined) {
    if (!number(rawProgress.completed) || !number(rawProgress.total) || rawProgress.completed > rawProgress.total) return null;
    progress.completed = rawProgress.completed; progress.total = rawProgress.total;
  }
  const result = row.result === null ? null : metadata(row.result, selected.operationId);
  if (row.result !== null && result === null) return null;
  if (result?.operation_check && (row.job_type !== "operation.check" || result.operation_check.bindings.contractRevisionId !== row.contract_revision_id)) return null;
  const job: WorkbenchJob = { id: row.id, episode_id: row.episode_id, contract_revision_id: row.contract_revision_id,
    job_type: row.job_type as JobType, state: row.state as JobState, owner_principal_id: selected.principalId,
    operation_check_id: row.operation_check_id, update_token: row.update_token, selected_models: selections, progress,
    cancellation_requested: row.cancellation_requested, result, error_code: row.error_code, output_bytes: row.output_bytes,
    created_at: row.created_at, updated_at: row.updated_at, started_at: row.started_at, finished_at: row.finished_at,
    dispatched_at: row.dispatched_at, heartbeat_at: row.heartbeat_at, lease_until: row.lease_until };
  if (row.event_cursor !== undefined) { if (!number(row.event_cursor)) return null; job.event_cursor = row.event_cursor; }
  return job;
}
export function projectJobEnvelope(value: unknown, workspace: string, operation: string, jobId?: string, principal?: string): JobEnvelope | null {
  const row = object(value), selected = scope(value, workspace, operation, principal);
  if (!row || !selected) return null;
  const job = projectJob(row.job, selected, jobId);
  return job ? { ...selected, job } : null;
}

export function projectEvents(value: unknown, workspace: string, operation: string, jobId: string, after = 0, principal?: string): EventEnvelope | null {
  const row = object(value), selected = scope(value, workspace, operation, principal);
  if (!row || !selected || !Array.isArray(row.items) || row.items.length > 100 || !number(row.next_cursor) ||
      row.next_cursor < after || typeof row.has_more !== "boolean") return null;
  const items: WorkbenchEvent[] = []; let sequence = after;
  for (const value of row.items) {
    const event = object(value), raw = event && object(event.data);
    if (!event || !raw || !number(event.sequence) || event.sequence <= sequence || !identifier(event.id, UUID) || event.job_id !== jobId ||
        !identifier(event.event_type) || !String(event.event_type).startsWith("job.") || !time(event.recorded_at)) return null;
    const data: WorkbenchEvent["data"] = {};
    if (raw.text !== undefined) { if (!boundedText(raw.text, 65_536)) return null; data.text = raw.text; }
    for (const key of ["provider", "role", "stage"] as const) { if (raw[key] !== undefined) { if (!identifier(raw[key])) return null; data[key] = raw[key]; } }
    if (raw.model !== undefined) { if (!identifier(raw.model, MODEL)) return null; data.model = raw.model; }
    const eventJob = object(raw.job), progress = eventJob && object(eventJob.progress);
    if (progress?.stage !== undefined) { if (!identifier(progress.stage)) return null; data.stage = progress.stage; }
    items.push({ sequence: event.sequence, id: event.id, job_id: jobId, event_type: event.event_type, recorded_at: event.recorded_at, data });
    sequence = event.sequence;
  }
  if (row.next_cursor !== sequence || (row.has_more && !items.length)) return null;
  return { ...selected, items, next_cursor: row.next_cursor, has_more: row.has_more };
}

export function projectResult(value: unknown, workspace: string, operation: string, jobId: string, principal?: string): ResultEnvelope | null {
  const row = object(value), job = projectJobEnvelope(value, workspace, operation, jobId, principal);
  if (!row || !job || !boundedText(row.output, 262_144) || typeof row.truncated !== "boolean" ||
      !(row.artifactText === null || boundedText(row.artifactText, 262_144)) || typeof row.artifactTruncated !== "boolean") return null;
  const artifact = row.artifact === null ? null : projectArtifactMetadata(row.artifact);
  if ((row.artifact !== null && !artifact) || (!artifact && (row.artifactText !== null || row.artifactTruncated))) return null;
  return { ...job, output: row.output, truncated: row.truncated, artifact, artifactText: row.artifactText, artifactTruncated: row.artifactTruncated };
}
function projectArtifactMetadata(value: unknown): Artifact | null {
  const row = object(value);
  if (!row || !identifier(row.id, UUID) || !identifier(row.sha256, SHA256) || !number(row.bytes) ||
      !boundedText(row.mediaType, 127) || !/^[a-z0-9.+-]+\/[a-z0-9.+-]+(?:;[ -~]*)?$/i.test(row.mediaType)) return null;
  return { id: row.id, sha256: row.sha256, bytes: row.bytes, mediaType: row.mediaType };
}
export function artifactBytes(encoded: string): Uint8Array {
  if (encoded.length > 349_528 || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(encoded)) throw new Error("Invalid artifact range encoding");
  const binary = atob(encoded); if (btoa(binary) !== encoded) throw new Error("Noncanonical artifact range encoding");
  return Uint8Array.from(binary, character => character.charCodeAt(0));
}
export function projectArtifact(value: unknown, workspace: string, operation: string, offset: number, length: number, principal?: string): ArtifactEnvelope | null {
  const row = object(value), selected = scope(value, workspace, operation, principal), artifact = row && projectArtifactMetadata(row.artifact);
  if (!row || !selected || !artifact || row.offset !== offset || !number(offset) || typeof row.dataBase64 !== "string" ||
      !(row.nextOffset === null || number(row.nextOffset))) return null;
  let bytes: Uint8Array; try { bytes = artifactBytes(row.dataBase64); } catch { return null; }
  const next = offset + bytes.length;
  if (bytes.length > length || next > artifact.bytes || (row.nextOffset === null ? next !== artifact.bytes : row.nextOffset !== next || !bytes.length || next >= artifact.bytes)) return null;
  return { ...selected, artifact, offset, dataBase64: row.dataBase64, nextOffset: row.nextOffset };
}

/** Exact user request; model and operation references always come from the selected contract. */
export type WorkbenchForm = { taskId: string; agentId: string; repositoryStateId: string; profileSha256: string; typeRef: string; value: string;
  provider: string; role: "reasoning" | "coding"; prompt: string; maxOutputTokens: string; timeoutSeconds: string };
export const EMPTY_FORM: WorkbenchForm = { taskId: "", agentId: "", repositoryStateId: "", profileSha256: "", typeRef: "", value: "{}",
  provider: "openai", role: "reasoning", prompt: "", maxOutputTokens: "2048", timeoutSeconds: "60" };

function canonicalRequestBytes(value: JobSpec): number {
  // Python canonical_json uses compact JSON, ensure_ascii=False and UTF-8.
  // Key ordering does not change length. Its small noninteger floats use
  // scientific notation with a two-digit minimum exponent (1e-07).
  let floatAdjustment = 0;
  const encoded = JSON.stringify(value, (_key, item: unknown) => {
    if (typeof item === "number" && !Number.isInteger(item) && Math.abs(item) < 0.0001) {
      const canonical = item.toExponential().replace(/e([+-])(\d)$/, "e$10$2");
      floatAdjustment += canonical.length - JSON.stringify(item).length;
    }
    return item;
  });
  return new TextEncoder().encode(encoded).length + floatAdjustment;
}

export function validateJobSpec(value: unknown, maxInputBytes = 16_384): JobSpec | null {
  const row = object(value), input = row && object(row.input);
  if (!row || !input || Object.keys(row).sort().join(",") !== "episode_id,input,job_type,selected_models" ||
      !identifier(row.episode_id, UUID) || !Array.isArray(row.selected_models) || !finiteJSON(input)) return null;
  if (row.job_type === "cog.execute") {
    if (Object.keys(input).sort().join(",") !== "agent_id,operation_ref,profile_sha256,repository_state_id,type_ref,value" || row.selected_models.length ||
        !identifier(input.agent_id, UUID) || !identifier(input.repository_state_id, UUID) || !identifier(input.profile_sha256, SHA256) ||
        typeof input.operation_ref !== "string" || !/^syn:\/\/thing\/[a-z0-9]+(?:[-_][a-z0-9]+)*$/.test(input.operation_ref) ||
        typeof input.type_ref !== "string" || input.type_ref.length > 270 || !/^syn(?:ny)?:\/\/type\/[a-z0-9]+(?:[-_][a-z0-9]+)*$/.test(input.type_ref)) return null;
    const request: JobSpec = { job_type: "cog.execute", episode_id: row.episode_id, input, selected_models: [] };
    return canonicalRequestBytes(request) <= maxInputBytes ? request : null;
  }
  if (row.job_type !== "model.generate" || row.selected_models.length !== 1 ||
      Object.keys(input).sort().join(",") !== "max_output_tokens,prompt,timeout_seconds" || !boundedText(input.prompt, maxInputBytes) || !input.prompt.trim() ||
      !number(input.max_output_tokens, 131_072) || input.max_output_tokens < 1 || !number(input.timeout_seconds, 3600) || input.timeout_seconds < 1) return null;
  const rawSelection = object(row.selected_models[0]);
  if (!rawSelection || Object.keys(rawSelection).sort().join(",") !== "model,provider,role") return null;
  const model = selection(rawSelection); if (!model || !PROVIDERS.includes(model.provider as typeof PROVIDERS[number])) return null;
  const request: JobSpec = { job_type: "model.generate", episode_id: row.episode_id, input, selected_models: [model] };
  return canonicalRequestBytes(request) <= maxInputBytes ? request : null;
}
export function buildJobSpec(contract: OperationContract, form: WorkbenchForm): JobSpec {
  if (!contract.supported || !contract.jobType) throw new Error("This operation has no supported execution contract.");
  if (!UUID.test(form.taskId.trim())) throw new Error("Enter a canonical UUID for an existing active task.");
  let request: unknown;
  if (contract.jobType === "cog.execute") {
    let value: unknown; try { value = JSON.parse(form.value); } catch { throw new Error("Typed input must be valid JSON."); }
    request = { job_type: "cog.execute", episode_id: form.taskId.trim(), selected_models: [], input: {
      agent_id: form.agentId.trim(), repository_state_id: form.repositoryStateId.trim(), operation_ref: contract.executionReference,
      profile_sha256: form.profileSha256.trim(), type_ref: form.typeRef.trim(), value } };
  } else {
    request = { job_type: "model.generate", episode_id: form.taskId.trim(), selected_models: [{ provider: form.provider,
      model: contract.reference.replace(/^model:/, ""), role: form.role }], input: { prompt: form.prompt,
      max_output_tokens: /^[1-9]\d*$/.test(form.maxOutputTokens) ? Number(form.maxOutputTokens) : -1,
      timeout_seconds: /^[1-9]\d*$/.test(form.timeoutSeconds) ? Number(form.timeoutSeconds) : -1 } };
  }
  const result = validateJobSpec(request, contract.maxInputBytes);
  if (!result) throw new Error(contract.jobType === "cog.execute" ? "Check the agent, repository UUID, profile hash and Syn type reference; the input must fit the contract." : "Check the provider, prompt and positive output/time limits; the input must fit the contract.");
  return result;
}
export function terminal(state: JobState): boolean { return ["succeeded", "failed", "cancelled", "indeterminate"].includes(state); }
export function checkUsable(job: WorkbenchJob | null, operation: string, now = Date.now()): boolean {
  const report = job?.result?.operation_check;
  return Boolean(job?.job_type === "operation.check" && job.state === "succeeded" && report?.eligible && report.operationId === operation &&
    report.bindings.contractRevisionId === job.contract_revision_id && Date.parse(report.checkedAt) <= now && Date.parse(report.expiresAt) > now);
}
export type MutationAttempt = { path: string; body: string; key: string };
export function mutationAttempt(path: string, body: unknown, prior: MutationAttempt | null = null): MutationAttempt {
  const encoded = JSON.stringify(body);
  if (prior) {
    if (prior.path !== path || prior.body !== encoded) throw new Error("Resolve the pending request before changing its payload.");
    return prior;
  }
  return { path, body: encoded, key: "workbench:" + crypto.randomUUID() };
}

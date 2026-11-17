/** Per-tab recovery of user-initiated submissions. Reading a receipt never retries it. */
import { EMPTY_FORM, SHA256, UUID, validateJobSpec, type JobSpec, type MutationAttempt, type WorkbenchForm } from "./operation-workbench.ts";

type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem">;
export type PendingSubmission = { attempt: MutationAttempt; request: JobSpec | null; jobId: string | null };
const memory = new Map<string, string>();
const prefix = "potbot:operation-pending:v1:";

export function submissionKey(workspace: string, principal: string, operation: string): string {
  if (!UUID.test(workspace) || !UUID.test(principal) || !SHA256.test(operation)) throw new Error("Invalid submission scope");
  return prefix + workspace + ":" + principal + ":" + operation;
}

export function parsePendingSubmission(encoded: string | null, operation: string): PendingSubmission | null {
  if (!encoded || encoded.length > 131_072 || !SHA256.test(operation)) return null;
  try {
    const attempt = JSON.parse(encoded) as MutationAttempt;
    if (!attempt || Object.keys(attempt).sort().join(",") !== "body,key,path" || typeof attempt.path !== "string" ||
        typeof attempt.body !== "string" || new TextEncoder().encode(attempt.body).length > 65_536 ||
        typeof attempt.key !== "string" || !/^[A-Za-z0-9_.:-]{1,200}$/.test(attempt.key)) return null;
    const body: unknown = JSON.parse(attempt.body), base = `/api/ui/operations/${operation}`;
    if (attempt.path === `${base}/checks`) {
      const request = validateJobSpec(body); return request ? { attempt, request, jobId: null } : null;
    }
    if (!body || typeof body !== "object" || Array.isArray(body)) return null;
    const row = body as Record<string, unknown>;
    if (attempt.path === `${base}/runs`) {
      const request = validateJobSpec(row.request);
      return Object.keys(row).sort().join(",") === "checkJobId,confirm,request" && row.confirm === true &&
        typeof row.checkJobId === "string" && UUID.test(row.checkJobId) && request ? { attempt, request, jobId: null } : null;
    }
    const path = attempt.path.slice(base.length), match = /^\/jobs\/([a-f0-9-]{36})\/cancel$/.exec(path);
    return attempt.path.startsWith(base) && match && UUID.test(match[1]) && Object.keys(row).join(",") === "confirm" && row.confirm === true ?
      { attempt, request: null, jobId: match[1] } : null;
  } catch { return null; }
}

export function readPendingSubmission(key: string, operation: string, storage?: StorageLike): PendingSubmission | null {
  let encoded = memory.get(key) ?? null;
  try { encoded ??= storage?.getItem(key) ?? null; } catch { /* Memory still covers navigation if browser storage is unavailable. */ }
  return parsePendingSubmission(encoded, operation);
}

export function retainSubmission(key: string, attempt: MutationAttempt, storage?: StorageLike): void {
  const encoded = JSON.stringify(attempt); memory.set(key, encoded);
  try { storage?.setItem(key, encoded); } catch { /* A visible recovery key and in-memory copy remain available. */ }
}

export function clearSubmission(key: string, requestKey: string, storage?: StorageLike): void {
  // A late response can never clear a newer attempt in the same operation scope.
  const matches = (value: string | null | undefined) => { try { return value ? JSON.parse(value).key === requestKey : false; } catch { return false; } };
  if (matches(memory.get(key))) memory.delete(key);
  try { if (matches(storage?.getItem(key))) storage?.removeItem(key); } catch { /* Server receipt has already been established. */ }
}

export function formFromRequest(request: JobSpec): WorkbenchForm {
  const form: WorkbenchForm = { ...EMPTY_FORM, taskId: request.episode_id };
  if (request.job_type === "cog.execute") return { ...form, agentId: String(request.input.agent_id), repositoryStateId: String(request.input.repository_state_id),
    profileSha256: String(request.input.profile_sha256), typeRef: String(request.input.type_ref), value: JSON.stringify(request.input.value, null, 2) };
  return { ...form, provider: request.selected_models[0].provider, role: request.selected_models[0].role, prompt: String(request.input.prompt),
    maxOutputTokens: String(request.input.max_output_tokens), timeoutSeconds: String(request.input.timeout_seconds) };
}

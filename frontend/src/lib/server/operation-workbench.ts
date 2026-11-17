/** Fixed, authenticated operation routes. No client controls an upstream path or setup credential. */
import { UUID, SHA256, projectArtifact, projectContract, projectEvents, projectJobEnvelope, projectResult, validateJobSpec, type JobSpec } from "../operation-workbench.ts";
import { potbotConfigForRequest, upstream, upstreamFailure } from "./potbot.ts";
import { requireOperator, safeError, validOrigin } from "./security.ts";

export async function workbenchJSON(response: Request | Response, maximum = 1_048_576): Promise<unknown> {
  if (!response.body) throw new Error("Missing workbench body");
  const reader = response.body.getReader(), chunks: Uint8Array[] = []; let size = 0;
  try {
    while (true) {
      const part = await reader.read(); if (part.done) break;
      size += part.value.byteLength;
      if (size > maximum) { await reader.cancel(); throw new Error("Workbench body exceeds bound"); }
      chunks.push(part.value);
    }
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
}

type Route = { id: string; action: string; jobId?: string };
function object(value: unknown): Record<string, unknown> | null { return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null; }

export async function operationWorkbenchRoute(request: Request, route: Route): Promise<Response> {
  const operator = requireOperator(request); if (operator instanceof Response) return operator;
  const mutation = request.method === "POST";
  if (mutation && !validOrigin(request, operator)) return safeError("Untrusted operation origin.", 403);
  const { id, action, jobId } = route;
  const allowed = jobId ? (mutation ? ["cancel"] : ["inspect", "events", "result", "artifact"]) : (mutation ? ["checks", "runs"] : ["contract"]);
  if (!["GET", "POST"].includes(request.method) || !SHA256.test(id) || (jobId !== undefined && !UUID.test(jobId)) || !allowed.includes(action))
    return safeError("Unsupported operation workbench route.", 400);
  const config = potbotConfigForRequest(request); if (!config) return safeError("Workspace backend unavailable.", 503);
  const query = new URL(request.url).searchParams;
  let suffix = "", after = 0, length = 262_144;
  if (action === "events") {
    if ([...query.keys()].some(key => !["after", "limit"].includes(key) || query.getAll(key).length !== 1)) return safeError("Invalid event cursor.", 400);
    const cursor = query.get("after") ?? "0", limit = query.get("limit") ?? "100";
    if (!/^(0|[1-9]\d{0,15})$/.test(cursor) || !Number.isSafeInteger(Number(cursor)) || !/^[1-9]\d{0,2}$/.test(limit) || Number(limit) > 100)
      return safeError("Invalid event cursor.", 400);
    after = Number(cursor); suffix = `?after=${after}&limit=${Number(limit)}`;
  } else if (action === "artifact") {
    if ([...query.keys()].some(key => !["offset", "length"].includes(key) || query.getAll(key).length !== 1)) return safeError("Invalid artifact range.", 400);
    const offset = query.get("offset") ?? "0", size = query.get("length") ?? "262144";
    if (!/^(0|[1-9]\d{0,15})$/.test(offset) || !Number.isSafeInteger(Number(offset)) || !/^[1-9]\d{0,5}$/.test(size) || Number(size) > 262_144)
      return safeError("Invalid artifact range.", 400);
    after = Number(offset); length = Number(size); suffix = `?offset=${after}&length=${length}`;
  } else if (query.size) return safeError("Unexpected operation query.", 400);
  const path = `/v1/ui/operations/${id}` + (jobId ? `/jobs/${jobId}${action === "inspect" ? "" : "/" + action}` : "/" + action) + suffix;
  const headers: Record<string, string> = {};
  let body: string | undefined, expectedSpec: JobSpec | undefined, expectedCheck: string | undefined;
  if (mutation) {
    const key = request.headers.get("Idempotency-Key");
    if (!key || !/^[A-Za-z0-9_.:-]{1,200}$/.test(key) || request.headers.get("content-type")?.split(";")[0] !== "application/json")
      return safeError("A stable request key and JSON body are required.", 400);
    if (action !== "cancel") {
      const token = process.env.POTBOT_UI_SETUP_TOKEN;
      if (!token || !/^[!-~]{32,4096}$/.test(token)) return safeError("Operator setup permission is unavailable.", 503);
      headers["X-Potbot-Setup-Token"] = token;
    }
    let raw: unknown; try { raw = await workbenchJSON(request, 65_536); } catch { return safeError("Invalid bounded operation request.", 400); }
    if (action === "checks") {
      const spec = validateJobSpec(raw); if (!spec) return safeError("The readiness request does not match a supported job contract.", 400);
      expectedSpec = spec; body = JSON.stringify(spec);
    } else if (action === "runs") {
      const value = object(raw), spec = value && validateJobSpec(value.request);
      if (!value || Object.keys(value).sort().join(",") !== "checkJobId,confirm,request" || value.confirm !== true ||
          typeof value.checkJobId !== "string" || !UUID.test(value.checkJobId) || !spec) return safeError("An exact checked request and explicit launch confirmation are required.", 400);
      expectedSpec = spec; expectedCheck = value.checkJobId;
      body = JSON.stringify({ checkJobId: value.checkJobId, request: spec, confirm: true });
    } else {
      const value = object(raw);
      if (!value || Object.keys(value).join(",") !== "confirm" || value.confirm !== true) return safeError("Explicit cancellation confirmation is required.", 400);
      body = JSON.stringify({ confirm: true });
    }
    Object.assign(headers, { "Idempotency-Key": key, "Content-Type": "application/json", Origin: operator.publicOrigin });
  }
  try {
    const response = await upstream(config, path, { method: request.method, headers,
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(8000)]), ...(body === undefined ? {} : { body }) });
    if (!response.ok) {
      if (response.status === 409) return safeError("The request, task, check or runtime binding changed. Refresh and check again.", 409);
      if (response.status === 400) return safeError("The operation request was rejected. Review its task and typed input.", 400);
      return upstreamFailure(response);
    }
    const raw = await workbenchJSON(response);
    const projected = action === "contract" ? projectContract(raw, config.workspaceId, id) :
      action === "artifact" ? projectArtifact(raw, config.workspaceId, id, after, length) :
      action === "events" ? projectEvents(raw, config.workspaceId, id, jobId!, after) :
      action === "result" ? projectResult(raw, config.workspaceId, id, jobId!) : projectJobEnvelope(raw, config.workspaceId, id, jobId);
    if (!projected) return safeError("The operation service returned an invalid scoped response.", 502);
    if (expectedSpec && (!("job" in projected) || projected.job.episode_id !== expectedSpec.episode_id ||
        projected.job.job_type !== (action === "checks" ? "operation.check" : expectedSpec.job_type) ||
        projected.job.operation_check_id !== (expectedCheck ?? null) ||
        JSON.stringify(projected.job.selected_models) !== JSON.stringify(action === "checks" ? [] : expectedSpec.selected_models)))
      return safeError("The operation service returned a different request binding.", 502);
    return Response.json(projected, { status: ["checks", "runs"].includes(action) ? 202 : 200, headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("The operation response is unavailable. Preserve the request key before retrying a submission.", 503); }
}

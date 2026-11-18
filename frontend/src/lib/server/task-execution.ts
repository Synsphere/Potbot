/** Fixed selected-operation routes; launch consumes the retained checked binding. */
import { UUID, MAX_TASK_EXECUTION_REQUEST, MAX_TASK_EXECUTION_RESPONSE, validateTaskExecutionMutation, projectTaskExecutionMutation, projectTaskExecutionContext, projectTaskExecutionJobs, projectTaskExecutionJob, projectTaskExecutionEvents, projectTaskExecutionResult, projectTaskExecutionArtifact, type TaskExecutionEnvelope } from "../task-execution.ts";
import { taskDiscoveryJSON } from "./task-discovery.ts";
import { potbotConfigForRequest, upstream, upstreamFailure } from "./potbot.ts";
import { requireOperator, safeError, validOrigin } from "./security.ts";

export async function taskExecutionRoute(request: Request, path: string[]): Promise<Response> {
  const operator = requireOperator(request); if (operator instanceof Response) return operator; const mutation = request.method === "POST";
  if ((mutation || request.headers.has("origin")) && !validOrigin(request, operator)) return safeError("Untrusted selected execution origin.", 403);
  if (!["GET", "POST"].includes(request.method) || !Array.isArray(path) || !path.every(value => typeof value === "string") || path[0] !== "selections" || !UUID.test(path[1] ?? "")) return safeError("Unsupported selected execution route.", 400);
  const selection = path[1], action = path[2], jobId = path[3], tail = path[4];
  const context = !mutation && path.length === 3 && action === "context", list = !mutation && path.length === 3 && action === "jobs";
  const job = action === "jobs" && UUID.test(jobId ?? "") && (path.length === 4 && !mutation || path.length === 5 && (mutation ? tail === "cancel" : ["events", "result", "artifact"].includes(tail)));
  const decision = mutation && path.length === 3 && ["checks", "runs"].includes(action);
  if (!context && !list && !job && !decision) return safeError("Unsupported selected execution route.", 400);
  const config = potbotConfigForRequest(request); if (!config) return safeError("Selected execution backend unavailable.", 503);
  const query = new URL(request.url).searchParams, forwarded = new URLSearchParams(), allowed = list ? ["limit", "after"] : !mutation && tail === "events" ? ["after", "limit"] : !mutation && tail === "artifact" ? ["offset", "length"] : [];
  if (query.toString().length > 4096 || [...query.keys()].some(key => !allowed.includes(key) || query.getAll(key).length !== 1)) return safeError("Unexpected selected execution query.", 400);
  let after = 0, length = 262144;
  const count = (value: string, minimum: number, maximum: number) => /^(?:0|[1-9]\d{0,15})$/.test(value) && Number.isSafeInteger(Number(value)) && Number(value) >= minimum && Number(value) <= maximum;
  if (list) { const limit = query.get("limit") ?? "25", cursor = query.get("after"); if (!count(limit, 1, 25) || cursor !== null && (cursor.length < 1 || cursor.length > 512 || /[\u0000-\u0020\u007f]/.test(cursor))) return safeError("Invalid selected job history cursor.", 400); forwarded.set("limit", limit); if (cursor !== null) forwarded.set("after", cursor); }
  else if (tail === "events") { const cursor = query.get("after") ?? "0", limit = query.get("limit") ?? "100"; if (!count(cursor, 0, Number.MAX_SAFE_INTEGER) || !count(limit, 1, 100)) return safeError("Invalid selected job event range.", 400); after = Number(cursor); forwarded.set("after", cursor); forwarded.set("limit", limit); }
  else if (tail === "artifact") { const offset = query.get("offset") ?? "0", size = query.get("length") ?? "262144"; if (!count(offset, 0, Number.MAX_SAFE_INTEGER) || !count(size, 1, 262144)) return safeError("Invalid selected artifact range.", 400); after = Number(offset); length = Number(size); forwarded.set("offset", offset); forwarded.set("length", size); }
  const headers: Record<string, string> = {}; let body: Record<string, unknown> | undefined;
  if (mutation) {
    const key = request.headers.get("Idempotency-Key"); if (!key || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(key) || request.headers.get("content-type")?.split(";")[0] !== "application/json") return safeError("An exact JSON body and stable command key are required.", 400);
    if (tail !== "cancel") { const token = process.env.POTBOT_UI_SETUP_TOKEN; if (!token || !/^[!-~]{32,4096}$/.test(token)) return safeError("Operator setup permission is unavailable.", 503); headers["X-Potbot-Setup-Token"] = token; }
    let raw: unknown; try { raw = await taskDiscoveryJSON(request, MAX_TASK_EXECUTION_REQUEST); } catch { return safeError("Selected execution requires bounded unambiguous JSON.", 400); }
    const parsed = validateTaskExecutionMutation(path, raw); if (!parsed) return safeError("Review the exact selected task, checked input and declared assertions.", 400); body = parsed;
    Object.assign(headers, { "Content-Type": "application/json", "Idempotency-Key": key, Origin: operator.publicOrigin });
  }
  try {
    const response = await upstream(config, "/v1/ui/task-execution/" + path.join("/") + (forwarded.size ? "?" + forwarded : ""), { method: request.method, headers, ...(body ? { body: JSON.stringify(body) } : {}), signal: AbortSignal.any([request.signal, AbortSignal.timeout(mutation ? 30000 : 15000)]) });
    if (!response.ok) { if (response.status === 409) return safeError("The selection, task, request or checked runtime changed. Read current context and inspect the retained job.", 409); if (response.status === 400) return safeError("The exact selected execution request was rejected.", 400); return upstreamFailure(response); }
    const raw = await taskDiscoveryJSON(response, MAX_TASK_EXECUTION_RESPONSE); let result: TaskExecutionEnvelope | null;
    if (mutation) result = projectTaskExecutionMutation(raw, config.workspaceId, path, body!);
    else if (context) result = projectTaskExecutionContext(raw, config.workspaceId, selection);
    else if (list) result = projectTaskExecutionJobs(raw, config.workspaceId, selection);
    else if (tail === "events") result = projectTaskExecutionEvents(raw, config.workspaceId, selection, jobId, after);
    else if (tail === "result") result = projectTaskExecutionResult(raw, config.workspaceId, selection, jobId);
    else if (tail === "artifact") result = projectTaskExecutionArtifact(raw, config.workspaceId, selection, jobId, after, length);
    else result = projectTaskExecutionJob(raw, config.workspaceId, selection, jobId);
    if (!result) return safeError("Selected execution returned an invalid scoped response.", 502);
    if (list && "nextCursor" in result && (result.items.length > Number(forwarded.get("limit")) || query.has("after") && result.nextCursor === query.get("after"))) return safeError("Selected job history did not advance safely.", 502);
    if (tail === "events" && "next_cursor" in result && result.items.length > Number(forwarded.get("limit"))) return safeError("Selected job events exceeded their requested range.", 502);
    return Response.json(result, { status: mutation && [201, 202].includes(response.status) ? response.status : 200, headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("Selected execution is unavailable. Preserve an uncertain command’s exact bytes and key before retrying.", 503); }
}

/** Fixed task ranking routes; local embedding inference remains a native command. */
import { UUID, SHA256, MAX_TASK_DISCOVERY_REQUEST, MAX_TASK_DISCOVERY_RESPONSE, parseMcpJSON, validateTaskMutation, projectTaskMutation, projectTaskSearches, projectTaskEnvelope } from "../task-discovery.ts";
import { potbotConfigForRequest, upstream, upstreamFailure } from "./potbot.ts";
import { requireOperator, safeError, validOrigin } from "./security.ts";
export async function taskDiscoveryJSON(response: Request | Response, maximum: number): Promise<unknown> {
  if (!response.body) throw new Error("Missing JSON body"); const reader = response.body.getReader(), parts: Uint8Array[] = []; let size = 0;
  try { while (true) { const part = await reader.read(); if (part.done) break; size += part.value.length; if (size > maximum) { await reader.cancel(); throw new Error("JSON byte limit exceeded"); } parts.push(part.value); } } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(size); let offset = 0; for (const part of parts) { bytes.set(part, offset); offset += part.length; } return parseMcpJSON(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
}
export async function taskDiscoveryRoute(request: Request, path: string[]): Promise<Response> {
  const operator = requireOperator(request); if (operator instanceof Response) return operator; const mutation = request.method === "POST";
  if ((mutation || request.headers.has("origin")) && !validOrigin(request, operator)) return safeError("Untrusted task discovery origin.", 403);
  if (!Array.isArray(path) || !path.every(part => typeof part === "string") || !["GET", "POST"].includes(request.method)) return safeError("Unsupported task discovery route.", 400);
  const context = !mutation && path.length === 1 && path[0] === "context", list = !mutation && path.length === 1 && path[0] === "searches";
  const detail = !mutation && path.length === 2 && ["searches", "results", "selections"].includes(path[0]) && UUID.test(path[1]);
  const candidate = !mutation && path.length === 4 && path[0] === "searches" && UUID.test(path[1]) && path[2] === "candidates" && SHA256.test(path[3]);
  const action = mutation && (path.length === 1 && path[0] === "searches" || path.length === 3 && path[0] === "searches" && UUID.test(path[1]) && ["cancel", "select"].includes(path[2]));
  if (!context && !list && !detail && !candidate && !action) return safeError("Unsupported task discovery route.", 400);
  const config = potbotConfigForRequest(request); if (!config) return safeError("Task discovery backend unavailable.", 503);
  const query = new URL(request.url).searchParams, forwarded = new URLSearchParams(), allowed = context ? ["episodeId", "stateId"] : list ? ["episodeId", "limit", "after"] : [];
  if (query.toString().length > 4096 || [...query.keys()].some(key => !allowed.includes(key) || query.getAll(key).length !== 1)) return safeError("Unexpected task discovery query.", 400);
  if (context && (!UUID.test(query.get("episodeId") ?? "") || !UUID.test(query.get("stateId") ?? ""))) return safeError("An exact task and repository state are required.", 400);
  if (list) { const limit = query.get("limit") ?? "25", after = query.get("after"), episode = query.get("episodeId"); if (!/^(?:[1-9]|1\d|2[0-5])$/.test(limit) || after !== null && !UUID.test(after) || episode !== null && !UUID.test(episode)) return safeError("Invalid bounded search history.", 400); forwarded.set("limit", limit); }
  for (const key of allowed) if (query.has(key)) forwarded.set(key, query.get(key)!);
  const headers: Record<string, string> = {}; let body: Record<string, unknown> | undefined;
  if (mutation) {
    const key = request.headers.get("Idempotency-Key"), token = process.env.POTBOT_UI_SETUP_TOKEN;
    if (!key || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(key) || request.headers.get("content-type")?.split(";")[0] !== "application/json") return safeError("An exact JSON body and stable request key are required.", 400);
    if (!token || !/^[!-~]{32,4096}$/.test(token)) return safeError("Operator setup permission is unavailable.", 503);
    let raw: unknown; try { raw = await taskDiscoveryJSON(request, MAX_TASK_DISCOVERY_REQUEST); } catch { return safeError("Task discovery requires bounded unambiguous JSON.", 400); }
    const parsed = validateTaskMutation(path, raw); if (!parsed) return safeError("Review the exact task, constraints and current identity before confirming.", 400);
    body = parsed; Object.assign(headers, { "Content-Type": "application/json", "Idempotency-Key": key, "X-Potbot-Setup-Token": token, Origin: operator.publicOrigin });
  }
  try {
    const suffix = forwarded.size ? "?" + forwarded : "", response = await upstream(config, "/v1/ui/task-discovery/" + path.join("/") + suffix, { method: request.method, headers, ...(body ? { body: JSON.stringify(body) } : {}), signal: AbortSignal.any([request.signal, AbortSignal.timeout(mutation ? 30000 : 15000)]) });
    if (!response.ok) { if (response.status === 409) return safeError("Task, sources or reviewed evidence changed. Read current context before a new decision.", 409); if (response.status === 400) return safeError("The exact task discovery request was rejected.", 400); return upstreamFailure(response); }
    const raw = await taskDiscoveryJSON(response, MAX_TASK_DISCOVERY_RESPONSE), kind = context ? "context" : candidate ? "candidate" : path[0] === "searches" ? "search" : path[0] === "results" ? "result" : "selection";
    const result = mutation ? projectTaskMutation(raw, config.workspaceId, path, body!) : list ? projectTaskSearches(raw, config.workspaceId, undefined, query.get("episodeId") ?? undefined) : projectTaskEnvelope(raw, config.workspaceId, kind, candidate ? path[3] : detail ? path[1] : undefined);
    if (!result) return safeError("Task discovery returned an invalid scoped response.", 502);
    if (context && "context" in result && (result.context.taskBinding.episodeId !== query.get("episodeId") || result.context.taskBinding.repositoryStateId !== query.get("stateId"))) return safeError("Task context differs from the requested existing state.", 502);
    if (list && "items" in result && (result.items.length > Number(forwarded.get("limit")) || query.has("after") && (result.nextCursor === query.get("after") || result.items.some(item => item.id <= query.get("after")!)))) return safeError("Search history differs from its bounded cursor.", 502);
    return Response.json(result, { status: mutation && [201, 202].includes(response.status) ? response.status : 200, headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("Task discovery is unavailable. Retain the exact command before retrying an uncertain change.", 503); }
}

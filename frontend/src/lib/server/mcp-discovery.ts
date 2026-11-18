/** Fixed retained-catalogue routes. Browser requests never supply endpoints or native commands. */
import { UUID, MCP_KINDS, MAX_MCP_REQUEST, MAX_MCP_RESPONSE, parseMcpJSON, validateMcpMutation, projectMcpMutation,
  projectMcpBindings, projectMcpBindingEnvelope, projectMcpEntries, projectMcpAttempts, projectMcpAttemptEnvelope, projectMcpReferenceEnvelope } from "../mcp-discovery.ts";
import { potbotConfigForRequest, upstream, upstreamFailure } from "./potbot.ts";
import { requireOperator, safeError, validOrigin } from "./security.ts";

async function mcpJSON(response: Request | Response, maximum: number): Promise<unknown> {
  if (!response.body) throw new Error("Missing MCP JSON body");
  const reader = response.body.getReader(), chunks: Uint8Array[] = []; let size = 0;
  try {
    while (true) { const part = await reader.read(); if (part.done) break; size += part.value.byteLength;
      if (size > maximum) { await reader.cancel(); throw new Error("MCP JSON exceeds bound"); } chunks.push(part.value); }
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return parseMcpJSON(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
}

export async function mcpDiscoveryRoute(request: Request, path: string[]): Promise<Response> {
  const operator = requireOperator(request); if (operator instanceof Response) return operator;
  const mutation = request.method === "POST";
  if ((mutation || request.headers.has("origin")) && !validOrigin(request, operator)) return safeError("Untrusted MCP discovery origin.", 403);
  if (!Array.isArray(path) || !path.every(part => typeof part === "string") || !["GET", "POST"].includes(request.method)) return safeError("Unsupported MCP discovery route.", 400);
  const list = !mutation && path.length === 1 && ["bindings", "attempts"].includes(path[0]);
  const detail = !mutation && path.length === 2 && ["bindings", "attempts"].includes(path[0]) && UUID.test(path[1]);
  const entries = !mutation && path.length === 3 && path[0] === "bindings" && UUID.test(path[1]) && path[2] === "entries";
  const reference = !mutation && path.length === 5 && path[0] === "bindings" && UUID.test(path[1]) && path[2] === "entries" && UUID.test(path[3]) && path[4] === "reference";
  const action = mutation && path.length === 3 && UUID.test(path[1]) && (path[0] === "bindings" && ["enable", "disable", "revoke", "reconcile"].includes(path[2]) || path[0] === "attempts" && path[2] === "cancel");
  if (!list && !detail && !entries && !reference && !action) return safeError("Unsupported MCP discovery route.", 400);
  const config = potbotConfigForRequest(request); if (!config) return safeError("Workspace MCP discovery backend unavailable.", 503);
  const query = new URL(request.url).searchParams, forwarded = new URLSearchParams();
  const allowed = list || entries ? ["limit", "after", ...(entries ? ["kind", "q"] : path[0] === "attempts" ? ["bindingId"] : [])] : [];
  if ([...query.keys()].some(key => !allowed.includes(key) || query.getAll(key).length !== 1)) return safeError("Unexpected MCP discovery query.", 400);
  if (list || entries) {
    const limit = query.get("limit") ?? "25", after = query.get("after"), kind = query.get("kind"), q = query.get("q"), bindingId = query.get("bindingId");
    if (!/^(?:[1-9]|[1-4]\d|50)$/.test(limit) || after !== null && !UUID.test(after) || bindingId !== null && !UUID.test(bindingId) || kind !== null && !MCP_KINDS.includes(kind as typeof MCP_KINDS[number]) || q !== null && (Array.from(q).length > 256 || /[\u0000-\u001f\u007f]/.test(q))) return safeError("Invalid bounded MCP discovery filter or cursor.", 400);
    forwarded.set("limit", limit); for (const key of ["after", "kind", "q", "bindingId"]) if (query.has(key)) forwarded.set(key, query.get(key)!);
  }
  const headers: Record<string, string> = {}; let body: Record<string, unknown> | undefined;
  if (mutation) {
    const key = request.headers.get("Idempotency-Key"), token = process.env.POTBOT_UI_SETUP_TOKEN;
    if (!key || !/^[A-Za-z0-9_.:-]{1,128}$/.test(key) || request.headers.get("content-type")?.split(";", 1)[0] !== "application/json") return safeError("An exact JSON body and stable request key are required.", 400);
    if (!token || !/^[!-~]{32,4096}$/.test(token)) return safeError("Operator setup permission is unavailable.", 503);
    let value: unknown; try { value = await mcpJSON(request, MAX_MCP_REQUEST); } catch { return safeError("The MCP discovery request exceeds its bounded JSON contract.", 400); }
    const parsed = validateMcpMutation(path, value); if (!parsed) return safeError("An exact reviewed revision, digest and confirmation are required.", 400);
    body = parsed; Object.assign(headers, { "Content-Type": "application/json", "Idempotency-Key": key, "X-Potbot-Setup-Token": token, Origin: operator.publicOrigin });
  }
  try {
    const suffix = forwarded.size ? "?" + forwarded.toString() : "";
    const response = await upstream(config, "/v1/ui/mcp-discovery/" + path.join("/") + suffix, { method: request.method, headers, ...(body ? { body: JSON.stringify(body) } : {}), signal: AbortSignal.any([request.signal, AbortSignal.timeout(15_000)]) });
    if (!response.ok) {
      if (response.status === 409) return safeError("The MCP binding, catalogue or attempt changed. Refresh and inspect its current exact revision.", 409);
      if (response.status === 400) return safeError("The exact MCP discovery request was rejected.", 400);
      return upstreamFailure(response);
    }
    const raw = await mcpJSON(response, MAX_MCP_RESPONSE);
    const result = mutation ? projectMcpMutation(raw, config.workspaceId, path, body) : reference ? projectMcpReferenceEnvelope(raw, config.workspaceId, path[1], path[3]) :
      entries ? projectMcpEntries(raw, config.workspaceId, path[1]) : detail ? path[0] === "bindings" ? projectMcpBindingEnvelope(raw, config.workspaceId, path[1]) : projectMcpAttemptEnvelope(raw, config.workspaceId, path[1]) :
        path[0] === "bindings" ? projectMcpBindings(raw, config.workspaceId) : projectMcpAttempts(raw, config.workspaceId, undefined, query.get("bindingId") ?? undefined);
    if (!result) return safeError("The MCP discovery service returned an invalid scoped response.", 502);
    if ("items" in result && (result.items.length > Number(forwarded.get("limit")) || query.has("after") && result.items.some(item => item.id <= query.get("after")!))) return safeError("MCP pagination differs from the requested bounded cursor.", 502);
    if (entries && query.has("kind") && "items" in result && result.items.some(item => !("kind" in item) || item.kind !== query.get("kind"))) return safeError("MCP entries differ from the selected interface kind.", 502);
    return Response.json(result, { status: mutation && [201, 202].includes(response.status) ? response.status : 200, headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("MCP discovery is unavailable. Retain the exact request before retrying an uncertain change.", 503); }
}

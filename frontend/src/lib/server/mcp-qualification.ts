/** Fixed retained-evidence routes. Native execution and receipt recovery remain host CLI actions. */
import { UUID, MAX_QUALIFICATION_REQUEST, MAX_QUALIFICATION_RESPONSE, parseMcpJSON, validateQualificationMutation, projectQualificationMutation,
  projectQualificationSuites, projectQualificationSuiteEnvelope, projectQualificationAttempts, projectQualificationAttemptEnvelope, projectQualificationReportEnvelope } from "../mcp-qualification.ts";
import { potbotConfigForRequest, upstream, upstreamFailure } from "./potbot.ts";
import { requireOperator, safeError, validOrigin } from "./security.ts";

async function qualificationJSON(response: Request | Response, maximum: number): Promise<unknown> {
  if (!response.body) throw new Error("Missing qualification JSON body");
  const reader = response.body.getReader(), chunks: Uint8Array[] = []; let size = 0;
  try { while (true) { const part = await reader.read(); if (part.done) break; size += part.value.byteLength;
    if (size > maximum) { await reader.cancel(); throw new Error("Qualification JSON exceeds bound"); } chunks.push(part.value); } }
  finally { reader.releaseLock(); }
  const bytes = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return parseMcpJSON(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
}

export async function mcpQualificationRoute(request: Request, path: string[]): Promise<Response> {
  const operator = requireOperator(request); if (operator instanceof Response) return operator;
  const mutation = request.method === "POST";
  if ((mutation || request.headers.has("origin")) && !validOrigin(request, operator)) return safeError("Untrusted MCP qualification origin.", 403);
  if (!Array.isArray(path) || !path.every(part => typeof part === "string") || !["GET", "POST"].includes(request.method)) return safeError("Unsupported MCP qualification route.", 400);
  const list = !mutation && path.length === 1 && ["suites", "attempts"].includes(path[0]);
  const detail = !mutation && path.length === 2 && ["suites", "attempts", "reports"].includes(path[0]) && UUID.test(path[1]);
  const action = mutation && path.length === 3 && UUID.test(path[1]) && (path[0] === "suites" && ["enable", "disable", "revoke", "qualify"].includes(path[2]) || path[0] === "attempts" && path[2] === "cancel");
  if (!list && !detail && !action) return safeError("Unsupported MCP qualification route.", 400);
  const config = potbotConfigForRequest(request); if (!config) return safeError("Workspace MCP qualification backend unavailable.", 503);
  const query = new URL(request.url).searchParams, forwarded = new URLSearchParams(), ownerKey = path[0] === "suites" ? "bindingId" : "suiteId";
  const allowed = list ? ["limit", "after", ownerKey] : [];
  if (query.toString().length > 4096 || [...query.keys()].some(key => !allowed.includes(key) || query.getAll(key).length !== 1)) return safeError("Unexpected MCP qualification query.", 400);
  if (list) {
    const limit = query.get("limit") ?? "25", after = query.get("after"), owner = query.get(ownerKey);
    if (!/^(?:[1-9]|[1-4]\d|50)$/.test(limit) || after !== null && !UUID.test(after) || owner !== null && !UUID.test(owner)) return safeError("Invalid bounded MCP qualification filter or cursor.", 400);
    forwarded.set("limit", limit); for (const key of ["after", ownerKey]) if (query.has(key)) forwarded.set(key, query.get(key)!);
  }
  const headers: Record<string, string> = {}; let body: Record<string, unknown> | undefined;
  if (mutation) {
    const key = request.headers.get("Idempotency-Key"), token = process.env.POTBOT_UI_SETUP_TOKEN;
    if (!key || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(key) || request.headers.get("content-type")?.split(";", 1)[0] !== "application/json") return safeError("An exact JSON body and stable request key are required.", 400);
    if (!token || !/^[!-~]{32,4096}$/.test(token)) return safeError("Operator setup permission is unavailable.", 503);
    let value: unknown; try { value = await qualificationJSON(request, MAX_QUALIFICATION_REQUEST); } catch { return safeError("The qualification request exceeds its bounded JSON contract.", 400); }
    const parsed = validateQualificationMutation(path, value); if (!parsed) return safeError("An exact reviewed revision, digest and confirmation are required.", 400);
    body = parsed; Object.assign(headers, { "Content-Type": "application/json", "Idempotency-Key": key, "X-Potbot-Setup-Token": token, Origin: operator.publicOrigin });
  }
  try {
    const suffix = forwarded.size ? "?" + forwarded.toString() : "";
    const response = await upstream(config, "/v1/ui/mcp-qualification/" + path.join("/") + suffix, { method: request.method, headers, ...(body ? { body: JSON.stringify(body) } : {}), signal: AbortSignal.any([request.signal, AbortSignal.timeout(15_000)]) });
    if (!response.ok) {
      if (response.status === 409) return safeError("The qualification suite, task binding or attempt changed. Refresh its current exact revision.", 409);
      if (response.status === 400) return safeError("The exact qualification request was rejected.", 400);
      return upstreamFailure(response);
    }
    const raw = await qualificationJSON(response, MAX_QUALIFICATION_RESPONSE);
    const result = mutation ? projectQualificationMutation(raw, config.workspaceId, path, body) : detail ? path[0] === "suites" ? projectQualificationSuiteEnvelope(raw, config.workspaceId, path[1]) :
      path[0] === "attempts" ? projectQualificationAttemptEnvelope(raw, config.workspaceId, path[1]) : projectQualificationReportEnvelope(raw, config.workspaceId, path[1]) :
      path[0] === "suites" ? projectQualificationSuites(raw, config.workspaceId, undefined, query.get("bindingId") ?? undefined) : projectQualificationAttempts(raw, config.workspaceId, undefined, query.get("suiteId") ?? undefined);
    if (!result) return safeError("The qualification service returned an invalid scoped response.", 502);
    if ("items" in result && (result.items.length > Number(forwarded.get("limit")) || query.has("after") && result.items.some(item => item.id <= query.get("after")!))) return safeError("Qualification pagination differs from the requested bounded cursor.", 502);
    return Response.json(result, { status: mutation && [201, 202].includes(response.status) ? response.status : 200, headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("MCP qualification is unavailable. Retain the exact request before retrying an uncertain change.", 503); }
}

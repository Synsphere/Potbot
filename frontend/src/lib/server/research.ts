/** Fixed workspace research routes. No provider URL or transport credential is caller supplied. */
import { UUID, SHA256, MAX_RESEARCH_REQUEST, MAX_RESEARCH_RESPONSE, researchCursor, projectCitationSpec, validateResearchMutation, projectResearchNonRunMutation, projectResearchContext, projectResearchCitationEnvelope, projectResearchComparisonEnvelope } from "../research.ts";
import { projectResearchRunEnvelope, projectResearchRuns, projectResearchEvents, projectResearchResult, projectResearchSource, projectResearchRunMutation } from "../research-execution.ts";
import { taskDiscoveryJSON } from "./task-discovery.ts";
import { potbotConfigForRequest, upstream, upstreamFailure } from "./potbot.ts";
import { requireOperator, safeError, validOrigin } from "./security.ts";

export async function researchRoute(request: Request, path: string[]): Promise<Response> {
  const operator = requireOperator(request); if (operator instanceof Response) return operator;
  const post = request.method === "POST", citation = post && path.length === 1 && path[0] === "citations";
  if ((post || request.headers.has("origin")) && !validOrigin(request, operator)) return safeError("Untrusted research origin.", 403);
  if (!["GET", "POST"].includes(request.method) || !Array.isArray(path) || !path.every(value => typeof value === "string")) return safeError("Unsupported research route.", 400);
  const name = path[0], id = path[1], tail = path[2], sourceId = path[3];
  const context = !post && path.length === 1 && name === "context", list = !post && path.length === 1 && name === "runs";
  const run = name === "runs" && UUID.test(id ?? "") && (post ? path.length === 3 && tail === "cancel" : path.length === 2 || path.length === 3 && ["events", "result"].includes(tail) || path.length === 4 && tail === "sources" && SHA256.test(sourceId ?? ""));
  const comparison = !post && path.length === 2 && name === "comparisons" && UUID.test(id ?? "");
  const mutation = post && (path.length === 1 && ["needs", "actions", "runs", "comparisons"].includes(name) || run);
  if (!context && !list && !run && !comparison && !mutation && !citation) return safeError("Unsupported research route.", 400);
  const expectedPrincipal = request.headers.get("X-Potbot-Expected-Principal");
  if ((post || expectedPrincipal !== null) && !UUID.test(expectedPrincipal ?? "")) return safeError("Research requires one exact reviewed principal UUID.", 400);
  const config = potbotConfigForRequest(request); if (!config) return safeError("Research backend unavailable.", 503);
  const query = new URL(request.url).searchParams, forwarded = new URLSearchParams(), allowed = context ? ["episodeId", "repositoryStateId", "needId"] : list ? ["episodeId", "limit", "after"] : !post && tail === "events" ? ["after", "limit"] : [];
  if (query.toString().length > 4096 || [...query.keys()].some(key => !allowed.includes(key) || query.getAll(key).length !== 1)) return safeError("Unexpected research query.", 400);
  const count = (value: string, min: number, max: number) => /^(?:0|[1-9]\d{0,15})$/.test(value) && Number.isSafeInteger(Number(value)) && Number(value) >= min && Number(value) <= max;
  let after = 0;
  if (context) {
    if (!UUID.test(query.get("episodeId") ?? "") || !UUID.test(query.get("repositoryStateId") ?? "") || query.has("needId") && !UUID.test(query.get("needId") ?? "")) return safeError("Research context requires exact task, state and optional need UUIDs.", 400);
    for (const key of allowed) if (query.has(key)) forwarded.set(key, query.get(key)!);
  } else if (list) {
    const limit = query.get("limit") ?? "25", cursor = query.get("after"), episode = query.get("episodeId");
    if (!count(limit, 1, 25) || cursor !== null && !researchCursor(cursor) || episode !== null && !UUID.test(episode)) return safeError("Invalid research history range.", 400);
    if (episode !== null) forwarded.set("episodeId", episode); forwarded.set("limit", limit); if (cursor !== null) forwarded.set("after", cursor);
  } else if (tail === "events") {
    const cursor = query.get("after") ?? "0", limit = query.get("limit") ?? "100";
    if (!count(cursor, 0, Number.MAX_SAFE_INTEGER) || !count(limit, 1, 100)) return safeError("Invalid research event range.", 400);
    after = Number(cursor); forwarded.set("after", cursor); forwarded.set("limit", limit);
  }
  const headers: Record<string, string> = expectedPrincipal === null ? {} : { "X-Potbot-Expected-Principal": expectedPrincipal }; let body: Record<string, unknown> | undefined;
  if (post) {
    if (request.headers.get("content-type")?.split(";")[0] !== "application/json") return safeError("Research requires an exact JSON body.", 400);
    let raw: unknown; try { raw = await taskDiscoveryJSON(request, MAX_RESEARCH_REQUEST); } catch { return safeError("Research requires bounded unambiguous UTF-8 JSON.", 400); }
    const parsed = citation ? projectCitationSpec(raw) : validateResearchMutation(path, raw); if (!parsed) return safeError("Review the exact task, information need, policy and requested evidence.", 400); body = parsed;
    Object.assign(headers, { "Content-Type": "application/json", Origin: operator.publicOrigin });
    if (!citation) {
      const key = request.headers.get("Idempotency-Key"); if (!key || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(key)) return safeError("Research changes require a stable command key.", 400);
      headers["Idempotency-Key"] = key;
      if (tail !== "cancel") { const token = process.env.POTBOT_UI_SETUP_TOKEN; if (!token || !/^[!-~]{32,4096}$/.test(token)) return safeError("Operator setup permission is unavailable.", 503); headers["X-Potbot-Setup-Token"] = token; }
    } else if (request.headers.has("Idempotency-Key")) return safeError("Citation verification is a read and accepts no mutation key.", 400);
  }
  try {
    const response = await upstream(config, "/v1/ui/research/" + path.join("/") + (forwarded.size ? "?" + forwarded : ""), { method: request.method, headers, ...(body ? { body: JSON.stringify(body) } : {}), signal: AbortSignal.any([request.signal, AbortSignal.timeout(post ? 30000 : 15000)]) });
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) return safeError("Research authorization changed. Read the current workspace session before another decision.", response.status);
      if (response.status === 409) return safeError("The task, need, policy or retained evidence changed. Read current context before another decision.", 409);
      if (response.status === 400) return safeError("The exact research request was rejected.", 400);
      return upstreamFailure(response);
    }
    const raw = await taskDiscoveryJSON(response, MAX_RESEARCH_RESPONSE);
    const principal = expectedPrincipal ?? undefined;
    const result = citation ? projectResearchCitationEnvelope(raw, config.workspaceId, projectCitationSpec(body)!, principal) : mutation ? name === "runs" ? projectResearchRunMutation(raw, config.workspaceId, path, body!, principal) : projectResearchNonRunMutation(raw, config.workspaceId, path, body!, principal) : context ? projectResearchContext(raw, config.workspaceId, query.get("episodeId")!, query.get("repositoryStateId")!, query.get("needId"), principal) : list ? projectResearchRuns(raw, config.workspaceId, query.get("episodeId") ?? undefined, principal) : comparison ? projectResearchComparisonEnvelope(raw, config.workspaceId, id, principal) : tail === "events" ? projectResearchEvents(raw, config.workspaceId, id, after, principal) : tail === "result" ? projectResearchResult(raw, config.workspaceId, id, principal) : tail === "sources" ? projectResearchSource(raw, config.workspaceId, id, sourceId, principal) : projectResearchRunEnvelope(raw, config.workspaceId, id, principal);
    if (!result) return safeError("Research returned an invalid scoped response.", 502);
    if (list && "items" in result && (result.items.length > Number(forwarded.get("limit")) || query.has("after") && "nextCursor" in result && result.nextCursor === query.get("after"))) return safeError("Research history did not advance safely.", 502);
    if (tail === "events" && "items" in result && result.items.length > Number(forwarded.get("limit"))) return safeError("Research events exceeded their requested range.", 502);
    return Response.json(result, { status: mutation && [201, 202].includes(response.status) ? response.status : 200, headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("Research is unavailable. Preserve an uncertain mutation’s exact body and key before retrying.", 503); }
}

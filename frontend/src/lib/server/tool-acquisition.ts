/** Fixed, operator-authenticated acquisition BFF; no client-selected host route. */
import { UUID, TOOL_ACTIONS, projectRecipeList, projectAcquisitionList, projectAcquisition, projectAcquisitionEvents, projectToolMutation, validateToolMutation, type ToolAction } from "../tool-acquisition.ts";
import { workbenchJSON } from "./operation-workbench.ts";
import { potbotConfigForRequest, upstream, upstreamFailure } from "./potbot.ts";
import { requireOperator, safeError, validOrigin } from "./security.ts";

function integer(value: string | null, fallback: number, max: number, min = 0): number | null {
  if (value === null) return fallback;
  if (!/^(0|[1-9]\d{0,15})$/.test(value)) return null;
  const result = Number(value); return Number.isSafeInteger(result) && result >= min && result <= max ? result : null;
}
export async function toolAcquisitionRoute(request: Request, path: string[]): Promise<Response> {
  const operator = requireOperator(request); if (operator instanceof Response) return operator;
  const mutation = request.method === "POST";
  if ((mutation || request.headers.has("origin")) && !validOrigin(request, operator)) return safeError("Untrusted tool workshop origin.", 403);
  const route = Array.isArray(path) && path.every(part => typeof part === "string") ? path : null;
  const kind = !route || !["GET", "POST"].includes(request.method) ? null : mutation ?
    route.length === 0 ? "request" : route.length === 2 && UUID.test(route[0]) && TOOL_ACTIONS.includes(route[1] as ToolAction) ? "action" : null :
    route.length === 0 ? "list" : route.length === 1 && route[0] === "recipes" ? "recipes" :
    route.length === 1 && UUID.test(route[0]) ? "inspect" : route.length === 2 && UUID.test(route[0]) && route[1] === "events" ? "events" : null;
  if (!route || !kind) return safeError("Unsupported tool acquisition route.", 400);
  const config = potbotConfigForRequest(request); if (!config) return safeError("Workspace backend unavailable.", 503);
  const query = new URL(request.url).searchParams, forwarded = new URLSearchParams();
  const allowed = ["list", "recipes", "events"].includes(kind) ? ["limit", "after"] : [];
  if ([...query.keys()].some(key => !allowed.includes(key) || query.getAll(key).length !== 1)) return safeError("Unexpected tool acquisition query.", 400);
  let after = 0;
  if (allowed.length) {
    const limit = integer(query.get("limit"), kind === "events" ? 50 : 25, 50, 1), cursor = query.get("after");
    if (limit === null) return safeError("Invalid acquisition page limit.", 400);
    forwarded.set("limit", String(limit));
    if (kind === "events") { const parsed = integer(cursor, 0, Number.MAX_SAFE_INTEGER); if (parsed === null) return safeError("Invalid event cursor.", 400); after = parsed; forwarded.set("after", String(parsed)); }
    else if (cursor !== null) { if (!UUID.test(cursor)) return safeError("Invalid acquisition page cursor.", 400); forwarded.set("after", cursor); }
  }
  const headers: Record<string, string> = {}; let body: Record<string, unknown> | undefined;
  if (mutation) {
    const key = request.headers.get("Idempotency-Key"), setup = process.env.POTBOT_UI_SETUP_TOKEN;
    if (!key || !/^[A-Za-z0-9_.:-]{1,128}$/.test(key) || request.headers.get("content-type")?.split(";")[0] !== "application/json") return safeError("A stable request key and exact JSON body are required.", 400);
    if (!setup || !/^[!-~]{32,4096}$/.test(setup)) return safeError("Operator setup permission is unavailable.", 503);
    let raw: unknown; try { raw = await workbenchJSON(request, 8192); } catch { return safeError("The acquisition request exceeds its bounded JSON contract.", 400); }
    const parsed = validateToolMutation(route, raw); if (!parsed) return safeError("An exact published recipe or reviewed revision, digest and confirmation are required.", 400);
    body = parsed; Object.assign(headers, { "Content-Type": "application/json", "Idempotency-Key": key, "X-Potbot-Setup-Token": setup, Origin: operator.publicOrigin });
  }
  const suffix = forwarded.size ? "?" + forwarded.toString() : "";
  try {
    const response = await upstream(config, "/v1/ui/tools/acquisitions" + (route.length ? "/" + route.join("/") : "") + suffix,
      { method: request.method, headers, ...(body ? { body: JSON.stringify(body) } : {}), signal: AbortSignal.any([request.signal, AbortSignal.timeout(15_000)]) });
    if (!response.ok) {
      if (response.status === 409) return safeError("The tool revision or reviewed evidence changed. Refresh and review its current state.", 409);
      if (response.status === 400) return safeError("The exact source or lifecycle action was rejected.", 400);
      return upstreamFailure(response);
    }
    const raw = await workbenchJSON(response, 2 * 1024 ** 2);
    const result = mutation ? projectToolMutation(raw, config.workspaceId, route, body) : kind === "list" ? projectAcquisitionList(raw, config.workspaceId) :
      kind === "recipes" ? projectRecipeList(raw, config.workspaceId) : kind === "events" ? projectAcquisitionEvents(raw, config.workspaceId, after, undefined, route[0]) : projectAcquisition(raw, config.workspaceId, route[0]);
    if (!result) return safeError("The acquisition service returned an invalid scoped response.", 502);
    return Response.json(result, { status: mutation && [201, 202].includes(response.status) ? response.status : 200, headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("The acquisition result is unavailable. Retain the exact request before retrying an uncertain action.", 503); }
}

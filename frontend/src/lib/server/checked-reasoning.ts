/** Explicit routes for the operator-owned checked reasoning lifecycle. */
import { MAX_REASONING_PLAN, object, reasoningPlan, reasoningResult, reasoningStatus } from "../checked-reasoning.ts";
import { UUID } from "../operation-workbench.ts";
import { potbotConfigForRequest, upstream, upstreamFailure } from "./potbot.ts";
import { requireOperator, safeError, validOrigin } from "./security.ts";
import { taskDiscoveryJSON } from "./task-discovery.ts";

export async function checkedReasoningRoute(request: Request, path: string[]): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  const [id, action] = path;
  const post = request.method === "POST";
  const allowed = post
    ? path.length === 2 && ["start", "advance", "interrupt"].includes(action)
    : request.method === "GET" && (path.length === 1 || path.length === 2 && action === "result");
  if (!UUID.test(id ?? "") || !allowed || new URL(request.url).search)
    return safeError("Unsupported checked reasoning route.", 400);
  if ((post || request.headers.has("origin")) && !validOrigin(request, operator))
    return safeError("Unapproved checked reasoning origin.", 403);
  const config = potbotConfigForRequest(request);
  if (!config) return safeError("Workspace backend unavailable.", 503);
  const headers: Record<string, string> = {};
  const principal = request.headers.get("X-Potbot-Expected-Principal");
  if ((post || principal !== null) && !UUID.test(principal ?? ""))
    return safeError("Checked reasoning requires the current principal.", 400);
  if (principal !== null) headers["X-Potbot-Expected-Principal"] = principal;
  let body: Record<string, unknown> | undefined;
  if (post) {
    if (request.headers.get("content-type")?.split(";", 1)[0] !== "application/json")
      return safeError("Checked reasoning requires JSON.", 400);
    try {
      const row = object(await taskDiscoveryJSON(request, action === "start" ? MAX_REASONING_PLAN + 256 : 256));
      if (!row || row.confirm !== true) return safeError("Confirm the exact reasoning request.", 400);
      const keys = Object.keys(row).sort().join(",");
      if (action === "start" && (keys !== "confirm,plan" || !reasoningPlan(row.plan)) ||
          action === "advance" && (keys !== "confirm,expected_stage" ||
            typeof row.expected_stage !== "number" || !Number.isInteger(row.expected_stage) ||
            row.expected_stage < 0 || row.expected_stage > 2) ||
          action === "interrupt" && keys !== "confirm")
        return safeError("The exact reasoning request is invalid.", 400);
      body = row;
    } catch { return safeError("Checked reasoning requires bounded, unambiguous JSON.", 400); }
    headers.Origin = operator.publicOrigin;
    headers["Content-Type"] = "application/json";
    if (action === "advance") {
      const token = process.env.POTBOT_UI_SETUP_TOKEN;
      if (!token || !/^[!-~]{32,4096}$/.test(token))
        return safeError("Operator setup permission is unavailable.", 503);
      headers["X-Potbot-Setup-Token"] = token;
    }
  }
  try {
    const response = await upstream(config, "/v1/ui/checked-reasoning/" + path.join("/"), {
      method: request.method, headers, ...(body ? { body: JSON.stringify(body) } : {}),
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(post ? 30_000 : 15_000)]),
    });
    if (!response.ok) {
      if ([400, 401, 403, 409].includes(response.status))
        return safeError("The run, principal, or stage changed. Inspect its retained status.", response.status);
      return upstreamFailure(response);
    }
    const value = await taskDiscoveryJSON(response, action === "result" ? 262_144 : 8192);
    const projected = action === "result"
      ? object(value)?.schema === "potbot.checked-reasoning-result/1" && object(value)?.role === "synthesizer" && reasoningResult(value)
      : reasoningStatus(value, id);
    if (!projected) return safeError("Checked reasoning returned an invalid response.", 502);
    return Response.json(projected, { headers: { "Cache-Control": "private, no-store" } });
  } catch {
    return safeError("Checked reasoning unavailable. Read the retained status before issuing another command.", 503);
  }
}

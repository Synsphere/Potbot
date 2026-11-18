/** Exact private graph-route proxy. No arbitrary backend URL or model-supplied request. */
import { graphList, graphStatus, UUID } from "../atomic-graphs.ts";
import { potbotConfigForRequest, upstream, upstreamFailure } from "./potbot.ts";
import { boundedJSON, requireOperator, safeError, validOrigin } from "./security.ts";

const KEY = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;

export async function atomicGraphRoute(request: Request, path: string[]): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  const mutation = request.method === "POST";
  if (!Array.isArray(path) || path.some(x => typeof x !== "string") ||
      !["GET", "POST"].includes(request.method) ||
      (mutation || request.headers.has("origin")) && !validOrigin(request, operator))
    return safeError("Invalid graph route or origin.", 403);
  const list = !mutation && path.length === 0;
  const inspect = !mutation && path.length === 1 && UUID.test(path[0]);
  const action = mutation && path.length === 2 && UUID.test(path[0]) &&
    ["tick", "interrupt"].includes(path[1]);
  if (!list && !inspect && !action) return safeError("Unsupported graph route.", 400);
  const config = potbotConfigForRequest(request);
  if (!config) return safeError("Graph backend unavailable.", 503);
  const query = new URL(request.url).searchParams;
  const allowed = list ? ["limit", "after", "activeOnly"] : [];
  if (query.toString().length > 4096 || [...query.keys()].some(k => !allowed.includes(k) || query.getAll(k).length !== 1))
    return safeError("Invalid graph query.", 400);
  const limit = query.get("limit") || "25", after = query.get("after"), active = query.get("activeOnly") || "false";
  if (list && (!/^(?:[1-9]|[1-9]\d|100)$/.test(limit) ||
      after !== null && (after.length > 200 || !/^[A-Za-z0-9_-]+={0,2}$/.test(after)) ||
      !["true", "false"].includes(active))) return safeError("Invalid graph page.", 400);
  let body: Record<string, unknown> | null = null;
  const headers: Record<string, string> = {};
  if (mutation) {
    if (request.headers.get("content-type")?.split(";", 1)[0] !== "application/json")
      return safeError("Graph mutations require JSON.", 400);
    try {
      const value: unknown = await boundedJSON(request, 4096);
      if (!value || typeof value !== "object" || Array.isArray(value)) return safeError("Invalid graph mutation.", 400);
      const object = value as Record<string, unknown>;
      if (object.confirm !== true || (path[1] === "tick" ?
          Object.keys(object).sort().join(",") !== "confirm,limit" ||
          !Number.isInteger(object.limit) || Number(object.limit) < 1 || Number(object.limit) > 8 :
          Object.keys(object).join(",") !== "confirm")) return safeError("Graph mutation requires reviewed confirmation.", 400);
      body = object;
    } catch { return safeError("Invalid bounded JSON graph mutation.", 400); }
    const key = request.headers.get("idempotency-key"), expected = request.headers.get("x-potbot-expected-principal");
    if (!key || !KEY.test(key) || !expected || !UUID.test(expected))
      return safeError("Graph mutation requires stable owner and command identity.", 400);
    headers["Idempotency-Key"] = key;
    headers["X-Potbot-Expected-Principal"] = expected;
    headers.Origin = operator.publicOrigin;
    headers["Content-Type"] = "application/json";
    if (path[1] === "tick") {
      const token = process.env.POTBOT_UI_SETUP_TOKEN;
      if (!token || !/^[!-~]{32,4096}$/.test(token)) return safeError("Operator setup capability unavailable.", 503);
      headers["X-Potbot-Setup-Token"] = token;
    }
  }
  const params = new URLSearchParams();
  if (list) {
    params.set("limit", limit);
    params.set("activeOnly", active);
    if (after !== null) params.set("after", after);
  }
  try {
    const suffix = list ? "" : "/" + path.join("/");
    const response = await upstream(config, "/v1/ui/atomic-graphs" + suffix + (params.size ? "?" + params : ""), {
      method: request.method, headers, ...(body ? { body: JSON.stringify(body) } : {}),
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(mutation ? 30000 : 15000)]),
    });
    if (!response.ok) {
      if ([401, 403, 409].includes(response.status)) return safeError("Graph authority or state changed; inspect before retrying.", response.status);
      if (response.status === 400) return safeError("Graph request was rejected.", 400);
      return upstreamFailure(response);
    }
    const bytes = await response.arrayBuffer();
    if (bytes.byteLength > 1_000_000) return safeError("Graph response exceeded its bound.", 502);
    const data: unknown = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    if (list && !graphList(data, config.workspaceId) || inspect && !graphStatus(data, path[0]))
      return safeError("Graph response violated its scoped contract.", 502);
    if (action) {
      const value = data && typeof data === "object" && !Array.isArray(data) ? data as Record<string, unknown> : null;
      if (!value || value.graph_id !== path[0] || (path[1] === "tick" ?
          !["no_dispatch", "launched", "reconciled_race", "admission_blocked"].includes(String(value.decision)) ||
          !graphStatus(value.status, path[0]) : value.state !== "interrupted" || !Array.isArray(value.jobs)))
        return safeError("Graph mutation response was invalid.", 502);
    }
    return Response.json(data, { status: mutation ? 202 : 200, headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("Graph backend unavailable. Inspect committed status before retrying.", 503); }
}

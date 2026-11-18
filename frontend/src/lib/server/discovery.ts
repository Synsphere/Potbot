/** Fixed scoped discovery routes; source refresh and runtime probing remain host actions. */
import { UUID, DISCOVERY_SEARCH_CURSOR, DISCOVERY_FILTERS, discoverySearchParams, MAX_DISCOVERY_REQUEST, MAX_DISCOVERY_RESPONSE, validateDiscoveryMutation, projectDiscoveryMutation,
  projectDiscoverySources, projectDiscoveryPublishers, projectDiscoveryCandidates, projectDiscoveryChecks, projectDiscoveryCandidateEnvelope, projectDiscoveryCheckEnvelope } from "../discovery.ts";
import { workbenchJSON } from "./operation-workbench.ts";
import { potbotConfigForRequest, upstream, upstreamFailure } from "./potbot.ts";
import { requireOperator, safeError, validOrigin } from "./security.ts";

export async function discoveryRoute(request: Request, path: string[]): Promise<Response> {
  const operator = requireOperator(request); if (operator instanceof Response) return operator;
  const mutation = request.method === "POST";
  if ((mutation || request.headers.has("origin")) && !validOrigin(request, operator)) return safeError("Untrusted discovery origin.", 403);
  if (!Array.isArray(path) || !path.every(part => typeof part === "string") || !["GET", "POST"].includes(request.method)) return safeError("Unsupported discovery route.", 400);
  const list = !mutation && path.length === 1 && ["sources", "publishers", "candidates", "checks"].includes(path[0]);
  const detail = !mutation && path.length === 2 && ["candidates", "checks"].includes(path[0]) && UUID.test(path[1]);
  const action = mutation && path.length === 3 && UUID.test(path[1]) && ((path[0] === "sources" && ["enable", "disable", "revoke"].includes(path[2])) ||
    (path[0] === "publishers" && ["approve", "block", "revoke"].includes(path[2])) || (path[0] === "candidates" && path[2] === "checks") || (path[0] === "checks" && ["handoff", "cancel"].includes(path[2])));
  if (!list && !detail && !action) return safeError("Unsupported discovery route.", 400);
  const config = potbotConfigForRequest(request); if (!config) return safeError("Workspace discovery backend unavailable.", 503);
  const query = new URL(request.url).searchParams, forwarded = new URLSearchParams();
  const allowed = list ? ["limit", "after", ...(path[0] === "candidates" ? DISCOVERY_FILTERS : path[0] === "publishers" ? ["sourceId"] : [])] : [];
  if ([...query.keys()].some(key => !allowed.includes(key) || query.getAll(key).length !== 1)) return safeError("Unexpected discovery query.", 400);
  if (list) {
    const limit = query.get("limit") ?? "25", after = query.get("after"), source = query.get("sourceId"), filters = discoverySearchParams(query);
    if (!/^(?:[1-9]|[1-4]\d|50)$/.test(limit) || (after !== null && !(path[0] === "candidates" ? DISCOVERY_SEARCH_CURSOR : UUID).test(after)) || (source !== null && !UUID.test(source)) || !filters) return safeError("Invalid bounded discovery filter or cursor.", 400);
    forwarded.set("limit", limit); if (after !== null) forwarded.set("after", after); for (const [key, value] of filters) forwarded.set(key, value);
    if (new TextEncoder().encode(forwarded.toString()).length > 8192) return safeError("Discovery filters exceed the encoded query allowance.", 400);
  }
  const headers: Record<string, string> = {}; let body: Record<string, unknown> | undefined;
  if (mutation) {
    const key = request.headers.get("Idempotency-Key"), token = process.env.POTBOT_UI_SETUP_TOKEN;
    if (!key || !/^[A-Za-z0-9_.:-]{1,128}$/.test(key) || request.headers.get("content-type")?.split(";")[0] !== "application/json") return safeError("An exact JSON body and stable request key are required.", 400);
    if (!token || !/^[!-~]{32,4096}$/.test(token)) return safeError("Operator setup permission is unavailable.", 503);
    let value: unknown; try { value = await workbenchJSON(request, MAX_DISCOVERY_REQUEST); } catch { return safeError("The discovery request exceeds its bounded JSON contract.", 400); }
    const parsed = validateDiscoveryMutation(path, value); if (!parsed) return safeError("An exact reviewed identity, revision and confirmation are required.", 400);
    body = parsed; Object.assign(headers, { "Content-Type": "application/json", "Idempotency-Key": key, "X-Potbot-Setup-Token": token, Origin: operator.publicOrigin });
  }
  try {
    const suffix = forwarded.size ? "?" + forwarded.toString() : "";
    const response = await upstream(config, "/v1/ui/discovery/" + path.join("/") + suffix, { method: request.method, headers, ...(body ? { body: JSON.stringify(body) } : {}),
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(mutation ? 30_000 : 15_000)]) });
    if (!response.ok) {
      if (response.status === 409) return safeError("The discovery source, admission or review changed. Refresh and inspect its current exact binding.", 409);
      if (response.status === 400) return safeError("The exact discovery request was rejected.", 400);
      return upstreamFailure(response);
    }
    const raw = await workbenchJSON(response, MAX_DISCOVERY_RESPONSE);
    const result = mutation ? projectDiscoveryMutation(raw, config.workspaceId, path, body) : detail ? path[0] === "candidates" ? projectDiscoveryCandidateEnvelope(raw, config.workspaceId, path[1]) : projectDiscoveryCheckEnvelope(raw, config.workspaceId, path[1]) :
      path[0] === "sources" ? projectDiscoverySources(raw, config.workspaceId) : path[0] === "publishers" ? projectDiscoveryPublishers(raw, config.workspaceId, undefined, query.get("sourceId") ?? undefined) :
        path[0] === "candidates" ? projectDiscoveryCandidates(raw, config.workspaceId) : projectDiscoveryChecks(raw, config.workspaceId);
    if (!result) return safeError("The discovery service returned an invalid scoped response.", 502);
    if (list && path[0] === "candidates" && "items" in result) {
      const sourceId = query.get("sourceId"), kind = query.get("kind");
      if (result.items.some(item => !("observation" in item) || (sourceId && item.sourceId !== sourceId) || (kind && item.observation.kind !== kind))) return safeError("Discovery candidates differ from the selected source or kind.", 502);
    }
    return Response.json(result, { status: mutation && [201, 202].includes(response.status) ? response.status : 200, headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("Discovery is unavailable. Retain the exact request before retrying an uncertain change.", 503); }
}

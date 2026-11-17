/** Fixed operator BFF: bounded portable-skill data, never an arbitrary proxy or installer. */
import { ACTIONS, UUID, SHA256, RESOURCE_CHUNK, projectHistory, projectPackage, projectPackageList, projectPlan, projectResource,
  projectSkill, projectSkillList, projectSkillMutation, resourcePath, validateSkillMutation, type SkillAction } from "../skill-acquisition.ts";
import { workbenchJSON } from "./operation-workbench.ts";
import { potbotConfigForRequest, upstream, upstreamFailure } from "./potbot.ts";
import { requireOperator, safeError, validOrigin } from "./security.ts";

type Kind = "list" | "packages" | "package" | "plan" | "package_resource" | "skill" | "history" | "resource" | "import" | "install" | "control";
function kindFor(path: string[], mutation: boolean): Kind | null {
  if (mutation) {
    if (path.length === 2 && path[0] === "imports" && ["zip", "github"].includes(path[1])) return "import";
    if (path.length === 3 && path[0] === "packages" && UUID.test(path[1]) && path[2] === "install") return "install";
    if (path.length === 2 && UUID.test(path[0]) && ACTIONS.includes(path[1] as SkillAction)) return "control";
  } else {
    if (!path.length) return "list";
    if (path.length === 1 && path[0] === "packages") return "packages";
    if (path.length >= 2 && path[0] === "packages" && UUID.test(path[1])) {
      if (path.length === 2) return "package";
      if (path.length === 3 && path[2] === "plan") return "plan";
      if (path.length === 3 && path[2] === "resources") return "package_resource";
    }
    if (path.length === 1 && UUID.test(path[0])) return "skill";
    if (path.length === 2 && UUID.test(path[0]) && path[1] === "history") return "history";
    if (path.length === 2 && UUID.test(path[0]) && path[1] === "resources") return "resource";
  }
  return null;
}
function boundedInteger(value: string | null, defaultValue: number, maximum: number, minimum = 0): number | null {
  if (value === null) return defaultValue;
  if (!/^(0|[1-9]\d{0,15})$/.test(value)) return null;
  const number = Number(value); return Number.isSafeInteger(number) && number >= minimum && number <= maximum ? number : null;
}

export async function skillAcquisitionRoute(request: Request, path: string[]): Promise<Response> {
  const operator = requireOperator(request); if (operator instanceof Response) return operator;
  const mutation = request.method === "POST";
  if ((mutation || request.headers.has("origin")) && !validOrigin(request, operator)) return safeError("Untrusted skill workshop origin.", 403);
  const kind = ["GET", "POST"].includes(request.method) && Array.isArray(path) && path.every(item => typeof item === "string") ? kindFor(path, mutation) : null;
  if (!kind) return safeError("Unsupported skill workshop route.", 400);
  const config = potbotConfigForRequest(request); if (!config) return safeError("Workspace backend unavailable.", 503);
  const query = new URL(request.url).searchParams, forwarded = new URLSearchParams();
  const allowed = kind === "list" || kind === "packages" || kind === "history" ? ["limit", "after"] : kind === "plan" ? ["skillId"] :
    kind === "resource" ? ["releaseSha256", "path", "offset", "length"] : kind === "package_resource" ? ["path", "offset", "length"] : [];
  if ([...query.keys()].some(key => !allowed.includes(key) || query.getAll(key).length !== 1)) return safeError("Unexpected skill workshop query.", 400);
  let offset = 0, length = RESOURCE_CHUNK, resource = "", historyAfter = 0, skillId: string | undefined;
  if (kind === "list" || kind === "packages" || kind === "history") {
    const limit = boundedInteger(query.get("limit"), 25, 50, 1), after = query.get("after");
    const cursor = kind === "history" ? boundedInteger(after, 0, Number.MAX_SAFE_INTEGER, 1) : after === null || UUID.test(after) ? 0 : null;
    if (limit === null || cursor === null) return safeError("Invalid skill page cursor.", 400);
    historyAfter = cursor;
    forwarded.set("limit", String(limit)); if (after) forwarded.set("after", after);
  } else if (kind === "plan") {
    const selected = query.get("skillId");
    if (selected !== null) { if (!UUID.test(selected)) return safeError("Invalid update target.", 400); skillId = selected; forwarded.set("skillId", selected); }
  } else if (kind === "resource" || kind === "package_resource") {
    const name = query.get("path"), start = boundedInteger(query.get("offset"), 0, Number.MAX_SAFE_INTEGER), size = boundedInteger(query.get("length"), RESOURCE_CHUNK, RESOURCE_CHUNK, 1);
    if (!resourcePath(name) || start === null || size === null) return safeError("Invalid skill resource range.", 400);
    resource = name; offset = start; length = size;
    if (kind === "resource") { const release = query.get("releaseSha256"); if (!release || !SHA256.test(release)) return safeError("An exact skill release is required.", 400); forwarded.set("releaseSha256", release); }
    forwarded.set("path", name); forwarded.set("offset", String(start)); forwarded.set("length", String(size));
  }
  const headers: Record<string, string> = {}; let body: string | undefined, mutationBody: Record<string, unknown> | undefined;
  if (mutation) {
    const key = request.headers.get("Idempotency-Key"), token = process.env.POTBOT_UI_SETUP_TOKEN;
    if (!key || !/^[A-Za-z0-9_.:-]{1,128}$/.test(key) || request.headers.get("content-type")?.split(";")[0] !== "application/json") return safeError("A stable request key and JSON body are required.", 400);
    if (!token || !/^[!-~]{32,4096}$/.test(token)) return safeError("Operator setup permission is unavailable.", 503);
    let raw: unknown;
    try { raw = await workbenchJSON(request, path.join("/") === "imports/zip" ? 6 * 1024 * 1024 : 65_536); }
    catch { return safeError("The skill request exceeds its bounded JSON contract.", 400); }
    const parsed = validateSkillMutation(path, raw); if (!parsed) return safeError("The skill action requires an exact source or reviewed revision and explicit confirmation.", 400);
    mutationBody = parsed; body = JSON.stringify(parsed); Object.assign(headers, { "Idempotency-Key": key, "Content-Type": "application/json", "X-Potbot-Setup-Token": token, Origin: operator.publicOrigin });
  }
  // The route segments are fixed above. Encode dots within query data so the
  // shared upstream path guard does not mistake valid file names for traversal.
  const search = forwarded.toString().replaceAll(".", "%2E"), endpoint = "/v1/ui/skills" + (path.length ? "/" + path.join("/") : "") + (search ? "?" + search : "");
  try {
    const response = await upstream(config, endpoint, { method: request.method, headers, ...(body === undefined ? {} : { body }),
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(kind === "import" ? 60_000 : 15_000)]) });
    if (!response.ok) {
      if (response.status === 409) return safeError("The package, review plan or skill revision changed. Reload and review again.", 409);
      if (response.status === 400) return safeError("The source or skill action was rejected. Review the exact source, manifest and current revision.", 400);
      return upstreamFailure(response);
    }
    const raw = await workbenchJSON(response, kind === "resource" || kind === "package_resource" ? 131_072 : 2 * 1024 * 1024);
    const result = mutation ? await projectSkillMutation(raw, config.workspaceId, path, mutationBody) : kind === "list" ? projectSkillList(raw, config.workspaceId) : kind === "packages" ? projectPackageList(raw, config.workspaceId) :
      kind === "package" ? projectPackage(raw, config.workspaceId, path[1]) : kind === "import" ? projectPackage(raw, config.workspaceId) :
      kind === "plan" ? projectPlan(raw, config.workspaceId, path[1], skillId) : kind === "history" ? projectHistory(raw, config.workspaceId, path[0], undefined, historyAfter) :
      kind === "resource" || kind === "package_resource" ? projectResource(raw, config.workspaceId, resource, offset, length) :
      projectSkill(raw, config.workspaceId, kind === "control" || kind === "skill" ? path[0] : undefined);
    if (!result || (kind === "package" && (!("content" in result) || result.content === undefined)) ||
        (mutation && "skill" in result && !result.skill.owned)) return safeError("The skill service returned an invalid scoped response.", 502);
    return Response.json(result, { status: mutation && response.status === 201 ? 201 : 200, headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("The skill response is unavailable. Preserve the request key before retrying a submission.", 503); }
}

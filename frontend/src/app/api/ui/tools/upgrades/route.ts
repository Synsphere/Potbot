import { potbotConfigForRequest, upstream, upstreamFailure } from "../../../../../lib/server/potbot.ts";
import { requireOperator, safeError } from "../../../../../lib/server/security.ts";
import { upgradeListing } from "../../../../../lib/tool-upgrades.ts";
import { workbenchJSON } from "../../../../../lib/server/operation-workbench.ts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  if (new URL(request.url).search) return safeError("Upgrade query not supported.", 400);
  const config = potbotConfigForRequest(request);
  if (!config) return safeError("Workspace backend unavailable.", 503);
  try {
    const result = await upstream(config, "/v1/ui/tools/upgrades", { method: "GET", signal: AbortSignal.any([request.signal, AbortSignal.timeout(8000)]) });
    if (!result.ok) return upstreamFailure(result);
    let raw: unknown; try { raw = await workbenchJSON(result, 65_536); } catch { return safeError("Upgrade list exceeded its bounded JSON contract.", 502); }
    const record = upgradeListing(raw, config.workspaceId);
    if (!record) return safeError("Invalid upgrade response.", 502);
    return Response.json({ ...record, workspaceId: config.workspaceId },
      { headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("Cannot load tool upgrades.", 503); }
}

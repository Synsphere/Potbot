import { projectOperationDetail } from "../../../../../lib/operation-catalogue.ts";
import { catalogueChanged, catalogueJSON } from "../../../../../lib/server/operation-catalogue.ts";
import { potbotConfigForRequest, upstream, upstreamFailure } from "../../../../../lib/server/potbot.ts";
import { requireOperator, safeError } from "../../../../../lib/server/security.ts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: Request, context: { params: Promise<{ id: string }> }): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  const { id } = await context.params;
  if (!/^[a-f0-9]{64}$/.test(id) || new URL(request.url).search) return safeError("Invalid operation identity or query.", 400);
  const config = potbotConfigForRequest(request);
  if (!config) return safeError("Workspace backend unavailable.", 503);
  try {
    const result = await upstream(config, "/v1/ui/operations/" + id);
    if (result.status === 409) return catalogueChanged();
    if (!result.ok) return upstreamFailure(result);
    const detail = projectOperationDetail(await catalogueJSON(result), config.workspaceId, id);
    if (!detail) return safeError("Invalid operation detail response.", 502);
    return Response.json(detail, { headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("Operation detail unavailable.", 503); }
}

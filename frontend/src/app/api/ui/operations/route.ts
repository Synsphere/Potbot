import { catalogueQuery, catalogueSearch, projectOperationListing } from "../../../../lib/operation-catalogue.ts";
import { catalogueChanged, catalogueJSON } from "../../../../lib/server/operation-catalogue.ts";
import { potbotConfigForRequest, upstream, upstreamFailure } from "../../../../lib/server/potbot.ts";
import { requireOperator, safeError } from "../../../../lib/server/security.ts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  const query = catalogueQuery(new URL(request.url).searchParams);
  if (!query) return safeError("Invalid operation catalogue query.", 400);
  const config = potbotConfigForRequest(request);
  if (!config) return safeError("Workspace backend unavailable.", 503);
  try {
    const result = await upstream(config, "/v1/ui/operations?" + catalogueSearch(query));
    if (result.status === 409) return catalogueChanged();
    if (!result.ok) return upstreamFailure(result);
    const listing = projectOperationListing(await catalogueJSON(result), config.workspaceId);
    if (!listing) return safeError("Invalid operation catalogue response.", 502);
    return Response.json(listing, { headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("Operation catalogue unavailable.", 503); }
}

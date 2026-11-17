import { operationWorkbenchRoute } from "../../../../../../lib/server/operation-workbench.ts";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export async function POST(request: Request, context: { params: Promise<{ id: string; action: string }> }): Promise<Response> {
  return operationWorkbenchRoute(request, await context.params);
}

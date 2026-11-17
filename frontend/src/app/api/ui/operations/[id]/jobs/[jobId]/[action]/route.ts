import { operationWorkbenchRoute } from "../../../../../../../../lib/server/operation-workbench.ts";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
type Context = { params: Promise<{ id: string; jobId: string; action: string }> };
export async function GET(request: Request, context: Context): Promise<Response> {
  return operationWorkbenchRoute(request, await context.params);
}
export async function POST(request: Request, context: Context): Promise<Response> {
  return operationWorkbenchRoute(request, await context.params);
}

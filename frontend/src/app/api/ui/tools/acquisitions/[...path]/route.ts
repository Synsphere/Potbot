import { toolAcquisitionRoute } from "../../../../../../lib/server/tool-acquisition.ts";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
type Context = { params: Promise<{ path: string[] }> };
export async function GET(request: Request, context: Context): Promise<Response> { return toolAcquisitionRoute(request, (await context.params).path); }
export async function POST(request: Request, context: Context): Promise<Response> { return toolAcquisitionRoute(request, (await context.params).path); }

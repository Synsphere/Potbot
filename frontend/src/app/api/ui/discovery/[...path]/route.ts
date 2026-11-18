import { discoveryRoute } from "../../../../../lib/server/discovery.ts";
type Context = { params: Promise<{ path: string[] }> };
export const dynamic = "force-dynamic";
export async function GET(request: Request, context: Context): Promise<Response> { return discoveryRoute(request, (await context.params).path); }
export async function POST(request: Request, context: Context): Promise<Response> { return discoveryRoute(request, (await context.params).path); }

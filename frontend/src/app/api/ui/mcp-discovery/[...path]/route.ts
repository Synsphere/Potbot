import { mcpDiscoveryRoute } from "../../../../../lib/server/mcp-discovery.ts";
type Context = { params: Promise<{ path: string[] }> };
export const dynamic = "force-dynamic";
export async function GET(request: Request, context: Context): Promise<Response> { return mcpDiscoveryRoute(request, (await context.params).path); }
export async function POST(request: Request, context: Context): Promise<Response> { return mcpDiscoveryRoute(request, (await context.params).path); }

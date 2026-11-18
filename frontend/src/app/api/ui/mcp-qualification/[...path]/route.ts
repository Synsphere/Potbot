import { mcpQualificationRoute } from "../../../../../lib/server/mcp-qualification.ts";
type Context = { params: Promise<{ path: string[] }> };
export const dynamic = "force-dynamic";
export async function GET(request: Request, context: Context): Promise<Response> { return mcpQualificationRoute(request, (await context.params).path); }
export async function POST(request: Request, context: Context): Promise<Response> { return mcpQualificationRoute(request, (await context.params).path); }

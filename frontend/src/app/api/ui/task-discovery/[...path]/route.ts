import { taskDiscoveryRoute } from "../../../../../lib/server/task-discovery.ts";
export const dynamic = "force-dynamic";
type Params = { params: Promise<{ path: string[] }> };
export async function GET(request: Request, context: Params) { return taskDiscoveryRoute(request, (await context.params).path); }
export async function POST(request: Request, context: Params) { return taskDiscoveryRoute(request, (await context.params).path); }

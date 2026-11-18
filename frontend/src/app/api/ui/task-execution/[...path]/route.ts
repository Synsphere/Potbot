import { taskExecutionRoute } from "../../../../../lib/server/task-execution.ts";
export const dynamic = "force-dynamic";
export async function GET(request: Request, context: { params: Promise<{ path: string[] }> }) { return taskExecutionRoute(request, (await context.params).path); }
export async function POST(request: Request, context: { params: Promise<{ path: string[] }> }) { return taskExecutionRoute(request, (await context.params).path); }

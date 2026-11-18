import { atomicGraphRoute } from "../../../../lib/server/atomic-graphs.ts";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export async function GET(request: Request): Promise<Response> { return atomicGraphRoute(request, []); }

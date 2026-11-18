import { toolAcquisitionRoute } from "../../../../../lib/server/tool-acquisition.ts";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export async function GET(request: Request): Promise<Response> { return toolAcquisitionRoute(request, []); }
export async function POST(request: Request): Promise<Response> { return toolAcquisitionRoute(request, []); }

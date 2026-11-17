import { skillAcquisitionRoute } from "../../../../lib/server/skill-acquisition.ts";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export async function GET(request: Request): Promise<Response> { return skillAcquisitionRoute(request, []); }

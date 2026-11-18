import { authConfig, localRequest } from "../../../../../lib/server/security.ts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export function GET(request: Request): Response {
  const config = authConfig();
  return Response.json({ configured: config !== null,
    authenticated: config !== null && localRequest(request, config),
  }, { headers: { "Cache-Control": "no-store" } });
}

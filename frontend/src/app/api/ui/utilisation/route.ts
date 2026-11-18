import { projectUtilisation } from "../../../../lib/utilisation.ts";
import { potbotConfig, upstream, upstreamFailure } from "../../../../lib/server/potbot.ts";
import { requireOperator, safeError } from "../../../../lib/server/security.ts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** Authenticated proxy; only validated aggregate counters cross to the browser. */
export async function GET(request: Request): Promise<Response> {
  const operator = requireOperator(request);
  if (operator instanceof Response) return operator;
  if (new URL(request.url).search) return safeError("Unrecognized utilisation query.", 400);
  const config = potbotConfig();
  if (!config) return safeError("Potbot backend not configured.", 503);
  try {
    const response = await upstream(config, "/v1/system/utilisation", {}, 3500);
    // On native Windows the optional Linux monitoring service is absent.
    // Report unavailable, never claim fabricated host measurements.
    if (response.status === 503 || response.status === 404) return Response.json({
      schema: 1, available: false,
      system: {load3m: 0, load2s: 0, memoryUsed: 0, logicalCpus: 1,
        pressure: {cpu: {}, memory: {}, io: {}}}, agents: [],
    }, {headers: {"Cache-Control": "private, no-store"}});
    if (!response.ok) return upstreamFailure(response);
    const bytes = await response.arrayBuffer();
    if (bytes.byteLength > 100_000) return safeError("Telemetry exceeds the allowed bound.", 502);
    const payload = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    const result = payload && payload.schema_version === 1 && payload.error === null &&
      projectUtilisation(payload.data);
    if (!result) return safeError("Invalid operator telemetry.", 502);
    return Response.json(result, { headers: { "Cache-Control": "private, no-store" } });
  } catch { return safeError("Operator telemetry unavailable.", 503); }
}

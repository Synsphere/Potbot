/** Browser navigation and recovery belong to the current workspace session. */
import { UUID, RESEARCH_SCHEMA, MAX_RESEARCH_REQUEST, researchObject, researchKeys, researchId, projectCitationSpec, validateResearchMutation, parseMcpJSON, sameTaskValue, type ResearchScope, type ResearchContext, type ResearchAction, type CitationSpec, type ResearchCitation, type ResearchExtraction } from "./research.ts";
import { workshopJournalScope, type WorkshopAttempt } from "./skill-workshop-client.ts";
import { researchQuote, researchTextSha256 } from "./research-text.ts";
export const RESEARCH_ENDPOINT = "/api/ui/research", RESEARCH_MAX_POLLS = 30;

export type ResearchSelection = { episode: string | null; state: string | null; need: string | null; run: string | null; invalid: boolean };
export const EMPTY_RESEARCH_SELECTION: ResearchSelection = { episode: null, state: null, need: null, run: null, invalid: false };
export function researchSelection(query: string, workspace: string): ResearchSelection {
  const params = new URLSearchParams(query), fields = ["episode", "state", "need", "run"] as const;
  if (!fields.some(field => params.has(field))) return { ...EMPTY_RESEARCH_SELECTION };
  if (!UUID.test(workspace) || params.getAll("workspace").length !== 1 || params.get("workspace") !== workspace || fields.some(field => params.has(field) && (params.getAll(field).length !== 1 || !UUID.test(params.get(field) ?? "")))) return { ...EMPTY_RESEARCH_SELECTION, invalid: true };
  return { episode: params.get("episode"), state: params.get("state"), need: params.get("need"), run: params.get("run"), invalid: false };
}
export function researchScopeMatches(left: ResearchScope, right: ResearchScope): boolean { return left.workspaceId === right.workspaceId && left.principalId === right.principalId; }
export function researchRequestHeaders(scope: ResearchScope): Record<string, string> {
  if (!UUID.test(scope.workspaceId) || !UUID.test(scope.principalId)) throw new Error("Research requests require the exact reviewed workspace and principal.");
  return { "X-Potbot-Workspace": scope.workspaceId, "X-Potbot-Expected-Principal": scope.principalId };
}
export function researchScopeInvalidated(status: number, value: unknown, scope: ResearchScope): boolean {
  const row = researchObject(value), returned = researchObject(row?.scope);
  return status === 401 || status === 403 || row?.schema === RESEARCH_SCHEMA && returned && researchId(returned.workspaceId) && researchId(returned.principalId) && (returned.workspaceId !== scope.workspaceId || returned.principalId !== scope.principalId) ? true : false;
}
export function researchActionMatchesContext(action: ResearchAction, context: ResearchContext): boolean {
  const scope = action.scope, task = context.task, need = context.need;
  return !!need && scope.workspace_id === context.scope.workspaceId && scope.task_id === task.episodeId && scope.contract_revision_id === task.contractRevisionId && scope.repository_state_id === task.repositoryStateId && scope.semantic_release_digest === task.semanticReleaseSha256 && scope.need_id === need.id && scope.need_revision_id === need.revisionId && action.query.target_version === context.policy?.sha256 && action.query.max_results === context.policy.content.max_results && action.query.max_source_bytes === context.policy.content.max_source_bytes;
}
export function researchCanQueue(action: ResearchAction, context: ResearchContext): boolean { return context.current && context.task.status === "running" && context.need?.status === "open" && context.policy?.content.allow_query_disclosure === true && context.policy.content.providers.length > 0 && researchActionMatchesContext(action, context); }
export function researchCitationFitsContext(citation: ResearchCitation, context: ResearchContext): boolean {
  const source = citation.researchScope, task = context.task, need = context.need;
  return !!need && source.workspace_id === context.scope.workspaceId && source.task_id === task.episodeId && source.contract_revision_id === task.contractRevisionId && source.repository_state_id === task.repositoryStateId && source.semantic_release_digest === task.semanticReleaseSha256 && source.need_id === need.id && source.need_revision_id === need.revisionId;
}
export function researchJournalScope(workspace: string, principal: string): string { return "research:" + workshopJournalScope(workspace, principal); }
export function researchAttemptRoute(path: string): string[] | null { return path.startsWith(RESEARCH_ENDPOINT + "/") ? path.slice(RESEARCH_ENDPOINT.length + 1).split("/") : null; }
export function newResearchAttempt(path: string, body: unknown): WorkshopAttempt { return { path, body: JSON.stringify(body), key: "research:" + crypto.randomUUID() }; }
export function validateResearchAttempt(value: unknown): WorkshopAttempt | null {
  const row = researchObject(value);
  if (!row || !researchKeys(row, ["path", "body", "key"]) || typeof row.path !== "string" || typeof row.body !== "string" || new TextEncoder().encode(row.body).length > MAX_RESEARCH_REQUEST || typeof row.key !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(row.key)) return null;
  const route = researchAttemptRoute(row.path); if (!route) return null;
  try { return validateResearchMutation(route, parseMcpJSON(row.body)) ? { path: row.path, body: row.body, key: row.key } : null; } catch { return null; }
}
export async function researchCitationRequest(runId: string, sourceId: string, extraction: ResearchExtraction, startByte: number, endByte: number): Promise<CitationSpec> {
  if (extraction.status !== "extracted" || !extraction.complete || await researchTextSha256(extraction.text) !== extraction.textSha256) throw new Error("The retained extracted source is incomplete or its text hash does not match.");
  const quote = researchQuote(extraction.text, startByte, endByte); if (!quote || quote.bytes.length > 16384) throw new Error("Choose a nonempty UTF-8 byte range of at most 16,384 bytes on codepoint boundaries.");
  const locations = extraction.segments.filter(segment => segment.startByte < endByte && segment.endByte > startByte);
  if (locations.length < 1 || locations.length > 64) throw new Error("Choose a quote that intersects between one and 64 authored source locations; a generated separator alone is not a citation.");
  const spec = projectCitationSpec({ runId, sourceId, startByte, endByte, quoteSha256: await researchTextSha256(quote.text) }); if (!spec) throw new Error("The exact source and citation identities are invalid."); return spec;
}
export async function researchCitationMatchesSource(citation: ResearchCitation, spec: CitationSpec, extraction: ResearchExtraction): Promise<boolean> {
  const quote = researchQuote(extraction.text, spec.startByte, spec.endByte);
  return !!quote && sameTaskValue({ runId: citation.runId, sourceId: citation.sourceId, startByte: citation.startByte, endByte: citation.endByte, quoteSha256: citation.quoteSha256 }, spec) && citation.rawSha256 === extraction.sourceSha256 && citation.textSha256 === extraction.textSha256 && quote.text === citation.quote && await researchTextSha256(quote.text) === spec.quoteSha256;
}

/** Fixed research projections. Source text is data, never a browser instruction. */
import { UUID, SHA256 } from "./operation-workbench.ts";
import { taskObject, sameTaskValue, parseMcpJSON } from "./task-discovery.ts";
import { researchUTF8 } from "./research-text.ts";
export { UUID, SHA256, sameTaskValue, parseMcpJSON };
export const RESEARCH_SCHEMA = "potbot-research-workbench/v1" as const;
export const MAX_RESEARCH_REQUEST = 131072, MAX_RESEARCH_RESPONSE = 1500000;
export type ResearchScope = { workspaceId: string; principalId: string };
export type ResearchEnvelope = { schema: typeof RESEARCH_SCHEMA; scope: ResearchScope };
export type NeedSpec = { schema_version: 1; question: string; pending_decision: string; alternatives: string[]; required_evidence: string[]; resolution_criteria: string[]; scope: string; known_evidence: string[]; priority: number; budget: Record<string, number>; query_profiles: string[] };
export type NeedStatus = "open" | "resolved" | "blocked" | "cancelled";
export type ResearchNeed = { id: string; revisionId: string; contentSha256: string; status: NeedStatus; spec: NeedSpec };
export type ResearchNeedSummary = Omit<ResearchNeed, "spec"> & { question: string };
export type ResearchTask = { episodeId: string; contractRevisionId: string; repositoryStateId: string; semanticReleaseSha256: string; status: string };
export type ResearchActionScope = { workspace_id: string; task_id: string; contract_revision_id: string; need_id: string; need_revision_id: string; repository_state_id: string; semantic_release_digest: string };
export type ResearchQuery = { category: "documentation"; operation: "query_source"; locator: "external-research"; search_text: string; required_identifiers: string[]; expected_observation: string; decision_effect: string; evidence_requirement: string; stopping_condition: string; fallback: string; target_version: string; freshness_seconds: number; max_results: number; max_source_bytes: number };
export type ResearchAction = { schema_version: 1; primary_type_ref: "synny://type/potbot-research-action"; action_id: string; scope: ResearchActionScope; query: ResearchQuery; predecessor_id: string | null; identity: string };
export type ResearchPolicy = { schema: "potbot-external-research-policy/v1"; allow_query_disclosure: boolean; providers: { provider: ResearchProviderId; cost_microunits_per_call: number; minimum_interval_ms: number }[]; fetch_hosts: { host: string; terms_approved: boolean; minimum_interval_ms: number }[]; fallback_statuses: ("rate_limited" | "quota_exhausted" | "unavailable")[]; max_results: number; max_documents: number; max_requests: number; max_source_bytes: number; timeout_seconds: number };
export type ResearchProviderId = keyof typeof RESEARCH_ENDPOINTS;
export type ResearchProvider = { id: ResearchProviderId; method: "POST"; endpoint: string; adapterRevision: 1; mode: "web_search" | "specialist_retrieval"; scope: "explicit_query_disclosure" };
export type ResearchContext = ResearchEnvelope & { task: ResearchTask; needs: ResearchNeedSummary[]; need: ResearchNeed | null; policy: { sha256: string; content: ResearchPolicy } | null; providerDescriptors: ResearchProvider[]; contextSha256: string; queryTemplate: ResearchQuery | null; current: boolean; reasonCodes: string[]; visibility: "workspace" };
export type ResearchActionReceipt = ResearchEnvelope & { action: ResearchAction; actionIdentity: string; reused: boolean };
export type ResearchNeedReceipt = ResearchEnvelope & { need: ResearchNeed };
export type ResearchSegment = { startByte: number; endByte: number; kind: "text" | "html_element" | "json_pointer" | "pdf_page"; locator: string; page: number | null };
export type ResearchExtraction = { schema: "potbot-extracted-source/v1"; status: "extracted" | "unsupported" | "invalid" | "over_limit" | "extractor_unavailable"; sourceSha256: string; mediaType: string; extractorId: string; extractorSha256: string; text: string; textSha256: string; segments: ResearchSegment[]; metadata: { title: string | null; publishedAt: string | null; publicationProvenance: "source_reported" | null }; execution: { profileSha256: string; executableSha256: string; isolationSha256: string | null; elapsedMs: number; processesStopped: boolean } | null; complete: boolean; license: "unknown"; reasonCodes: string[] };
export type CitationSpec = { runId: string; sourceId: string; startByte: number; endByte: number; quoteSha256: string };
export type ResearchCitation = CitationSpec & { schema: "potbot-research-citation/v1"; rawSha256: string; textSha256: string; receiptSha256: string; quote: string; requestedUrl: string; resolvedUrl: string; retrievedAt: string; publishedAt: string | null; publicationProvenance: "source_reported" | null; license: "unknown"; lineStart: number; lineEnd: number; segments: ResearchSegment[]; researchScope: ResearchActionScope; freshnessSeconds: number; sha256: string };
export type ResearchClaimSpec = { key: string; label: string; citations: CitationSpec[] };
export type ResearchClaim = { key: string; label: string; status: "supported" | "unresolved" | "unsupported"; citations: ResearchCitation[]; values: string[]; reasonCodes: string[]; unavailableCitationCount: number };
export type ResearchComparison = { schema: "potbot-research-comparison/v1"; workspaceId: string; episodeId: string; needId: string; needRevisionId: string; contextSha256: string; evaluatedAt: string; claims: ResearchClaim[]; status: "supported" | "unresolved" | "unsupported"; qualification: "verified_cited_source_agreement_only"; needResolved: false; sha256: string };
export type ResearchComparisonEnvelope = ResearchEnvelope & { evidenceId: string; report: ResearchComparison };
export type ResearchCitationEnvelope = ResearchEnvelope & { citation: ResearchCitation };

export const researchObject = taskObject;
export const researchKeys = (row: Record<string, unknown>, fields: readonly string[]) => Object.keys(row).sort().join(",") === [...fields].sort().join(",");
export const researchId = (value: unknown): value is string => typeof value === "string" && UUID.test(value);
export const researchSha = (value: unknown): value is string => typeof value === "string" && SHA256.test(value);
export const researchInteger = (value: unknown, min = 0, max = Number.MAX_SAFE_INTEGER): value is number => Number.isSafeInteger(value) && Number(value) >= min && Number(value) <= max;
export const researchText = (value: unknown, max = 8192, nonempty = true): value is string => typeof value === "string" && (!nonempty || !!value.trim()) && Array.from(value).length <= max && !value.includes("\0") && researchUTF8(value) !== null;
export const researchBytes = (value: unknown, max: number, nonempty = true): value is string => researchText(value, max, nonempty) && researchUTF8(value)!.length <= max;
export const researchCodes = (value: unknown): value is string[] => Array.isArray(value) && value.length <= 32 && value.every(item => typeof item === "string" && /^[a-z][a-z0-9_]{0,99}$/.test(item)) && new Set(value).size === value.length;
export const researchTime = (value: unknown): value is string => typeof value === "string" && value.length <= 64 && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value) && Number.isFinite(Date.parse(value));
export const researchCursor = (value: unknown): value is string => typeof value === "string" && value.length > 0 && value.length <= 512 && !/[\u0000-\u0020\u007f]/.test(value);
export function researchURL(value: unknown): value is string {
  if (typeof value !== "string" || value.length > 2048 || !/^https:\/\/[!-~]+$/.test(value) || /[\\#]/.test(value)) return false;
  try { const url = new URL(value); return url.protocol === "https:" && !url.username && !url.password && !url.port && url.hostname === url.host && value.startsWith(`https://${url.hostname}/`); } catch { return false; }
}
const strings = (value: unknown, max = 128, size = 8192): value is string[] => Array.isArray(value) && value.length <= max && value.every(item => researchText(item, size, false) && item.length > 0);
const distinct = (value: unknown[]): boolean => new Set(value).size === value.length;
const taskStates = ["submitted", "initializing", "running", "paused", "blocked", "verified", "partial", "budget_exhausted", "cancelled", "failed"];
const needStates = ["open", "resolved", "blocked", "cancelled"];
const NEED_FIELDS = ["schema_version", "question", "pending_decision", "alternatives", "required_evidence", "resolution_criteria", "scope", "known_evidence", "priority", "budget", "query_profiles"];
const QUERY_FIELDS = ["category", "operation", "locator", "search_text", "required_identifiers", "expected_observation", "decision_effect", "evidence_requirement", "stopping_condition", "fallback", "target_version", "freshness_seconds", "max_results", "max_source_bytes"];
export function projectResearchAmounts(value: unknown): Record<string, number> | null {
  const row = taskObject(value); if (!row || Object.keys(row).length > 128 || Object.entries(row).some(([name, amount]) => !/^[A-Za-z][A-Za-z0-9_.-]{0,95}$/.test(name) || !researchInteger(amount) || name === "concurrency" && amount === 0)) return null;
  return { ...row } as Record<string, number>;
}
export function projectNeedSpec(value: unknown): NeedSpec | null {
  const row = taskObject(value), budget = projectResearchAmounts(row?.budget);
  if (!row || !researchKeys(row, NEED_FIELDS) || row.schema_version !== 1 || ![row.question, row.pending_decision, row.scope].every(value => researchText(value, 8000)) || ![row.alternatives, row.required_evidence, row.resolution_criteria, row.query_profiles].every(value => strings(value)) || !(row.required_evidence as string[]).length || !(row.resolution_criteria as string[]).length || !distinct(row.query_profiles as string[]) || !Array.isArray(row.known_evidence) || row.known_evidence.length > 128 || !row.known_evidence.every(researchId) || !distinct(row.known_evidence) || !researchInteger(row.priority, 0, 100) || !budget) return null;
  return { ...structuredClone(row), budget } as NeedSpec;
}
export function projectResearchNeed(value: unknown): ResearchNeed | null {
  const row = taskObject(value), spec = projectNeedSpec(row?.spec);
  if (!row || !researchKeys(row, ["id", "revisionId", "contentSha256", "status", "spec"]) || !researchId(row.id) || !researchId(row.revisionId) || !researchSha(row.contentSha256) || !needStates.includes(String(row.status)) || !spec) return null;
  return { id: row.id, revisionId: row.revisionId, contentSha256: row.contentSha256, status: row.status as NeedStatus, spec };
}
export function projectResearchActionScope(value: unknown): ResearchActionScope | null {
  const row = taskObject(value), ids = ["workspace_id", "task_id", "contract_revision_id", "need_id", "need_revision_id", "repository_state_id"];
  return row && researchKeys(row, [...ids, "semantic_release_digest"]) && ids.every(key => researchId(row[key])) && researchSha(row.semantic_release_digest) ? { ...row } as ResearchActionScope : null;
}
export function projectResearchQuery(value: unknown): ResearchQuery | null {
  const row = taskObject(value);
  if (!row || !researchKeys(row, QUERY_FIELDS) || row.category !== "documentation" || row.operation !== "query_source" || row.locator !== "external-research" || !researchSha(row.target_version) || ![row.search_text, row.expected_observation, row.decision_effect, row.evidence_requirement, row.stopping_condition, row.fallback].every(value => researchBytes(value, 1024) && !/[\u0000-\u0008\u000b-\u001f\u007f]/.test(value)) || !strings(row.required_identifiers, 24, 160) || !distinct(row.required_identifiers) || !row.required_identifiers.every(value => researchBytes(value, 256) && !/[\s\u0000-\u001f]/.test(value) && (row.search_text as string).includes(value)) || !researchInteger(row.freshness_seconds, 0, 30 * 86400) || !researchInteger(row.max_results, 1, 20) || !researchInteger(row.max_source_bytes, 1, 262144)) return null;
  return structuredClone(row) as ResearchQuery;
}
/** ResearchAction.identity uses Python split whitespace and sorted identifiers. */
export function researchQueryEquivalent(left: ResearchQuery, right: ResearchQuery): boolean {
  const normalize = (value: ResearchQuery) => ({ ...value, search_text: value.search_text.split(/[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/).filter(Boolean).join(" "), required_identifiers: [...value.required_identifiers].sort() });
  return sameTaskValue(normalize(left), normalize(right));
}
export function projectResearchAction(value: unknown, workspace: string): ResearchAction | null {
  const row = taskObject(value), scope = projectResearchActionScope(row?.scope), query = projectResearchQuery(row?.query);
  return row && researchKeys(row, ["schema_version", "primary_type_ref", "action_id", "scope", "query", "predecessor_id", "identity"]) && row.schema_version === 1 && row.primary_type_ref === "synny://type/potbot-research-action" && researchId(row.action_id) && (row.predecessor_id === null || researchId(row.predecessor_id)) && researchSha(row.identity) && scope?.workspace_id === workspace && query ? { schema_version: 1, primary_type_ref: "synny://type/potbot-research-action", action_id: row.action_id, scope, query, predecessor_id: row.predecessor_id as string | null, identity: row.identity } : null;
}
export function projectResearchPolicy(value: unknown): ResearchPolicy | null {
  const row = taskObject(value);
  if (!row || !researchKeys(row, ["schema", "allow_query_disclosure", "providers", "fetch_hosts", "fallback_statuses", "max_results", "max_documents", "max_requests", "max_source_bytes", "timeout_seconds"]) || row.schema !== "potbot-external-research-policy/v1" || typeof row.allow_query_disclosure !== "boolean" || !Array.isArray(row.providers) || row.providers.length > RESEARCH_PROVIDER_IDS.length || !Array.isArray(row.fetch_hosts) || row.fetch_hosts.length > 32 || !strings(row.fallback_statuses, 3, 32) || !distinct(row.fallback_statuses) || !row.fallback_statuses.every(value => ["rate_limited", "quota_exhausted", "unavailable"].includes(value)) || !researchInteger(row.max_results, 1, 20) || !researchInteger(row.max_documents, 0, 8) || !researchInteger(row.max_requests, 1, 32) || !researchInteger(row.max_source_bytes, 1, 262144) || !researchInteger(row.timeout_seconds, 1, 30)) return null;
  for (const raw of row.providers) { const item = taskObject(raw); if (!item || !researchKeys(item, ["provider", "cost_microunits_per_call", "minimum_interval_ms"]) || !researchProviderId(item.provider) || !researchInteger(item.cost_microunits_per_call, 0, 1e12) || !researchInteger(item.minimum_interval_ms, 0, 3600000)) return null; }
  for (const raw of row.fetch_hosts) { const item = taskObject(raw); if (!item || !researchKeys(item, ["host", "terms_approved", "minimum_interval_ms"]) || typeof item.host !== "string" || item.host.length > 253 || !/^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z][a-z0-9-]*$/.test(item.host) || typeof item.terms_approved !== "boolean" || !researchInteger(item.minimum_interval_ms, 0, 3600000)) return null; }
  if (!distinct(row.providers.map(item => item.provider)) || !distinct(row.fetch_hosts.map(item => item.host))) return null;
  return structuredClone(row) as ResearchPolicy;
}
/** Current admission ceiling preview; the native budget owner makes the decision. */
export function researchReservationPreview(policy: ResearchPolicy): Record<string, number> {
  const interval = Math.max(0, ...policy.providers.map(researchProviderInterval), ...policy.fetch_hosts.map(item => item.minimum_interval_ms));
  return { calls: policy.max_requests, tool_ms: policy.max_requests * (policy.timeout_seconds * 1000 + interval) + policy.max_documents * 5000, bytes_read: policy.max_requests * policy.max_source_bytes, money_microunits: policy.max_requests * Math.max(0, ...policy.providers.map(item => item.cost_microunits_per_call)), concurrency: 1 };
}
export const RESEARCH_PROVIDER_INTERVALS: Record<ResearchProviderId, number> = { brave: 0, tavily: 0, crossref: 1000, openalex: 1000, arxiv: 3000, pubmed: 400, semantic_scholar: 1000, wikidata: 1000, internet_archive: 1000 };
export const researchProviderInterval = (rule: ResearchPolicy["providers"][number]): number => Math.max(rule.minimum_interval_ms, RESEARCH_PROVIDER_INTERVALS[rule.provider]);
export const researchProviderRequestAllowance = (policy: ResearchPolicy): number => policy.providers.reduce((count, item) => count + (item.provider === "pubmed" ? 2 : 1), 0);
export const RESEARCH_ENDPOINTS = {
  brave: "https://api.search.brave.com/res/v1/web/search",
  tavily: "https://api.tavily.com/search",
  crossref: "https://api.crossref.org/works",
  openalex: "https://api.openalex.org/works",
  arxiv: "https://export.arxiv.org/api/query",
  pubmed: "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
  semantic_scholar: "https://api.semanticscholar.org/graph/v1/paper/search",
  wikidata: "https://query.wikidata.org/sparql",
  internet_archive: "https://archive.org/advancedsearch.php",
} as const;
export const RESEARCH_PROVIDER_IDS = Object.keys(RESEARCH_ENDPOINTS) as ResearchProviderId[];
export const researchProviderId = (value: unknown): value is ResearchProviderId => typeof value === "string" && Object.hasOwn(RESEARCH_ENDPOINTS, value);
export const researchSpecialistProvider = (value: unknown): value is ResearchProviderId => researchProviderId(value) && value !== "brave" && value !== "tavily";
export function projectResearchProvider(value: unknown): ResearchProvider | null {
  const row = taskObject(value);
  return row && researchKeys(row, ["id", "method", "endpoint", "adapterRevision", "mode", "scope"]) && researchProviderId(row.id) && row.method === "POST" && row.endpoint === RESEARCH_ENDPOINTS[row.id] && row.adapterRevision === 1 && row.mode === (researchSpecialistProvider(row.id) ? "specialist_retrieval" : "web_search") && row.scope === "explicit_query_disclosure" ? { ...row } as ResearchProvider : null;
}
export function projectResearchEnvelope(value: unknown, workspace: string, principal?: string): ResearchEnvelope | null {
  const row = taskObject(value), scope = taskObject(row?.scope);
  return row?.schema === RESEARCH_SCHEMA && scope && researchKeys(scope, ["workspaceId", "principalId"]) && researchId(workspace) && scope.workspaceId === workspace && researchId(scope.principalId) && (!principal || scope.principalId === principal) ? { schema: RESEARCH_SCHEMA, scope: { workspaceId: workspace, principalId: scope.principalId } } : null;
}
export function projectResearchContext(value: unknown, workspace: string, episode?: string, state?: string, needId?: string | null, principal?: string): ResearchContext | null {
  const row = taskObject(value), envelope = projectResearchEnvelope(value, workspace, principal), task = taskObject(row?.task);
  if (!row || !envelope || !task || !researchKeys(task, ["episodeId", "contractRevisionId", "repositoryStateId", "semanticReleaseSha256", "status"]) || ![task.episodeId, task.contractRevisionId, task.repositoryStateId].every(researchId) || !researchSha(task.semanticReleaseSha256) || !taskStates.includes(String(task.status)) || episode && task.episodeId !== episode || state && task.repositoryStateId !== state || !Array.isArray(row.needs) || row.needs.length > 25 || !Array.isArray(row.providerDescriptors) || row.providerDescriptors.length > RESEARCH_PROVIDER_IDS.length || !researchSha(row.contextSha256) || typeof row.current !== "boolean" || !researchCodes(row.reasonCodes) || row.visibility !== "workspace") return null;
  const needs: ResearchNeedSummary[] = [];
  for (const raw of row.needs) { const item = taskObject(raw); if (!item || !researchKeys(item, ["id", "revisionId", "contentSha256", "status", "question"]) || !researchId(item.id) || !researchId(item.revisionId) || !researchSha(item.contentSha256) || !needStates.includes(String(item.status)) || !researchText(item.question, 512) || needs.some(need => need.id === item.id)) return null; needs.push({ ...item } as ResearchNeedSummary); }
  const need = row.need === null ? null : projectResearchNeed(row.need), rawPolicy = taskObject(row.policy), policyContent = projectResearchPolicy(rawPolicy?.content), queryTemplate = row.queryTemplate === null ? null : projectResearchQuery(row.queryTemplate), providers = row.providerDescriptors.map(projectResearchProvider);
  if (!(row.need === null || need) || (needId === null && need !== null || needId && need?.id !== needId) || row.policy !== null && (!rawPolicy || !researchKeys(rawPolicy, ["sha256", "content"]) || !researchSha(rawPolicy.sha256) || !policyContent) || !(row.queryTemplate === null || queryTemplate) || providers.some(item => item === null) || !distinct(providers.map(item => item?.id))) return null;
  const policy = rawPolicy && policyContent ? { sha256: rawPolicy.sha256 as string, content: policyContent } : null;
  if (queryTemplate && (!need || !policy || queryTemplate.target_version !== policy.sha256 || queryTemplate.max_results !== policy.content.max_results || queryTemplate.max_source_bytes !== policy.content.max_source_bytes) || row.current && (task.status !== "running" || !need || need.status !== "open" || !policy) || !sameTaskValue(providers.map(item => item?.id), policy?.content.providers.map(item => item.provider) ?? []) || need && needs.some(item => item.id === need.id && (item.revisionId !== need.revisionId || item.contentSha256 !== need.contentSha256 || item.status !== need.status || item.question !== Array.from(need.spec.question).slice(0, 512).join("")))) return null;
  return { ...envelope, task: { ...task } as ResearchTask, needs, need, policy, providerDescriptors: providers as ResearchProvider[], contextSha256: row.contextSha256, queryTemplate, current: row.current, reasonCodes: [...row.reasonCodes], visibility: "workspace" };
}
export function projectResearchSegment(value: unknown, maximum = 4 * 1024 * 1024): ResearchSegment | null {
  const row = taskObject(value);
  return row && researchKeys(row, ["startByte", "endByte", "kind", "locator", "page"]) && researchInteger(row.startByte, 0, maximum) && researchInteger(row.endByte, row.startByte, maximum) && ["text", "html_element", "json_pointer", "pdf_page"].includes(String(row.kind)) && researchBytes(row.locator, 2048, false) && (row.page === null || researchInteger(row.page, 1, 10000)) ? { ...row } as ResearchSegment : null;
}
export function projectCitationSpec(value: unknown): CitationSpec | null {
  const row = taskObject(value);
  return row && researchKeys(row, ["runId", "sourceId", "startByte", "endByte", "quoteSha256"]) && researchId(row.runId) && researchSha(row.sourceId) && researchSha(row.quoteSha256) && researchInteger(row.startByte, 0, 4 * 1024 * 1024) && researchInteger(row.endByte, row.startByte + 1, 4 * 1024 * 1024) && row.endByte - row.startByte <= 16384 ? { ...row } as CitationSpec : null;
}
export function citationSpec(citation: ResearchCitation): CitationSpec { return { runId: citation.runId, sourceId: citation.sourceId, startByte: citation.startByte, endByte: citation.endByte, quoteSha256: citation.quoteSha256 }; }
export function projectResearchCitation(value: unknown, workspace: string): ResearchCitation | null {
  const row = taskObject(value); if (!row || !researchKeys(row, ["schema", "runId", "sourceId", "startByte", "endByte", "quoteSha256", "rawSha256", "textSha256", "receiptSha256", "quote", "requestedUrl", "resolvedUrl", "retrievedAt", "publishedAt", "publicationProvenance", "license", "lineStart", "lineEnd", "segments", "researchScope", "freshnessSeconds", "sha256"]) || row.schema !== "potbot-research-citation/v1") return null;
  const spec = projectCitationSpec(Object.fromEntries(["runId", "sourceId", "startByte", "endByte", "quoteSha256"].map(key => [key, row[key]]))), scope = projectResearchActionScope(row.researchScope);
  if (!spec || scope?.workspace_id !== workspace || ![row.rawSha256, row.textSha256, row.receiptSha256, row.sha256].every(researchSha) || !researchBytes(row.quote, 16384, false) || researchUTF8(row.quote)!.length !== spec.endByte - spec.startByte || row.quote.includes("\r") || !researchURL(row.requestedUrl) || !researchURL(row.resolvedUrl) || !researchTime(row.retrievedAt) || !(row.publishedAt === null ? row.publicationProvenance === null : researchTime(row.publishedAt) && row.publicationProvenance === "source_reported") || row.license !== "unknown" || !researchInteger(row.lineStart, 1) || !researchInteger(row.lineEnd, row.lineStart) || row.lineEnd - row.lineStart !== ((row.quote.match(/\n/g)?.length ?? 0) - (row.quote.endsWith("\n") ? 1 : 0)) || !Array.isArray(row.segments) || row.segments.length < 1 || row.segments.length > 64 || !researchInteger(row.freshnessSeconds, 0, 30 * 86400)) return null;
  const segments = row.segments.map(item => projectResearchSegment(item));
  if (segments.some(item => !item || item.startByte >= spec.endByte || item.endByte <= spec.startByte)) return null;
  return { ...structuredClone(row), researchScope: scope, segments } as ResearchCitation;
}
export function projectResearchClaims(value: unknown): ResearchClaimSpec[] | null {
  if (!Array.isArray(value) || value.length < 1 || value.length > 16) return null;
  const claims: ResearchClaimSpec[] = []; let total = 0;
  for (const raw of value) {
    const row = taskObject(raw); if (!row || !researchKeys(row, ["key", "label", "citations"]) || typeof row.key !== "string" || !/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$/.test(row.key) || claims.some(item => item.key === row.key) || !researchBytes(row.label, 512) || /[\u0000-\u001f]/.test(row.label) || !Array.isArray(row.citations) || row.citations.length < 1 || row.citations.length > 16) return null;
    const citations = row.citations.map(projectCitationSpec);
    if (citations.some(item => !item) || citations.some((item, index) => citations.slice(0, index).some(prior => sameTaskValue(item, prior))) || (total += citations.length) > 32) return null;
    claims.push({ key: row.key, label: row.label, citations: citations as CitationSpec[] });
  }
  return new TextEncoder().encode(JSON.stringify(claims)).length <= 65536 ? claims : null;
}
export function projectResearchComparison(value: unknown, workspace: string): ResearchComparison | null {
  const row = taskObject(value);
  if (!row || !researchKeys(row, ["schema", "workspaceId", "episodeId", "needId", "needRevisionId", "contextSha256", "evaluatedAt", "claims", "status", "qualification", "needResolved", "sha256"]) || row.schema !== "potbot-research-comparison/v1" || row.workspaceId !== workspace || ![row.episodeId, row.needId, row.needRevisionId].every(researchId) || !researchSha(row.contextSha256) || !researchSha(row.sha256) || !researchTime(row.evaluatedAt) || !["supported", "unresolved", "unsupported"].includes(String(row.status)) || row.qualification !== "verified_cited_source_agreement_only" || row.needResolved !== false || !Array.isArray(row.claims) || row.claims.length < 1 || row.claims.length > 16) return null;
  const claims: ResearchClaim[] = []; let total = 0;
  for (const raw of row.claims) {
    const item = taskObject(raw); if (!item || !researchKeys(item, ["key", "label", "status", "citations", "values", "reasonCodes", "unavailableCitationCount"]) || typeof item.key !== "string" || !/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$/.test(item.key) || claims.some(claim => claim.key === item.key) || !researchBytes(item.label, 512) || /[\u0000-\u001f]/.test(item.label) || !["supported", "unresolved", "unsupported"].includes(String(item.status)) || !Array.isArray(item.citations) || item.citations.length > 16 || !Array.isArray(item.values) || item.values.length > 16 || !item.values.every(value => researchBytes(value, 16384, false)) || !researchCodes(item.reasonCodes) || !researchInteger(item.unavailableCitationCount, 0, 16)) return null;
    const citations = item.citations.map(value => projectResearchCitation(value, workspace));
    if (citations.some(citation => !citation || citation.researchScope.task_id !== row.episodeId || citation.researchScope.need_id !== row.needId || citation.researchScope.need_revision_id !== row.needRevisionId) || (total += citations.length + item.unavailableCitationCount) > 32 || citations.length + item.unavailableCitationCount < 1 || citations.length + item.unavailableCitationCount > 16 || citations.some((citation, index) => citations.slice(0, index).some(prior => sameTaskValue(citationSpec(citation!), citationSpec(prior!))))) return null;
    const verified = citations as ResearchCitation[], values = [...new Set(verified.map(citation => citation.quote))];
    if (!sameTaskValue(item.values, values) || item.status !== (!verified.length ? "unsupported" : item.reasonCodes.length ? "unresolved" : "supported") || values.length > 1 && !item.reasonCodes.includes("conflicting_cited_values") || item.unavailableCitationCount && !item.reasonCodes.length) return null;
    if (item.status === "supported" && verified.some(citation => citation.freshnessSeconds > 0 && (citation.publishedAt === null || Date.parse(row.evaluatedAt as string) < Date.parse(citation.publishedAt) || Date.parse(row.evaluatedAt as string) - Date.parse(citation.publishedAt) > citation.freshnessSeconds * 1000))) return null;
    claims.push({ key: item.key, label: item.label, status: item.status as ResearchClaim["status"], citations: verified, values, reasonCodes: [...item.reasonCodes], unavailableCitationCount: item.unavailableCitationCount });
  }
  const status = claims.every(claim => claim.status === "supported") ? "supported" : claims.every(claim => claim.status === "unsupported") ? "unsupported" : "unresolved";
  return row.status === status ? { ...structuredClone(row), claims } as ResearchComparison : null;
}
export function projectResearchCitationEnvelope(value: unknown, workspace: string, expected?: CitationSpec, principal?: string): ResearchCitationEnvelope | null {
  const row = taskObject(value), envelope = projectResearchEnvelope(value, workspace, principal), citation = projectResearchCitation(row?.citation, workspace);
  return envelope && citation && (!expected || sameTaskValue(citationSpec(citation), expected)) ? { ...envelope, citation } : null;
}
export function projectResearchComparisonEnvelope(value: unknown, workspace: string, evidenceId?: string, principal?: string): ResearchComparisonEnvelope | null {
  const row = taskObject(value), envelope = projectResearchEnvelope(value, workspace, principal), report = projectResearchComparison(row?.report, workspace);
  return row && envelope && report && researchId(row.evidenceId) && (!evidenceId || evidenceId === row.evidenceId) ? { ...envelope, evidenceId: row.evidenceId, report } : null;
}
export function validateResearchMutation(path: string[], value: unknown): Record<string, unknown> | null {
  const row = taskObject(value); if (!row || row.confirm !== true) return null;
  if (path.length === 3 && path[0] === "runs" && researchId(path[1]) && path[2] === "cancel") return researchKeys(row, ["confirm"]) ? { confirm: true } : null;
  if (path.length !== 1) return null;
  if (path[0] === "needs") { const spec = projectNeedSpec(row.spec); return researchKeys(row, ["episodeId", "repositoryStateId", "spec", "confirm"]) && researchId(row.episodeId) && researchId(row.repositoryStateId) && spec ? { episodeId: row.episodeId, repositoryStateId: row.repositoryStateId, spec, confirm: true } : null; }
  if (path[0] === "actions") { const query = projectResearchQuery(row.query); return researchKeys(row, ["episodeId", "repositoryStateId", "needId", "contextSha256", "query", "confirm"]) && researchId(row.episodeId) && researchId(row.repositoryStateId) && researchId(row.needId) && researchSha(row.contextSha256) && query && Array.from(query.search_text).length <= 500 ? { episodeId: row.episodeId, repositoryStateId: row.repositoryStateId, needId: row.needId, contextSha256: row.contextSha256, query, confirm: true } : null; }
  if (path[0] === "runs") return researchKeys(row, ["actionId", "policySha256", "disclosureConfirmed", "confirm"]) && researchId(row.actionId) && researchSha(row.policySha256) && row.disclosureConfirmed === true ? { actionId: row.actionId, policySha256: row.policySha256, disclosureConfirmed: true, confirm: true } : null;
  if (path[0] === "comparisons") { const claims = projectResearchClaims(row.claims); return researchKeys(row, ["episodeId", "needId", "contextSha256", "claims", "confirm"]) && researchId(row.episodeId) && researchId(row.needId) && researchSha(row.contextSha256) && claims ? { episodeId: row.episodeId, needId: row.needId, contextSha256: row.contextSha256, claims, confirm: true } : null; }
  return null;
}
export function projectResearchNonRunMutation(value: unknown, workspace: string, path: string[], body: Record<string, unknown>, principal?: string): ResearchNeedReceipt | ResearchActionReceipt | ResearchComparisonEnvelope | null {
  const row = taskObject(value), envelope = projectResearchEnvelope(value, workspace, principal); if (!row || !envelope || path.length !== 1) return null;
  if (path[0] === "needs") { const need = projectResearchNeed(row.need); return need && sameTaskValue(need.spec, body.spec) ? { ...envelope, need } : null; }
  if (path[0] === "actions") { const action = projectResearchAction(row.action, workspace), query = projectResearchQuery(body.query); return action && query && action.scope.task_id === body.episodeId && action.scope.repository_state_id === body.repositoryStateId && action.scope.need_id === body.needId && action.predecessor_id === null && (row.reused === true ? researchQueryEquivalent(action.query, query) : sameTaskValue(action.query, query)) && action.identity === row.actionIdentity && typeof row.reused === "boolean" ? { ...envelope, action, actionIdentity: action.identity, reused: row.reused } : null; }
  if (path[0] === "comparisons") {
    const receipt = projectResearchComparisonEnvelope(value, workspace, undefined, principal), claims = projectResearchClaims(body.claims);
    if (!receipt || !claims || receipt.report.episodeId !== body.episodeId || receipt.report.needId !== body.needId || receipt.report.contextSha256 !== body.contextSha256 || receipt.report.claims.length !== claims.length || receipt.report.claims.some((claim, index) => claim.key !== claims[index].key || claim.label !== claims[index].label || claim.citations.length + claim.unavailableCitationCount !== claims[index].citations.length || claim.citations.some(citation => !claims[index].citations.some(spec => sameTaskValue(spec, citationSpec(citation)))))) return null;
    return receipt;
  }
  return null;
}

import test from 'node:test';
import assert from 'node:assert/strict';
import * as dto from '../src/lib/research.ts';
import * as run from '../src/lib/research-execution.ts';
import * as metadata from '../src/lib/research-specialist.ts';
import * as f from './research-fixtures.mjs';

const date = { value: '2024-02-29', precision: 'day', provenance: 'provider_reported' };
const specialist = {
  schema: 'potbot-specialist-metadata/v1', recordId: '10.1234/example', doi: '10.1234/example',
  authors: ['Ada Lovelace', 'Grace Hopper'], publicationDate: date, eventDate: null, updatedDate: null,
  sourceRole: 'secondary', recordKind: 'publisher_registry', retraction: { state: 'not_reported', notices: [] },
};
const item = (provider, changes = {}) => ({ ...f.searchItem, provider, title: 'A sufficiently specific paper title', specialist: structuredClone(specialist), ...changes });
function resultFor(items, groups = [items]) {
  const providers = [...new Set(items.map(item => item.provider))];
  const requests = providers.map((provider, index) => ({ ...structuredClone(f.requests[0]), id: f.uid(100 + index), ordinal: index + 1, requestSha256: f.hash('specialist-request-' + provider), responseSha256: f.hash('specialist-response-' + provider), request: { ...f.requests[0].request, provider, url: dto.RESEARCH_ENDPOINTS[provider] }, response: { ...f.requests[0].response, requestSha256: f.hash('specialist-request-' + provider) } }));
  const policy = { ...f.policyContent, providers: providers.map(provider => ({ provider, cost_microunits_per_call: 0, minimum_interval_ms: 1000 })) };
  const execution = { ...f.execution, run: { ...f.execution.run, requestCount: requests.length }, authorization: { ...f.authorization, policy }, receipt: { ...f.receipt, requestIds: requests.map(item => item.id), requestSha256s: requests.map(item => item.requestSha256), responseSha256s: requests.map(item => item.responseSha256) } };
  const batch = { ...f.batch, requests, documents: [], searchResults: providers.map(provider => ({ provider, retrievedAt: f.now, items: items.filter(item => item.provider === provider), rejected: [] })), mergedResults: groups.map(group => ({ id: f.hash(group[0].canonicalUrl), canonicalUrl: group[0].canonicalUrl, title: group[0].title, snippet: group[0].snippet, lineage: group })) };
  return { ...f.result, execution, batch };
}

test('all configured specialist providers retain fixed logical descriptors and bounded disclosure policies', () => {
  const providers = dto.RESEARCH_PROVIDER_IDS.map(provider => ({ provider, cost_microunits_per_call: 0, minimum_interval_ms: 1000 }));
  const providerDescriptors = providers.map(({ provider }) => ({ id: provider, method: 'POST', endpoint: dto.RESEARCH_ENDPOINTS[provider], adapterRevision: 1, mode: dto.researchSpecialistProvider(provider) ? 'specialist_retrieval' : 'web_search', scope: 'explicit_query_disclosure' }));
  const context = { ...f.context, policy: { ...f.policy, content: { ...f.policyContent, providers } }, providerDescriptors };
  assert.deepEqual(dto.projectResearchContext(context, f.w), context);
  for (const change of [{ id: 'unknown' }, { endpoint: 'https://unreviewed.example/works' }, { mode: 'web_search' }, { queryContract: 'Run arbitrary SPARQL' }, { method: 'GET' }]) assert.equal(dto.projectResearchProvider({ ...providerDescriptors[2], ...change }), null);
  assert.equal(dto.projectResearchPolicy({ ...context.policy.content, providers: [...providers, providers[0]] }), null);
  assert.equal(dto.projectResearchPolicy({ ...context.policy.content, providers: [{ ...providers[0], provider: '__proto__' }] }), null);
});

test('reservation preview includes enforced specialist rate floors and PubMed child allowance', () => {
  const policy = { ...f.policyContent, providers: [{ provider: 'arxiv', cost_microunits_per_call: 0, minimum_interval_ms: 0 }, { provider: 'pubmed', cost_microunits_per_call: 0, minimum_interval_ms: 0 }] };
  assert.equal(dto.researchProviderInterval(policy.providers[0]), 3000);
  assert.equal(dto.researchProviderInterval(policy.providers[1]), 400);
  assert.equal(dto.researchProviderRequestAllowance(policy), 3);
  assert.equal(dto.researchReservationPreview(policy).tool_ms, policy.max_requests * 18000 + policy.max_documents * 5000);
  assert.equal(dto.researchProviderInterval({ ...policy.providers[0], minimum_interval_ms: 7000 }), 7000);
});

test('PubMed metadata is a separately admitted fixed provider request, never an arbitrary GET', () => {
  const request = { ...f.requests[0].request, kind: 'metadata', provider: 'pubmed', url: dto.RESEARCH_ENDPOINTS.pubmed };
  assert.deepEqual(run.projectResearchRequest(request), request);
  for (const change of [{ provider: 'crossref', url: dto.RESEARCH_ENDPOINTS.crossref }, { method: 'GET' }, { url: 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi' }, { url: request.url + '?secret=unexpected' }]) assert.equal(run.projectResearchRequest({ ...request, ...change }), null);
});

test('scientific metadata preserves partial date precision and reported correction direction', () => {
  const value = { ...specialist, publicationDate: { ...date, value: '2024', precision: 'year' }, eventDate: { ...date, value: 'Spring 2024', precision: 'reported' }, updatedDate: { ...date, value: f.now, precision: 'instant' }, retraction: { state: 'reported_retracted', notices: [{ kind: 'retraction', relation: 'updated_by', targetDoi: '10.1234/notice', date }] } };
  assert.deepEqual(metadata.projectResearchSpecialistMetadata(value), value);
  assert.match(metadata.researchMetadataDateLabel(value.publicationDate), /2024 · year precision/);
  assert.match(metadata.researchRetractionLabel('not_reported'), /in this record/);
  assert.match(metadata.researchRetractionLabel('unknown'), /unavailable/);
  for (const malformed of [{ ...date, value: '2023-02-29' }, { ...date, value: '2024-13', precision: 'month' }, { ...date, value: '2024-01-01', precision: 'instant' }, { ...date, value: '2024-02-29T00:00:00Z', precision: 'day' }, { ...date, precision: 'inferred' }]) assert.equal(metadata.projectResearchMetadataDate(malformed), null);
  for (const change of [{ doi: 'https://doi.org/10.1234/example' }, { doi: '10.1234/UPPERCASE' }, { authors: Array(65).fill('Author') }, { sourceRole: 'verified' }, { retraction: { state: 'safe', notices: [] } }, { eventDate: { ...date, provenance: '' } }, { extra: 'authority' }]) assert.equal(metadata.projectResearchSpecialistMetadata({ ...specialist, ...change }), null);
  assert.equal(run.projectResearchSearchItem({ ...item('crossref'), publishedAt: date.value, publicationProvenance: 'provider_reported' }), null, 'a date-only report is not an instant for freshness');
  assert.equal(run.projectResearchSearchItem({ ...f.searchItem, specialist }), null, 'web observations cannot impersonate specialist records');
});

test('matching DOI preserves distinct URLs and full provider lineage without claiming independence', () => {
  const first = item('crossref'), second = item('openalex', { canonicalUrl: 'https://openalex.org/W1234', specialist: { ...specialist, recordId: 'https://openalex.org/W1234', recordKind: 'bibliographic_index' } });
  assert.equal(run.sameResearchWork(first, second), true);
  const result = resultFor([first, second]);
  assert.deepEqual(run.projectResearchResult(result, f.w, f.runId), result);
  const bad = { ...second, specialist: { ...second.specialist, doi: '10.5678/other' } };
  assert.equal(run.projectResearchResult(resultFor([first, bad]), f.w, f.runId), null);
  const dropped = resultFor([first, second], [[first]]);
  assert.equal(run.projectResearchResult(dropped, f.w, f.runId), null, 'merged projection cannot silently discard source lineage');
  const duplicated = resultFor([first, second], [[first, first, second]]);
  assert.equal(run.projectResearchResult(duplicated, f.w, f.runId), null, 'repeated lineage does not count as another source');
});

test('author-title matches require every group pair and reject ambiguous DOI bridges', () => {
  const first = item('crossref'), second = item('openalex', { canonicalUrl: 'https://openalex.org/W1234', title: 'A SUFFICIENTLY specific—paper title', specialist: { ...specialist, doi: null, authors: ['Grace Hopper', 'Ａｄａ Lovelace'] } });
  assert.equal(run.sameResearchWork(first, second), true);
  assert.ok(run.projectResearchResult(resultFor([first, second]), f.w, f.runId));
  assert.equal(run.sameResearchWork(first, { ...second, specialist: { ...second.specialist, authors: ['Ada Lovelace'] } }), false);
  assert.equal(run.sameResearchWork({ ...first, title: 'Short' }, { ...second, title: 'Short' }), false);
  const third = item('semantic_scholar', { canonicalUrl: 'https://www.semanticscholar.org/paper/1234', specialist: { ...specialist, doi: '10.5678/different' } });
  assert.equal(run.sameResearchWork(second, third), true);
  assert.equal(run.projectResearchResult(resultFor([first, second, third]), f.w, f.runId), null, 'a DOI-less intermediary cannot merge conflicting identities');
});

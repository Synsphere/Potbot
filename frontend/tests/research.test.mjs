import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import * as dto from '../src/lib/research.ts';
import * as run from '../src/lib/research-execution.ts';
import * as client from '../src/lib/research-client.ts';
import * as f from './research-fixtures.mjs';

test('research context accepts a task without a need and keeps exact public workspace sharing semantics', () => {
  assert.deepEqual(dto.projectResearchContext({ ...f.context, privateToken: 'must disappear' }, f.w, f.episode, f.state, f.needId, f.p), f.context);
  assert.deepEqual(dto.projectResearchContext(f.emptyContext, f.w, f.episode, f.state, null, f.p), f.emptyContext);
  for (const value of [{ ...f.context, scope: { ...f.scope, principalId: f.other } }, { ...f.context, visibility: 'principal' }, { ...f.context, task: { ...f.task, repositoryStateId: f.other } }, { ...f.context, queryTemplate: { ...f.query, target_version: f.hash('different') } }, { ...f.context, providerDescriptors: [{ ...f.context.providerDescriptors[0], endpoint: 'https://evil.example/search' }] }, { ...f.emptyContext, current: true }]) assert.equal(dto.projectResearchContext(value, f.w, f.episode, f.state, f.needId, f.p), null);
  const question = '🍀'.repeat(1000), long = { ...f.context, need: { ...f.need, spec: { ...f.spec, question } }, needs: [{ ...f.context.needs[0], question: '🍀'.repeat(512) }], queryTemplate: { ...f.query, search_text: '🍀'.repeat(256) } };
  assert.deepEqual(dto.projectResearchContext(long, f.w, f.episode, f.state, f.needId, f.p), long);
  assert.equal(dto.projectResearchContext({ ...long, needs: [{ ...long.needs[0], question: question.slice(0, 512) }] }, f.w), null, 'summary truncation counts Unicode codepoints rather than UTF-16 code units');
});

test('need creation uses existing bounded criteria, references and safe integer budgets', () => {
  assert.deepEqual(dto.validateResearchMutation(['needs'], f.needBody), f.needBody);
  for (const change of [{ schema_version: 2 }, { question: ' ' }, { priority: true }, { required_evidence: [] }, { resolution_criteria: [] }, { query_profiles: ['same', 'same'] }, { known_evidence: [f.other, f.other] }, { budget: { calls: -1 } }, { budget: { concurrency: 0 } }, { budget: { calls: 9007199254740992 } }, { semantic_release_id: f.other }]) assert.equal(dto.validateResearchMutation(['needs'], { ...f.needBody, spec: { ...f.spec, ...change } }), null);
  assert.equal(dto.validateResearchMutation(['needs'], { ...f.needBody, needResolved: true }), null);
  assert.equal(dto.validateResearchMutation(['needs'], { ...f.needBody, confirm: false }), null);
});

test('action admission cannot replace configured selectors or ceilings, and reused identity preserves original literal query', () => {
  assert.deepEqual(dto.validateResearchMutation(['actions'], f.actionBody), f.actionBody);
  for (const change of [{ locator: 'https://arbitrary.example/' }, { operation: 'execute' }, { category: 'mcp' }, { target_version: 'latest' }, { max_source_bytes: 262145 }, { max_results: 21 }, { freshness_seconds: -1 }, { required_identifiers: ['missing'] }, { search_text: 'a'.repeat(501) }, { search_text: '\ud800' }, { search_text: 'é'.repeat(600) }]) assert.equal(dto.validateResearchMutation(['actions'], { ...f.actionBody, query: { ...f.query, ...change } }), null);
  const requested = { ...f.actionBody, query: { ...f.query, search_text: 'documented\u0085\u00a0limit' } }, retained = { ...f.actionReceipt, reused: true };
  assert.deepEqual(dto.projectResearchNonRunMutation(retained, f.w, ['actions'], requested, f.p), retained);
  assert.equal(dto.projectResearchNonRunMutation(f.actionReceipt, f.w, ['actions'], requested, f.p), null);
  assert.equal(dto.projectResearchNonRunMutation(retained, f.w, ['actions'], { ...requested, query: { ...requested.query, fallback: 'Different plan' } }, f.p), null);
  assert.equal(dto.researchQueryEquivalent(f.query, { ...f.query, search_text: '\ufeffdocumented limit' }), false, 'Python does not discard BOM as whitespace');
  assert.equal(dto.projectResearchNonRunMutation({ ...retained, action: { ...f.action, predecessor_id: f.other } }, f.w, ['actions'], requested, f.p), null);
});

test('queue requires explicit disclosure and current exact context, while current reader may differ from admitting actor', () => {
  assert.deepEqual(dto.validateResearchMutation(['runs'], f.runBody), f.runBody);
  assert.equal(dto.validateResearchMutation(['runs'], { ...f.runBody, disclosureConfirmed: false }), null);
  assert.equal(dto.validateResearchMutation(['runs'], { ...f.runBody, endpoint: 'https://evil.example/' }), null);
  assert.equal(client.researchCanQueue(f.action, f.context), true);
  for (const value of [{ ...f.context, current: false }, { ...f.context, need: { ...f.need, status: 'resolved' } }, { ...f.context, need: { ...f.need, revisionId: f.other } }, { ...f.context, policy: { ...f.policy, content: { ...f.policyContent, allow_query_disclosure: false } } }, { ...f.context, task: { ...f.task, contractRevisionId: f.other } }]) assert.equal(client.researchCanQueue(f.action, value), false);
  const receipt = run.projectResearchRunMutation(f.queued, f.w, ['runs'], f.runBody, f.p); assert.ok(receipt); assert.notEqual(receipt.execution.authorization.actorPrincipalId, receipt.scope.principalId);
});

test('run, requests and completed batch join exact source, policy, consumer, receipt and settled usage identities', () => {
  assert.deepEqual(run.projectResearchResult(f.result, f.w, f.runId, f.p), f.result);
  assert.deepEqual(run.projectResearchResult(f.pendingResult, f.w, f.runId, f.p), f.pendingResult);
  for (const change of [{ authorizationSha256: f.hash('different') }, { consumerSha256: f.hash('different') }, { usage: { ...f.batch.usage, origin: 'measured' } }, { requests: [...f.requests, f.requests[0]] }, { documents: [{ ...f.source, runId: f.other }] }, { mergedResults: [{ ...f.batch.mergedResults[0], lineage: [{ ...f.searchItem, rank: 8 }] }] }, { typedTransformation: { ...f.transformation, synActivation: 'qualified' } }]) assert.equal(run.projectResearchResult({ ...f.result, batch: { ...f.batch, ...change } }, f.w, f.runId, f.p), null);
  assert.equal(run.projectResearchResult({ ...f.pendingResult, verification: 'verified' }, f.w, f.runId), null);
  assert.equal(run.projectResearchResult({ ...f.result, verification: 'pending' }, f.w, f.runId), null);
  const wrongReceipt = { ...f.receipt, typedTransformation: { ...f.receipt.typedTransformation, receipt: { ...f.receipt.typedTransformation.receipt, invocation_id: f.other } } };
  assert.equal(run.projectResearchExecution({ ...f.execution, receipt: wrongReceipt }, f.w), null);
});

test('search publication metadata requires an actual UTC instant or explicit unknown date', () => {
  const withPublication = publishedAt => {
    const item = { ...f.searchItem, publishedAt, publicationProvenance: 'provider_reported_published_or_modified' };
    return { ...f.result, batch: { ...f.batch, searchResults: [{ ...f.batch.searchResults[0], items: [item] }], mergedResults: [{ ...f.batch.mergedResults[0], lineage: [item] }] } };
  };
  assert.ok(run.projectResearchResult(withPublication('2026-11-18T10:00:00Z'), f.w, f.runId, f.p));
  assert.equal(run.projectResearchResult(withPublication('2026-11-18'), f.w, f.runId, f.p), null);
  assert.equal(run.projectResearchResult(withPublication('2026-11-18T10:00:00'), f.w, f.runId, f.p), null);
});

test('negative integrity remains inspectable and cancelable without supplying positive authority', () => {
  const execution = { ...f.queuedExecution, action: null, authorization: null, run: { ...f.queuedRun, needId: null, needRevisionId: null, authorizationSha256: null, policySha256: null, consumerSha256: null, recordIntegrityValid: false, cancelRequested: true } }, negative = { ...f.envelope, execution };
  assert.deepEqual(run.projectResearchRunEnvelope(negative, f.w, f.runId), negative);
  assert.deepEqual(run.projectResearchRunMutation(negative, f.w, ['runs', f.runId, 'cancel'], { confirm: true }), negative);
  assert.equal(run.projectResearchRunMutation(negative, f.w, ['runs'], f.runBody), null);
  assert.equal(run.projectResearchResult({ ...negative, batch: f.batch, verification: 'verified', reasonCodes: [] }, f.w, f.runId), null);
  assert.equal(run.projectResearchRunEnvelope({ ...negative, execution: { ...execution, run: { ...execution.run, recordIntegrityValid: true } } }, f.w, f.runId), null);
});

test('source extraction retains complete bounded text and PDF isolation evidence without unsafe URL links', async () => {
  assert.deepEqual(run.projectResearchSource(f.sourceEnvelope, f.w, f.runId, f.source.id), f.sourceEnvelope);
  for (const value of ['javascript:alert(1)', 'https://u:p@docs.example.com/limits', 'https://docs.example.com:443/limits', 'https://docs.example.com/limits#fragment', 'https://docs.example.com/\\private']) assert.equal(dto.researchURL(value), false);
  for (const change of [{ complete: false }, { text: '4\r2' }, { sourceSha256: f.hash('other') }, { execution: { profileSha256: f.hash('profile'), executableSha256: f.hash('exe'), isolationSha256: f.hash('sandbox'), elapsedMs: 1, processesStopped: true } }, { segments: [{ ...f.extraction.segments[0], endByte: 999 }] }]) assert.equal(run.projectResearchSource({ ...f.sourceEnvelope, extraction: { ...f.extraction, ...change } }, f.w, f.runId, f.source.id), null);
  const pdf = { ...f.extraction, mediaType: 'application/pdf', extractorId: 'potbot.isolated_pdftotext/v1', execution: { profileSha256: f.hash('profile'), executableSha256: f.hash('exe'), isolationSha256: f.hash('sandbox'), elapsedMs: 1, processesStopped: true } };
  assert.ok(run.projectResearchExtraction(pdf, f.source));
  assert.equal(run.projectResearchExtraction({ ...pdf, execution: null }, f.source), null);
  assert.equal(run.projectResearchExtraction({ ...pdf, execution: { ...pdf.execution, processesStopped: false } }, f.source), null);
  assert.deepEqual(await client.researchCitationRequest(f.runId, f.source.id, f.extraction, 0, 2), f.citationBody);
  await assert.rejects(client.researchCitationRequest(f.runId, f.source.id, { ...f.extraction, text: '43' }, 0, 2), /hash/);
  assert.equal(await client.researchCitationMatchesSource(f.citation, f.citationBody, f.extraction), true);
  assert.equal(await client.researchCitationMatchesSource({ ...f.citation, quote: '43' }, f.citationBody, f.extraction), false);
  const separated = { ...f.extraction, text: 'one\ntwo', textSha256: f.hash('one\ntwo'), segments: [{ startByte: 0, endByte: 3, kind: 'text', locator: 'first', page: null }, { startByte: 4, endByte: 7, kind: 'text', locator: 'second', page: null }] };
  await assert.rejects(client.researchCitationRequest(f.runId, f.source.id, separated, 3, 4), /generated separator/);
  assert.equal(client.researchCitationFitsContext(f.citation, f.context), true);
  assert.equal(client.researchCitationFitsContext(f.citation, { ...f.context, task: { ...f.task, repositoryStateId: f.other } }), false);
  assert.equal(client.researchCitationFitsContext(f.citation, { ...f.context, task: { ...f.task, contractRevisionId: f.other } }), false);
  assert.equal(client.researchCitationFitsContext(f.citation, { ...f.context, need: { ...f.need, revisionId: f.other } }), false);
});

test('citation specifications and comparison inputs cannot inject verdicts, arbitrary resources or repeated evidence', () => {
  assert.deepEqual(dto.projectCitationSpec(f.citationBody), f.citationBody);
  for (const change of [{ startByte: -1 }, { startByte: 1.5 }, { endByte: 0 }, { endByte: 16385 }, { endByte: 4194305 }, { quote: '42' }, { url: 'https://docs.example.com/limits' }]) assert.equal(dto.projectCitationSpec({ ...f.citationBody, ...change }), null);
  assert.deepEqual(dto.projectResearchClaims(f.claims), f.claims);
  for (const claims of [[...f.claims, ...f.claims], [{ ...f.claims[0], status: 'supported' }], [{ ...f.claims[0], label: 'x\ncommand' }], [{ ...f.claims[0], label: 'é'.repeat(257) }], [{ ...f.claims[0], citations: [f.citationBody, f.citationBody] }], []]) assert.equal(dto.projectResearchClaims(claims), null);
  assert.equal(dto.projectResearchCitationEnvelope({ ...f.citationEnvelope, citation: { ...f.citation, lineEnd: 2 } }, f.w, f.citationBody), null);
  assert.equal(dto.projectResearchCitationEnvelope({ ...f.citationEnvelope, citation: { ...f.citation, segments: [] } }, f.w, f.citationBody), null);
});

test('aggregate source and search projections stay bounded even when individual fields fit', () => {
  const extraction = { ...f.extraction, segments: Array.from({ length: 4096 }, () => ({ ...f.extraction.segments[0], locator: 'x'.repeat(256) })) };
  assert.ok(new TextEncoder().encode(JSON.stringify(extraction)).length > 1000000);
  assert.equal(run.projectResearchExtraction(extraction, f.source), null);
  const searchResults = [{ ...f.batch.searchResults[0], items: Array.from({ length: 20 }, (_, index) => ({ ...f.searchItem, rank: index + 1, snippet: 'x'.repeat(16384) })) }];
  assert.ok(new TextEncoder().encode(JSON.stringify(searchResults[0])).length > 100000);
  assert.equal(run.projectResearchResult({ ...f.result, batch: { ...f.batch, searchResults } }, f.w, f.runId), null);
  const result = { ...f.result, batch: { ...f.batch, searchResults: [{ ...f.batch.searchResults[0], rejected: [{ rank: 2, reasonCode: 'normalized_result_limit' }] }] } };
  assert.ok(run.projectResearchResult(result, f.w, f.runId));
});

test('comparison verdicts retain exact quote disagreements, unavailable evidence and publication freshness', () => {
  assert.deepEqual(dto.projectResearchComparisonEnvelope(f.comparison, f.w, f.evidenceId, f.p), f.comparison);
  const second = { ...f.citation, runId: f.other, quote: '43', quoteSha256: f.hash('43'), sha256: f.hash('citation-2') };
  const conflictingClaim = { ...f.report.claims[0], citations: [f.citation, second], values: ['42', '43'], status: 'unresolved', reasonCodes: ['conflicting_cited_values'] };
  const conflict = { ...f.comparison, report: { ...f.report, claims: [conflictingClaim], status: 'unresolved' } };
  assert.deepEqual(dto.projectResearchComparisonEnvelope(conflict, f.w), conflict);
  assert.equal(dto.projectResearchComparisonEnvelope({ ...conflict, report: { ...conflict.report, status: 'supported' } }, f.w), null);
  for (const change of [{ needResolved: true }, { qualification: 'factual_truth' }, { claims: [{ ...f.report.claims[0], values: ['43'] }] }, { claims: [{ ...f.report.claims[0], unavailableCitationCount: 1 }] }, { claims: [{ ...f.report.claims[0], citations: [{ ...f.citation, freshnessSeconds: 60 }] }] }, { claims: [{ ...f.report.claims[0], citations: [{ ...f.citation, freshnessSeconds: 60, publishedAt: '2025-01-01T00:00:00Z', publicationProvenance: 'source_reported' }] }] }]) assert.equal(dto.projectResearchComparisonEnvelope({ ...f.comparison, report: { ...f.report, ...change } }, f.w), null);
});

test('event cursors are ordered and causal children cannot be relabelled as another research run', () => {
  assert.deepEqual(run.projectResearchEvents(f.events, f.w, f.runId, 0, f.p), f.events);
  assert.equal(run.projectResearchEvents(f.events, f.w, f.runId, 1, f.p), null);
  const child = { ...f.event, id: f.uid(61), event_ref: `syn://event/${f.uid(61)}`, sequence: 2, run_id: f.uid(30), emitter_run_id: f.uid(30), parent_run_id: f.runId, parent_event_id: f.event.id, trigger_kind: 'parent_event', trigger_ref: f.event.event_ref };
  assert.ok(run.projectResearchEvents({ ...f.events, items: [f.event, child], nextCursor: 2 }, f.w, f.runId));
  const completedChild = { ...child, sequence: 3, parent_run_id: child.run_id };
  assert.ok(run.projectResearchEvents({ ...f.events, items: [completedChild], nextCursor: 3 }, f.w, f.runId, 2), 'a later child occurrence may name the same request run as its immediate causal parent');
  assert.equal(run.projectResearchEvents({ ...f.events, items: [{ ...child, parent_run_id: f.other }], nextCursor: 2 }, f.w, f.runId), null);
  assert.equal(run.projectResearchEvents({ ...f.events, nextCursor: 999 }, f.w, f.runId), null);
});

test('research page source keeps reads passive, disclosure explicit and authored content inert', async () => {
  const [page, views, nav] = await Promise.all([readFile(new URL('../src/app/research/ResearchWorkbench.tsx', import.meta.url), 'utf8'), readFile(new URL('../src/app/research/ResearchViews.tsx', import.meta.url), 'utf8'), readFile(new URL('../src/components/Panels.tsx', import.meta.url), 'utf8')]);
  assert.match(page, /recoverAttempt\(journal\.current, validateResearchAttempt\)/);
  assert.match(page, /headers: \{ \.\.\.researchRequestHeaders\(scope\), "Content-Type": "application\/json", "Idempotency-Key": attempt.key \}/);
  assert.match(page, /headers: \{ \.\.\.researchRequestHeaders\(scope\), "Content-Type": "application\/json" \}/);
  assert.match(page, /const \[live, setLive\] = useState\(false\)/);
  assert.match(page, /disclosureConfirmed: true/);
  assert.match(page, /selectionStart[\s\S]*researchUTF8/);
  assert.match(page, /current\.reset\(\)/);
  assert.match(views, /shared with authenticated members/);
  assert.match(views, /does not establish truth/);
  assert.match(nav, /\/research\?workspace=/);
  assert.doesNotMatch(page + views, /dangerouslySetInnerHTML|<iframe|eval\(|new Function|localStorage/);
});

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { parseMcpJSON, projectResearchContext, projectResearchNonRunMutation, projectResearchCitationEnvelope, projectResearchComparisonEnvelope, citationSpec, researchReservationPreview } from '../src/lib/research.ts';
import { projectResearchRunEnvelope, projectResearchRunMutation, projectResearchRuns, projectResearchEvents, projectResearchResult, projectResearchSource } from '../src/lib/research-execution.ts';
import { researchCanQueue, researchCitationMatchesSource } from '../src/lib/research-client.ts';

test('actual PostgreSQL HTTP research workflow joins disclosure, retained sources, literal citations and historical comparison', async () => {
  // This fixture is produced by the real HTTP/DB/native worker lifecycle. Its
  // explicitly synthetic provider transport is not live-provider qualification.
  const path = process.env.POTBOT_RESEARCH_HTTP_FIXTURE ?? new URL('./fixtures/research-http.json', import.meta.url);
  const wire = parseMcpJSON(await readFile(path, 'utf8')), { workspaceId: w, principalId: p, episodeId: episode, repositoryStateId: state, needId, runId } = wire;
  assert.match(wire.description, /synthetic provider/i);
  const before = projectResearchContext(wire.contextBeforeNeed, w, episode, state, null, p); assert.ok(before); assert.equal(before.need, null); assert.equal(before.task.status, 'running'); assert.equal(before.visibility, 'workspace');
  const created = projectResearchNonRunMutation(wire.createdNeed, w, ['needs'], { episodeId: episode, repositoryStateId: state, spec: wire.createdNeed.need.spec, confirm: true }, p); assert.ok(created); assert.equal(created.need.id, needId);
  const context = projectResearchContext(wire.context, w, episode, state, needId, p); assert.ok(context); assert.equal(context.current, true);
  const action = projectResearchNonRunMutation(wire.action, w, ['actions'], { episodeId: episode, repositoryStateId: state, needId, contextSha256: context.contextSha256, query: wire.action.action.query, confirm: true }, p); assert.ok(action); assert.equal(researchCanQueue(action.action, context), true);
  const queued = projectResearchRunMutation(wire.queued, w, ['runs'], { actionId: action.action.action_id, policySha256: context.policy.sha256, disclosureConfirmed: true, confirm: true }, p); assert.ok(queued); assert.equal(queued.execution.run.id, runId); assert.equal(queued.execution.run.state, 'queued');
  assert.deepEqual(researchReservationPreview(context.policy.content), queued.execution.authorization.limits, 'displayed current policy ceiling must match the actual budget owner reservation');
  const pending = projectResearchResult(wire.pending, w, runId, p); assert.ok(pending); assert.equal(pending.verification, 'pending'); assert.equal(pending.batch, null);
  const completed = projectResearchRunEnvelope(wire.execution, w, runId, p), result = projectResearchResult(wire.result, w, runId, p); assert.ok(completed); assert.ok(result); assert.equal(completed.execution.run.state, 'succeeded'); assert.equal(result.verification, 'verified'); assert.equal(result.execution.run.usageOrigin, 'synthetic');
  assert.equal(result.batch.authorizationSha256, queued.execution.authorization.sha256); assert.equal(result.batch.consumerSha256, queued.execution.authorization.consumerSha256); assert.equal(result.execution.receipt.needResolved, false); assert.equal(result.execution.receipt.grantsTaskCompletion, false); assert.equal(result.batch.visibility, 'workspace');
  const events = projectResearchEvents(wire.events, w, runId, 0, p), runs = projectResearchRuns(wire.runs, w, episode, p); assert.ok(events); assert.ok(runs); assert.ok(events.items.length > 0); assert.ok(runs.items.some(item => item.run.id === runId));
  const source = projectResearchSource(wire.source, w, runId, wire.source.source.id, p); assert.ok(source); assert.equal(source.extraction.status, 'extracted'); assert.equal(source.extraction.complete, true);
  const spec = citationSpec(wire.citation.citation), citation = projectResearchCitationEnvelope(wire.citation, w, spec, p); assert.ok(citation); assert.equal(await researchCitationMatchesSource(citation.citation, spec, source.extraction), true); assert.equal(citation.citation.receiptSha256, result.execution.run.receiptSha256);
  const comparison = projectResearchComparisonEnvelope(wire.comparison, w, wire.comparison.evidenceId, p), readback = projectResearchComparisonEnvelope(wire.readComparison, w, wire.comparison.evidenceId, p); assert.ok(comparison); assert.ok(readback); assert.deepEqual(comparison, readback); assert.equal(comparison.report.needResolved, false); assert.equal(comparison.report.qualification, 'verified_cited_source_agreement_only'); assert.equal(comparison.report.needRevisionId, context.need.revisionId);
  for (const [key, value] of Object.entries({ contextBeforeNeed: before, createdNeed: created, context, action, queued, pending, execution: completed, result, events, runs, source, citation, comparison, readComparison: readback })) assert.deepEqual(value, wire[key], key + ' must retain exactly its public producer fields');
  assert.doesNotMatch(JSON.stringify({ context, action, queued, completed, result, source, citation, comparison }), /PRIVATE|setup_token|external_mac|claim_token|"mac"\s*:/);
});

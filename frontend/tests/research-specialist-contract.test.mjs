import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { parseMcpJSON, projectResearchContext, researchReservationPreview } from '../src/lib/research.ts';
import { projectResearchRunEnvelope, projectResearchResult } from '../src/lib/research-execution.ts';

test('actual PostgreSQL specialist workflow retains separate disclosures, PubMed child and source metadata', async () => {
  // These bytes come from the actual admitted HTTP/DB/worker path. The provider
  // transport is explicitly synthetic and establishes no live-service quality.
  const path = process.env.POTBOT_SPECIALIST_HTTP_FIXTURE ?? new URL('./fixtures/research-specialist-http.json', import.meta.url);
  const wire = parseMcpJSON(await readFile(path, 'utf8'));
  const { workspaceId: workspace, principalId: principal, episodeId: episode, repositoryStateId: state, needId, runId } = wire;
  assert.match(wire.description, /synthetic provider/i);
  const context = projectResearchContext(wire.context, workspace, episode, state, needId, principal);
  const run = projectResearchRunEnvelope(wire.execution, workspace, runId, principal);
  const result = projectResearchResult(wire.result, workspace, runId, principal);
  assert.ok(context); assert.ok(run); assert.ok(result);
  const execution = run.execution;
  assert.deepEqual(context, wire.context);
  assert.deepEqual(run, wire.execution);
  assert.deepEqual(result, wire.result);
  assert.deepEqual(researchReservationPreview(context.policy.content), execution.authorization.limits);
  assert.equal(result.verification, 'verified');
  assert.equal(execution.run.usageOrigin, 'synthetic');
  assert.deepEqual(result.batch.requests.map(item => [item.request.provider, item.request.kind]), [['crossref', 'search'], ['openalex', 'search'], ['pubmed', 'search'], ['pubmed', 'metadata']]);
  assert.equal(result.batch.searchResults.length, 3);
  assert.equal(result.batch.mergedResults.length, 1);
  const lineage = result.batch.mergedResults[0].lineage;
  assert.equal(lineage.length, 3);
  assert.equal(new Set(lineage.map(item => item.canonicalUrl)).size, 3);
  assert.equal(new Set(lineage.map(item => item.specialist.doi)).size, 1);
  assert.ok(lineage.every(item => item.specialist.publicationDate !== null));
  assert.ok(lineage.some(item => item.specialist.retraction.notices.length > 0));
  assert.ok(lineage.some(item => item.specialist.retraction.state === 'reported_retracted'));
  assert.equal(result.batch.documents.length, 0);
  assert.equal(execution.receipt.needResolved, false);
  assert.equal(execution.receipt.grantsTaskCompletion, false);
  assert.equal(result.batch.typedTransformation.synActivation, 'not_qualified');
  assert.doesNotMatch(JSON.stringify({ context, execution, result }), /PRIVATE|setup_token|external_mac|claim_token|"mac"\s*:/);
});

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { projectDiscoverySources, projectDiscoveryPublishers, projectDiscoveryCandidates, projectDiscoveryChecks, projectDiscoverySourceEnvelope,
  projectDiscoveryPublisherEnvelope, projectDiscoveryCandidateEnvelope, projectDiscoveryCheckEnvelope, projectDiscoveryMutation } from '../src/lib/discovery.ts';

test('actual HTTP and PostgreSQL discovery lifecycle agrees with scoped browser projections and original receipts', async () => {
  // Runtime/provider outcomes are explicit integration fixtures. This verifies
  // the real API/service/database wire, not a new live-host qualification.
  const path = process.env.POTBOT_DISCOVERY_CONTRACT_OUTPUT ?? new URL('./fixtures/discovery-http.json', import.meta.url);
  const wire = JSON.parse(await readFile(path, 'utf8')), { workspaceId: w, principalId: p } = wire.sources;
  for (const [key, project] of [['sources', projectDiscoverySources], ['publishers', projectDiscoveryPublishers], ['candidates', projectDiscoveryCandidates],
    ['unreviewedCandidates', projectDiscoveryCandidates], ['emptyChecks', projectDiscoveryChecks], ['checks', projectDiscoveryChecks]]) assert.deepEqual(project(wire[key], w, p), wire[key], key);
  for (const key of ['source', 'disabledSource', 'enabledSource']) assert.deepEqual(projectDiscoverySourceEnvelope(wire[key], w, wire[key].source.id, p), wire[key], key);
  for (const key of ['approvedPublisher', 'blockedPublisher']) assert.deepEqual(projectDiscoveryPublisherEnvelope(wire[key], w, wire[key].publisher.id, p), wire[key], key);
  assert.deepEqual(projectDiscoveryCandidateEnvelope(wire.candidate, w, wire.candidate.candidate.id, p), wire.candidate);
  for (const key of ['pendingCheck', 'measuringCheck', 'measuredCheck', 'handedOffCheck', 'cancelledCheck', 'expiredCheck', 'failedCheck']) assert.deepEqual(projectDiscoveryCheckEnvelope(wire[key], w, wire[key].check.id, p), wire[key], key);
  for (const [key, group, action] of [['disabledSource', 'sources', 'disable'], ['enabledSource', 'sources', 'enable'], ['approvedPublisher', 'publishers', 'approve'], ['blockedPublisher', 'publishers', 'block']]) {
    const record = wire[key][group === 'sources' ? 'source' : 'publisher'];
    assert.deepEqual(projectDiscoveryMutation(wire[key], w, [group, record.id, action], { revision: record.revision - 1, digest: '0'.repeat(64), confirm: true }, p), wire[key]);
  }
  const pending = wire.pendingCheck.check;
  assert.deepEqual(projectDiscoveryMutation(wire.pendingCheck, w, ['candidates', pending.candidateId, 'checks'], { snapshotId: pending.snapshotId,
    candidateSha256: pending.requestedCandidateSha256, agentId: pending.agentId, confirm: true }, p), wire.pendingCheck);
  for (const [key, action] of [['handedOffCheck', 'handoff'], ['cancelledCheck', 'cancel']]) {
    const record = wire[key].check;
    assert.deepEqual(projectDiscoveryMutation(wire[key], w, ['checks', record.id, action], { revision: record.revision - 1, digest: '0'.repeat(64), confirm: true }, p), wire[key]);
  }
  assert.equal(wire.unreviewedCandidates.items[0].canReview, false);
  assert.equal(wire.pendingCheck.check.measurement, null);
  assert.equal(wire.measuringCheck.check.status, 'measuring');
  assert.ok(wire.measuredCheck.check.measurement.checks.some(clause => clause.kind === 'any_binary'));
  assert.ok(wire.measuredCheck.check.requirements.some(clause => clause.group === 'operating_system'));
  assert.equal(wire.handedOffCheck.check.target.id, pending.packageId);
  assert.equal(wire.expiredCheck.check.measurementCurrent, false);
  assert.equal(wire.failedCheck.check.status, 'failed');
});

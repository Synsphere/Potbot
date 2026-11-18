import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { parseMcpJSON, projectTaskEnvelope, projectTaskSearches, projectTaskMutation } from '../src/lib/task-discovery.ts';
import { taskResultMatches, taskSelectionMatches, taskCanSelect, taskHandoffHref, taskDiscoveryNeedsMonitoring } from '../src/lib/task-discovery-client.ts';

test('actual HTTP and PostgreSQL task discovery lifecycle agrees with browser DTOs and receipts', async () => {
  // The integration service exports these HTTP responses. Its synthetic embedding
  // adapter exercises accounting and lifecycle; it does not qualify a real model.
  const path = process.env.POTBOT_TASK_DISCOVERY_CONTRACT_OUTPUT ?? new URL('./fixtures/task-discovery-http.json', import.meta.url);
  const wire = parseMcpJSON(await readFile(path, 'utf8')), { workspaceId: w, principalId: p } = wire.context;
  for (const key of ['context', 'hybridContext']) assert.deepEqual(projectTaskEnvelope(wire[key], w, 'context', undefined, p), wire[key], key);
  for (const key of ['emptySearches', 'searches', 'firstPage', 'secondPage']) assert.deepEqual(projectTaskSearches(wire[key], w, p), wire[key], key);
  for (const key of ['createdSearch', 'search', 'selectedSearch', 'requestedSearch', 'runningSearch', 'hybridSearch', 'cancelledSearch', 'staleSearch']) assert.deepEqual(projectTaskEnvelope(wire[key], w, 'search', wire[key].search.id, p), wire[key], key);
  for (const key of ['result', 'hybridResult', 'staleResult']) assert.deepEqual(projectTaskEnvelope(wire[key], w, 'result', wire[key].result.id, p), wire[key], key);
  for (const key of ['selected', 'selection', 'staleSelection']) assert.deepEqual(projectTaskEnvelope(wire[key], w, 'selection', wire[key].selection.id, p), wire[key], key);
  assert.deepEqual(projectTaskEnvelope(wire.inspection, w, 'candidate', wire.inspection.candidate.id, p), wire.inspection);
  assert.deepEqual(projectTaskMutation(wire.createdSearch, w, ['searches'], wire.requests.create, p), wire.createdSearch);
  assert.deepEqual(projectTaskMutation(wire.requestedSearch, w, ['searches'], wire.requests.hybrid, p), wire.requestedSearch);
  assert.deepEqual(projectTaskMutation(wire.selected, w, ['searches', wire.search.search.id, 'select'], wire.requests.select, p), wire.selected);
  assert.deepEqual(projectTaskMutation(wire.cancelledSearch, w, ['searches', wire.cancelledSearch.search.id, 'cancel'], wire.requests.cancel, p), wire.cancelledSearch);
  assert.equal(taskResultMatches(wire.search.search, wire.result.result), true);
  assert.equal(taskResultMatches(wire.hybridSearch.search, wire.hybridResult.result), true);
  assert.equal(taskSelectionMatches(wire.selectedSearch.search, wire.result.result, wire.selection.selection), true);
  assert.equal(taskCanSelect(wire.search.search, wire.result.result, wire.result.result.items[0]), true);
  assert.ok(taskHandoffHref(w, wire.selection.selection));
  assert.equal(taskCanSelect(wire.staleSearch.search, wire.staleResult.result, wire.staleResult.result.items[0]), false);
  assert.equal(taskHandoffHref(w, wire.staleSelection.selection), null);
  assert.equal(wire.emptySearches.items.length, 0); assert.equal(wire.createdSearch.search.status, 'completed'); assert.equal(wire.selectedSearch.search.status, 'selected');
  assert.equal(taskDiscoveryNeedsMonitoring(wire.requestedSearch.search), true); assert.equal(taskDiscoveryNeedsMonitoring(wire.runningSearch.search), true);
  assert.equal(wire.hybridSearch.search.status, 'completed'); assert.equal(wire.cancelledSearch.search.status, 'cancelled');
  assert.equal(wire.staleResult.result.authorityCurrent, false); assert.equal(wire.staleSelection.selection.authorityCurrent, false);
  assert.ok(wire.result.result.items.every(item => item.executable === false)); assert.ok(wire.hybridResult.result.items.every(item => item.executable === false));
  assert.equal(wire.hybridResult.result.semantic.usageOrigin, 'synthetic'); assert.ok(wire.hybridResult.result.semantic.reservationIds.length > 0);
  assert.ok(wire.firstPage.nextCursor); assert.ok(wire.secondPage.items.every(item => item.id > wire.firstPage.nextCursor));
});

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { parseMcpJSON, projectMcpBindings, projectMcpBindingEnvelope, projectMcpEntries, projectMcpAttempts, projectMcpAttemptEnvelope, projectMcpReferenceEnvelope, projectMcpMutation } from '../src/lib/mcp-discovery.ts';
import { mcpReferenceCurrent } from '../src/lib/mcp-discovery-client.ts';

test('actual HTTP and PostgreSQL MCP lifecycle agrees with strict browser projections and original receipts', async () => {
  // Native transport results are explicit integration fixtures. This checks the
  // real service/API/database wire without qualifying an external MCP provider.
  const path = process.env.POTBOT_MCP_DISCOVERY_CONTRACT_OUTPUT ?? new URL('./fixtures/mcp-discovery-http.json', import.meta.url);
  const wire = parseMcpJSON(await readFile(path, 'utf8')), { workspaceId: w, principalId: p } = wire.bindings, bindingId = wire.binding.binding.id;
  assert.deepEqual(projectMcpBindings(wire.bindings, w, p), wire.bindings);
  for (const key of ['binding', 'freshBinding', 'staleBinding', 'disabledBinding', 'enabledBinding', 'revokedBinding']) assert.deepEqual(projectMcpBindingEnvelope(wire[key], w, bindingId, p), wire[key], key);
  for (const key of ['emptyEntries', 'entries', 'tools', 'resources', 'templates', 'prompts', 'search', 'firstPage', 'secondPage', 'staleEntries']) assert.deepEqual(projectMcpEntries(wire[key], w, bindingId, p), wire[key], key);
  for (const key of ['emptyAttempts', 'attempts']) assert.deepEqual(projectMcpAttempts(wire[key], w, p, bindingId), wire[key], key);
  for (const key of ['requestedAttempt', 'runningAttempt', 'succeededAttempt', 'failedAttempt', 'cancelledAttempt']) assert.deepEqual(projectMcpAttemptEnvelope(wire[key], w, wire[key].attempt.id, p), wire[key], key);
  const ref = wire.reference.reference;
  assert.deepEqual(projectMcpReferenceEnvelope(wire.reference, w, bindingId, ref.entryId, p), wire.reference);
  for (const [key, group, action, recordKey] of [['requestedAttempt', 'bindings', 'reconcile', 'attempt'], ['disabledBinding', 'bindings', 'disable', 'binding'],
    ['enabledBinding', 'bindings', 'enable', 'binding'], ['revokedBinding', 'bindings', 'revoke', 'binding'], ['cancelledAttempt', 'attempts', 'cancel', 'attempt']]) {
    const identity = group === 'bindings' ? bindingId : wire[key][recordKey].id;
    assert.deepEqual(projectMcpMutation(wire[key], w, [group, identity, action], wire.requests[action], p), wire[key], key);
  }
  assert.equal(projectMcpMutation(wire.runningAttempt, w, ['bindings', bindingId, 'reconcile'], wire.requests.reconcile, p), null);
  assert.equal(projectMcpMutation(wire.succeededAttempt, w, ['bindings', bindingId, 'reconcile'], wire.requests.reconcile, p), null);
  assert.equal(wire.binding.binding.snapshotId, null);
  assert.equal(wire.emptyEntries.items.length, 0); assert.equal(wire.emptyAttempts.items.length, 0);
  assert.equal(wire.runningAttempt.attempt.status, 'running'); assert.equal(wire.succeededAttempt.attempt.status, 'succeeded');
  assert.equal(wire.failedAttempt.attempt.status, 'failed'); assert.equal(wire.cancelledAttempt.attempt.status, 'cancelled');
  assert.equal(wire.revokedBinding.binding.actions.length, 0);
  assert.ok(wire.firstPage.nextCursor); assert.equal(wire.firstPage.nextCursor, wire.firstPage.items.at(-1).id);
  assert.ok(wire.secondPage.items.every(item => item.id > wire.firstPage.nextCursor));
  assert.deepEqual(new Set(wire.entries.items.map(item => item.kind)), new Set(['tool', 'resource', 'resource_template', 'prompt']));
  assert.ok(wire.entries.items.every(item => item.executable === false));
  assert.equal(wire.tools.items.filter(item => item.configuredTool).length, 1);
  assert.ok(wire.tools.items.some(item => !item.configuredTool && item.reasonCodes.includes('tool_requires_separate_admission')));
  assert.ok(wire.staleEntries.items.every(item => !item.referenceAvailable));
  const entry = wire.entries.items.find(item => item.id === ref.entryId), observed = Date.parse(wire.freshBinding.binding.fetchedAt) + 1;
  assert.ok(entry); assert.equal(mcpReferenceCurrent(ref, entry, wire.freshBinding.binding, observed), true);
  assert.equal(mcpReferenceCurrent(ref, entry, wire.staleBinding.binding, observed), false);
  assert.equal(mcpReferenceCurrent(ref, entry, wire.disabledBinding.binding, observed), false);
  assert.equal(mcpReferenceCurrent(ref, entry, wire.revokedBinding.binding, observed), false);
  assert.equal(mcpReferenceCurrent(ref, entry, wire.freshBinding.binding, Date.parse(ref.expiresAt)), false);
});

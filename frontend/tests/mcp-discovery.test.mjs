import test from 'node:test';
import assert from 'node:assert/strict';
import { parseMcpJSON, projectMcpCatalogInfo, projectMcpBinding, projectMcpEntry, projectMcpAttempt, projectMcpBindings, projectMcpEntries, projectMcpAttempts,
  projectMcpBindingEnvelope, projectMcpAttemptEnvelope, projectMcpReferenceEnvelope, validateMcpMutation, projectMcpMutation } from '../src/lib/mcp-discovery.ts';
import { workspaceId as w, principalId as p, bindingId, snapshotId, other, scope, hash, clone, catalogInfo, binding, entries, tool, resource, template, prompt, requested, running, succeeded, disabled, revoked, reference, wire, page, decision } from './mcp-discovery-fixtures.mjs';

test('MCP JSON rejects duplicate keys, escaped duplicates, nonfinite numbers and excessive nesting', () => {
  for (const text of ['{"a":1,"a":2}', '{"a":1,"\\u0061":2}', '{"definition":{"name":"one","name":"two"}}', '{"ttl":1e999}', '['.repeat(65) + '0' + ']'.repeat(65)]) assert.throws(() => parseMcpJSON(text));
  assert.deepEqual(parseMcpJSON('{"description":"{ \\"key\\": \\"value\\" }", "items":[{"name":"same"},{"name":"same"}]}'), { description: '{ "key": "value" }', items: [{ name: 'same' }, { name: 'same' }] });
});

test('MCP binding projections retain exact public identity and discard native credentials and paths', () => {
  assert.deepEqual(projectMcpBinding({ ...binding, argv: ['private'], endpoint: 'https://private', nativePath: '/private', signature: 'private' }), binding);
  assert.deepEqual(projectMcpBindings({ ...wire.bindings, setupToken: 'private' }, w, p), wire.bindings);
  assert.deepEqual(projectMcpBindingEnvelope(wire.binding, w, bindingId, p), wire.binding);
  for (const patch of [{ workspaceId: other }, { principalId: other }, { schema: 'next-version' }]) assert.equal(projectMcpBindings({ ...wire.bindings, ...patch }, w, p), null);
  assert.equal(projectMcpBindingEnvelope(wire.binding, w, other, p), null);
});

test('binding state, signature, native limits and observation coherence fail closed', () => {
  for (const patch of [{ id: 'not-uuid' }, { name: 'x'.repeat(97) }, { revision: 0 }, { name: 'line\nbreak' }, { transport: 'websocket' }, { ttlSeconds: 59 }, { ttlSeconds: 3601 },
    { watchMs: 249 }, { watchMs: 5001, transport: 'streamable_http' }, { watchMs: 250 }, { state: 'revoked' }, { state: 'disabled' }, { approvalValid: false }, { activeAttemptId: other },
    { actions: ['call'] }, { actions: ['revoke', 'revoke'] }, { counts: { ...binding.counts, tools: 257 } }, { counts: { ...binding.counts, extra: 1 } }, { fetchedAt: null }, { catalogInfo: null },
    { expiresAt: '2026-11-18T11:00:00Z' }, { lastErrorCode: 'secret /native/path' }, { bindingSha256: hash('A') }]) assert.equal(projectMcpBinding({ ...binding, ...patch }), null, JSON.stringify(patch));
  for (const watchMs of [0, 250, 5000]) assert.ok(projectMcpBinding({ ...binding, transport: 'streamable_http', watchMs }));
  assert.ok(projectMcpBinding({ ...binding, approvalValid: false, stale: true, actions: ['revoke'] }));
  assert.ok(projectMcpBinding(disabled)); assert.ok(projectMcpBinding(revoked));
  const unobserved = { ...binding, snapshotId: null, snapshotSha256: null, fetchedAt: null, expiresAt: null, catalogInfo: null, counts: { tools: 0, resources: 0, resourceTemplates: 0, prompts: 0 }, stale: true };
  assert.ok(projectMcpBinding(unobserved)); assert.equal(projectMcpBinding({ ...unobserved, stale: false }), null);
});

test('server claims are bounded data with explicit negotiated versions and collection support', () => {
  assert.deepEqual(projectMcpCatalogInfo(catalogInfo), catalogInfo);
  assert.ok(projectMcpCatalogInfo({ ...catalogInfo, capabilities: { ...catalogInfo.capabilities, futureClaim: { url: 'https://untrusted.example', arbitrary: [true] } } }));
  for (const patch of [{ protocolVersion: '2999-01-01' }, { serverInfo: { name: 'a', version: '' } }, { serverInfo: { ...catalogInfo.serverInfo, title: 42 } },
    { serverInfo: { ...catalogInfo.serverInfo, padding: 'x'.repeat(16384) } }, { capabilities: { tools: true } }, { capabilities: { ...catalogInfo.capabilities, tools: { listChanged: 'yes' } } },
    { collections: { ...catalogInfo.collections, tools: 'not_advertised' } }, { collections: { tools: 'observed' } }, { notificationSupport: { ...catalogInfo.notificationSupport, tools: false } },
    { notificationSupport: { ...catalogInfo.notificationSupport, extra: false } }, { cacheTtlMs: -1 }, { cacheTtlMs: 0.1 }, { cacheTtlMs: Number.MAX_SAFE_INTEGER + 1 }]) assert.equal(projectMcpCatalogInfo({ ...catalogInfo, ...patch }), null, JSON.stringify(patch).slice(0, 160));
  for (const cacheTtlMs of [null, 0, Number.MAX_SAFE_INTEGER]) assert.ok(projectMcpCatalogInfo({ ...catalogInfo, cacheTtlMs }));
  assert.ok(projectMcpCatalogInfo({ ...catalogInfo, serverInfo: null }));
});

test('all retained interface kinds remain non-executable and bind name, identity and exact definition', () => {
  for (const entry of entries) assert.deepEqual(projectMcpEntry({ ...entry, internalOwner: 'private' }), entry);
  assert.deepEqual(projectMcpEntries(wire.entries, w, bindingId, p, snapshotId), wire.entries);
  for (const patch of [{ executable: true }, { executable: null }, { kind: 'arbitrary' }, { identity: 'different' }, { definition: { ...tool.definition, name: 'different' } },
    { definition: { ...tool.definition, description: 'different' } }, { description: 'x'.repeat(4097) }, { name: 'x'.repeat(129) }, { name: 'line\nbreak' }, { definitionSha256: 'latest' },
    { reasonCodes: ['bad reason'] }, { reasonCodes: ['duplicate', 'duplicate'] }, { definition: { ...tool.definition, value: Infinity } }, { definition: { ...tool.definition, padding: 'x'.repeat(131072) } }]) assert.equal(projectMcpEntry({ ...tool, ...patch }), null);
  assert.equal(projectMcpEntry({ ...resource, configuredTool: true }), null);
  assert.equal(projectMcpEntry({ ...resource, identity: '/relative', definition: { ...resource.definition, uri: '/relative' } }), null);
  assert.equal(projectMcpEntry({ ...template, identity: 'report://bad id', definition: { ...template.definition, uriTemplate: 'report://bad id' } }), null);
  assert.ok(projectMcpEntry({ ...prompt, definition: { ...prompt.definition, untrustedMarkup: '<script>alert(1)</script>', url: 'file:///private' } }));
});

test('metadata depth, node and key bounds are enforced before retained data reaches views', () => {
  let nested = {}; for (let index = 0; index < 33; index++) nested = { nested };
  for (const metadata of [nested, Array(8192).fill(null), { ['k'.repeat(513)]: 'value' }]) assert.equal(projectMcpEntry({ ...tool, definition: { ...tool.definition, metadata } }), null);
});

test('pages reject mixed bindings, snapshots, duplicate IDs, noncanonical or nonadvancing cursors', () => {
  for (const items of [[tool, { ...resource, bindingId: other }], [tool, { ...resource, snapshotId: other }], [tool, { ...resource, snapshotSha256: hash('0') }], [tool, tool], [resource, tool]]) assert.equal(projectMcpEntries(page(items), w, bindingId, p), null);
  assert.equal(projectMcpEntries(wire.entries, w, bindingId, p, other), null);
  for (const nextCursor of ['not-a-uuid', other, tool.id.toUpperCase()]) assert.equal(projectMcpEntries({ ...wire.entries, nextCursor }, w, bindingId, p), null);
  assert.equal(projectMcpEntries({ ...page([]), nextCursor: tool.id }, w, bindingId, p), null);
  assert.deepEqual(projectMcpEntries({ ...page([tool]), nextCursor: tool.id }, w, bindingId, p), { ...page([tool]), nextCursor: tool.id });
  assert.equal(projectMcpBindings({ ...wire.bindings, items: Array(51).fill(binding) }, w, p), null);
});

test('attempt projection retains complete terminal evidence without inventing retry or cancel authority', () => {
  for (const attempt of [requested, running, succeeded]) assert.deepEqual(projectMcpAttempt({ ...attempt, workerToken: 'private' }), attempt);
  assert.deepEqual(projectMcpAttempts(wire.attempts, w, p, bindingId), wire.attempts);
  assert.equal(projectMcpAttempts(wire.attempts, w, p, other), null);
  assert.equal(projectMcpAttemptEnvelope(wire.requestedAttempt, w, other, p), null);
  for (const patch of [{ status: 'queued' }, { actions: ['run'] }, { status: 'succeeded' }, { changes: { added: -1, removed: 0, changed: 0, unchanged: 0 } }, { snapshotId: other }, { reasonCode: 'private /path' }]) assert.equal(projectMcpAttempt({ ...requested, ...patch }), null);
  for (const status of ['failed', 'cancelled', 'indeterminate']) assert.ok(projectMcpAttempt({ ...requested, status, actions: [], reasonCode: 'reconciliation_failed' }));
});

test('exact references require outer and inner scope plus canonical immutable identities', () => {
  assert.deepEqual(projectMcpReferenceEnvelope({ ...wire.reference, reference: { ...reference, executionGrant: 'private' } }, w, bindingId, tool.id, p), wire.reference);
  for (const patch of [{ schema: 'next-version' }, { workspaceId: other }, { principalId: other }, { bindingId: other }, { entryId: other }, { bindingRevision: 0 }, { snapshotId: 'latest' }, { definitionSha256: 'bad' }, { expiresAt: 'never' }]) assert.equal(projectMcpReferenceEnvelope({ ...scope, reference: { ...reference, ...patch } }, w, bindingId, tool.id, p), null);
});

test('mutations accept only exact reviewed lifecycle decisions and original receipts', () => {
  for (const action of ['enable', 'disable', 'revoke', 'reconcile']) assert.deepEqual(validateMcpMutation(['bindings', bindingId, action], decision), decision);
  assert.deepEqual(validateMcpMutation(['attempts', requested.id, 'cancel'], decision), decision);
  for (const patch of [{ revision: 0 }, { revision: Number.MAX_SAFE_INTEGER }, { confirm: false }, { digest: 'latest' }, { argv: ['sh'] }, { url: 'https://evil' }, { snapshotId }]) assert.equal(validateMcpMutation(['bindings', bindingId, 'reconcile'], { ...decision, ...patch }), null);
  for (const path of [['bindings', bindingId, 'admit'], ['bindings', bindingId, 'call'], ['attempts', requested.id, 'run'], ['bindings', bindingId, 'reconcile', 'extra']]) assert.equal(validateMcpMutation(path, decision), null);
  assert.deepEqual(projectMcpMutation(wire.requestedAttempt, w, ['bindings', bindingId, 'reconcile'], decision, p), wire.requestedAttempt);
  assert.equal(projectMcpMutation(wire.runningAttempt, w, ['bindings', bindingId, 'reconcile'], decision, p), null);
  assert.equal(projectMcpMutation(wire.requestedAttempt, w, ['bindings', bindingId, 'reconcile'], { ...decision, digest: hash('0') }, p), null);
  for (const [key, group, action, recordKey] of [['disabledBinding', 'bindings', 'disable', 'binding'], ['enabledBinding', 'bindings', 'enable', 'binding'], ['revokedBinding', 'bindings', 'revoke', 'binding'], ['cancelledAttempt', 'attempts', 'cancel', 'attempt']]) {
    const record = wire[key][recordKey], body = { ...decision, revision: record.revision - 1 };
    assert.deepEqual(projectMcpMutation(wire[key], w, [group, record.id, action], body, p), wire[key]);
    assert.equal(projectMcpMutation({ ...wire[key], [recordKey]: { ...record, revision: record.revision + 1 } }, w, [group, record.id, action], body, p), null);
  }
});

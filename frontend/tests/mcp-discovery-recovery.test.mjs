import test from 'node:test';
import assert from 'node:assert/strict';
import { mcpBindingSelection, mcpDiscoveryScopeInvalidated, mcpAttemptRoute, newMcpDiscoveryAttempt, validateMcpAttempt, mcpDiscoveryJournalScope,
  mcpBindingCurrent, mcpCanReference, mcpReferenceCurrent, mcpAttemptNeedsMonitoring, MCP_DISCOVERY_SCHEMA, MCP_DISCOVERY_ENDPOINT } from '../src/lib/mcp-discovery-client.ts';
import { WorkshopRequests, retainAttempt, recoverAttempt, clearAttempt, clearRejectedAttempt, workshopJournalScope } from '../src/lib/skill-workshop-client.ts';
import { binding as retainedBinding, tool, reference, requested, decision, hash } from './mcp-discovery-fixtures.mjs';

const workspaceId = '11111111-1111-4111-8111-111111111111', principalId = '22222222-2222-4222-8222-222222222222', bindingId = '33333333-3333-4333-8333-333333333333', other = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee';

test('MCP binding deep links select one record only with an exact explicit matching workspace', () => {
  assert.equal(mcpBindingSelection(`?workspace=${workspaceId}&binding=${bindingId}`, workspaceId), bindingId);
  for (const search of [`?binding=${bindingId}`, `?workspace=${other}&binding=${bindingId}`, `?workspace=${workspaceId}&workspace=${workspaceId}&binding=${bindingId}`, `?workspace=${workspaceId}&binding=${bindingId}&binding=${other}`, `?workspace=${workspaceId}&binding=not-a-uuid`, `?workspace=${workspaceId}&check=${bindingId}`]) assert.equal(mcpBindingSelection(search, workspaceId), null, search);
});

test('read and mutation auth failures or changed scope hide retained private MCP views', () => {
  const scope = { schema: MCP_DISCOVERY_SCHEMA, workspaceId, principalId };
  for (const status of [401, 403]) assert.equal(mcpDiscoveryScopeInvalidated(status, null, workspaceId, principalId), true);
  for (const status of [200, 202]) {
    assert.equal(mcpDiscoveryScopeInvalidated(status, { ...scope, principalId: other }, workspaceId, principalId), true);
    assert.equal(mcpDiscoveryScopeInvalidated(status, { ...scope, workspaceId: other }, workspaceId, principalId), true);
    assert.equal(mcpDiscoveryScopeInvalidated(status, scope, workspaceId, principalId), false);
  }
  assert.equal(mcpDiscoveryScopeInvalidated(409, null, workspaceId, principalId), false);
  assert.equal(mcpDiscoveryScopeInvalidated(200, { ...scope, schema: 'untrusted' }, workspaceId, principalId), false);
});

test('creating an exact MCP request retains its original bytes without dispatching it', () => {
  const path = `${MCP_DISCOVERY_ENDPOINT}/bindings/${bindingId}/disable`, body = { revision: 4, digest: 'a'.repeat(64), confirm: true };
  const attempt = newMcpDiscoveryAttempt(path, body);
  assert.deepEqual(mcpAttemptRoute(attempt.path), ['bindings', bindingId, 'disable']);
  assert.deepEqual(JSON.parse(attempt.body), body);
  assert.match(attempt.key, /^mcp-discovery:[a-f0-9-]{36}$/);
  assert.equal(mcpAttemptRoute('https://untrusted.example' + path), null);
  assert.equal(mcpAttemptRoute('/api/ui/mcp-discovery-else/bindings'), null);
});

function storage() { const records = new Map(); return { records, read: async scope => records.get(scope), write: async (scope, value) => { records.set(scope, structuredClone(value)); }, remove: async (scope, key) => { if (records.get(scope)?.key === key) records.delete(scope); } }; }
const mutationPath = `${MCP_DISCOVERY_ENDPOINT}/bindings/${retainedBinding.id}/reconcile`;

test('MCP recovery accepts only exact bounded lifecycle bodies and preserves original JSON formatting', () => {
  const attempt = newMcpDiscoveryAttempt(mutationPath, decision), formatted = { ...attempt, body: JSON.stringify(decision, null, 2) };
  assert.deepEqual(validateMcpAttempt(attempt), attempt); assert.deepEqual(validateMcpAttempt(formatted), formatted);
  for (const patch of [{ path: 'https://evil.example' + mutationPath }, { path: '/api/ui/discovery/sources' }, { path: `${MCP_DISCOVERY_ENDPOINT}/bindings/${retainedBinding.id}/admit` },
    { path: `${MCP_DISCOVERY_ENDPOINT}/attempts/${requested.id}/run` }, { path: mutationPath + '?endpoint=evil' }, { body: JSON.stringify({ ...decision, url: 'https://evil.example' }) },
    { body: ' '.repeat(8192) + attempt.body }, { body: `{"revision":1,"revision":3,"digest":"${decision.digest}","confirm":true}` }, { key: 'unsafe key' }, { extra: true }]) assert.equal(validateMcpAttempt({ ...attempt, ...patch }), null);
});

test('MCP journals isolate workspace, principal, tab and other acquisition owners', () => {
  const prior = globalThis.window, values = new Map(); globalThis.window = { sessionStorage: { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) } };
  try {
    const scope = mcpDiscoveryJournalScope(workspaceId, principalId); assert.equal(scope, 'mcp-discovery:' + workshopJournalScope(workspaceId, principalId));
    assert.equal(scope, mcpDiscoveryJournalScope(workspaceId, principalId)); assert.notEqual(scope, mcpDiscoveryJournalScope(other, principalId)); assert.notEqual(scope, mcpDiscoveryJournalScope(workspaceId, other));
    values.clear(); assert.notEqual(scope, mcpDiscoveryJournalScope(workspaceId, principalId));
  } finally { if (prior === undefined) delete globalThis.window; else globalThis.window = prior; }
});

test('uncertain MCP requests survive retries and scope failures without automatic dispatch or replacement', async () => {
  const store = storage(), journal = 'mcp-discovery:recovery:' + crypto.randomUUID(), attempt = newMcpDiscoveryAttempt(mutationPath, decision);
  const prior = globalThis.fetch; let calls = 0; globalThis.fetch = async () => { calls++; throw Error('Unexpected automatic dispatch'); };
  try {
    assert.equal(await retainAttempt(journal, attempt, store), true); assert.deepEqual(await recoverAttempt(journal, validateMcpAttempt, store), attempt);
    for (const status of [400, 401, 403, 404, 409, 413, 422, 502, 503]) { assert.equal(await clearRejectedAttempt(journal, attempt.key, status, true, store), false); assert.deepEqual(await recoverAttempt(journal, validateMcpAttempt, store), attempt); }
    await assert.rejects(retainAttempt(journal, newMcpDiscoveryAttempt(mutationPath, decision), store), /unresolved/);
    await assert.rejects(retainAttempt(journal, { ...attempt, body: JSON.stringify({ ...decision, revision: decision.revision + 1 }) }, store), /unresolved/);
    await clearAttempt(journal, 'wrong-key', store); assert.deepEqual(await recoverAttempt(journal, validateMcpAttempt, store), attempt);
    assert.equal(mcpDiscoveryScopeInvalidated(401, null, workspaceId, principalId), true); assert.deepEqual(await recoverAttempt(journal, validateMcpAttempt, store), attempt);
    await clearAttempt(journal, attempt.key, store); assert.equal(await recoverAttempt(journal, validateMcpAttempt, store), null); assert.equal(calls, 0);
  } finally { globalThis.fetch = prior; }
});

test('unavailable durable MCP storage retains an honest temporary receipt and definite first rejection clears it', async () => {
  const journal = 'mcp-discovery:memory:' + crypto.randomUUID(), attempt = newMcpDiscoveryAttempt(mutationPath, decision);
  const unavailable = { read: async () => { throw Error('unavailable'); }, write: async () => { throw Error('unavailable'); }, remove: async () => { throw Error('unavailable'); } };
  assert.equal(await retainAttempt(journal, attempt, unavailable), false); assert.deepEqual(await recoverAttempt(journal, validateMcpAttempt, unavailable), attempt);
  assert.equal(await clearRejectedAttempt(journal, attempt.key, 503, false, unavailable), false); assert.equal(await clearRejectedAttempt(journal, attempt.key, 409, false, unavailable), true);
  assert.equal(await recoverAttempt(journal, validateMcpAttempt, unavailable), null);
});

test('catalogue, workspace and filter generation changes fence late status and reference responses', async () => {
  const requests = new WorkshopRequests(), oldReference = requests.open(), oldPage = requests.open(); let release;
  const response = new Promise(resolve => { release = resolve; }); let applied = false;
  const flight = (async () => { await response; if (oldReference.current()) applied = true; oldReference.finish(); })();
  requests.reset(); const current = requests.open(); assert.equal(oldReference.signal.aborted, true); assert.equal(oldPage.signal.aborted, true); release(); await flight;
  assert.equal(applied, false); assert.equal(current.current(), true); requests.reset(); assert.equal(current.current(), false);
});

test('MCP references expire locally and require the exact current signed installation, revision, snapshot and definition', () => {
  const now = Date.parse(retainedBinding.fetchedAt) + 1;
  assert.equal(mcpBindingCurrent(retainedBinding, now), true); assert.equal(mcpCanReference(tool, retainedBinding, now), true); assert.equal(mcpReferenceCurrent(reference, tool, retainedBinding, now), true);
  for (const at of [Date.parse(retainedBinding.expiresAt), Date.parse(retainedBinding.expiresAt) + 1, NaN]) assert.equal(mcpReferenceCurrent(reference, tool, retainedBinding, at), false);
  for (const patch of [{ state: 'disabled' }, { state: 'revoked' }, { stale: true }, { approvalValid: false }, { snapshotId: null }, { snapshotId: other }, { snapshotSha256: hash('0') }, { revision: retainedBinding.revision + 1 }, { bindingSha256: hash('0') }, { installationSha256: hash('0') }, { expiresAt: null }]) assert.equal(mcpReferenceCurrent(reference, tool, { ...retainedBinding, ...patch }, now), false, JSON.stringify(patch));
  for (const patch of [{ referenceAvailable: false }, { bindingId: other }, { snapshotId: other }, { snapshotSha256: hash('0') }, { id: other }, { definitionSha256: hash('0') }, { kind: 'prompt' }, { identity: 'different' }]) assert.equal(mcpReferenceCurrent(reference, { ...tool, ...patch }, retainedBinding, now), false);
  for (const patch of [{ bindingRevision: reference.bindingRevision + 1 }, { bindingSha256: hash('0') }, { installationSha256: hash('0') }, { snapshotSha256: hash('0') }, { definitionSha256: hash('0') }, { expiresAt: '2026-11-18T12:06:00Z' }]) assert.equal(mcpReferenceCurrent({ ...reference, ...patch }, tool, retainedBinding, now), false);
  assert.equal(mcpAttemptNeedsMonitoring(requested), true); assert.equal(mcpAttemptNeedsMonitoring({ ...requested, status: 'running' }), true);
  for (const status of ['succeeded', 'failed', 'cancelled', 'indeterminate']) assert.equal(mcpAttemptNeedsMonitoring({ ...requested, status }), false);
});

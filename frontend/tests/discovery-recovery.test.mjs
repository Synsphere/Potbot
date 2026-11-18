import test from 'node:test';
import assert from 'node:assert/strict';
import { DISCOVERY_ENDPOINT, EMPTY_SEARCH, discoverySearchQuery, discoveryJournalScope, newDiscoveryAttempt, validateDiscoveryAttempt,
  discoveryAttemptRoute, discoveryCanHandoff, discoveryMeasurementCurrent, discoveryNeedsMonitoring, discoveryScopeInvalidated } from '../src/lib/discovery-client.ts';
import { WorkshopRequests, retainAttempt, recoverAttempt, clearAttempt, clearRejectedAttempt, workshopJournalScope } from '../src/lib/skill-workshop-client.ts';
import { workspaceId as w, principalId as p, scope, pending, measured, other, clone } from './discovery-fixtures.mjs';

const body = { snapshotId: pending.snapshotId, candidateSha256: pending.requestedCandidateSha256, agentId: pending.agentId, confirm: true };
const path = `${DISCOVERY_ENDPOINT}/candidates/${pending.candidateId}/checks`;
function storage() { const records = new Map(); return { records, read: async scope => records.get(scope), write: async (scope, value) => { records.set(scope, structuredClone(value)); }, remove: async (scope, key) => { if (records.get(scope)?.key === key) records.delete(scope); } }; }

test('discovery recovery retains only exact fixed reviewed requests with the original body bytes and stable key', () => {
  const attempt = newDiscoveryAttempt(path, body); assert.deepEqual(validateDiscoveryAttempt(attempt), attempt); assert.match(attempt.key, /^discovery:[a-f0-9-]{36}$/);
  assert.deepEqual(discoveryAttemptRoute(path), ['candidates', pending.candidateId, 'checks']);
  const formatted = { ...attempt, body: JSON.stringify(body, null, 2) }; assert.deepEqual(validateDiscoveryAttempt(formatted), formatted);
  for (const patch of [{ path: 'https://evil.example' + path }, { path: '/api/ui/skills/imports/zip' }, { path: DISCOVERY_ENDPOINT + '/../run' }, { path: `${DISCOVERY_ENDPOINT}/checks/${pending.id}/measure` }, { body: JSON.stringify({ ...body, argv: ['sh'] }) }, { body: ' '.repeat(8192) + attempt.body }, { key: 'unsafe key' }, { unknown: true }]) assert.equal(validateDiscoveryAttempt({ ...attempt, ...patch }), null);
});

test('discovery journals remain separate from acquired packages and other workspace or principal scopes', () => {
  const prior = globalThis.window, values = new Map(); globalThis.window = { sessionStorage: { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) } };
  try {
    const scope = discoveryJournalScope(w, p); assert.equal(scope, 'discovery:' + workshopJournalScope(w, p)); assert.equal(scope, discoveryJournalScope(w, p));
    assert.notEqual(scope, discoveryJournalScope(other, p)); assert.notEqual(scope, discoveryJournalScope(w, other)); values.clear(); assert.notEqual(scope, discoveryJournalScope(w, p));
  } finally { if (prior === undefined) delete globalThis.window; else globalThis.window = prior; }
});

test('uncertain discovery decisions survive authorization and revision rejections without automatic replay', async () => {
  const store = storage(), scope = 'discovery:recovery:' + crypto.randomUUID(), attempt = newDiscoveryAttempt(path, body);
  const priorFetch = globalThis.fetch; let calls = 0; globalThis.fetch = async () => { calls++; throw Error('No automatic replay'); };
  try {
    assert.equal(await retainAttempt(scope, attempt, store), true); assert.deepEqual(await recoverAttempt(scope, validateDiscoveryAttempt, store), attempt);
    for (const status of [400, 401, 403, 404, 409, 413, 422, 502, 503]) { assert.equal(await clearRejectedAttempt(scope, attempt.key, status, true, store), false); assert.deepEqual(await recoverAttempt(scope, validateDiscoveryAttempt, store), attempt); }
    await assert.rejects(retainAttempt(scope, newDiscoveryAttempt(path, body), store), /unresolved/);
    await assert.rejects(retainAttempt(scope, { ...attempt, body: JSON.stringify({ ...body, agentId: other }) }, store), /unresolved/);
    await clearAttempt(scope, 'wrong-key', store); assert.deepEqual(await recoverAttempt(scope, validateDiscoveryAttempt, store), attempt);
    await clearAttempt(scope, attempt.key, store); assert.equal(await recoverAttempt(scope, validateDiscoveryAttempt, store), null); assert.equal(calls, 0);
  } finally { globalThis.fetch = priorFetch; }
});

test('discovery recovery reports an unavailable durable store honestly and keeps the open-tab receipt', async () => {
  const scope = 'discovery:memory:' + crypto.randomUUID(), attempt = newDiscoveryAttempt(path, body);
  const unavailable = { read: async () => { throw Error('unavailable'); }, write: async () => { throw Error('unavailable'); }, remove: async () => { throw Error('unavailable'); } };
  assert.equal(await retainAttempt(scope, attempt, unavailable), false); assert.deepEqual(await recoverAttempt(scope, validateDiscoveryAttempt, unavailable), attempt);
  assert.equal(await clearRejectedAttempt(scope, attempt.key, 503, false, unavailable), false); assert.equal(await clearRejectedAttempt(scope, attempt.key, 409, false, unavailable), true);
  assert.equal(await recoverAttempt(scope, validateDiscoveryAttempt, unavailable), null);
});

test('authorization denials and changed-principal receipts invalidate private views without discarding original recovery', async () => {
  const store = storage(), key = 'discovery:scope-change:' + crypto.randomUUID(), attempt = newDiscoveryAttempt(path, body);
  await retainAttempt(key, attempt, store);
  for (const status of [401, 403]) { assert.equal(discoveryScopeInvalidated(status, null, w, p), true); assert.deepEqual(await recoverAttempt(key, validateDiscoveryAttempt, store), attempt); }
  assert.equal(discoveryScopeInvalidated(200, { ...scope, principalId: other, check: pending }, w, p), true);
  assert.equal(discoveryScopeInvalidated(202, { ...scope, principalId: other, check: pending }, w, p), true);
  assert.equal(discoveryScopeInvalidated(200, { ...scope, check: pending }, w, p), false);
  assert.equal(discoveryScopeInvalidated(409, null, w, p), false);
  assert.equal(discoveryScopeInvalidated(200, { principalId: other }, w, p), false);
  assert.deepEqual(await recoverAttempt(key, validateDiscoveryAttempt, store), attempt); await clearAttempt(key, attempt.key, store);
});

test('new search inputs and scoped selection generations abort reads and fence late bodies', async () => {
  const reads = new WorkshopRequests(), oldPage = reads.open(), oldDetail = reads.open(); let finish;
  const response = new Promise(resolve => { finish = resolve; }); let applied = false;
  const flight = (async () => { await response; if (oldPage.current()) applied = true; oldPage.finish(); })();
  reads.reset(); const nextPage = reads.open(); assert.equal(oldPage.signal.aborted, true); assert.equal(oldDetail.signal.aborted, true); finish(); await flight;
  assert.equal(applied, false); oldDetail.finish(); assert.equal(nextPage.current(), true); reads.reset(); assert.equal(nextPage.current(), false);
});

test('search form includes each hard constraint in pagination identity and distinguishes no permission from no constraint', () => {
  const baseline = discoverySearchQuery(EMPTY_SEARCH); assert.equal(baseline.toString(), 'limit=25');
  const constrained = discoverySearchQuery({ ...EMPTY_SEARCH, q: 'inspect', sourceId: pending.sourceId, kind: 'skill', platform: 'linux', local: true, restrictEffects: true, effects: [], restrictScopes: true, scopes: '', classes: 'code_search', inputTypes: 'syn://type/text', outputTypes: 'synny://type/text' });
  assert.equal(constrained.get('permittedEffects'), ''); assert.equal(constrained.get('permittedScopes'), ''); assert.equal(constrained.get('requireLocal'), 'true');
  for (const key of ['q', 'sourceId', 'kind', 'platform', 'classes', 'inputTypes', 'outputTypes']) assert.ok(constrained.has(key), key);
  const next = new URLSearchParams(constrained); next.set('after', 'opaque-safe-cursor'); for (const [key, value] of constrained) assert.equal(next.get(key), value);
  assert.equal(discoverySearchQuery({ ...EMPTY_SEARCH, q: 'a'.repeat(501) }), null);
});

test('handoff obeys the current server action and expiry, while missing prerequisites remain reviewable', () => {
  const now = Date.parse(measured.measurement.checkedAt) + 1, value = clone(measured); value.actions = ['handoff', 'cancel']; value.measurementCurrent = true;
  assert.equal(discoveryMeasurementCurrent(value, now), true); assert.equal(discoveryCanHandoff(value, now), true);
  value.measurement.eligible = false; assert.equal(discoveryCanHandoff(value, now), true);
  assert.equal(discoveryCanHandoff(value, Date.parse(value.measurement.expiresAt)), false);
  for (const patch of [{ actions: ['cancel'] }, { measurementCurrent: false }, { measurement: null }, { status: 'failed' }, { status: 'cancelled' }, { status: 'handed_off' }]) assert.equal(discoveryCanHandoff({ ...value, ...patch }, now), false);
  assert.equal(discoveryNeedsMonitoring(pending), true); assert.equal(discoveryNeedsMonitoring({ ...pending, status: 'measuring' }), true); assert.equal(discoveryNeedsMonitoring(measured), false);
});

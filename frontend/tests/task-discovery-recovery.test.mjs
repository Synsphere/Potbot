import test from 'node:test';
import assert from 'node:assert/strict';
import { taskDiscoveryJournalScope, taskDiscoveryAttemptRoute, newTaskDiscoveryAttempt, validateTaskDiscoveryAttempt, TASK_DISCOVERY_ENDPOINT as BASE } from '../src/lib/task-discovery-client.ts';
import { WorkshopRequests, retainAttempt, recoverAttempt, clearAttempt, clearRejectedAttempt, workshopJournalScope } from '../src/lib/skill-workshop-client.ts';
import { workspaceId as w, principalId as p, other, create, completed } from './task-discovery-fixtures.mjs';
const path = BASE + '/searches';
function storage() { const map = new Map(); return { read: async key => map.get(key) ?? null, write: async (key, value) => map.set(key, value), remove: async key => map.delete(key) }; }

test('task recovery preserves exact request bytes and excludes native or execution routes', () => {
  const command = newTaskDiscoveryAttempt(path, create); assert.deepEqual(validateTaskDiscoveryAttempt(command), command); assert.deepEqual(taskDiscoveryAttemptRoute(path), ['searches']);
  const padded = { ...command, body: ' ' + command.body + '\n' }; assert.deepEqual(validateTaskDiscoveryAttempt(padded), padded);
  for (const candidate of [path + '?url=private', path + '/extra', `https://evil.example${path}`, `${BASE}/searches/${completed.id}/refine`, `${BASE}/searches/${completed.id}/run`, `${BASE}/results`, `${BASE}/publish-index`]) assert.equal(validateTaskDiscoveryAttempt({ ...command, path: candidate }), null);
  for (const patch of [{ key: 'unsafe key' }, { key: ':invalid' }, { body: 'x'.repeat(8193) }, { body: '{"confirm":true,"confirm":false}' }, { body: JSON.stringify({ ...create, vectors: [[1]] }) }, { receipt: {} }]) assert.equal(validateTaskDiscoveryAttempt({ ...command, ...patch }), null);
  assert.notEqual(command.key, newTaskDiscoveryAttempt(path, create).key);
});

test('task journal separates principals, workspaces, tabs and existing discovery owners', () => {
  const prior = globalThis.window, values = new Map(); globalThis.window = { sessionStorage: { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) } };
  try { const scope = taskDiscoveryJournalScope(w, p); assert.equal(scope, 'task-discovery:' + workshopJournalScope(w, p)); assert.equal(scope, taskDiscoveryJournalScope(w, p)); assert.notEqual(scope, taskDiscoveryJournalScope(other, p)); assert.notEqual(scope, taskDiscoveryJournalScope(w, other)); values.clear(); assert.notEqual(scope, taskDiscoveryJournalScope(w, p)); }
  finally { if (prior === undefined) delete globalThis.window; else globalThis.window = prior; }
});

test('uncertain task commands cannot be auto-posted, replaced or cleared by retry rejection', async () => {
  const saved = storage(), scope = 'task-test:' + crypto.randomUUID(), command = newTaskDiscoveryAttempt(path, create), prior = globalThis.fetch; let calls = 0; globalThis.fetch = async () => { calls++; throw Error('automatic dispatch'); };
  try {
    assert.equal(await retainAttempt(scope, command, saved), true); assert.deepEqual(await recoverAttempt(scope, validateTaskDiscoveryAttempt, saved), command);
    for (const status of [400, 401, 403, 404, 409, 413, 422, 502, 503]) { assert.equal(await clearRejectedAttempt(scope, command.key, status, true, saved), false); assert.deepEqual(await recoverAttempt(scope, validateTaskDiscoveryAttempt, saved), command); }
    await assert.rejects(retainAttempt(scope, newTaskDiscoveryAttempt(path, create), saved), /unresolved/); await assert.rejects(retainAttempt(scope, { ...command, body: JSON.stringify({ ...create, query: 'different request' }) }, saved), /unresolved/);
    await clearAttempt(scope, 'wrong-key', saved); assert.deepEqual(await recoverAttempt(scope, validateTaskDiscoveryAttempt, saved), command); await clearAttempt(scope, command.key, saved); assert.equal(await recoverAttempt(scope, validateTaskDiscoveryAttempt, saved), null); assert.equal(calls, 0);
  } finally { globalThis.fetch = prior; }
});

test('temporary recovery is honest and definite first rejection permits another reviewed request', async () => {
  const scope = 'task-memory:' + crypto.randomUUID(), command = newTaskDiscoveryAttempt(path, create), unavailable = { read: async () => { throw Error('unavailable'); }, write: async () => { throw Error('unavailable'); }, remove: async () => { throw Error('unavailable'); } };
  assert.equal(await retainAttempt(scope, command, unavailable), false); assert.deepEqual(await recoverAttempt(scope, validateTaskDiscoveryAttempt, unavailable), command);
  assert.equal(await clearRejectedAttempt(scope, command.key, 503, false, unavailable), false); assert.equal(await clearRejectedAttempt(scope, command.key, 409, false, unavailable), true); assert.equal(await recoverAttempt(scope, validateTaskDiscoveryAttempt, unavailable), null);
});

test('changed task, candidate selection or workspace aborts late reads before applying evidence', async () => {
  const requests = new WorkshopRequests(), previous = requests.open(); let release; const response = new Promise(resolve => { release = resolve; }); let applied = false;
  const flight = (async () => { await response; if (previous.current()) applied = true; previous.finish(); })(); requests.reset(); const current = requests.open(); release(); await flight;
  assert.equal(applied, false); assert.equal(previous.signal.aborted, true); assert.equal(current.current(), true); requests.reset(); assert.equal(current.current(), false);
});

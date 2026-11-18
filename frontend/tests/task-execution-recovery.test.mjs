import test from 'node:test';
import assert from 'node:assert/strict';
import { taskExecutionBase, taskExecutionJournalScope, taskExecutionAttemptRoute, newTaskExecutionAttempt, validateTaskExecutionAttempt, readTaskExecutionArtifact } from '../src/lib/task-execution-client.ts';
import { WorkshopRequests, retainAttempt, recoverAttempt, clearAttempt, clearRejectedAttempt } from '../src/lib/skill-workshop-client.ts';
import { workspaceId as w, principalId as p, selectionId as s, runId, other, hash, run, binding, checkBody, runBody } from './task-execution-fixtures.mjs';
const base = taskExecutionBase(s), path = base + '/checks';
function storage() { const map = new Map(); return { read: async key => map.get(key) ?? null, write: async (key, value) => map.set(key, value), remove: async key => map.delete(key) }; }

test('selected recovery keeps exact bytes and forbids altered operation, selection and execution routes', () => {
  const command = newTaskExecutionAttempt(path, checkBody); assert.deepEqual(validateTaskExecutionAttempt(command, s), command); assert.deepEqual(taskExecutionAttemptRoute(path), ['selections', s, 'checks']);
  assert.deepEqual(validateTaskExecutionAttempt({ ...command, body: ' ' + command.body + '\n' }, s), { ...command, body: ' ' + command.body + '\n' });
  assert.equal(validateTaskExecutionAttempt(command, other), null);
  for (const route of [path + '?url=private', path + '/extra', `https://evil.example${path}`, base + '/dispatch', base + '/jobs/' + runId + '/execute', base.replace(s, other) + '/checks']) assert.equal(validateTaskExecutionAttempt({ ...command, path: route }, s), null);
  for (const patch of [{ key: 'unsafe key' }, { key: ':invalid' }, { body: 'x'.repeat(131073) }, { body: '{"confirm":true,"confirm":false}' }, { body: JSON.stringify({ ...checkBody, result: 'passed' }) }, { receipt: {} }]) assert.equal(validateTaskExecutionAttempt({ ...command, ...patch }, s), null);
  assert.ok(validateTaskExecutionAttempt(newTaskExecutionAttempt(base + '/runs', runBody), s));
  assert.ok(validateTaskExecutionAttempt(newTaskExecutionAttempt(base + '/jobs/' + runId + '/cancel', { confirm: true }), s));
  assert.notEqual(command.key, newTaskExecutionAttempt(path, checkBody).key);
});

test('selected command journals separate workspaces, principals, selections and browser tabs', () => {
  const prior = globalThis.window, values = new Map(); globalThis.window = { sessionStorage: { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) } };
  try { const scope = taskExecutionJournalScope(w, p, s); assert.equal(scope, taskExecutionJournalScope(w, p, s)); assert.notEqual(scope, taskExecutionJournalScope(other, p, s)); assert.notEqual(scope, taskExecutionJournalScope(w, other, s)); assert.notEqual(scope, taskExecutionJournalScope(w, p, other)); values.clear(); assert.notEqual(scope, taskExecutionJournalScope(w, p, s)); }
  finally { if (prior === undefined) delete globalThis.window; else globalThis.window = prior; }
});

test('restart recovery never posts and a rejected retry cannot erase an uncertain original command', async () => {
  const saved = storage(), scope = 'selected-test:' + crypto.randomUUID(), command = newTaskExecutionAttempt(base + '/runs', runBody), prior = globalThis.fetch; let calls = 0; globalThis.fetch = async () => { calls++; throw Error('automatic dispatch'); };
  try {
    assert.equal(await retainAttempt(scope, command, saved), true); assert.deepEqual(await recoverAttempt(scope, value => validateTaskExecutionAttempt(value, s), saved), command);
    for (const status of [400, 401, 403, 404, 409, 413, 422, 502, 503]) { assert.equal(await clearRejectedAttempt(scope, command.key, status, true, saved), false); assert.deepEqual(await recoverAttempt(scope, value => validateTaskExecutionAttempt(value, s), saved), command); }
    await assert.rejects(retainAttempt(scope, newTaskExecutionAttempt(path, checkBody), saved), /unresolved/); await assert.rejects(retainAttempt(scope, { ...command, body: JSON.stringify({ ...runBody, bindingSha256: hash('f') }) }, saved), /unresolved/);
    await clearAttempt(scope, 'wrong-key', saved); assert.deepEqual(await recoverAttempt(scope, value => validateTaskExecutionAttempt(value, s), saved), command); await clearAttempt(scope, command.key, saved); assert.equal(await recoverAttempt(scope, value => validateTaskExecutionAttempt(value, s), saved), null); assert.equal(calls, 0);
  } finally { globalThis.fetch = prior; }
});

test('temporary journals preserve unknown outcomes and definite first rejection allows a new reviewed check', async () => {
  const scope = 'selected-memory:' + crypto.randomUUID(), command = newTaskExecutionAttempt(path, checkBody), unavailable = { read: async () => { throw Error('unavailable'); }, write: async () => { throw Error('unavailable'); }, remove: async () => { throw Error('unavailable'); } };
  assert.equal(await retainAttempt(scope, command, unavailable), false); assert.deepEqual(await recoverAttempt(scope, value => validateTaskExecutionAttempt(value, s), unavailable), command);
  assert.equal(await clearRejectedAttempt(scope, command.key, 503, false, unavailable), false); assert.equal(await clearRejectedAttempt(scope, command.key, 409, false, unavailable), true); assert.equal(await recoverAttempt(scope, value => validateTaskExecutionAttempt(value, s), unavailable), null);
});

test('changing the selected owner aborts in-flight reads before stale evidence reaches the page', async () => {
  const requests = new WorkshopRequests(), previous = requests.open(); let release; const response = new Promise(resolve => { release = resolve; }); let applied = false;
  const flight = (async () => { await response; if (previous.current()) applied = true; previous.finish(); })(); requests.reset(); const current = requests.open(); release(); await flight;
  assert.equal(applied, false); assert.equal(previous.signal.aborted, true); assert.equal(current.current(), true); requests.reset(); assert.equal(current.current(), false);
});

test('artifact download joins every chunk to the selected job and binding then verifies complete SHA-256', async () => {
  const data = new TextEncoder().encode('owned selected result'), digest = Buffer.from(await crypto.subtle.digest('SHA-256', data)).toString('hex'), artifact = { id: other, sha256: digest, bytes: data.length, mediaType: 'text/plain' };
  const options = { scope: run, bindingSha256: binding.sha256, jobId: runId, artifact, signal: new AbortController().signal, current: () => true };
  let calls = 0;
  const read = async (url, init) => { calls++; assert.ok(url.startsWith(base + '/jobs/' + runId + '/artifact?')); assert.equal(init.headers['X-Potbot-Workspace'], w); assert.equal(init.headers['X-Potbot-Setup-Token'], undefined); const offset = Number(new URL(url, 'https://fixture.example').searchParams.get('offset')), end = Math.min(offset + 7, data.length); return Response.json({ ...run, artifact, offset, dataBase64: Buffer.from(data.slice(offset, end)).toString('base64'), nextOffset: end === data.length ? null : end }); };
  assert.deepEqual(await readTaskExecutionArtifact({ ...options, read }), data); assert.equal(calls, 3);
  for (const patch of [{ selectionId: other }, { binding: { ...binding, sha256: hash('f') } }, { job: { ...run.job, id: other } }, { artifact: { ...artifact, sha256: hash('f') } }]) await assert.rejects(readTaskExecutionArtifact({ ...options, read: async () => Response.json({ ...run, artifact, offset: 0, dataBase64: Buffer.from(data).toString('base64'), nextOffset: null, ...patch }) }), /identity changed/);
  await assert.rejects(readTaskExecutionArtifact({ ...options, artifact: { ...artifact, bytes: 16777217 }, read }), /boundary/);
  const wrong = { ...artifact, sha256: hash('0') }; await assert.rejects(readTaskExecutionArtifact({ ...options, artifact: wrong, read: async () => Response.json({ ...run, artifact: wrong, offset: 0, dataBase64: Buffer.from(data).toString('base64'), nextOffset: null }) }), /SHA-256/);
});

test('artifact read fences late responses after selection change without exposing their bytes', async () => {
  let active = true, release; const waiting = new Promise(resolve => { release = resolve; }), artifact = { id: other, sha256: hash('a'), bytes: 2, mediaType: 'application/json' };
  const flight = readTaskExecutionArtifact({ scope: run, bindingSha256: binding.sha256, jobId: runId, artifact, signal: new AbortController().signal, current: () => active, read: async () => { await waiting; return Response.json({ ...run, artifact, offset: 0, dataBase64: btoa('{}'), nextOffset: null }); } });
  active = false; release(); await assert.rejects(flight, error => error.name === 'AbortError');
});

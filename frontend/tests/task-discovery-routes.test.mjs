import test from 'node:test';
import assert from 'node:assert/strict';
import { sessionCookie } from '../src/lib/server/security.ts';
import { taskDiscoveryRoute, taskDiscoveryJSON } from '../src/lib/server/task-discovery.ts';
import { GET, POST } from '../src/app/api/ui/task-discovery/[...path]/route.ts';
import { workspaceId as w, principalId as p, other, taskBinding, candidate, completed, requested, result, selection, create, wire, hash } from './task-discovery-fixtures.mjs';

const auth = { password: 'operator-password-very-long', signingKey: 'a'.repeat(64), publicOrigin: 'http://localhost:3100' }, cookie = sessionCookie(auth).split(';')[0];
const env = { POTBOT_UI_PASSWORD: auth.password, POTBOT_UI_COOKIE_SECRET: auth.signingKey, POTBOT_UI_PUBLIC_ORIGIN: auth.publicOrigin, POTBOT_UI_BACKEND_TOKEN: 'PRIVATE-backend-token', POTBOT_UI_BACKEND_URL: 'http://127.0.0.1:8765/', POTBOT_UI_WORKSPACE_ID: w, POTBOT_UI_SETUP_TOKEN: 'PRIVATE-setup-token-'.repeat(4) };
const params = path => ({ params: Promise.resolve({ path: path.split('/') }) });
function request(method = 'GET', body, options = {}) {
  return new Request(`${auth.publicOrigin}/api/ui/task-discovery${options.query ?? ''}`, { method, headers: { Cookie: cookie, 'X-Potbot-Workspace': w,
    ...(method === 'POST' ? { Origin: auth.publicOrigin, 'Sec-Fetch-Site': 'same-origin', 'Content-Type': 'application/json', 'Idempotency-Key': 'task-discovery:exact-key' } : {}), ...options.headers }, ...(body === undefined ? {} : { body: options.raw ? body : JSON.stringify(body) }) });
}

test('task-discovery BFF has fixed scoped reads and reviewed commands only', async t => {
  const prior = { ...process.env }, priorFetch = globalThis.fetch; Object.assign(process.env, env); delete process.env.POTBOT_UI_WORKSPACES_JSON;
  t.after(() => { globalThis.fetch = priorFetch; for (const key of [...Object.keys(env), 'POTBOT_UI_WORKSPACES_JSON']) if (prior[key] === undefined) delete process.env[key]; else process.env[key] = prior[key]; });
  await t.test('operator session, Origin and route allowlist precede all network use', async () => {
    let calls = 0; globalThis.fetch = async () => { calls++; throw Error('unexpected network'); };
    assert.equal((await GET(new Request(auth.publicOrigin + '/api/ui/task-discovery/searches'), params('searches'))).status, 401);
    assert.equal((await GET(request('GET', undefined, { headers: { Origin: 'https://evil.example' } }), params('searches'))).status, 403);
    assert.equal((await GET(request('GET', undefined, { headers: { 'X-Potbot-Workspace': other } }), params('searches'))).status, 503);
    assert.equal((await taskDiscoveryRoute(request('PUT'), ['searches'])).status, 400);
    for (const path of ['infer', 'refine', 'embeddings', 'register', 'publish-index', 'provider', '../searches', `searches/${completed.id}/run`, `searches/${completed.id}/refine`, `searches/${completed.id}/execute`, `searches/${completed.id}/select/extra`, `results/${result.id}`, `selections/${selection.id}`]) assert.equal((await POST(request('POST', create), params(path))).status, 400, path);
    for (const headers of [{ Origin: '' }, { Origin: 'https://evil.example' }, { 'Sec-Fetch-Site': 'cross-site' }]) assert.equal((await POST(request('POST', create, { headers }), params('searches'))).status, 403);
    assert.equal(calls, 0);
  });
  await t.test('query parsing rejects ambiguity, private parameters and invalid bounds', async () => {
    let calls = 0; globalThis.fetch = async () => { calls++; throw Error('unexpected network'); };
    for (const query of ['?limit=0', '?limit=26', '?limit=01', '?limit=1&limit=2', '?after=latest', '?after=' + other.toUpperCase(), '?url=https://evil.example', '?query=private', '?episodeId=' + other + '&episodeId=' + other]) assert.equal((await GET(request('GET', undefined, { query }), params('searches'))).status, 400, query);
    for (const query of ['', '?episodeId=' + taskBinding.episodeId, '?episodeId=' + taskBinding.episodeId + '&stateId=latest', '?episodeId=' + taskBinding.episodeId + '&stateId=' + taskBinding.repositoryStateId + '&stateId=' + other]) assert.equal((await GET(request('GET', undefined, { query }), params('context'))).status, 400, query);
    assert.equal((await GET(request('GET', undefined, { query: '?limit=1' }), params(`searches/${completed.id}`))).status, 400);
    assert.equal((await POST(request('POST', create, { query: '?modelDir=/PRIVATE' }), params('searches'))).status, 400); assert.equal(calls, 0);
  });
  await t.test('passive routes send no setup token and return private bounded projections', async () => {
    const cases = [['searches', wire.searches, '', '?limit=25'], ['context', wire.context, `?episodeId=${taskBinding.episodeId}&stateId=${taskBinding.repositoryStateId}`], [`searches/${completed.id}`, wire.search], [`results/${result.id}`, wire.result], [`selections/${selection.id}`, wire.selection], [`searches/${completed.id}/candidates/${candidate.id}`, wire.candidate]];
    for (const [path, value, query = '', expectedQuery = query] of cases) {
      globalThis.fetch = async (url, options) => { assert.equal(url, 'http://127.0.0.1:8765/v1/ui/task-discovery/' + path + expectedQuery); assert.equal(options.method, 'GET'); assert.equal(options.headers.Authorization, 'Bearer PRIVATE-backend-token'); assert.equal(options.headers['X-Potbot-Setup-Token'], undefined); assert.equal(options.redirect, 'error'); assert.equal(options.cache, 'no-store'); return Response.json({ ...value, nativeClaim: 'PRIVATE' }); };
      const response = await GET(request('GET', undefined, { query }), params(path)); assert.equal(response.status, 200, path); assert.equal(response.headers.get('Cache-Control'), 'private, no-store'); assert.deepEqual(await response.json(), value);
    }
  });
  await t.test('reviewed mutations forward fixed authority and verify exact receipt identity', async () => {
    const select = { revision: completed.revision, digest: completed.bindingSha256, candidateId: candidate.id, confirm: true }, cancel = { revision: requested.revision, digest: requested.bindingSha256, confirm: true };
    for (const [path, body, value] of [['searches', create, wire.search], [`searches/${completed.id}/select`, select, wire.selection], [`searches/${requested.id}/cancel`, cancel, wire.cancelled]]) {
      globalThis.fetch = async (url, options) => { assert.equal(url, 'http://127.0.0.1:8765/v1/ui/task-discovery/' + path); assert.equal(options.method, 'POST'); assert.equal(options.headers['Idempotency-Key'], 'task-discovery:exact-key'); assert.equal(options.headers['X-Potbot-Setup-Token'], env.POTBOT_UI_SETUP_TOKEN); assert.equal(options.headers.Origin, auth.publicOrigin); assert.deepEqual(JSON.parse(options.body), body); return Response.json(value, { status: 201 }); };
      const response = await POST(request('POST', body), params(path)); assert.equal(response.status, 201); assert.deepEqual(await response.json(), value);
    }
    globalThis.fetch = async () => Response.json({ ...wire.selection, selection: { ...selection, candidateId: hash('f') } });
    assert.equal((await POST(request('POST', select), params(`searches/${completed.id}/select`))).status, 502);
    globalThis.fetch = async () => Response.json({ ...wire.search, search: { ...completed, query: 'different task' } }); assert.equal((await POST(request('POST', create), params('searches'))).status, 502);
  });
  await t.test('byte, duplicate-key and exact shape boundaries reject before network', async () => {
    let calls = 0; globalThis.fetch = async () => { calls++; throw Error('unexpected network'); };
    for (const raw of [' '.repeat(8193), '{"confirm":true,"confirm":false}', JSON.stringify({ ...create, runtimePath: '/PRIVATE' }), JSON.stringify({ ...create, mode: 'hybrid', embedding: { spaceId: other, profileSha256: hash('a'), embeddingName: 'test', maxCandidates: 17, runtimeBudgetMs: 100 } })]) assert.equal((await POST(request('POST', raw, { raw: true }), params('searches'))).status, 400);
    for (const headers of [{ 'Idempotency-Key': '' }, { 'Idempotency-Key': ':invalid' }, { 'Content-Type': 'text/plain' }]) assert.equal((await POST(request('POST', create, { headers }), params('searches'))).status, 400);
    delete process.env.POTBOT_UI_SETUP_TOKEN; assert.equal((await POST(request('POST', create), params('searches'))).status, 503); process.env.POTBOT_UI_SETUP_TOKEN = env.POTBOT_UI_SETUP_TOKEN; assert.equal(calls, 0);
  });
  await t.test('foreign identities, reordered cursors and stale task contexts fail closed', async () => {
    for (const patch of [{ workspaceId: other }, { principalId: 'not-a-uuid' }, { items: [{ ...completed, nativeClaim: 'PRIVATE' }] }, { nextCursor: other }]) { globalThis.fetch = async () => Response.json({ ...wire.searches, ...patch }); assert.equal((await GET(request(), params('searches'))).status, 502); }
    globalThis.fetch = async () => Response.json(wire.searches); assert.equal((await GET(request('GET', undefined, { query: '?after=' + completed.id }), params('searches'))).status, 502);
    globalThis.fetch = async () => Response.json(wire.context); assert.equal((await GET(request('GET', undefined, { query: `?episodeId=${taskBinding.episodeId}&stateId=${other}` }), params('context'))).status, 502);
    globalThis.fetch = async () => Response.json({ ...wire.candidate, detail: { raw: 'x'.repeat(131072) } }); assert.equal((await GET(request(), params(`searches/${completed.id}/candidates/${candidate.id}`))).status, 502);
  });
  await t.test('errors and malformed or oversized responses never expose private upstream text', async () => {
    for (const status of [400, 401, 403, 409, 500]) { globalThis.fetch = async () => new Response('PRIVATE-stack-with-token', { status }); const response = await GET(request(), params('searches')); assert.notEqual(response.status, 200); assert.doesNotMatch(await response.text(), /PRIVATE/); }
    for (const raw of ['{"schema":"potbot-task-discovery/v1","schema":"PRIVATE"}', 'x'.repeat(1572865)]) { globalThis.fetch = async () => new Response(raw); const response = await GET(request(), params('searches')); assert.equal(response.status, 503); assert.doesNotMatch(await response.text(), /PRIVATE/); }
  });
});

test('stream parser enforces actual bytes and valid UTF-8 without Content-Length', async () => {
  await assert.rejects(taskDiscoveryJSON(new Response(new Uint8Array([0xc0, 0x80])), 100));
  let cancelled = false; const body = new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode('x'.repeat(10))); }, cancel() { cancelled = true; } });
  await assert.rejects(taskDiscoveryJSON(new Response(body), 9), /limit/); assert.equal(cancelled, true);
  assert.deepEqual(await taskDiscoveryJSON(new Response('{"ok":true}'), 11), { ok: true });
});

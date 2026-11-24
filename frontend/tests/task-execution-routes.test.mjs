import test from 'node:test';
import assert from 'node:assert/strict';
import { sessionCookie } from '../src/lib/server/security.ts';
import { taskExecutionRoute } from '../src/lib/server/task-execution.ts';
import { GET, POST } from '../src/app/api/ui/task-execution/[...path]/route.ts';
import { workspaceId as w, selectionId as s, checkId, runId, other, hash, context, check, run, checkBody, runBody, result, events, jobs, binding } from './task-execution-fixtures.mjs';

const auth = { password: 'operator-password-very-long', signingKey: 'a'.repeat(64), publicOrigin: 'http://localhost:3100' }, cookie = sessionCookie(auth).split(';')[0];
const env = { POTBOT_UI_PASSWORD: auth.password, POTBOT_UI_COOKIE_SECRET: auth.signingKey, POTBOT_UI_PUBLIC_ORIGIN: auth.publicOrigin, POTBOT_UI_BACKEND_TOKEN: 'PRIVATE-backend-token', POTBOT_UI_BACKEND_URL: 'http://127.0.0.1:8765/', POTBOT_UI_WORKSPACE_ID: w, POTBOT_UI_SETUP_TOKEN: 'PRIVATE-setup-token-'.repeat(4) };
const base = `selections/${s}`, params = path => ({ params: Promise.resolve(path.split('/')).then(path => ({ path })) });
function request(method = 'GET', body, options = {}) { return new Request(`${auth.publicOrigin}/api/ui/task-execution${options.query ?? ''}`, { method, headers: { Cookie: cookie, 'X-Potbot-Workspace': w, ...(method === 'POST' ? { Origin: auth.publicOrigin, 'Sec-Fetch-Site': 'same-origin', 'Content-Type': 'application/json', 'Idempotency-Key': 'task-execution:fixed-command' } : {}), ...options.headers }, ...(body === undefined ? {} : { body: options.raw ? body : JSON.stringify(body) }) }); }

test('selected-task BFF exposes only fixed owner routes with bounded scoped responses', async t => {
  const prior = { ...process.env }, priorFetch = globalThis.fetch; Object.assign(process.env, env); delete process.env.POTBOT_UI_WORKSPACES_JSON;
  t.after(() => { globalThis.fetch = priorFetch; for (const key of [...Object.keys(env), 'POTBOT_UI_WORKSPACES_JSON']) if (prior[key] === undefined) delete process.env[key]; else process.env[key] = prior[key]; });
  await t.test('loopback, origin, workspace and route checks precede backend access', async () => {
    let calls = 0; globalThis.fetch = async () => { calls++; throw Error('unexpected network'); };
    assert.equal((await GET(new Request('http://remote.invalid:3100' + '/api/ui/task-execution'), params(base + '/context'))).status, 403);
    assert.equal((await GET(request('GET', undefined, { headers: { Origin: 'https://evil.example' } }), params(base + '/context'))).status, 403);
    assert.equal((await GET(request('GET', undefined, { headers: { 'X-Potbot-Workspace': other } }), params(base + '/context'))).status, 503);
    assert.equal((await taskExecutionRoute(request('PUT'), [base, 'checks'])).status, 400);
    for (const path of ['run', 'provider', 'selections/latest/context', `${base}/execute`, `${base}/refine`, `${base}/checks/extra`, `${base}/jobs/${runId}/dispatch`, `${base}/jobs/${runId}/cancel/extra`, `${base}/../runs`]) assert.equal((await POST(request('POST', checkBody), params(path))).status, 400, path);
    for (const headers of [{ Origin: '' }, { Origin: 'https://evil.example' }, { 'Sec-Fetch-Site': 'cross-site' }]) assert.equal((await POST(request('POST', checkBody, { headers }), params(base + '/checks'))).status, 403);
    assert.equal(calls, 0);
  });
  await t.test('passive context, history, exact jobs, events, outcomes and artifacts send no setup capability', async () => {
    const artifact = { ...run, artifact: { id: other, sha256: hash('a'), bytes: 2, mediaType: 'application/json' }, offset: 0, dataBase64: btoa('{}'), nextOffset: null };
    for (const [path, value, suffix] of [[base + '/context', context, ''], [base + '/jobs', jobs, '?limit=25'], [base + '/jobs/' + checkId, check, ''], [base + '/jobs/' + runId + '/events', events, '?after=0&limit=100'], [base + '/jobs/' + runId + '/result', result, ''], [base + '/jobs/' + runId + '/artifact', artifact, '?offset=0&length=262144']]) {
      globalThis.fetch = async (url, options) => { assert.equal(url, 'http://127.0.0.1:8765/v1/ui/task-execution/' + path + suffix); assert.equal(options.method, 'GET'); assert.equal(options.headers.Authorization, 'Bearer PRIVATE-backend-token'); assert.equal(options.headers['X-Potbot-Setup-Token'], undefined); assert.equal(options.redirect, 'error'); assert.equal(options.cache, 'no-store'); return Response.json({ ...value, privateField: 'PRIVATE' }); };
      const response = await GET(request(), params(path)); assert.equal(response.status, 200, path); assert.equal(response.headers.get('Cache-Control'), 'private, no-store'); assert.deepEqual(await response.json(), value);
    }
  });
  await t.test('checks and runs use setup capability and stable keys; launch cannot replace retained input', async () => {
    for (const [tail, body, receipt] of [['checks', checkBody, check], ['runs', runBody, run]]) {
      globalThis.fetch = async (url, options) => { assert.equal(url, 'http://127.0.0.1:8765/v1/ui/task-execution/' + base + '/' + tail); assert.equal(options.method, 'POST'); assert.equal(options.headers['X-Potbot-Setup-Token'], env.POTBOT_UI_SETUP_TOKEN); assert.equal(options.headers['Idempotency-Key'], 'task-execution:fixed-command'); assert.equal(options.headers.Origin, auth.publicOrigin); assert.deepEqual(JSON.parse(options.body), body); return Response.json(receipt, { status: 202 }); };
      const response = await POST(request('POST', body), params(base + '/' + tail)); assert.equal(response.status, 202); assert.deepEqual(await response.json(), receipt);
    }
    let calls = 0; globalThis.fetch = async () => { calls++; throw Error('unexpected network'); };
    for (const body of [{ ...runBody, request: binding.request }, { ...runBody, acceptance: binding.acceptance }, { ...runBody, provider: 'arbitrary' }]) assert.equal((await POST(request('POST', body), params(base + '/runs'))).status, 400);
    delete process.env.POTBOT_UI_SETUP_TOKEN; assert.equal((await POST(request('POST', checkBody), params(base + '/checks'))).status, 503); process.env.POTBOT_UI_SETUP_TOKEN = env.POTBOT_UI_SETUP_TOKEN; assert.equal(calls, 0);
  });
  await t.test('negative cancellation does not depend on setup permission or usable positive binding', async () => {
    delete process.env.POTBOT_UI_SETUP_TOKEN;
    const negative = { ...run, binding: null, job: { ...run.job, state: 'cancelling', cancellation_requested: true } };
    globalThis.fetch = async (url, options) => { assert.equal(url, 'http://127.0.0.1:8765/v1/ui/task-execution/' + base + '/jobs/' + runId + '/cancel'); assert.equal(options.headers['X-Potbot-Setup-Token'], undefined); assert.equal(options.headers.Origin, auth.publicOrigin); assert.deepEqual(JSON.parse(options.body), { confirm: true }); return Response.json(negative); };
    const response = await POST(request('POST', { confirm: true }), params(base + '/jobs/' + runId + '/cancel')); assert.equal(response.status, 200); assert.deepEqual(await response.json(), negative);
    process.env.POTBOT_UI_SETUP_TOKEN = env.POTBOT_UI_SETUP_TOKEN;
    globalThis.fetch = async () => Response.json(negative); assert.equal((await GET(request(), params(base + '/jobs/' + runId))).status, 502);
  });
  await t.test('query validation accepts opaque keyset cursors but rejects duplicates, unsupported parameters and ranges', async () => {
    let calls = 0; globalThis.fetch = async () => { calls++; throw Error('unexpected network'); };
    for (const query of ['?limit=0', '?limit=26', '?limit=01', '?limit=1&limit=2', '?after=', '?after=bad+cursor', '?after=' + 'x'.repeat(513), '?url=https://evil.example']) assert.equal((await GET(request('GET', undefined, { query }), params(base + '/jobs'))).status, 400, query);
    for (const query of ['?after=-1', '?after=01', '?after=9007199254740992', '?limit=101', '?after=1&after=2']) assert.equal((await GET(request('GET', undefined, { query }), params(base + '/jobs/' + runId + '/events'))).status, 400, query);
    for (const query of ['?offset=-1', '?length=0', '?length=262145', '?offset=0&offset=1']) assert.equal((await GET(request('GET', undefined, { query }), params(base + '/jobs/' + runId + '/artifact'))).status, 400, query);
    assert.equal((await GET(request('GET', undefined, { query: '?limit=1' }), params(base + '/context'))).status, 400);
    assert.equal((await POST(request('POST', checkBody, { query: '?provider=arbitrary' }), params(base + '/checks'))).status, 400); assert.equal(calls, 0);
    globalThis.fetch = async (url) => { assert.ok(url.endsWith('?limit=25&after=opaque_keyset-cursor')); return Response.json(jobs); };
    assert.equal((await GET(request('GET', undefined, { query: '?after=opaque_keyset-cursor' }), params(base + '/jobs'))).status, 200);
  });
  await t.test('request byte, UTF-8, duplicate-key and exact-shape boundaries reject before forwarding', async () => {
    let calls = 0; globalThis.fetch = async () => { calls++; throw Error('unexpected network'); };
    for (const raw of [' '.repeat(131073), '{"confirm":true,"confirm":false}', JSON.stringify({ ...checkBody, result: { status: 'passed' } }), JSON.stringify({ ...checkBody, acceptance: { ...binding.acceptance, assertions: [{ op: 'expression', pointer: '/text', value: 'execute' }] } })]) assert.equal((await POST(request('POST', raw, { raw: true }), params(base + '/checks'))).status, 400);
    for (const headers of [{ 'Idempotency-Key': '' }, { 'Idempotency-Key': ':invalid' }, { 'Idempotency-Key': 'x'.repeat(129) }, { 'Content-Type': 'text/plain' }]) assert.equal((await POST(request('POST', checkBody, { headers }), params(base + '/checks'))).status, 400);
    assert.equal(calls, 0);
  });
  await t.test('foreign receipts, copied assertions and nonadvancing history fail closed', async () => {
    for (const receipt of [{ ...check, selectionId: other }, { ...check, binding: { ...binding, consumerSha256: 'missing' } }, { ...check, binding: { ...binding, acceptance: { ...binding.acceptance, assertions: [binding.acceptance.assertions[0]] } } }]) { globalThis.fetch = async () => Response.json(receipt); assert.equal((await POST(request('POST', checkBody), params(base + '/checks'))).status, 502); }
    globalThis.fetch = async () => Response.json({ ...run, job: { ...run.job, operation_check_id: other } }); assert.equal((await POST(request('POST', runBody), params(base + '/runs'))).status, 502);
    globalThis.fetch = async () => Response.json({ ...jobs, nextCursor: 'same-cursor', hasMore: true }); assert.equal((await GET(request('GET', undefined, { query: '?after=same-cursor' }), params(base + '/jobs'))).status, 502);
    globalThis.fetch = async () => Response.json(jobs); assert.equal((await GET(request('GET', undefined, { query: '?limit=1' }), params(base + '/jobs'))).status, 502);
  });
  await t.test('upstream private errors, duplicate JSON and oversized bodies are never returned as evidence', async () => {
    for (const status of [400, 401, 403, 409, 500]) { globalThis.fetch = async () => new Response('PRIVATE-stack-with-token', { status }); const response = await GET(request(), params(base + '/context')); assert.notEqual(response.status, 200); assert.doesNotMatch(await response.text(), /PRIVATE/); }
    for (const raw of ['{"schema":"potbot-task-execution/v1","schema":"PRIVATE"}', 'x'.repeat(1500001)]) { globalThis.fetch = async () => new Response(raw); const response = await GET(request(), params(base + '/context')); assert.equal(response.status, 503); assert.doesNotMatch(await response.text(), /PRIVATE/); }
  });
});

import test from 'node:test';
import assert from 'node:assert/strict';
import { sessionCookie } from '../src/lib/server/security.ts';
import { researchRoute } from '../src/lib/server/research.ts';
import { GET, POST } from '../src/app/api/ui/research/[...path]/route.ts';
import * as f from './research-fixtures.mjs';
const auth = { password: 'operator-password-very-long', signingKey: 'a'.repeat(64), publicOrigin: 'http://localhost:3100' }, cookie = sessionCookie(auth).split(';')[0];
const env = { POTBOT_UI_PASSWORD: auth.password, POTBOT_UI_COOKIE_SECRET: auth.signingKey, POTBOT_UI_PUBLIC_ORIGIN: auth.publicOrigin, POTBOT_UI_BACKEND_TOKEN: 'PRIVATE-backend-token', POTBOT_UI_BACKEND_URL: 'http://127.0.0.1:8765/', POTBOT_UI_WORKSPACE_ID: f.w, POTBOT_UI_SETUP_TOKEN: 'PRIVATE-setup-token-'.repeat(4) };
const params = path => ({ params: Promise.resolve({ path: path.split('/') }) }), contextQuery = `?episodeId=${f.episode}&repositoryStateId=${f.state}&needId=${f.needId}`;
function request(method = 'GET', body, options = {}) { return new Request(`${auth.publicOrigin}/api/ui/research${options.query ?? ''}`, { method, headers: { Cookie: cookie, 'X-Potbot-Workspace': f.w, ...(method === 'POST' ? { Origin: auth.publicOrigin, 'Sec-Fetch-Site': 'same-origin', 'Content-Type': 'application/json', 'X-Potbot-Expected-Principal': f.p, ...(options.citation ? {} : { 'Idempotency-Key': 'research:exact-key' }) } : {}), ...options.headers }, ...(body === undefined ? {} : { body: options.raw ? body : JSON.stringify(body) }) }); }

test('research BFF exposes only fixed workspace owners and projects safe bounded responses', async t => {
  const prior = { ...process.env }, priorFetch = globalThis.fetch; Object.assign(process.env, env); delete process.env.POTBOT_UI_WORKSPACES_JSON;
  t.after(() => { globalThis.fetch = priorFetch; for (const key of [...Object.keys(env), 'POTBOT_UI_WORKSPACES_JSON']) if (prior[key] === undefined) delete process.env[key]; else process.env[key] = prior[key]; });
  await t.test('authentication, workspace and same-origin checks precede upstream access', async () => {
    let calls = 0; globalThis.fetch = async () => { calls++; throw Error('unexpected'); };
    assert.equal((await GET(new Request(auth.publicOrigin + '/api/ui/research'), params('runs'))).status, 401);
    assert.equal((await GET(request('GET', undefined, { headers: { Origin: 'https://evil.example' } }), params('runs'))).status, 403);
    assert.equal((await GET(request('GET', undefined, { headers: { 'X-Potbot-Workspace': f.other } }), params('runs'))).status, 503);
    assert.equal((await researchRoute(request('PUT'), ['runs'])).status, 400);
    for (const path of ['provider/search', 'search', 'fetch', 'run', 'runs/latest', `runs/${f.runId}/dispatch`, `runs/${f.runId}/sources/https://evil.example`, `runs/${f.runId}/cancel/extra`, 'citations/extra', 'comparisons/latest', 'context/extra']) assert.equal((await POST(request('POST', f.runBody), params(path))).status, 400, path);
    for (const headers of [{ Origin: '' }, { Origin: 'https://evil.example' }, { 'Sec-Fetch-Site': 'cross-site' }]) assert.equal((await POST(request('POST', f.citationBody, { citation: true, headers }), params('citations'))).status, 403);
    assert.equal(calls, 0);
  });
  await t.test('GET context, history, exact run, result, source, events and comparison are passive', async () => {
    const rows = [['context', f.context, contextQuery, contextQuery], ['runs', f.runs, '', '?limit=25'], [`runs/${f.runId}`, { ...f.envelope, execution: f.execution }, '', ''], [`runs/${f.runId}/events`, f.events, '', '?after=0&limit=100'], [`runs/${f.runId}/result`, f.result, '', ''], [`runs/${f.runId}/sources/${f.source.id}`, f.sourceEnvelope, '', ''], [`comparisons/${f.evidenceId}`, f.comparison, '', '']];
    for (const [path, value, query, suffix] of rows) {
      globalThis.fetch = async (url, options) => { assert.equal(url, 'http://127.0.0.1:8765/v1/ui/research/' + path + suffix); assert.equal(options.method, 'GET'); assert.equal(options.headers.Authorization, 'Bearer PRIVATE-backend-token'); assert.equal(options.headers['X-Potbot-Setup-Token'], undefined); assert.equal(options.headers['Idempotency-Key'], undefined); assert.equal(options.body, undefined); assert.equal(options.cache, 'no-store'); assert.equal(options.redirect, 'error'); return Response.json({ ...value, privateField: 'PRIVATE' }); };
      const response = await GET(request('GET', undefined, { query }), params(path)); assert.equal(response.status, 200, path); assert.equal(response.headers.get('Cache-Control'), 'private, no-store'); assert.deepEqual(await response.json(), value);
    }
  });
  await t.test('real mutations forward setup permission, stable key and the exact validated body', async () => {
    for (const [path, body, receipt] of [['needs', f.needBody, f.needReceipt], ['actions', f.actionBody, f.actionReceipt], ['runs', f.runBody, f.queued], ['comparisons', f.comparisonBody, f.comparison]]) {
      globalThis.fetch = async (url, options) => { assert.equal(url, 'http://127.0.0.1:8765/v1/ui/research/' + path); assert.equal(options.headers['X-Potbot-Setup-Token'], env.POTBOT_UI_SETUP_TOKEN); assert.equal(options.headers['Idempotency-Key'], 'research:exact-key'); assert.equal(options.headers['X-Potbot-Expected-Principal'], f.p); assert.equal(options.headers.Origin, auth.publicOrigin); assert.deepEqual(JSON.parse(options.body), body); return Response.json(receipt, { status: 202 }); };
      const response = await POST(request('POST', body), params(path)); assert.equal(response.status, 202, path); assert.deepEqual(await response.json(), receipt);
    }
    delete process.env.POTBOT_UI_SETUP_TOKEN; let calls = 0; globalThis.fetch = async () => { calls++; throw Error('unexpected'); };
    assert.equal((await POST(request('POST', f.runBody), params('runs'))).status, 503); assert.equal(calls, 0); process.env.POTBOT_UI_SETUP_TOKEN = env.POTBOT_UI_SETUP_TOKEN;
  });
  await t.test('every POST requires one canonical reviewed principal before upstream access', async () => {
    let calls = 0; globalThis.fetch = async () => { calls++; throw Error('unexpected'); };
    const rows = [['needs', f.needBody], ['actions', f.actionBody], ['runs', f.runBody], ['comparisons', f.comparisonBody], [`runs/${f.runId}/cancel`, { confirm: true }], ['citations', f.citationBody]];
    for (const [path, body] of rows) {
      const missing = request('POST', body, { citation: path === 'citations' }); missing.headers.delete('X-Potbot-Expected-Principal');
      assert.equal((await POST(missing, params(path))).status, 400, path);
      const duplicate = request('POST', body, { citation: path === 'citations' }); duplicate.headers.append('X-Potbot-Expected-Principal', f.p);
      assert.equal((await POST(duplicate, params(path))).status, 400, path);
    }
    for (const value of ['', 'latest', 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA', f.p + ',' + f.other]) {
      assert.equal((await POST(request('POST', f.runBody, { headers: { 'X-Potbot-Expected-Principal': value } }), params('runs'))).status, 400);
      assert.equal((await GET(request('GET', undefined, { headers: { 'X-Potbot-Expected-Principal': value } }), params('runs'))).status, 400);
    }
    assert.equal(calls, 0);
  });
  await t.test('principal rotation is fenced before owner effects and its denial reaches scope recovery', async () => {
    let effects = 0;
    globalThis.fetch = async (_url, options) => {
      assert.equal(options.headers['X-Potbot-Expected-Principal'], f.p);
      const authenticatedPrincipal = f.other;
      if (options.headers['X-Potbot-Expected-Principal'] !== authenticatedPrincipal) return new Response('PRIVATE-principal-rotation', { status: 403 });
      effects++; return Response.json(f.queued);
    };
    const response = await POST(request('POST', f.runBody), params('runs'));
    assert.equal(response.status, 403); assert.equal(effects, 0); assert.doesNotMatch(await response.text(), /PRIVATE/);
    globalThis.fetch = async () => Response.json({ ...f.needReceipt, scope: { ...f.scope, principalId: f.other } });
    assert.equal((await POST(request('POST', f.needBody), params('needs'))).status, 502, 'a foreign receipt is never accepted as the reviewed command');
    globalThis.fetch = async (_url, options) => { assert.equal(options.headers['X-Potbot-Expected-Principal'], f.p); return Response.json(f.runs); };
    assert.equal((await GET(request('GET', undefined, { headers: { 'X-Potbot-Expected-Principal': f.p } }), params('runs'))).status, 200);
    globalThis.fetch = async () => Response.json({ ...f.runs, scope: { ...f.scope, principalId: f.other }, items: [] });
    assert.equal((await GET(request('GET', undefined, { headers: { 'X-Potbot-Expected-Principal': f.p } }), params('runs'))).status, 502);
  });
  await t.test('citation POST is an explicit origin-protected read with no setup, confirmation or mutation journal key', async () => {
    delete process.env.POTBOT_UI_SETUP_TOKEN;
    globalThis.fetch = async (url, options) => { assert.equal(url, 'http://127.0.0.1:8765/v1/ui/research/citations'); assert.equal(options.method, 'POST'); assert.equal(options.headers['X-Potbot-Setup-Token'], undefined); assert.equal(options.headers['Idempotency-Key'], undefined); assert.equal(options.headers.Origin, auth.publicOrigin); assert.equal(options.headers['X-Potbot-Expected-Principal'], f.p); assert.deepEqual(JSON.parse(options.body), f.citationBody); return Response.json(f.citationEnvelope); };
    const response = await POST(request('POST', f.citationBody, { citation: true }), params('citations')); assert.equal(response.status, 200); assert.deepEqual(await response.json(), f.citationEnvelope);
    let calls = 0; globalThis.fetch = async () => { calls++; throw Error('unexpected'); };
    assert.equal((await POST(request('POST', f.citationBody), params('citations'))).status, 400);
    assert.equal((await POST(request('POST', { ...f.citationBody, confirm: true }, { citation: true }), params('citations'))).status, 400); assert.equal(calls, 0);
    process.env.POTBOT_UI_SETUP_TOKEN = env.POTBOT_UI_SETUP_TOKEN;
  });
  await t.test('cancellation remains available without setup or intact positive authority', async () => {
    delete process.env.POTBOT_UI_SETUP_TOKEN;
    const negative = { ...f.queued, execution: { ...f.queuedExecution, authorization: null, run: { ...f.queuedRun, authorizationSha256: null, policySha256: null, consumerSha256: null, recordIntegrityValid: false, state: 'blocked', finishedAt: f.now, errorCode: 'research_cancelled', cancelRequested: true } } };
    globalThis.fetch = async (url, options) => { assert.equal(options.headers['X-Potbot-Setup-Token'], undefined); assert.equal(options.headers['Idempotency-Key'], 'research:exact-key'); assert.equal(options.headers['X-Potbot-Expected-Principal'], f.p); assert.deepEqual(JSON.parse(options.body), { confirm: true }); return Response.json(negative); };
    const response = await POST(request('POST', { confirm: true }), params(`runs/${f.runId}/cancel`)); assert.equal(response.status, 200); assert.deepEqual(await response.json(), negative); process.env.POTBOT_UI_SETUP_TOKEN = env.POTBOT_UI_SETUP_TOKEN;
  });
  await t.test('query selectors distinguish opaque history keys, integer events and exact task context', async () => {
    let calls = 0; globalThis.fetch = async () => { calls++; throw Error('unexpected'); };
    for (const query of ['?limit=0', '?limit=26', '?limit=01', '?limit=1&limit=2', '?after=', '?after=bad+cursor', '?after=' + 'x'.repeat(513), '?episodeId=latest', `?episodeId=${f.episode}&episodeId=${f.episode}`, '?url=https://evil.example']) assert.equal((await GET(request('GET', undefined, { query }), params('runs'))).status, 400, query);
    for (const query of ['?after=-1', '?after=01', '?after=9007199254740992', '?limit=101', '?after=1&after=2']) assert.equal((await GET(request('GET', undefined, { query }), params(`runs/${f.runId}/events`))).status, 400, query);
    assert.equal((await GET(request(), params('context'))).status, 400);
    assert.equal((await GET(request('GET', undefined, { query: contextQuery + '&needId=' + f.other }), params('context'))).status, 400);
    assert.equal((await GET(request('GET', undefined, { query: '?url=https://evil.example' }), params(`runs/${f.runId}/sources/${f.source.id}`))).status, 400);
    assert.equal((await POST(request('POST', f.runBody, { query: '?provider=brave' }), params('runs'))).status, 400); assert.equal(calls, 0);
    globalThis.fetch = async (url) => { assert.ok(url.endsWith('?episodeId=' + f.episode + '&limit=25&after=opaque_keyset-cursor')); return Response.json(f.runs); };
    assert.equal((await GET(request('GET', undefined, { query: `?episodeId=${f.episode}&after=opaque_keyset-cursor` }), params('runs'))).status, 200);
  });
  await t.test('ambiguous JSON, unsafe numbers, confirmation/key omissions and body bounds fail before forwarding', async () => {
    let calls = 0; globalThis.fetch = async () => { calls++; throw Error('unexpected'); };
    for (const body of [' '.repeat(131073), '{"confirm":true,"confirm":true}', '{"confirm":true,"amount":NaN}', JSON.stringify({ ...f.runBody, confirm: false }), JSON.stringify({ ...f.runBody, url: 'https://evil.example' })]) assert.equal((await POST(request('POST', body, { raw: true }), params('runs'))).status, 400);
    for (const headers of [{ 'Idempotency-Key': '' }, { 'Idempotency-Key': ':invalid' }, { 'Idempotency-Key': 'x'.repeat(129) }, { 'Content-Type': 'text/plain' }]) assert.equal((await POST(request('POST', f.runBody, { headers }), params('runs'))).status, 400);
    assert.equal(calls, 0);
  });
  await t.test('foreign action receipts, false citations and nonadvancing pages do not become evidence', async () => {
    globalThis.fetch = async () => Response.json({ ...f.actionReceipt, action: { ...f.action, scope: { ...f.action.scope, need_id: f.other } } }); assert.equal((await POST(request('POST', f.actionBody), params('actions'))).status, 502);
    globalThis.fetch = async () => Response.json({ ...f.citationEnvelope, citation: { ...f.citation, runId: f.other } }); assert.equal((await POST(request('POST', f.citationBody, { citation: true }), params('citations'))).status, 502);
    globalThis.fetch = async () => Response.json({ ...f.runs, nextCursor: 'same-cursor' }); assert.equal((await GET(request('GET', undefined, { query: '?after=same-cursor' }), params('runs'))).status, 502);
    globalThis.fetch = async () => Response.json({ ...f.result, batch: { ...f.batch, visibility: 'principal_private' } }); assert.equal((await GET(request(), params(`runs/${f.runId}/result`))).status, 502);
  });
  await t.test('upstream secrets, errors, duplicate response keys and oversized responses stay private', async () => {
    for (const status of [400, 401, 403, 409, 500]) { globalThis.fetch = async () => new Response('PRIVATE-stack-with-token', { status }); const response = await GET(request(), params('runs')); assert.equal(response.status, status < 500 ? status : 502); assert.doesNotMatch(await response.text(), /PRIVATE/); }
    for (const raw of ['{"schema":"potbot-research-workbench/v1","schema":"PRIVATE"}', 'x'.repeat(1500001)]) { globalThis.fetch = async () => new Response(raw); const response = await GET(request(), params('runs')); assert.equal(response.status, 503); assert.doesNotMatch(await response.text(), /PRIVATE/); }
  });
});

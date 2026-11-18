import test from 'node:test';
import assert from 'node:assert/strict';
import { sessionCookie } from '../src/lib/server/security.ts';
import { mcpQualificationRoute } from '../src/lib/server/mcp-qualification.ts';
import { GET, POST } from '../src/app/api/ui/mcp-qualification/[...path]/route.ts';
import { workspaceId as w, suiteId, bindingId, reportId, other, suite, requested, report, evidence, wire, page, decision, hash } from './mcp-qualification-fixtures.mjs';

const auth = { password: 'operator-password-very-long', signingKey: 'a'.repeat(64), publicOrigin: 'http://localhost:3100' }, cookie = sessionCookie(auth).split(';')[0];
const env = { POTBOT_UI_PASSWORD: auth.password, POTBOT_UI_COOKIE_SECRET: auth.signingKey, POTBOT_UI_PUBLIC_ORIGIN: auth.publicOrigin,
  POTBOT_UI_BACKEND_TOKEN: 'PRIVATE-backend-token', POTBOT_UI_BACKEND_URL: 'http://127.0.0.1:8765/', POTBOT_UI_WORKSPACE_ID: w, POTBOT_UI_SETUP_TOKEN: 'PRIVATE-setup-token-'.repeat(4) };
const params = path => ({ params: Promise.resolve({ path: path.split('/') }) });
function request(method = 'GET', body, options = {}) {
  return new Request(`${auth.publicOrigin}/api/ui/mcp-qualification${options.query ?? ''}`, { method,
    headers: { Cookie: cookie, 'X-Potbot-Workspace': w, ...(method === 'POST' ? { Origin: auth.publicOrigin, 'Sec-Fetch-Site': 'same-origin', 'Content-Type': 'application/json', 'Idempotency-Key': 'mcp-qualification:exact-test-key' } : {}), ...options.headers },
    ...(body === undefined ? {} : { body: options.raw ? body : JSON.stringify(body) }), ...(options.signal ? { signal: options.signal } : {}) });
}

test('qualification BFF exposes only scoped retained evidence and exact suite decisions', async t => {
  const prior = { ...process.env }, priorFetch = globalThis.fetch; Object.assign(process.env, env); delete process.env.POTBOT_UI_WORKSPACES_JSON;
  t.after(() => { globalThis.fetch = priorFetch; for (const key of [...Object.keys(env), 'POTBOT_UI_WORKSPACES_JSON']) if (prior[key] === undefined) delete process.env[key]; else process.env[key] = prior[key]; });

  await t.test('session, workspace and origin checks precede network and host-only routes stay absent', async () => {
    let calls = 0; globalThis.fetch = async () => { calls++; throw Error('unexpected network'); };
    assert.equal((await GET(new Request(auth.publicOrigin + '/api/ui/mcp-qualification/suites'), params('suites'))).status, 401);
    assert.equal((await GET(request('GET', undefined, { headers: { Origin: 'https://evil.example' } }), params('suites'))).status, 403);
    assert.equal((await GET(request('GET', undefined, { headers: { 'X-Potbot-Workspace': other } }), params('suites'))).status, 503);
    assert.equal((await mcpQualificationRoute(request('PUT'), ['suites'])).status, 400);
    for (const path of ['admit', 'suites/admit', `suites/${suiteId}/admit`, `suites/${suiteId}/execute`, `suites/${suiteId}/run`, `suites/${suiteId}/call`, `attempts/${requested.id}/recover`, `attempts/${requested.id}/execute`, `reports/${reportId}`, '../suites', `suites/${suiteId}/qualify/extra`]) assert.equal((await POST(request('POST', decision), params(path))).status, 400, path);
    for (const headers of [{ Origin: '' }, { Origin: 'https://evil.example' }, { 'Sec-Fetch-Site': 'cross-site' }]) assert.equal((await POST(request('POST', decision, { headers }), params(`suites/${suiteId}/qualify`))).status, 403);
    assert.equal(calls, 0);
  });

  await t.test('queries reject injected URLs, duplicate filters, private arguments and invalid bounds', async () => {
    let calls = 0; globalThis.fetch = async () => { calls++; throw Error('unexpected network'); };
    for (const query of ['?limit=0', '?limit=51', '?limit=01', '?limit=1&limit=2', '?after=latest', '?after=' + other.toUpperCase(), '?url=https://evil.example', '?q=private', '?suiteId=' + suiteId, '?bindingId=' + bindingId + '&bindingId=' + bindingId]) assert.equal((await GET(request('GET', undefined, { query }), params('suites'))).status, 400, query);
    for (const query of ['?bindingId=' + bindingId, '?suiteId=latest', '?suiteId=' + suiteId + '&suiteId=' + suiteId, '?arguments=%7B%7D']) assert.equal((await GET(request('GET', undefined, { query }), params('attempts'))).status, 400, query);
    for (const path of [`suites/${suiteId}`, `attempts/${requested.id}`, `reports/${reportId}`]) assert.equal((await GET(request('GET', undefined, { query: '?limit=1' }), params(path))).status, 400);
    assert.equal((await POST(request('POST', decision, { query: '?endpoint=https://evil.example' }), params(`suites/${suiteId}/qualify`))).status, 400);
    assert.equal(calls, 0);
  });

  await t.test('passive reads omit setup authority, preserve no-store policy and discard native fields', async () => {
    globalThis.fetch = async (url, options) => {
      assert.equal(url, 'http://127.0.0.1:8765/v1/ui/mcp-qualification/suites?limit=25'); assert.equal(options.method, 'GET');
      assert.equal(options.headers.Authorization, 'Bearer PRIVATE-backend-token'); assert.equal(options.headers['X-Potbot-Setup-Token'], undefined);
      assert.equal(options.redirect, 'error'); assert.equal(options.cache, 'no-store');
      return Response.json({ ...wire.suites, setupToken: 'PRIVATE', items: [{ ...suite, nativePath: 'PRIVATE', argv: ['PRIVATE'], signature: 'PRIVATE' }] });
    };
    const response = await GET(request(), params('suites')); assert.equal(response.status, 200); assert.equal(response.headers.get('Cache-Control'), 'private, no-store'); assert.deepEqual(await response.json(), wire.suites);
    for (const [path, value] of [[`suites/${suiteId}`, wire.suite], ['attempts', wire.attempts], [`attempts/${requested.id}`, wire.requestedAttempt], [`reports/${reportId}`, wire.report]]) {
      globalThis.fetch = async (_url, options) => { assert.equal(options.headers['X-Potbot-Setup-Token'], undefined); return Response.json(value); };
      assert.deepEqual(await (await GET(request(), params(path))).json(), value, path);
    }
  });

  await t.test('scope, detail identity, case proof and report evidence mixing fail closed', async () => {
    for (const value of [{ ...wire.suite, workspaceId: other }, { ...wire.suite, suite: { ...suite, id: other } }, { ...wire.suite, principalId: 'not-a-uuid' }]) {
      globalThis.fetch = async () => Response.json(value); assert.equal((await GET(request(), params(`suites/${suiteId}`))).status, 502);
    }
    globalThis.fetch = async () => Response.json(wire.suites); assert.equal((await GET(request('GET', undefined, { query: '?bindingId=' + other }), params('suites'))).status, 502);
    globalThis.fetch = async () => Response.json(wire.attempts); assert.equal((await GET(request('GET', undefined, { query: '?suiteId=' + other }), params('attempts'))).status, 502);
    for (const patch of [{ workspaceId: other }, { principalId: other }, { attemptId: other }, { runtimeSha256: 'unverified' }, { privateArguments: {} }]) {
      globalThis.fetch = async () => Response.json({ ...wire.report, report: { ...report, evidence: { ...evidence, ...patch } } }); assert.equal((await GET(request(), params(`reports/${reportId}`))).status, 502);
    }
    globalThis.fetch = async () => Response.json({ ...wire.report, report: { ...report, cases: [{ ...report.cases[0], checks: [{ index: 0, passed: true, reasonCode: null, actual: 'PRIVATE' }] }, report.cases[1]] } });
    assert.equal((await GET(request(), params(`reports/${reportId}`))).status, 502);
  });

  await t.test('bounded pagination retains exact filters and rejects cursor regressions', async () => {
    const query = new URLSearchParams({ bindingId, after: suiteId, limit: '1' });
    globalThis.fetch = async (url, options) => {
      const next = new URL(url); assert.equal(next.pathname, '/v1/ui/mcp-qualification/suites');
      for (const [key, value] of query) assert.equal(next.searchParams.get(key), value); assert.equal(options.headers['X-Potbot-Setup-Token'], undefined);
      return Response.json(page([{ ...suite, id: other }]));
    };
    assert.equal((await GET(request('GET', undefined, { query: '?' + query }), params('suites'))).status, 200);
    globalThis.fetch = async () => Response.json(wire.suites); assert.equal((await GET(request('GET', undefined, { query: '?after=' + suiteId }), params('suites'))).status, 502);
    globalThis.fetch = async () => Response.json(page([suite, { ...suite, id: other }])); assert.equal((await GET(request('GET', undefined, { query: '?limit=1' }), params('suites'))).status, 502);
  });

  await t.test('one explicit request forwards server-owned setup authority and checks the original receipt', async () => {
    let calls = 0; globalThis.fetch = async (url, options) => {
      calls++; assert.equal(url, `http://127.0.0.1:8765/v1/ui/mcp-qualification/suites/${suiteId}/qualify`); assert.deepEqual(JSON.parse(options.body), decision);
      assert.equal(options.headers['Idempotency-Key'], 'mcp-qualification:exact-test-key'); assert.equal(options.headers['X-Potbot-Setup-Token'], env.POTBOT_UI_SETUP_TOKEN); assert.equal(options.headers.Origin, auth.publicOrigin);
      return Response.json({ ...wire.requestedAttempt, nativeCommand: 'PRIVATE' }, { status: 202 });
    };
    const response = await POST(request('POST', decision, { headers: { 'X-Potbot-Setup-Token': 'browser-PRIVATE' } }), params(`suites/${suiteId}/qualify`));
    assert.equal(response.status, 202); assert.deepEqual(await response.json(), wire.requestedAttempt); assert.equal(calls, 1);
    for (const patch of [{ requestedSuiteSha256: hash('0') }, { suiteId: other }, { revision: 2 }, { status: 'running' }]) {
      globalThis.fetch = async () => Response.json({ ...wire.requestedAttempt, attempt: { ...requested, ...patch } }); assert.equal((await POST(request('POST', decision), params(`suites/${suiteId}/qualify`))).status, 502);
    }
  });

  await t.test('exact decisions reject extra bytes, duplicate fields and missing independent setup permission', async () => {
    let calls = 0; globalThis.fetch = async () => { calls++; throw Error('unexpected network'); };
    for (const patch of [{ confirm: false }, { revision: 0 }, { digest: 'latest' }, { arguments: {} }, { oracle: {} }, { report }, { endpoint: 'https://evil.example' }, { argv: ['sh'] }, { token: 'PRIVATE' }, { extra: 'x'.repeat(8192) }]) assert.equal((await POST(request('POST', { ...decision, ...patch }), params(`suites/${suiteId}/qualify`))).status, 400);
    for (const key of ['', 'unsafe key', ':invalid', '.invalid', 'x'.repeat(129)]) assert.equal((await POST(request('POST', decision, { headers: { 'Idempotency-Key': key } }), params(`suites/${suiteId}/disable`))).status, 400);
    assert.equal((await POST(request('POST', decision, { headers: { 'Content-Type': 'text/plain' } }), params(`suites/${suiteId}/disable`))).status, 400);
    for (const raw of [`{"revision":2,"revision":3,"digest":"${suite.bindingSha256}","confirm":true}`, `{"revision":3,"digest":"${suite.bindingSha256}","confirm":true,"\\u0063onfirm":true}`, '{"revision":1e999}']) assert.equal((await POST(request('POST', raw, { raw: true }), params(`suites/${suiteId}/qualify`))).status, 400);
    delete process.env.POTBOT_UI_SETUP_TOKEN;
    assert.equal((await POST(request('POST', decision), params(`suites/${suiteId}/disable`))).status, 503); process.env.POTBOT_UI_SETUP_TOKEN = env.POTBOT_UI_SETUP_TOKEN;
    assert.equal(calls, 0);
  });

  await t.test('lifecycle controls require exact mapped state and revision-plus-one original receipts', async () => {
    for (const [key, group, action, recordKey] of [['disabledSuite', 'suites', 'disable', 'suite'], ['enabledSuite', 'suites', 'enable', 'suite'], ['revokedSuite', 'suites', 'revoke', 'suite'], ['cancelledAttempt', 'attempts', 'cancel', 'attempt']]) {
      const record = wire[key][recordKey], body = { ...decision, revision: record.revision - 1 }, path = `${group}/${record.id}/${action}`;
      globalThis.fetch = async () => Response.json(wire[key]); assert.equal((await POST(request('POST', body), params(path))).status, 200, key);
      globalThis.fetch = async () => Response.json({ ...wire[key], [recordKey]: { ...record, revision: record.revision + 1 } }); assert.equal((await POST(request('POST', body), params(path))).status, 502, key);
    }
  });

  await t.test('private errors, invalid UTF-8, oversized or ambiguous JSON and aborts cannot become evidence', async () => {
    for (const status of [400, 401, 403, 404, 409, 500]) {
      globalThis.fetch = async () => new Response('PRIVATE endpoint /native/path token', { status });
      const response = await GET(request(), params('suites')); assert.ok(response.status >= 400); assert.ok(!(await response.text()).includes('PRIVATE'));
    }
    for (const value of [JSON.stringify({ ...wire.suites, padding: 'x'.repeat(1572864) }), '{"schema":1,"schema":2}', '{"field":1e999}', '{', Uint8Array.of(0xff)]) {
      globalThis.fetch = async () => new Response(value, { headers: { 'Content-Type': 'application/json' } }); assert.equal((await GET(request(), params('suites'))).status, 503);
    }
    const controller = new AbortController(); controller.abort();
    globalThis.fetch = async (_url, options) => { assert.equal(options.signal.aborted, true); throw new DOMException('aborted', 'AbortError'); };
    assert.equal((await GET(request('GET', undefined, { signal: controller.signal }), params('suites'))).status, 503);
  });
});

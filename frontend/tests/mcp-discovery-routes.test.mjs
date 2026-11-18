import test from 'node:test';
import assert from 'node:assert/strict';
import { sessionCookie } from '../src/lib/server/security.ts';
import { mcpDiscoveryRoute } from '../src/lib/server/mcp-discovery.ts';
import { GET, POST } from '../src/app/api/ui/mcp-discovery/[...path]/route.ts';
import { workspaceId as w, principalId as p, bindingId, other, scope, binding, tool, resource, requested, wire, page, decision, hash } from './mcp-discovery-fixtures.mjs';

const auth = { password: 'operator-password-very-long', signingKey: 'a'.repeat(64), publicOrigin: 'http://localhost:3100' }, cookie = sessionCookie(auth).split(';')[0];
const env = { POTBOT_UI_PASSWORD: auth.password, POTBOT_UI_COOKIE_SECRET: auth.signingKey, POTBOT_UI_PUBLIC_ORIGIN: auth.publicOrigin,
  POTBOT_UI_BACKEND_TOKEN: 'PRIVATE-backend-token', POTBOT_UI_BACKEND_URL: 'http://127.0.0.1:8765/', POTBOT_UI_WORKSPACE_ID: w, POTBOT_UI_SETUP_TOKEN: 'PRIVATE-setup-token-'.repeat(4) };
const params = path => ({ params: Promise.resolve({ path: path.split('/') }) });
function request(method = 'GET', body, options = {}) {
  return new Request(`${auth.publicOrigin}/api/ui/mcp-discovery${options.query ?? ''}`, { method,
    headers: { Cookie: cookie, 'X-Potbot-Workspace': w, ...(method === 'POST' ? { Origin: auth.publicOrigin, 'Sec-Fetch-Site': 'same-origin', 'Content-Type': 'application/json', 'Idempotency-Key': 'mcp-discovery:exact-test-key' } : {}), ...options.headers },
    ...(body === undefined ? {} : { body: options.raw ? body : JSON.stringify(body) }), ...(options.signal ? { signal: options.signal } : {}) });
}

test('MCP BFF constrains retained observations and exact decisions to scoped fixed routes', async t => {
  const prior = { ...process.env }, priorFetch = globalThis.fetch; Object.assign(process.env, env); delete process.env.POTBOT_UI_WORKSPACES_JSON;
  t.after(() => { globalThis.fetch = priorFetch; for (const key of [...Object.keys(env), 'POTBOT_UI_WORKSPACES_JSON']) if (prior[key] === undefined) delete process.env[key]; else process.env[key] = prior[key]; });

  await t.test('session, origin, workspace and host-only or invocation routes fail before network access', async () => {
    let calls = 0; globalThis.fetch = async () => { calls++; throw Error('unexpected network'); };
    assert.equal((await GET(new Request(auth.publicOrigin + '/api/ui/mcp-discovery/bindings'), params('bindings'))).status, 401);
    assert.equal((await GET(request('GET', undefined, { headers: { Origin: 'https://evil.example' } }), params('bindings'))).status, 403);
    assert.equal((await GET(request('GET', undefined, { headers: { 'X-Potbot-Workspace': other } }), params('bindings'))).status, 503);
    assert.equal((await mcpDiscoveryRoute(request('PUT'), ['bindings'])).status, 400);
    for (const path of ['admit', 'bindings/admit', `bindings/${bindingId}/configure`, `bindings/${bindingId}/run`, `bindings/${bindingId}/call`, `attempts/${requested.id}/run`, `bindings/${bindingId}/entries/${tool.id}/call`, `bindings/${bindingId}/entries/${resource.id}/read`, '../bindings', `bindings/${bindingId}/reconcile/extra`]) assert.equal((await POST(request('POST', decision), params(path))).status, 400, path);
    for (const headers of [{ Origin: '' }, { Origin: 'https://evil.example' }, { 'Sec-Fetch-Site': 'cross-site' }]) assert.equal((await POST(request('POST', decision, { headers }), params(`bindings/${bindingId}/reconcile`))).status, 403);
    assert.equal(calls, 0);
  });

  await t.test('queries reject caller URLs, ambiguous filters and invalid bounds', async () => {
    let calls = 0; globalThis.fetch = async () => { calls++; throw Error('unexpected network'); };
    for (const query of ['?limit=0', '?limit=51', '?limit=01', '?limit=1&limit=2', '?after=latest', '?after=' + other.toUpperCase(), '?url=https://evil.example', '?q=unexpected', '?bindingId=' + bindingId]) assert.equal((await GET(request('GET', undefined, { query }), params('bindings'))).status, 400, query);
    for (const query of ['?kind=unknown', '?kind=tool&kind=prompt', '?q=' + 'x'.repeat(257), '?q=hello%00world', '?after=../escape', '?snapshotId=' + other]) assert.equal((await GET(request('GET', undefined, { query }), params(`bindings/${bindingId}/entries`))).status, 400, query.slice(0, 100));
    for (const path of [`bindings/${bindingId}`, `attempts/${requested.id}`, `bindings/${bindingId}/entries/${tool.id}/reference`]) assert.equal((await GET(request('GET', undefined, { query: '?limit=1' }), params(path))).status, 400);
    assert.equal((await POST(request('POST', decision, { query: '?endpoint=https://evil.example' }), params(`bindings/${bindingId}/reconcile`))).status, 400);
    assert.equal(calls, 0);
  });

  await t.test('passive reads omit setup authority and project only public identity', async () => {
    globalThis.fetch = async (url, options) => {
      assert.equal(url, 'http://127.0.0.1:8765/v1/ui/mcp-discovery/bindings?limit=25'); assert.equal(options.method, 'GET');
      assert.equal(options.headers.Authorization, 'Bearer PRIVATE-backend-token'); assert.equal(options.headers['X-Potbot-Setup-Token'], undefined);
      assert.equal(options.redirect, 'error'); assert.equal(options.cache, 'no-store');
      return Response.json({ ...wire.bindings, setupToken: 'SECRET', items: [{ ...binding, endpoint: 'SECRET', argv: ['SECRET'], signature: 'SECRET' }] });
    };
    const response = await GET(request(), params('bindings')); assert.equal(response.status, 200); assert.equal(response.headers.get('Cache-Control'), 'private, no-store'); assert.deepEqual(await response.json(), wire.bindings);
    for (const [path, value] of [[`bindings/${bindingId}`, wire.binding], [`bindings/${bindingId}/entries`, wire.entries], ['attempts', wire.attempts], [`attempts/${requested.id}`, wire.requestedAttempt], [`bindings/${bindingId}/entries/${tool.id}/reference`, wire.reference]]) {
      globalThis.fetch = async (_url, options) => { assert.equal(options.headers['X-Potbot-Setup-Token'], undefined); return Response.json(value); };
      assert.deepEqual(await (await GET(request(), params(path))).json(), value, path);
    }
  });

  await t.test('scope, detail identity, selected kind and snapshot mixing fail closed', async () => {
    for (const value of [{ ...wire.binding, workspaceId: other }, { ...wire.binding, binding: { ...binding, id: other } }, { ...wire.binding, principalId: 'not-a-uuid' }]) {
      globalThis.fetch = async () => Response.json(value); assert.equal((await GET(request(), params(`bindings/${bindingId}`))).status, 502);
    }
    globalThis.fetch = async () => Response.json(page([{ ...tool, executable: true }])); assert.equal((await GET(request(), params(`bindings/${bindingId}/entries`))).status, 502);
    globalThis.fetch = async () => Response.json(page([tool, { ...resource, snapshotId: other }])); assert.equal((await GET(request(), params(`bindings/${bindingId}/entries`))).status, 502);
    globalThis.fetch = async () => Response.json(page([resource])); assert.equal((await GET(request('GET', undefined, { query: '?kind=tool' }), params(`bindings/${bindingId}/entries`))).status, 502);
    globalThis.fetch = async () => Response.json(wire.attempts); assert.equal((await GET(request('GET', undefined, { query: '?bindingId=' + other }), params('attempts'))).status, 502);
    globalThis.fetch = async () => Response.json({ ...wire.reference, reference: { ...wire.reference.reference, principalId: other } }); assert.equal((await GET(request(), params(`bindings/${bindingId}/entries/${tool.id}/reference`))).status, 502);
  });

  await t.test('bounded pagination preserves exact filters and rejects cursor regressions', async () => {
    const query = new URLSearchParams({ kind: 'resource', q: 'report', after: tool.id, limit: '1' });
    globalThis.fetch = async (url, options) => {
      const next = new URL(url); assert.equal(next.pathname, `/v1/ui/mcp-discovery/bindings/${bindingId}/entries`);
      for (const [key, value] of query) assert.equal(next.searchParams.get(key), value); assert.equal(options.headers['X-Potbot-Setup-Token'], undefined);
      return Response.json(page([resource]));
    };
    assert.equal((await GET(request('GET', undefined, { query: '?' + query }), params(`bindings/${bindingId}/entries`))).status, 200);
    globalThis.fetch = async () => Response.json(page([tool])); assert.equal((await GET(request('GET', undefined, { query: '?after=' + tool.id }), params(`bindings/${bindingId}/entries`))).status, 502);
    globalThis.fetch = async () => Response.json(wire.entries); assert.equal((await GET(request('GET', undefined, { query: '?limit=1' }), params(`bindings/${bindingId}/entries`))).status, 502);
  });

  await t.test('exact request forwards server-owned setup authority and validates original requested receipt', async () => {
    globalThis.fetch = async (url, options) => {
      assert.equal(url, `http://127.0.0.1:8765/v1/ui/mcp-discovery/bindings/${bindingId}/reconcile`); assert.deepEqual(JSON.parse(options.body), decision);
      assert.equal(options.headers['Idempotency-Key'], 'mcp-discovery:exact-test-key'); assert.equal(options.headers['X-Potbot-Setup-Token'], env.POTBOT_UI_SETUP_TOKEN); assert.equal(options.headers.Origin, auth.publicOrigin);
      return Response.json({ ...wire.requestedAttempt, nativeCommand: 'SECRET' }, { status: 202 });
    };
    const response = await POST(request('POST', decision, { headers: { 'X-Potbot-Setup-Token': 'browser-supplied-SECRET' } }), params(`bindings/${bindingId}/reconcile`));
    assert.equal(response.status, 202); assert.deepEqual(await response.json(), wire.requestedAttempt);
    for (const patch of [{ requestedBindingSha256: hash('0') }, { bindingId: other }, { revision: 2 }, { status: 'running' }]) {
      globalThis.fetch = async () => Response.json({ ...wire.requestedAttempt, attempt: { ...requested, ...patch } }); assert.equal((await POST(request('POST', decision), params(`bindings/${bindingId}/reconcile`))).status, 502);
    }
  });

  await t.test('decisions reject ambiguity, excess bytes, missing setup permission and injected native fields', async () => {
    let calls = 0; globalThis.fetch = async () => { calls++; throw Error('unexpected network'); };
    for (const patch of [{ confirm: false }, { revision: 0 }, { digest: 'latest' }, { endpoint: 'https://evil.example' }, { argv: ['sh'] }, { token: 'private' }, { extra: 'x'.repeat(8192) }]) assert.equal((await POST(request('POST', { ...decision, ...patch }), params(`bindings/${bindingId}/disable`))).status, 400);
    for (const key of ['', 'unsafe key', 'x'.repeat(129)]) assert.equal((await POST(request('POST', decision, { headers: { 'Idempotency-Key': key } }), params(`bindings/${bindingId}/disable`))).status, 400);
    assert.equal((await POST(request('POST', decision, { headers: { 'Content-Type': 'text/plain' } }), params(`bindings/${bindingId}/disable`))).status, 400);
    const duplicate = `{"revision":2,"revision":3,"digest":"${binding.bindingSha256}","confirm":true}`;
    assert.equal((await POST(request('POST', duplicate, { raw: true }), params(`bindings/${bindingId}/disable`))).status, 400);
    delete process.env.POTBOT_UI_SETUP_TOKEN;
    assert.equal((await POST(request('POST', decision), params(`bindings/${bindingId}/disable`))).status, 503); process.env.POTBOT_UI_SETUP_TOKEN = env.POTBOT_UI_SETUP_TOKEN;
    assert.equal(calls, 0);
  });

  await t.test('lifecycle controls require matching terminal state and revision-plus-one receipt', async () => {
    for (const [key, group, action, recordKey] of [['disabledBinding', 'bindings', 'disable', 'binding'], ['enabledBinding', 'bindings', 'enable', 'binding'], ['revokedBinding', 'bindings', 'revoke', 'binding'], ['cancelledAttempt', 'attempts', 'cancel', 'attempt']]) {
      const record = wire[key][recordKey], body = { ...decision, revision: record.revision - 1 }, path = `${group}/${record.id}/${action}`;
      globalThis.fetch = async () => Response.json(wire[key]); assert.equal((await POST(request('POST', body), params(path))).status, 200, key);
      globalThis.fetch = async () => Response.json({ ...wire[key], [recordKey]: { ...record, revision: record.revision + 1 } }); assert.equal((await POST(request('POST', body), params(path))).status, 502, key);
    }
  });

  await t.test('private errors, malformed or oversized responses and aborted requests do not become public evidence', async () => {
    for (const status of [400, 401, 403, 404, 409, 500]) {
      globalThis.fetch = async () => new Response('SECRET endpoint /native/path token', { status });
      const response = await GET(request(), params('bindings')); assert.ok(response.status >= 400); assert.ok(!(await response.text()).includes('SECRET'));
    }
    for (const text of [JSON.stringify({ ...wire.bindings, padding: 'x'.repeat(1572864) }), '{"schema":1,"schema":2}', '{"field":1e999}', '{']) {
      globalThis.fetch = async () => new Response(text, { headers: { 'Content-Type': 'application/json' } }); assert.equal((await GET(request(), params('bindings'))).status, 503);
    }
    const controller = new AbortController(); controller.abort();
    globalThis.fetch = async (_url, options) => { assert.equal(options.signal.aborted, true); throw new DOMException('aborted', 'AbortError'); };
    assert.equal((await GET(request('GET', undefined, { signal: controller.signal }), params('bindings'))).status, 503);
  });
});

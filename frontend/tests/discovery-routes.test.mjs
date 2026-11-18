import test from 'node:test';
import assert from 'node:assert/strict';
import { sessionCookie } from '../src/lib/server/security.ts';
import { discoveryRoute } from '../src/lib/server/discovery.ts';
import { GET, POST } from '../src/app/api/ui/discovery/[...path]/route.ts';
import { wire, scope, workspaceId as w, principalId as p, source, publisher, candidate, pending, clone, other } from './discovery-fixtures.mjs';

const auth = { password: 'operator-password-very-long', signingKey: 'a'.repeat(64), publicOrigin: 'http://localhost:3100' }, cookie = sessionCookie(auth).split(';')[0];
const env = { POTBOT_UI_PASSWORD: auth.password, POTBOT_UI_COOKIE_SECRET: auth.signingKey, POTBOT_UI_PUBLIC_ORIGIN: auth.publicOrigin,
  POTBOT_UI_BACKEND_TOKEN: 'PRIVATE-backend-token', POTBOT_UI_BACKEND_URL: 'http://127.0.0.1:8765/', POTBOT_UI_WORKSPACE_ID: w, POTBOT_UI_SETUP_TOKEN: 'PRIVATE-setup-token-'.repeat(4) };
const params = path => ({ params: Promise.resolve({ path: path.split('/') }) });
const reviewBody = { snapshotId: pending.snapshotId, candidateSha256: pending.requestedCandidateSha256, agentId: pending.agentId, confirm: true };
function request(method = 'GET', body, options = {}) {
  return new Request(`${auth.publicOrigin}/api/ui/discovery${options.query ?? ''}`, { method,
    headers: { Cookie: cookie, 'X-Potbot-Workspace': w, ...(method === 'POST' ? { Origin: auth.publicOrigin, 'Sec-Fetch-Site': 'same-origin', 'Content-Type': 'application/json', 'Idempotency-Key': 'discovery:exact-test-key' } : {}), ...options.headers },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }), ...(options.signal ? { signal: options.signal } : {}) });
}

test('discovery BFF confines all public reads and reviewed mutations to fixed scoped routes', async t => {
  const prior = { ...process.env }, priorFetch = globalThis.fetch; Object.assign(process.env, env); delete process.env.POTBOT_UI_WORKSPACES_JSON;
  t.after(() => { globalThis.fetch = priorFetch; for (const key of [...Object.keys(env), 'POTBOT_UI_WORKSPACES_JSON']) if (prior[key] === undefined) delete process.env[key]; else process.env[key] = prior[key]; });
  await t.test('session, origin, method, workspace and fixed paths reject before network access', async () => {
    let calls = 0; globalThis.fetch = async () => { calls++; throw Error('unexpected'); };
    assert.equal((await GET(new Request(auth.publicOrigin + '/api/ui/discovery/sources'), params('sources'))).status, 401);
    assert.equal((await GET(request('GET', undefined, { headers: { Origin: 'https://evil.example' } }), params('sources'))).status, 403);
    assert.equal((await GET(request('GET', undefined, { headers: { 'X-Potbot-Workspace': other } }), params('sources'))).status, 503);
    assert.equal((await discoveryRoute(request('PUT'), ['sources'])).status, 400);
    for (const path of ['sources/refresh', `sources/${source.id}/refresh`, `candidates/${candidate.id}/run`, `checks/${pending.id}/measure`, '../sources', `checks/${pending.id}/handoff/extra`]) assert.equal((await POST(request('POST', {}), params(path))).status, 400, path);
    for (const headers of [{ Origin: 'https://evil.example' }, { Origin: '' }, { 'Sec-Fetch-Site': 'cross-site' }]) assert.equal((await POST(request('POST', reviewBody, { headers }), params(`candidates/${pending.candidateId}/checks`))).status, 403);
    assert.equal(calls, 0);
  });
  await t.test('page queries reject ambiguity, caller URLs and invalid constraints while preserving bounded cursors', async () => {
    let calls = 0; globalThis.fetch = async () => { calls++; throw Error('unexpected'); };
    for (const query of ['?after=bad', '?limit=0', '?limit=51', '?limit=1&limit=2', '?url=https://evil.example', '?q=unexpected']) assert.equal((await GET(request('GET', undefined, { query }), params('sources'))).status, 400, query);
    for (const query of ['?after=../escape', '?after=' + 'a'.repeat(513), '?platform=arbitrary', '?permittedEffects=run', '?requireLocal=1', '?sourceId=' + other + '&sourceId=' + w, '?inputTypes=unsafe', '?q=' + 'a'.repeat(501)]) assert.equal((await GET(request('GET', undefined, { query }), params('candidates'))).status, 400, query.slice(0, 80));
    assert.equal((await GET(request('GET', undefined, { query: '?limit=1' }), params(`checks/${pending.id}`))).status, 400);
    assert.equal((await POST(request('POST', reviewBody, { query: '?url=https://evil.example' }), params(`candidates/${pending.candidateId}/checks`))).status, 400);
    assert.equal(calls, 0);
  });
  await t.test('passive reads omit setup capability, strip secrets and enforce scope and detail identity', async () => {
    globalThis.fetch = async (url, options) => {
      assert.equal(url, 'http://127.0.0.1:8765/v1/ui/discovery/sources?limit=25'); assert.equal(options.method, 'GET');
      assert.equal(options.headers.Authorization, 'Bearer PRIVATE-backend-token'); assert.equal(options.headers['X-Potbot-Setup-Token'], undefined); assert.equal(options.redirect, 'error'); assert.equal(options.cache, 'no-store');
      return Response.json({ ...wire.sources, private_signature: 'SECRET', items: wire.sources.items.map(item => ({ ...item, storagePath: '/secret', decision_mac: 'SECRET' })) });
    };
    const response = await GET(request(), params('sources')); assert.equal(response.status, 200); assert.equal(response.headers.get('Cache-Control'), 'private, no-store'); assert.deepEqual(await response.json(), wire.sources);
    globalThis.fetch = async () => Response.json(wire.measuredCheck);
    assert.deepEqual(await (await GET(request(), params(`checks/${pending.id}`))).json(), wire.measuredCheck);
    for (const value of [{ ...wire.measuredCheck, workspaceId: other }, { ...wire.measuredCheck, check: { ...wire.measuredCheck.check, id: other } }]) {
      globalThis.fetch = async () => Response.json(value); assert.equal((await GET(request(), params(`checks/${pending.id}`))).status, 502);
    }
    globalThis.fetch = async () => Response.json({ ...wire.candidate, candidate: { ...candidate, executable: true } });
    assert.equal((await GET(request(), params(`candidates/${candidate.id}`))).status, 502);
  });
  await t.test('ranked pagination preserves empty permissions and every query-bound constraint', async () => {
    const cursor = Buffer.from(JSON.stringify({ fingerprint: 'a'.repeat(64), offset: 25 })).toString('base64url');
    const query = new URLSearchParams({ q: 'inspect repository', sourceId: source.id, kind: 'skill', platform: 'linux', permittedEffects: '', permittedScopes: '', classes: 'file_inspection', inputTypes: 'syn://type/repository', outputTypes: 'synny://type/text', requireLocal: 'true', after: cursor, limit: '25' });
    globalThis.fetch = async (url, options) => {
      const next = new URL(url); assert.equal(next.pathname, '/v1/ui/discovery/candidates');
      for (const [key, value] of query) assert.equal(next.searchParams.get(key), value, key); assert.equal(options.headers['X-Potbot-Setup-Token'], undefined);
      return Response.json({ ...wire.candidates, nextCursor: cursor });
    };
    assert.equal((await GET(request('GET', undefined, { query: '?' + query }), params('candidates'))).status, 200);
    globalThis.fetch = async () => Response.json({ ...wire.candidates, items: [{ ...candidate, sourceId: other }] });
    assert.equal((await GET(request('GET', undefined, { query: '?sourceId=' + source.id }), params('candidates'))).status, 502);
    globalThis.fetch = async () => Response.json(wire.publishers);
    assert.equal((await GET(request('GET', undefined, { query: '?sourceId=' + other }), params('publishers'))).status, 502);
  });
  await t.test('exact review forwards only the retained selection and server-owned setup capability', async () => {
    globalThis.fetch = async (url, options) => {
      assert.equal(url, `http://127.0.0.1:8765/v1/ui/discovery/candidates/${pending.candidateId}/checks`); assert.deepEqual(JSON.parse(options.body), reviewBody);
      assert.equal(options.headers['Idempotency-Key'], 'discovery:exact-test-key'); assert.equal(options.headers['X-Potbot-Setup-Token'], env.POTBOT_UI_SETUP_TOKEN); assert.equal(options.headers.Origin, auth.publicOrigin);
      return Response.json({ ...wire.pendingCheck, privateToken: 'SECRET' }, { status: 202 });
    };
    const response = await POST(request('POST', reviewBody), params(`candidates/${pending.candidateId}/checks`)); assert.equal(response.status, 202); assert.deepEqual(await response.json(), wire.pendingCheck);
    for (const patch of [{ requestedCandidateSha256: 'f'.repeat(64) }, { agentId: other }, { snapshotId: other }, { revision: 2 }, { status: 'measuring' }]) {
      globalThis.fetch = async () => Response.json({ ...wire.pendingCheck, check: { ...pending, ...patch } }); assert.equal((await POST(request('POST', reviewBody), params(`candidates/${pending.candidateId}/checks`))).status, 502);
    }
  });
  await t.test('bounded mutation keys and bodies reject arbitrary execution and missing capability', async () => {
    let calls = 0; globalThis.fetch = async () => { calls++; throw Error('unexpected'); };
    for (const patch of [{ confirm: false }, { argv: ['sh'] }, { url: 'https://evil.example' }, { snapshotId: 'latest' }, { agentId: null }, { candidateSha256: 'bad' }]) assert.equal((await POST(request('POST', { ...reviewBody, ...patch }), params(`candidates/${pending.candidateId}/checks`))).status, 400);
    for (const key of ['', 'unsafe key', 'x'.repeat(129)]) assert.equal((await POST(request('POST', reviewBody, { headers: { 'Idempotency-Key': key } }), params(`candidates/${pending.candidateId}/checks`))).status, 400);
    assert.equal((await POST(request('POST', reviewBody, { headers: { 'Content-Type': 'text/plain' } }), params(`candidates/${pending.candidateId}/checks`))).status, 400);
    assert.equal((await POST(request('POST', { ...reviewBody, oversized: 'x'.repeat(8192) }), params(`candidates/${pending.candidateId}/checks`))).status, 400);
    delete process.env.POTBOT_UI_SETUP_TOKEN;
    assert.equal((await POST(request('POST', reviewBody), params(`candidates/${pending.candidateId}/checks`))).status, 503); process.env.POTBOT_UI_SETUP_TOKEN = env.POTBOT_UI_SETUP_TOKEN; assert.equal(calls, 0);
  });
  await t.test('source, publisher and check controls require original revision-plus-one receipts', async () => {
    for (const [key, group, action, recordKey] of [['disabledSource', 'sources', 'disable', 'source'], ['enabledSource', 'sources', 'enable', 'source'], ['approvedPublisher', 'publishers', 'approve', 'publisher'], ['blockedPublisher', 'publishers', 'block', 'publisher'], ['handedOffCheck', 'checks', 'handoff', 'check'], ['cancelledCheck', 'checks', 'cancel', 'check']]) {
      const record = wire[key][recordKey], body = { revision: record.revision - 1, digest: 'a'.repeat(64), confirm: true }, path = `${group}/${record.id}/${action}`;
      globalThis.fetch = async () => Response.json(wire[key]); assert.equal((await POST(request('POST', body), params(path))).status, 200, key);
      globalThis.fetch = async () => Response.json({ ...wire[key], [recordKey]: { ...record, revision: record.revision + 1 } }); assert.equal((await POST(request('POST', body), params(path))).status, 502, key);
    }
  });
  await t.test('private errors, oversized upstreams and aborted transports never become public evidence', async () => {
    for (const status of [400, 401, 403, 404, 409, 500]) {
      globalThis.fetch = async () => new Response('SECRET host path and token', { status });
      const response = await GET(request(), params('sources')); assert.ok(response.status >= 400); assert.ok(!(await response.text()).includes('SECRET'));
    }
    globalThis.fetch = async () => new Response(JSON.stringify({ ...wire.sources, padding: 'x'.repeat(1572864) }), { headers: { 'Content-Type': 'application/json' } });
    assert.equal((await GET(request(), params('sources'))).status, 503);
    const controller = new AbortController(); controller.abort();
    globalThis.fetch = async (_url, options) => { assert.equal(options.signal.aborted, true); throw new DOMException('aborted', 'AbortError'); };
    assert.equal((await GET(request('GET', undefined, { signal: controller.signal }), params('sources'))).status, 503);
  });
});

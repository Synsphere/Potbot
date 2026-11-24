import test from 'node:test';
import assert from 'node:assert/strict';
import { requireOperator, validOrigin } from '../src/lib/server/security.ts';

test('local UI access has no cookie dependency and rejects remote hosts and ports', () => {
  const keys = ['POTBOT_UI_COOKIE_SECRET', 'POTBOT_UI_PUBLIC_ORIGIN', 'POTBOT_UI_PASSWORD'];
  const saved = Object.fromEntries(keys.map(key => [key, process.env[key]]));
  try {
    process.env.POTBOT_UI_COOKIE_SECRET = 'k'.repeat(64);
    process.env.POTBOT_UI_PUBLIC_ORIGIN = 'http://127.0.0.1:3100';
    delete process.env.POTBOT_UI_PASSWORD;
    const local = requireOperator(new Request('http://localhost:3100/api/ui/operations'));
    assert.equal(local instanceof Response, false);
    for (const url of ['http://remote.invalid:3100/api/ui/operations',
      'http://127.0.0.1:3101/api/ui/operations', 'https://remote.invalid/api/ui/operations']) {
      const denied = requireOperator(new Request(url));
      assert.equal(denied.status, 403, url);
    }
    assert.equal(validOrigin(new Request('http://localhost:3100/api/ui/operations', {
      method: 'POST', headers: { Origin: 'https://foreign.invalid' },
    }), local), false);
    delete process.env.POTBOT_UI_COOKIE_SECRET;
    assert.equal(requireOperator(new Request('http://localhost:3100/api/ui/operations')).status, 503);
  } finally {
    for (const key of keys) if (saved[key] === undefined) delete process.env[key];
    else process.env[key] = saved[key];
  }
});

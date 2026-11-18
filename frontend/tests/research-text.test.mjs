import test from 'node:test';
import assert from 'node:assert/strict';
import { researchUTF8, researchQuote, researchTextSha256, researchQuoteMatches } from '../src/lib/research-text.ts';
import { researchSelection } from '../src/lib/research-client.ts';

const workspace = '11111111-1111-4111-8111-111111111111', episode = '22222222-2222-4222-8222-222222222222', state = '33333333-3333-4333-8333-333333333333', need = '44444444-4444-4444-8444-444444444444', run = '55555555-5555-4555-8555-555555555555';

test('citation ranges select exact UTF-8 bytes across astral and multibyte characters', async () => {
  const text = 'A🌱 café\nSecond line';
  assert.equal(researchUTF8(text).length, 23);
  assert.equal(researchQuote(text, 1, 5).text, '🌱');
  assert.equal(researchQuote(text, 6, 11).text, 'café');
  assert.equal(researchQuote(text, 12, 23).text, 'Second line');
  const digest = await researchTextSha256('🌱');
  assert.equal(await researchQuoteMatches(text, 1, 5, '🌱', digest), true);
  assert.equal(await researchQuoteMatches(text, 1, 5, 'different quote', digest), false);
  assert.equal(await researchQuoteMatches(text, 1, 5, '🌱', '0'.repeat(64)), false);
});

test('invalid UTF-8 boundaries, empty spans and unsafe offsets cannot be citations', async () => {
  const text = 'A🌱 café\nSecond line';
  for (const [start, end] of [[2, 5], [1, 4], [6, 10], [-1, 1], [3, 3], [5, 1], [0, 24], [0.5, 1], [0, Infinity], [0, Number.MAX_SAFE_INTEGER + 1]]) assert.equal(researchQuote(text, start, end), null, `${start}:${end}`);
  assert.equal(researchUTF8('\ud800'), null); assert.equal(researchQuote('\ud800', 0, 1), null);
  await assert.rejects(researchTextSha256('\ud800'), /UTF-8/);
  assert.equal(await researchQuoteMatches(text, 2, 5, '🌱', await researchTextSha256('🌱')), false);
});

test('literal citation hashing preserves LF, case and Unicode normalization', async () => {
  assert.notEqual(await researchTextSha256('a\nb'), await researchTextSha256('a\r\nb'));
  assert.notEqual(await researchTextSha256('claim'), await researchTextSha256('Claim'));
  assert.notEqual(await researchTextSha256('é'), await researchTextSha256('e\u0301'));
  assert.equal(researchQuote('<script>untrusted source</script>', 0, 8).text, '<script>');
});

test('research deep links select exact workspace records without accepting ambiguous identities', () => {
  const query = `?workspace=${workspace}&episode=${episode}&state=${state}&need=${need}&run=${run}`;
  assert.deepEqual(researchSelection(query, workspace), { episode, state, need, run, invalid: false });
  assert.deepEqual(researchSelection(`?workspace=${workspace}&run=${run}`, workspace), { episode: null, state: null, need: null, run, invalid: false });
  for (const extra of [`&run=${run}`, '&need=latest', `&workspace=${workspace}`, '&state=https://example.com']) assert.equal(researchSelection(query + extra, workspace).invalid, true);
  assert.equal(researchSelection(query, episode).invalid, true);
});

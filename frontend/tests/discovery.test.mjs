import test from 'node:test';
import assert from 'node:assert/strict';
import { projectRegistryObservation, projectDiscoverySource, projectDiscoveryPublisher, projectDiscoveryCandidate, projectDiscoveryCheckEnvelope,
  projectDiscoverySources, projectDiscoveryPublishers, projectDiscoveryCandidates, projectDiscoveryChecks, projectDiscoveryMutation,
  discoveryControlBody, discoverySourceUrl, discoverySearchParams, validateDiscoveryMutation } from '../src/lib/discovery.ts';
import { wire, workspaceId as w, principalId as p, scope, source, publisher, candidate, pending, measured, clone, other } from './discovery-fixtures.mjs';

test('discovery public views bind workspace, principal and record joins while dropping private authority', () => {
  assert.equal(projectDiscoverySources(wire.sources, other), null);
  assert.equal(projectDiscoveryChecks(wire.checks, w, other), null);
  const value = clone(wire.measuredCheck); value.setupToken = 'SECRET'; value.check.signature = 'SECRET'; value.check.candidate.privateInput = 'SECRET'; value.check.measurement.secret_mac = 'SECRET';
  assert.deepEqual(projectDiscoveryCheckEnvelope(value, w, measured.id, p), wire.measuredCheck);
  for (const change of [{ id: other }, { candidateId: other }, { sourceId: other }, { snapshotId: other }, { candidateSha256: 'a'.repeat(64) }, { agentId: other }, { revision: 1.5 }, { actions: ['run'] }]) {
    assert.equal(projectDiscoveryCheckEnvelope({ ...wire.measuredCheck, check: { ...measured, ...change } }, w, measured.id, p), null, JSON.stringify(change));
  }
  for (const change of [{ target: { kind: 'skill_package', id: other } }, { target: { kind: 'execute', id: pending.packageId } }, { target: null }, { status: 'measured' }]) {
    assert.equal(projectDiscoveryCheckEnvelope({ ...wire.handedOffCheck, check: { ...wire.handedOffCheck.check, ...change } }, w), null);
  }
});

test('source and publisher admission preserve revoked and invalid-signature containment states', () => {
  assert.deepEqual(projectDiscoverySource(source), source); assert.deepEqual(projectDiscoveryPublisher(publisher), publisher);
  const invalidated = { ...source, approvalValid: false, snapshotSha256: null, fetchedAt: null, expiresAt: null, actions: ['revoke'] };
  assert.deepEqual(projectDiscoverySource(invalidated), invalidated);
  const revoked = { ...source, state: 'revoked', actions: [] }; assert.deepEqual(projectDiscoverySource(revoked), revoked);
  for (const change of [{ actions: ['refresh'] }, { adapter: 'arbitrary' }, { snapshotSha256: null }, { fetchedAt: source.expiresAt }]) assert.equal(projectDiscoverySource({ ...source, ...change }), null);
  assert.equal(projectDiscoveryPublisher(publisher, other), null);
  assert.deepEqual(discoveryControlBody(invalidated, 'revoke'), { revision: source.revision, digest: source.bindingSha256, confirm: true });
  assert.equal(discoveryControlBody(invalidated, 'enable'), null);
  assert.equal(projectDiscoverySource({ ...invalidated, actions: ['enable'] }), null);
  assert.equal(projectDiscoverySource({ ...revoked, actions: ['enable'] }), null);
  assert.equal(projectDiscoveryPublisher({ ...publisher, approvalValid: false, actions: ['approve'] }), null);
});

test('unresolved remote observations can request server-authorized exact resolution without executable claims', () => {
  const unresolved = clone(candidate); Object.assign(unresolved.observation, { adapter: 'skills_sh', version: null, status: 'unresolved', distribution: { kind: 'inspect_only' }, sourceUrl: 'https://skills.sh/owner/repo/skill' });
  unresolved.reasonCodes = ['exact_resolution_required']; unresolved.canReview = true;
  assert.deepEqual(projectDiscoveryCandidate(unresolved), unresolved);
  for (const change of [{ executable: true }, { stale: true }, { sourceApprovalValid: false }, { publisherApprovalValid: false }, { sourceState: 'disabled' }, { publisherState: 'unreviewed' }]) assert.equal(projectDiscoveryCandidate({ ...unresolved, ...change }), null);
  assert.ok(projectDiscoveryCandidate({ ...unresolved, canReview: false, stale: true }));
});

test('registry metadata is bounded escaped data with canonical capability and distribution fields', () => {
  const observation = clone(candidate.observation); observation.manifest = { code: '<script>alert(1)</script>', number: 1.25e-7, license: 'MIT', nested: [null, true] };
  assert.deepEqual(projectRegistryObservation(observation), observation);
  const unicode = { ...observation, name: '🧭'.repeat(128) }; assert.ok(projectRegistryObservation(unicode));
  assert.equal(projectRegistryObservation({ ...unicode, name: unicode.name + '🧭' }), null);
  for (const mutate of [v => { v.name = '\ud800'; }, v => { v.manifest.value = Number.MAX_SAFE_INTEGER + 1; }, v => { v.manifest.value = Infinity; },
    v => { v.manifest.data = 'x'.repeat(131072); }, v => { v.manifest.nodes = Array.from({ length: 8192 }, () => 0); }, v => { v.capabilities.classes = ['arbitrary_exec']; },
    v => { v.capabilities.effects = ['read_only', 'read_only']; }, v => { v.capabilities.platforms = ['linux-x64']; }, v => { v.version = 'latest'; },
    v => { v.capabilities.inputTypes = ['https://example.com/type']; }, v => { v.distribution.repository += '.git'; }, v => { v.distribution.commit = 'main'; },
    v => { v.distribution.path = '../outside'; }, v => { v.requirements = [{ kind: 'binary', name: 'curl', group: 'arbitrary' }]; },
    v => { v.requirements.push(...v.requirements); }, v => { v.requirements = Array.from({ length: 128 }, (_, i) => ({ kind: 'binary', name: `${i}` + '🧭'.repeat(500) })); }]) {
    const value = clone(observation); mutate(value); assert.equal(projectRegistryObservation(value), null);
  }
  assert.ok(projectRegistryObservation({ ...observation, capabilities: { ...observation.capabilities, inputTypes: ['syn://type/repository'], outputTypes: ['synny://type/text'] } }));
});

test('all retained distribution kinds stay exact and file inventories are complete and bounded', () => {
  const base = clone(candidate.observation), files = [{ path: 'SKILL.md', size: 123, sha256: 'a'.repeat(64) }];
  const claw = { ...base, adapter: 'clawhub', version: '1.2.3', distribution: { kind: 'clawhub_skill', owner: 'author', slug: 'skill', version: '1.2.3', files } };
  const skill = { ...base, adapter: 'skills_sh', version: 'snapshot:' + 'b'.repeat(64), distribution: { kind: 'skills_sh_snapshot', id: 'author/repository/skill', responseSha256: 'b'.repeat(64), files } };
  const tool = { ...base, kind: 'tool', distribution: { kind: 'tool_recipe', repository: 'https://github.com/owner/tool', commit: 'a'.repeat(40), recipeId: other } };
  for (const value of [claw, skill, tool, { ...base, kind: 'mcp_server', distribution: { kind: 'inspect_only' } }]) assert.deepEqual(projectRegistryObservation(value), value);
  for (const mutate of [v => { v.distribution.files[0].path = 'skill.md'; }, v => { v.distribution.files.push({ ...v.distribution.files[0], path: 'skill.MD' }); },
    v => { v.distribution.files[0].size = 1048577; }, v => { v.distribution.files[0].sha256 = 'bad'; }, v => { v.distribution.version = '1.2.4'; }, v => { v.distribution.url = 'https://evil.example/archive'; }]) {
    const value = clone(claw); mutate(value); assert.equal(projectRegistryObservation(value), null);
  }
  assert.equal(projectRegistryObservation({ ...skill, version: 'snapshot:' + 'c'.repeat(64) }), null);
});

test('public source links reject credential, script, whitespace and noncanonical transport forms', () => {
  for (const url of ['https://example.com/a?q=1', 'https://example.com:443/a']) assert.equal(discoverySourceUrl(url), true);
  assert.equal(discoverySourceUrl('potbot://curated/exact', true), true);
  for (const url of ['javascript:alert(1)', 'file:///etc/passwd', 'https://user:secret@example.com/a', 'https://example.com:8443/a', 'https://example.com/#fragment', 'https://example.com/a b', 'https://example.com\\other', 'potbot://curated/exact']) assert.equal(discoverySourceUrl(url), false, url);
});

test('measurement projections bind resolved content, principal, selected agent and a finite 120-second lifetime', () => {
  for (const mutate of [v => { v.measurement.checkId = other; }, v => { v.measurement.agentId = other; }, v => { v.measurement.candidateSha256 = 'a'.repeat(64); },
    v => { v.measurement.bindings.principalId = other; }, v => { v.measurement.bindings.workspaceId = other; }, v => { v.measurement.bindings.agentHomeRevision = 0; },
    v => { v.measurement.bindings.runtimeSha256 = 'invalid'; }, v => { v.measurement.bindings.extra = 'secret'; }, v => { v.measurement.checks = []; },
    v => { v.measurement.checks[0].status = 'blocked'; }, v => { v.measurement.eligible = !v.measurement.eligible; }, v => { v.measurement.checks[1].reasonCode = 'arbitrary_ready'; },
    v => { v.measurement.checks[1].reasonCode = 'binary_missing'; v.measurement.checks[1].status = 'passed'; }, v => { v.measurement.checks.push(v.measurement.checks[0]); },
    v => { v.measurement.expiresAt = new Date(Date.parse(v.measurement.checkedAt) + 120001).toISOString(); }, v => { v.measurement.expiresAt = v.measurement.checkedAt; },
    v => { v.measurement = null; v.measurementCurrent = true; }]) {
    const check = clone(measured); mutate(check); assert.equal(projectDiscoveryCheckEnvelope({ ...scope, check }, w, measured.id, p), null);
  }
  const resolved = clone(pending); resolved.requestedCandidateSha256 = 'f'.repeat(64);
  assert.ok(projectDiscoveryCheckEnvelope({ ...scope, check: resolved }, w));
  const pathBlocked = clone(measured); pathBlocked.measurement.checks.push({ kind: 'binary', name: 'writable-tool', status: 'unsupported', reasonCode: 'binary_path_unsupported' }); pathBlocked.measurement.eligible = false;
  assert.ok(projectDiscoveryCheckEnvelope({ ...scope, check: pathBlocked }, w));
});

test('bounded list projection distinguishes ranked opaque search cursors from owner UUID cursors', () => {
  const cursor = Buffer.from(JSON.stringify({ fingerprint: 'a'.repeat(64), offset: 25 })).toString('base64url');
  assert.ok(projectDiscoveryCandidates({ ...wire.candidates, nextCursor: cursor }, w, p));
  assert.equal(projectDiscoveryCandidates({ ...wire.candidates, nextCursor: 'a'.repeat(513) }, w, p), null);
  assert.equal(projectDiscoveryCandidates({ ...wire.candidates, items: [], nextCursor: cursor }, w, p), null);
  for (const [value, project] of [[wire.sources, projectDiscoverySources], [wire.publishers, projectDiscoveryPublishers], [wire.checks, projectDiscoveryChecks]]) {
    assert.equal(project({ ...value, nextCursor: cursor }, w, p), null);
    assert.equal(project({ ...value, items: [...value.items, value.items[0]] }, w, p), null);
    assert.equal(project({ ...value, items: Array(51).fill(value.items[0]) }, w, p), null);
  }
});

test('mutation receipts bind the original selected digest and action instead of later worker progress', () => {
  const route = ['candidates', pending.candidateId, 'checks'], body = { snapshotId: pending.snapshotId, candidateSha256: pending.requestedCandidateSha256, agentId: pending.agentId, confirm: true };
  assert.ok(projectDiscoveryMutation(wire.pendingCheck, w, route, body, p));
  assert.equal(projectDiscoveryMutation(wire.measuredCheck, w, route, body, p), null);
  for (const patch of [{ snapshotId: other }, { candidateSha256: 'e'.repeat(64) }, { agentId: other }, { confirm: false }, { argv: ['sh'] }, { setupToken: 'private' }]) assert.equal(projectDiscoveryMutation(wire.pendingCheck, w, route, { ...body, ...patch }, p), null);
  const resolved = { ...pending, requestedCandidateSha256: 'f'.repeat(64) };
  assert.ok(projectDiscoveryMutation({ ...scope, check: resolved }, w, route, { ...body, candidateSha256: resolved.requestedCandidateSha256 }, p));
  assert.equal(projectDiscoveryMutation({ ...scope, check: resolved }, w, route, body, p), null);
  for (const route of [['checks', pending.id, 'run'], ['sources', source.id, 'refresh'], ['publishers', publisher.id, 'install']]) assert.equal(validateDiscoveryMutation(route, { revision: 1, digest: 'a'.repeat(64), confirm: true }), null);
});

test('search constraints preserve empty permissions and bound Unicode, CSV identities and total encoded query', () => {
  const params = new URLSearchParams({ q: '🧭'.repeat(500), platform: 'linux', requireLocal: 'true', permittedEffects: '', permittedScopes: '', classes: 'code_search, file_inspection', inputTypes: 'syn://type/repository', outputTypes: 'synny://type/text' });
  const projected = discoverySearchParams(params); assert.ok(projected); assert.equal(projected.get('permittedEffects'), ''); assert.equal(projected.get('permittedScopes'), ''); assert.equal(projected.get('classes'), 'code_search,file_inspection');
  assert.equal(discoverySearchParams(new URLSearchParams()).has('permittedEffects'), false);
  for (const query of ['q=' + 'a'.repeat(501), 'platform=arbitrary', 'requireLocal=1', 'permittedEffects=read_only,read_only', 'permittedEffects=unknown', 'permittedScopes=bad/path', 'classes=unknown', 'inputTypes=https://example.com', 'q=a&q=b', 'classes=code_search,']) assert.equal(discoverySearchParams(new URLSearchParams(query)), null, query.slice(0, 100));
  assert.equal(discoverySearchParams(new URLSearchParams({ permittedScopes: Array.from({ length: 33 }, (_, i) => 'scope' + i).join(',') })), null);
  assert.equal(discoverySearchParams(new URLSearchParams({ permittedScopes: 'a'.repeat(4097) })), null);
  const big = new URLSearchParams({ q: '🧭'.repeat(500), inputTypes: Array.from({ length: 32 }, (_, i) => 'syn://type/' + 'a'.repeat(75) + '-' + i).join(',') });
  assert.ok(new TextEncoder().encode(big.toString()).length > 8192); assert.equal(discoverySearchParams(big), null);
});

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { projectSource, projectPackage, projectPlan, projectSkill, projectHistory } from '../src/lib/skill-acquisition.ts';

const fixture = JSON.parse(readFileSync(new URL('./fixtures/skill-acquisition-http.json', import.meta.url), 'utf8'));
const { workspaceId: w, principalId: p } = fixture.package;
const bindings = { registry_adapter: 'clawhub', registry_identity: 'clawhub:author/portable-skill', registry_version: '1.2.3', registry_candidate_sha256: 'a'.repeat(64), registry_source_sha256: 'b'.repeat(64) };
const claw = { kind: 'registry', adapter: 'clawhub', identity: bindings.registry_identity, version: bindings.registry_version, candidate_sha256: bindings.registry_candidate_sha256,
  archive_sha256: 'c'.repeat(64), path: '', verification: 'content-integrity-only', ...bindings };
const skills = { kind: 'registry', adapter: 'skills_sh', identity: 'owner/repo/skill', verification: 'content-integrity-only', ...bindings,
  registry_adapter: 'skills_sh', registry_identity: 'owner/repo/skill', registry_version: 'snapshot:' + 'e'.repeat(64) };
const github = { kind: 'github', archive_sha256: 'd'.repeat(64), path: 'repo/skills/portable', verification: 'content-integrity-only', url: 'https://github.com/owner/repo', commit: 'e'.repeat(40), requested_path: 'skills/portable', ...bindings };

test('ClawHub, skills.sh and verified GitHub registry provenance survive the existing skill lifecycle projections', () => {
  for (const source of [claw, skills, github]) {
    assert.deepEqual(projectSource({ ...source, privateToken: 'SECRET', storagePath: '/private' }), source);
    const packageEnvelope = structuredClone(fixture.package); packageEnvelope.package.source = source;
    assert.deepEqual(projectPackage(packageEnvelope, w, packageEnvelope.package.id, p), packageEnvelope);
    const plan = structuredClone(fixture.plan); plan.plan.package.source = source;
    assert.deepEqual(projectPlan(plan, w, packageEnvelope.package.id, undefined, p), plan);
    const active = structuredClone(fixture.active); active.skill.source = source;
    assert.deepEqual(projectSkill(active, w, active.skill.id, p), active);
    const history = structuredClone(fixture.history); history.skill.source = source; for (const release of history.releases) release.source = source; for (const event of history.events) event.source = source;
    assert.deepEqual(projectHistory(history, w, history.skill.id, p), history);
  }
});

test('registry source projection rejects missing or conflicting exact identity and provenance bindings', () => {
  for (const patch of [{ registry_adapter: 'mcp_registry' }, { registry_identity: 'other' }, { registry_version: 'latest' }, { registry_version: 'different' },
    { registry_candidate_sha256: 'f'.repeat(64) }, { registry_source_sha256: 'invalid' }, { archive_sha256: null }, { path: '../outside' }, { verification: 'signed-safe' },
    { adapter: 'skills_sh' }, { identity: '\ud800' }]) assert.equal(projectSource({ ...claw, ...patch }), null, JSON.stringify(patch));
  for (const field of ['registry_adapter', 'registry_identity', 'registry_version', 'registry_candidate_sha256', 'registry_source_sha256']) {
    const incomplete = { ...claw }; delete incomplete[field]; assert.equal(projectSource(incomplete), null);
  }
  assert.equal(projectSource({ ...skills, archive_sha256: 'a'.repeat(64), path: '' }), null);
  assert.equal(projectSource({ ...skills, registry_version: '1.2.3' }), null);
  const incompleteGitHub = { ...github }; delete incompleteGitHub.registry_source_sha256; assert.equal(projectSource(incompleteGitHub), null);
  const ordinaryGitHub = { ...github }; for (const field of Object.keys(bindings)) delete ordinaryGitHub[field]; assert.deepEqual(projectSource(ordinaryGitHub), ordinaryGitHub);
});

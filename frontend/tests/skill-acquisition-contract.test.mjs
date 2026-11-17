import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { projectPackage, projectPackageList, projectPlan, projectSkill, projectSkillList, projectHistory, projectResource } from '../src/lib/skill-acquisition.ts';

test('real HTTP → PostgreSQL skill lifecycle remains compatible with every browser projection',async()=>{
  // Captured from test_http_import_review_install_resource_update_rollback_and_revoke.
  // The acceptance CI gate supplies a fresh export from that actual service test.
  const path=process.env.POTBOT_SKILL_CONTRACT_OUTPUT??new URL('./fixtures/skill-acquisition-http.json',import.meta.url);
  const wire=JSON.parse(await readFile(path,'utf8'));
  const {workspaceId,principalId}=wire.package,packageId=wire.package.package.id,skillId=wire.installed.skill.id;
  assert.deepEqual(projectPackageList(wire.packages,workspaceId,principalId),wire.packages);
  assert.deepEqual(projectPackage(wire.package,workspaceId,packageId,principalId),wire.package);
  assert.deepEqual(projectPlan(wire.plan,workspaceId,packageId,undefined,principalId),wire.plan);
  for(const key of ['installed','active','updated','rolledBack','revoked']) {
    assert.deepEqual(projectSkill(wire[key],workspaceId,skillId,principalId),wire[key],key);
  }
  assert.equal(wire.installed.skill.enabled,false);assert.equal(wire.active.skill.enabled,true);
  assert.equal(wire.updated.skill.enabled,false);assert.equal(wire.rolledBack.skill.enabled,false);
  assert.equal(wire.rolledBack.skill.releaseSha256,wire.installed.skill.releaseSha256);assert.equal(wire.revoked.skill.state,'revoked');
  assert.deepEqual(projectResource(wire.resource,workspaceId,wire.resource.resource.path,0,65536,principalId),wire.resource);
  assert.deepEqual(projectHistory(wire.history,workspaceId,skillId,principalId),wire.history);
  assert.deepEqual(projectHistory(wire.previousHistory,workspaceId,skillId,principalId,Number(wire.history.nextCursor)),wire.previousHistory);
  assert.ok(wire.previousHistory.events.every(event=>event.revision<Number(wire.history.nextCursor)));
  assert.deepEqual(projectSkillList(wire.list,workspaceId,principalId),wire.list);
});

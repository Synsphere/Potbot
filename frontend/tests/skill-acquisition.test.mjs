import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { MAX_ARCHIVE_BYTES, projectManifest, projectSource, projectPackage, projectPackageList, projectPlan, projectSkill, projectSkillList,
  projectHistory, projectResource, projectSkillMutation, resourcePath, githubRepository, validateSkillMutation, skillInstallBody, skillControls } from '../src/lib/skill-acquisition.ts';

const fixture=JSON.parse(readFileSync(new URL('./fixtures/skill-acquisition-http.json',import.meta.url),'utf8'));
const {workspaceId:w,principalId:p}=fixture.package,packageId=fixture.package.package.id,skillId=fixture.installed.skill.id;
const other='11111111-1111-4111-8111-111111111111',clone=value=>structuredClone(value);

test('skill projectors enforce private imports, exact scope and truthful shared availability',()=>{
  assert.equal(projectPackage(fixture.package,other),null);
  assert.equal(projectPackage(fixture.package,w,undefined,other),null);
  assert.equal(projectPackage({...fixture.package,package:{...fixture.package.package,ownerPrincipalId:other}},w),null);
  for(const change of [{owned:false},{namespace:'someone-else'},{contextName:'unsafe.path'},{revision:0},{revision:1.2},{state:'revoked',enabled:true},{origin:'authored'}]) {
    assert.equal(projectSkill({...fixture.installed,skill:{...fixture.installed.skill,...change}},w),null);
  }
  const shared={...clone(fixture.active),principalId:other};shared.skill.owned=false;
  assert.ok(projectSkill(shared,w,skillId,other));
  for(const change of [{enabled:false},{approvalValid:false},{state:'revoked'}])assert.equal(projectSkill({...shared,skill:{...shared.skill,...change}},w),null);
  assert.equal(projectHistory({...fixture.history,principalId:other,skill:{...shared.skill}},w,skillId,other),null);
  const raw={...fixture.package,setupToken:'SECRET',package:{...fixture.package.package,control_mac:'SECRET',draft:'SECRET'}};
  assert.deepEqual(projectPackage(raw,w),fixture.package);
});

test('portable manifests preserve bounded metadata and requirements without exposing executable authority',()=>{
  const original=fixture.package.package.manifest;
  assert.deepEqual(projectManifest(original),original);
  const required=clone(original);required.metadata={hooks:{after:'echo preserved'},license:'MIT'};
  required.requirements=[{kind:'binary',name:'untrusted-binary',status:'unverified'},{kind:'environment',name:'EXAMPLE_TOKEN',status:'unverified'}];
  required.compatibility={status:'review_required',unsupported:['metadata.hooks']};
  required.findings=[{code:'unsupported_behavior',severity:'warning',message:'Preserved data only'}];
  assert.deepEqual(projectManifest(required),required);
  for(const alter of [m=>{m.requirements[0].status='installed';},m=>{m.metadata.big=Number.MAX_SAFE_INTEGER+1;},m=>{m.files.push({...m.files[0]});},
    m=>{m.files[0].size=1024*1024+1;},m=>{m.files[0].path='../SKILL.md';},m=>{m.content_sha256='e'.repeat(64);},m=>{m.schema='arbitrary-script';}]) {
    const value=clone(required);alter(value);assert.equal(projectManifest(value),null);
  }
  const oversized={...fixture.package,content:'é'.repeat(40000),contentTruncated:true};assert.equal(projectPackage(oversized,w),null);
  for(const field of ['files','bytes'])assert.equal(projectPackage({...fixture.package,package:{...fixture.package.package,[field]:0}},w),null);
});

test('Python codepoint bounds and bounded unsupported declarations remain representable in the browser',()=>{
  const manifest=clone(fixture.package.package.manifest);manifest.description='🧭'.repeat(1024);
  manifest.metadata={['🧭'.repeat(128)]:'🧭'.repeat(1024)};
  manifest.compatibility={status:'review_required',unsupported:Array.from({length:601},(_,index)=>`metadata.unknown_${index}`)};
  assert.deepEqual(projectManifest(manifest),manifest);
  manifest.description+='🧭';assert.equal(projectManifest(manifest),null);
  manifest.description='valid';manifest.metadata={bad:'\ud800'};assert.equal(projectManifest(manifest),null);
});

test('the first installation sends null while updates bind the existing skill and invalid approval permits containment only',()=>{
  const first=skillInstallBody(fixture.plan.plan);
  assert.deepEqual(first,{planSha256:fixture.plan.plan.sha256,expectedRevision:0,skillId:null,confirm:true});
  const update=skillInstallBody({...fixture.plan.plan,action:'update',expectedRevision:2});
  assert.equal(update.skillId,skillId);assert.equal(update.expectedRevision,2);
  assert.deepEqual(skillControls(fixture.installed.skill),{activate:true,disable:false,rollback:true,revoke:true});
  assert.deepEqual(skillControls(fixture.active.skill),{activate:false,disable:true,rollback:true,revoke:true});
  assert.deepEqual(skillControls({...fixture.active.skill,approvalValid:false}),{activate:false,disable:false,rollback:false,revoke:true});
  assert.deepEqual(skillControls(fixture.revoked.skill),{activate:false,disable:false,rollback:false,revoke:false});
  assert.deepEqual(skillControls({...fixture.active.skill,owned:false}),{activate:false,disable:false,rollback:false,revoke:false});
});

test('source and resource names remain canonical without admitting network or filesystem routes',()=>{
  for(const path of ['SKILL.md','references/a..b.md','assets/é.txt','nested/resource.json'])assert.equal(resourcePath(path),true,path);
  for(const path of ['', '../x','/x','a//b','a/./b','a/../b','a\\b','C:x','CON','con.txt','.git/config','a.','a ','e\u0301.txt','a/\u0000'])assert.equal(resourcePath(path),false,path);
  assert.equal(resourcePath('',true),true);
  assert.equal(githubRepository('https://github.com/owner/repo'),true);
  for(const url of ['https://github.com/owner/repo.git','http://github.com/owner/repo','https://user@github.com/o/r','https://github.com/o/r/tree/main','https://evil.example/o/r','https://github.com/o/r?x'])assert.equal(githubRepository(url),false,url);
  assert.deepEqual(projectSource({...fixture.package.package.source,credential:'SECRET'}),fixture.package.package.source);
  assert.equal(projectSource({...fixture.package.package.source,verification:'trusted-publisher'}),null);
  assert.equal(projectSource({...fixture.package.package.source,path:'../escape'}),null);
});

test('review plans and owner history bind exact identities, immutable releases and descending cursors',()=>{
  assert.ok(projectPlan(fixture.plan,w,packageId,skillId,p));
  for(const patch of [{executable:true},{willDisable:false},{expectedRevision:2},{sourceSha256:'e'.repeat(64)},{packageId:other},{added:['../escape']}]) {
    assert.equal(projectPlan({...fixture.plan,plan:{...fixture.plan.plan,...patch}},w,packageId),null);
  }
  const repeated=clone(fixture.plan);repeated.plan.changed=[repeated.plan.added[0]];assert.equal(projectPlan(repeated,w,packageId),null);
  assert.ok(projectHistory(fixture.history,w,skillId,p));
  const ascending=clone(fixture.history);ascending.events.reverse();assert.equal(projectHistory(ascending,w,skillId),null);
  assert.equal(projectHistory({...fixture.history,nextCursor:'999'},w,skillId),null);
  assert.equal(projectHistory(fixture.history,w,skillId,p,Number(fixture.history.nextCursor)),null);
  const duplicate=clone(fixture.history);duplicate.releases.push(duplicate.releases[0]);assert.equal(projectHistory(duplicate,w,skillId),null);
  for(const project of [projectPackageList,projectSkillList])assert.equal(project({schema:fixture.package.schema,workspaceId:w,principalId:p,items:[],nextCursor:'../next'},w),null);
});

test('bounded resource projections require exact paths, offsets, length and terminal cursors',()=>{
  const resource=fixture.resource,path=resource.resource.path;
  assert.ok(projectResource(resource,w,path,0,65536,p));
  for(const patch of [{offset:1},{nextOffset:1},{nextOffset:null},{dataBase64:'YQ'}, {dataBase64:'YQ==='}]) {
    assert.equal(projectResource({...resource,...patch},w,path,0,65536,p),null);
  }
  assert.equal(projectResource(resource,w,'wrong.md',0,65536,p),null);
  assert.equal(projectResource(resource,w,path,0,8,p),null);
  const empty={...resource,resource:{path:'empty.bin',size:0,sha256:createHash('sha256').update('').digest('hex'),mediaType:'application/octet-stream'},dataBase64:'',offset:0,nextOffset:null};
  assert.ok(projectResource(empty,w,'empty.bin',0,65536,p));
});

test('mutation bodies require exact source, explicit confirmation, revisions and idempotent receipt identity',async()=>{
  const archive=Buffer.from('exact archive bytes'),body={archiveBase64:archive.toString('base64'),subdirectory:''};
  assert.deepEqual(validateSkillMutation(['imports','zip'],body),body);
  assert.equal(validateSkillMutation(['imports','zip'],{...body,execute:true}),null);
  assert.equal(validateSkillMutation(['imports','zip'],{...body,archiveBase64:Buffer.alloc(MAX_ARCHIVE_BYTES+1).toString('base64')}),null);
  const imported=clone(fixture.package);imported.package.source.archive_sha256=createHash('sha256').update(archive).digest('hex');
  assert.ok(await projectSkillMutation(imported,w,['imports','zip'],body,p));
  assert.equal(await projectSkillMutation(fixture.package,w,['imports','zip'],body,p),null);
  const install={planSha256:fixture.plan.plan.sha256,expectedRevision:0,skillId,confirm:true};
  assert.ok(await projectSkillMutation(fixture.installed,w,['packages',packageId,'install'],install,p));
  assert.equal(await projectSkillMutation(fixture.active,w,['packages',packageId,'install'],install,p),null);
  const activate={expectedRevision:1,releaseSha256:null,confirm:true};
  assert.ok(await projectSkillMutation(fixture.active,w,[skillId,'activate'],activate,p));
  assert.equal(await projectSkillMutation(fixture.active,w,[skillId,'disable'],activate,p),null);
  for(const patch of [{confirm:false},{expectedRevision:0},{expectedRevision:'1'},{releaseSha256:'e'.repeat(64)},{command:'sh'}])assert.equal(validateSkillMutation([skillId,'activate'],{...activate,...patch}),null);
  assert.equal(validateSkillMutation([skillId,'execute'],activate),null);
});

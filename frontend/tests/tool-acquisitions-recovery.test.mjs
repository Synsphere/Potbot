import test from 'node:test';
import assert from 'node:assert/strict';
import { TOOL_ENDPOINT, newToolAttempt, toolAttemptRoute, toolJournalScope, validateToolAttempt } from '../src/lib/tool-acquisition-client.ts';
import { WorkshopRequests, workshopJournalScope, retainAttempt, recoverAttempt, clearAttempt, clearRejectedAttempt } from '../src/lib/skill-workshop-client.ts';
import { workspaceId,principalId,operationId,recipeId,queued,reviewed } from './tool-acquisition-fixtures.mjs';

function storage() { const records=new Map();return {records,read:async scope=>records.get(scope),write:async(scope,value)=>{records.set(scope,structuredClone(value));},remove:async(scope,key)=>{if(records.get(scope)?.key===key)records.delete(scope);}}; }
test('tool recovery accepts only exact fixed lifecycle requests and retains their original serialized body',()=>{
  const body={recipeId,commit:queued.newCommit,confirm:true},attempt=newToolAttempt(TOOL_ENDPOINT,body);
  assert.deepEqual(validateToolAttempt(attempt),attempt);assert.match(attempt.key,/^tool:[a-f0-9-]{36}$/);assert.deepEqual(toolAttemptRoute(attempt.path),[]);
  const formatted={...attempt,body:JSON.stringify(body,null,2)};assert.deepEqual(validateToolAttempt(formatted),formatted);
  for(const action of ['rollback','reconcile','repin','postflight','cancel'])assert.ok(validateToolAttempt(newToolAttempt(`${TOOL_ENDPOINT}/${operationId}/${action}`,{revision:3,bindingSha256:reviewed.bindingSha256,confirm:true})));
  assert.ok(validateToolAttempt(newToolAttempt(`${TOOL_ENDPOINT}/${operationId}/approve-build`,{revision:3,reviewSha256:reviewed.review.sha256,confirm:true})));
  for(const patch of [{path:'https://evil.example'+TOOL_ENDPOINT},{path:'/api/ui/skills/imports/zip'},{path:TOOL_ENDPOINT+'/../run'},{path:TOOL_ENDPOINT+`/${operationId}/run`},{body:JSON.stringify({...body,argv:['/bin/sh']})},{body:' '.repeat(8192)+attempt.body},{key:'unsafe key'},{unknown:true}])assert.equal(validateToolAttempt({...attempt,...patch}),null);
});
test('tool and skill journals are isolated by surface, tab, workspace and principal',()=>{
  const original=globalThis.window,values=new Map();globalThis.window={sessionStorage:{getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,value)}};
  try {
    const scope=toolJournalScope(workspaceId,principalId);assert.equal(scope,'tool:'+workshopJournalScope(workspaceId,principalId));
    assert.notEqual(scope,toolJournalScope(principalId,workspaceId));assert.equal(scope,toolJournalScope(workspaceId,principalId));
    values.clear();assert.notEqual(scope,toolJournalScope(workspaceId,principalId));
  } finally {if(original===undefined)delete globalThis.window;else globalThis.window=original;}
});
test('uncertain recovered actions keep the exact body and key through authorization and revision rejections without automatic replay',async()=>{
  const store=storage(),scope='tool:recovery:'+crypto.randomUUID(),attempt=newToolAttempt(TOOL_ENDPOINT,{recipeId,commit:queued.newCommit,confirm:true});
  const originalFetch=globalThis.fetch;let calls=0;globalThis.fetch=async()=>{calls++;throw Error('must not submit');};
  try {
    assert.equal(await retainAttempt(scope,attempt,store),true);assert.deepEqual(await recoverAttempt(scope,validateToolAttempt,store),attempt);assert.equal(calls,0);
    for(const status of [400,401,403,404,409,413,422,502,503]) {
      assert.equal(await clearRejectedAttempt(scope,attempt.key,status,true,store),false);assert.deepEqual(await recoverAttempt(scope,validateToolAttempt,store),attempt);
    }
    await assert.rejects(retainAttempt(scope,{...attempt,key:'tool:different'},store),/unresolved/);
    await assert.rejects(retainAttempt(scope,{...attempt,body:JSON.stringify({recipeId,commit:'b'.repeat(40),confirm:true})},store),/unresolved/);
    await clearAttempt(scope,'tool:wrong-key',store);assert.deepEqual(await recoverAttempt(scope,validateToolAttempt,store),attempt);
    await clearAttempt(scope,attempt.key,store);assert.equal(await recoverAttempt(scope,validateToolAttempt,store),null);assert.equal(calls,0);
  } finally {globalThis.fetch=originalFetch;}
});
test('storage failure reports non-durable recovery while preserving the live exact action until confirmation',async()=>{
  const scope='tool:nondurable:'+crypto.randomUUID(),attempt=newToolAttempt(TOOL_ENDPOINT,{recipeId,commit:queued.newCommit,confirm:true});
  const unavailable={read:async()=>{throw Error('unavailable');},write:async()=>{throw Error('unavailable');},remove:async()=>{throw Error('unavailable');}};
  assert.equal(await retainAttempt(scope,attempt,unavailable),false);assert.deepEqual(await recoverAttempt(scope,validateToolAttempt,unavailable),attempt);
  assert.equal(await clearRejectedAttempt(scope,attempt.key,503,false,unavailable),false);assert.deepEqual(await recoverAttempt(scope,validateToolAttempt,unavailable),attempt);
  assert.equal(await clearRejectedAttempt(scope,attempt.key,409,false,unavailable),true);assert.equal(await recoverAttempt(scope,validateToolAttempt,unavailable),null);
});
test('selection and scope changes abort all outstanding reads and fence late response bodies',async()=>{
  const gate=new WorkshopRequests(),detail=gate.open(),events=gate.open();let finishBody;
  const body=new Promise(resolve=>{finishBody=resolve;});let applied=false;
  const reading=(async()=>{await body;if(detail.current())applied=true;detail.finish();})();
  gate.reset();assert.equal(detail.signal.aborted,true);assert.equal(events.signal.aborted,true);finishBody();await reading;assert.equal(applied,false);
  const current=gate.open();assert.equal(current.current(),true);events.finish();assert.equal(current.current(),true);gate.reset();assert.equal(current.current(),false);
});

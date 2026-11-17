import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { WorkshopRequests, base64Bytes, clearAttempt, clearRejectedAttempt, newAttempt, recoverAttempt, retainAttempt, validateWorkshopAttempt, readSkillResource } from '../src/lib/skill-workshop-client.ts';

test('scope resets abort concurrent reads and permanently fence late body/file completions',async()=>{
  const requests=new WorkshopRequests(), oldRead=requests.open(), oldFile=requests.open();
  let resolve;const body=new Promise(done=>{resolve=done;});
  const applied=body.then(()=>oldRead.current());
  requests.reset();const current=requests.open();resolve();
  assert.equal(await applied,false);assert.equal(oldRead.signal.aborted,true);assert.equal(oldFile.current(),false);
  assert.equal(current.current(),true);oldRead.finish();oldFile.finish();assert.equal(current.current(),true);
  requests.reset();assert.equal(current.current(),false);
});

test('uncertain large imports retain exact payload/key by scope without initiating another request',async()=>{
  const records=new Map(),storage={read:async key=>records.get(key),write:async(key,value)=>{records.set(key,value);},
    remove:async(key,expected)=>{if(records.get(key)?.key===expected)records.delete(key);}};
  const scope='workspace:principal:import',other='workspace:another-principal:import';
  const attempt=newAttempt('/api/ui/skills/imports',{archiveBase64:'A'.repeat(5_592_408)});
  const valid=value=>value&&typeof value==='object'&&typeof value.key==='string'?value:null;
  assert.equal(await retainAttempt(scope,attempt,storage),true);
  assert.deepEqual(await recoverAttempt(scope,valid,storage),attempt);assert.equal(await recoverAttempt(other,valid,storage),null);
  const next=newAttempt('/api/ui/skills/imports',{archiveBase64:'next'});
  await assert.rejects(retainAttempt(scope,next,storage),/unresolved/);
  await clearAttempt(scope,attempt.key,storage);await retainAttempt(scope,next,storage);
  await clearAttempt(scope,attempt.key,storage);assert.equal((await recoverAttempt(scope,valid,storage)).key,next.key);
  await clearAttempt(scope,next.key,storage);assert.equal(await recoverAttempt(scope,valid,storage),null);
});

test('lost successful responses keep their exact receipt through later authorization and conflict rejections',async()=>{
  const records=new Map(),storage={read:async key=>records.get(key),write:async(key,value)=>{records.set(key,value);},
    remove:async(key,expected)=>{if(records.get(key)?.key===expected)records.delete(key);}};
  const scope='workspace:principal:auth-loss',attempt=newAttempt('/api/ui/skills/11111111-1111-4111-8111-111111111111/activate',{expectedRevision:1,releaseSha256:null,confirm:true});
  await retainAttempt(scope,attempt,storage);
  for(const status of [400,401,403,404,409,413,422,502,503]) {
    assert.equal(await clearRejectedAttempt(scope,attempt.key,status,true,storage),false);
    assert.deepEqual(await recoverAttempt(scope,validateWorkshopAttempt,storage),attempt);
  }
  assert.equal(await clearRejectedAttempt(scope,attempt.key,503,false,storage),false);
  assert.equal(await clearRejectedAttempt(scope,attempt.key,400,false,storage),true);
  assert.equal(await recoverAttempt(scope,validateWorkshopAttempt,storage),null);
});

test('duplicate browser tab identities cannot overwrite another unresolved IndexedDB receipt',async t=>{
  const records=new Map(),prior=globalThis.indexedDB;
  const db={close(){},transaction(){
    const tx={};let pending=0,aborted=false;
    function issue(fn){const request={};pending++;queueMicrotask(()=>{if(aborted)return;request.result=fn();request.onsuccess?.();pending--;if(!pending&&!aborted)queueMicrotask(()=>tx.oncomplete?.());});return request;}
    tx.objectStore=()=>({get:key=>issue(()=>records.get(key)),put:(value,key)=>issue(()=>records.set(key,value)),delete:key=>issue(()=>records.delete(key))});
    tx.abort=()=>{aborted=true;queueMicrotask(()=>tx.onabort?.());};return tx;
  }};
  globalThis.indexedDB={open(){const request={result:db};queueMicrotask(()=>request.onsuccess?.());return request;}};
  t.after(()=>{if(prior===undefined)delete globalThis.indexedDB;else globalThis.indexedDB=prior;});
  // Separate module instances model separate tabs with independent memory and a copied tab ID.
  const first=await import('../src/lib/skill-workshop-client.ts?duplicate-tab=first');
  const second=await import('../src/lib/skill-workshop-client.ts?duplicate-tab=second');
  const reloaded=await import('../src/lib/skill-workshop-client.ts?duplicate-tab=reloaded');
  const scope='copied-tab:workspace:principal';
  const a=newAttempt('/api/ui/skills/imports/github',{repository:'https://github.com/owner/repo',commit:'a'.repeat(40),path:''});
  const b=newAttempt('/api/ui/skills/imports/github',{repository:'https://github.com/owner/repo',commit:'b'.repeat(40),path:''});
  assert.equal(await first.retainAttempt(scope,a),true);
  assert.equal(await second.retainAttempt(scope,b),false,'second tab uses an explicit nondurable fallback');
  assert.deepEqual(records.get(scope),a);assert.deepEqual(await second.recoverAttempt(scope,validateWorkshopAttempt),b);
  assert.deepEqual(await reloaded.recoverAttempt(scope,validateWorkshopAttempt),a);
  await second.clearAttempt(scope,b.key);assert.deepEqual(records.get(scope),a);
  await first.clearAttempt(scope,a.key);assert.equal(records.has(scope),false);
});

test('restored mutation validation rejects arbitrary routes, altered confirmations and malformed payloads',()=>{
  const body={repository:'https://github.com/owner/repo',commit:'a'.repeat(40),path:''},valid=newAttempt('/api/ui/skills/imports/github',body);
  assert.deepEqual(validateWorkshopAttempt(valid),valid);
  for(const value of [{...valid,path:'https://evil.example/imports'}, {...valid,path:'/api/ui/skills/imports/execute'}, {...valid,key:'x'.repeat(129)},
    {...valid,body:JSON.stringify({...body,command:'sh'})},{...valid,body:'{broken'}, {...valid,workspace:'unexpected'}])assert.equal(validateWorkshopAttempt(value),null);
});

test('exact resource reads assemble the full bounded binary and verify its digest before returning it',async()=>{
  const bytes=Buffer.alloc(1024*1024);for(let index=0;index<bytes.length;index++)bytes[index]=index%251;
  const file={path:'assets/binary.dat',size:bytes.length,sha256:createHash('sha256').update(bytes).digest('hex'),media_type:'application/octet-stream',executable_hint:false};
  const scope={schema:'potbot-skill-acquisition/v1',workspaceId:'11111111-1111-4111-8111-111111111111',principalId:'22222222-2222-4222-8222-222222222222'};
  const requests=new WorkshopRequests();let calls=0;
  const read=async(url,options)=>{calls++;assert.equal(options.headers['X-Potbot-Workspace'],scope.workspaceId);const query=new URL(url,'https://local.test').searchParams;
    const offset=Number(query.get('offset')),chunk=bytes.subarray(offset,offset+65536);assert.equal(query.get('releaseSha256'),'e'.repeat(64));
    return Response.json({...scope,resource:{path:file.path,size:file.size,sha256:file.sha256,mediaType:file.media_type},offset,dataBase64:chunk.toString('base64'),nextOffset:offset+chunk.length===bytes.length?null:offset+chunk.length});};
  const result=await readSkillResource({...scope,base:'/api/ui/skills/33333333-3333-4333-8333-333333333333',file,releaseSha256:'e'.repeat(64),ticket:requests.open(),read});
  assert.deepEqual(Buffer.from(result),bytes);assert.equal(calls,16);
});

test('resource integrity, identity, range and late body failures never produce downloadable bytes',async()=>{
  const bytes=Buffer.from('verified resource'),file={path:'SKILL.md',size:bytes.length,sha256:createHash('sha256').update(bytes).digest('hex'),media_type:'text/markdown',executable_hint:false};
  const scope={schema:'potbot-skill-acquisition/v1',workspaceId:'11111111-1111-4111-8111-111111111111',principalId:'22222222-2222-4222-8222-222222222222'};
  const page={...scope,resource:{path:file.path,size:file.size,sha256:file.sha256,mediaType:file.media_type},offset:0,dataBase64:bytes.toString('base64'),nextOffset:null};
  const options={...scope,base:'/api/ui/skills/packages/33333333-3333-4333-8333-333333333333',file};
  const requests=new WorkshopRequests();
  const corrupt=Buffer.from(bytes);corrupt[0]^=1;
  await assert.rejects(readSkillResource({...options,ticket:requests.open(),read:async()=>Response.json({...page,dataBase64:corrupt.toString('base64')})}),/SHA-256/);
  await assert.rejects(readSkillResource({...options,ticket:requests.open(),read:async()=>Response.json({...page,resource:{...page.resource,sha256:'f'.repeat(64)}})}),/identity/);
  await assert.rejects(readSkillResource({...options,ticket:requests.open(),read:async()=>Response.json({...page,nextOffset:1})}),/identity/);
  await assert.rejects(readSkillResource({...options,ticket:requests.open(),read:async()=>({ok:true,json:async()=>{requests.reset();return page;}})}),error=>error.name==='AbortError');
});

test('storage unavailability leaves navigation recovery available and late clears cannot remove a newer attempt',async()=>{
  const unavailable={read:async()=>{throw Error('blocked');},write:async()=>{throw Error('quota');},remove:async()=>{throw Error('blocked');}};
  const scope='workspace:principal:quota',attempt=newAttempt('/api/ui/skills/imports',{test:1});
  assert.equal(await retainAttempt(scope,attempt,unavailable),false);
  assert.equal((await recoverAttempt(scope,value=>value??null,unavailable)).key,attempt.key);
  await clearAttempt(scope,attempt.key,unavailable);
  let remove;const records=new Map(),storage={read:async key=>records.get(key),write:async(key,value)=>{records.set(key,value);},
    remove:(key,expected)=>new Promise(done=>{remove=()=>{if(records.get(key)?.key===expected)records.delete(key);done();};})};
  await retainAttempt(scope,attempt,storage);const clearing=clearAttempt(scope,attempt.key,storage);
  const next=newAttempt('/api/ui/skills/imports',{test:2});await retainAttempt(scope,next,storage);
  remove();await clearing;assert.equal(records.get(scope).key,next.key);
});

test('binary archive encoding retains every byte up to the full upload bound',()=>{
  const bytes=new Uint8Array(4*1024*1024);for(let index=0;index<bytes.length;index++)bytes[index]=index%256;
  const encoded=base64Bytes(bytes);assert.equal(encoded.length,5_592_408);
  assert.deepEqual(new Uint8Array(Buffer.from(encoded,'base64')),bytes);
});

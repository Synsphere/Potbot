import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mutationAttempt } from '../src/lib/operation-workbench.ts';
import { clearSubmission, formFromRequest, parsePendingSubmission, readPendingSubmission, retainSubmission, submissionKey } from '../src/lib/operation-workbench-session.ts';
import { MAX_ARTIFACT_DOWNLOAD_BYTES, readVerifiedArtifact } from '../src/lib/operation-workbench-download.ts';

const workspaceId='c7687924-49a1-4a65-b48e-f1d9d29ed409', principalId='1050b1bb-21c2-4188-a053-01498ce7087c';
const operationId='a'.repeat(64), jobId='9a71e07c-150f-41ed-b248-4f55c42e83f0';
const scope={schema:'potbot-operation-workbench/v1',workspaceId,principalId,operationId};
const base=`/api/ui/operations/${operationId}`;
const request={job_type:'model.generate',episode_id:workspaceId,input:{prompt:'Keep this exact prompt',max_output_tokens:512,timeout_seconds:30},
  selected_models:[{provider:'anthropic',model:'example/model',role:'coding'}]};
const storage=()=>{const values=new Map();return {getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,value),removeItem:key=>values.delete(key)};};

test('uncertain submissions survive operation navigation with the exact key/body and remain isolated by principal/workspace',()=>{
  const store=storage(), key=submissionKey(workspaceId,principalId,operationId), attempt=mutationAttempt(`${base}/checks`,request);
  retainSubmission(key,attempt,store);
  assert.deepEqual(readPendingSubmission(key,operationId,store),{attempt,request,jobId:null});
  assert.equal(readPendingSubmission(submissionKey(principalId,workspaceId,operationId),operationId,store),null);
  assert.equal(parsePendingSubmission(store.getItem(key),'b'.repeat(64)),null);
  const recovered=parsePendingSubmission(store.getItem(key),operationId);
  assert.equal(recovered.attempt.body,attempt.body);assert.equal(recovered.attempt.key,attempt.key);
  assert.equal(formFromRequest(recovered.request).prompt,request.input.prompt);
  assert.equal(formFromRequest(recovered.request).provider,'anthropic');
  const newer=mutationAttempt(`${base}/checks`,request);retainSubmission(key,newer,store);
  clearSubmission(key,attempt.key,store);assert.equal(readPendingSubmission(key,operationId,store).attempt.key,newer.key);
  clearSubmission(key,newer.key,store);assert.equal(readPendingSubmission(key,operationId,store),null);
});

test('recovered launches and cancellations require a fixed route, explicit confirmation and exact supported payload',()=>{
  const launch=mutationAttempt(`${base}/runs`,{checkJobId:jobId,request,confirm:true});
  assert.deepEqual(parsePendingSubmission(JSON.stringify(launch),operationId),{attempt:launch,request,jobId:null});
  const cancel=mutationAttempt(`${base}/jobs/${jobId}/cancel`,{confirm:true});
  assert.deepEqual(parsePendingSubmission(JSON.stringify(cancel),operationId),{attempt:cancel,request:null,jobId});
  for(const wrong of [{...launch,path:'https://example.com'}, {...launch,path:`${base}/jobs/${jobId}/execute`},
    {...launch,body:JSON.stringify({checkJobId:jobId,request,confirm:false})},{...launch,body:JSON.stringify({...request,secret:'bad'})},
    {...launch,key:'bad/key'},{...launch,body:'x'.repeat(131073)}])
    assert.equal(parsePendingSubmission(JSON.stringify(wrong),operationId),null);
  assert.equal(parsePendingSubmission('{broken',operationId),null);
});

test('browser storage failure preserves in-memory recovery without replacing a newer uncertain receipt',()=>{
  const key=submissionKey(workspaceId,principalId,'c'.repeat(64)), attempt=mutationAttempt(`/api/ui/operations/${'c'.repeat(64)}/checks`,request);
  const unavailable={getItem(){throw new Error('unavailable');},setItem(){throw new Error('full');},removeItem(){throw new Error('unavailable');}};
  retainSubmission(key,attempt,unavailable);assert.equal(readPendingSubmission(key,'c'.repeat(64),unavailable).attempt.key,attempt.key);
  clearSubmission(key,attempt.key,unavailable);assert.equal(readPendingSubmission(key,'c'.repeat(64),unavailable),null);
});

const data=new TextEncoder().encode('{"answer":"verified"}');
const artifact={id:principalId,sha256:createHash('sha256').update(data).digest('hex'),bytes:data.length,mediaType:'application/json'};
const options=(read,extra={})=>({workspaceId,principalId,operationId,jobId,artifact,signal:new AbortController().signal,current:()=>true,read,...extra});
const page=(offset,end)=>({...scope,artifact,offset,dataBase64:Buffer.from(data.slice(offset,end)).toString('base64'),nextOffset:end<data.length?end:null});

test('artifact download follows owned ranges and verifies the entire digest before releasing bytes',async()=>{
  const reads=[],progress=[];
  const bytes=await readVerifiedArtifact(options(async(url,init)=>{
    assert.equal(init.headers['X-Potbot-Workspace'],workspaceId);assert.equal(init.credentials,'same-origin');
    assert.ok(url.startsWith(`${base}/jobs/${jobId}/artifact?`));
    const offset=Number(new URL(url,'http://localhost').searchParams.get('offset'));reads.push(offset);
    return Response.json(page(offset,Math.min(offset+7,data.length)));
  },{progress:(count,total)=>progress.push([count,total])}));
  assert.deepEqual(bytes,data);assert.deepEqual(reads,[0,7,14]);assert.deepEqual(progress.at(-1),[data.length,data.length]);
});

test('artifact download rejects changed identity, incomplete cursors, foreign principals and digest mismatches',async()=>{
  for(const changed of [{...page(0,data.length),artifact:{...artifact,id:workspaceId}},
    {...page(0,data.length),artifact:{...artifact,bytes:data.length+1}},
    {...page(0,data.length),principalId:workspaceId},{...page(0,7),nextOffset:null},
    {...page(0,7),nextOffset:8}])
    await assert.rejects(readVerifiedArtifact(options(async()=>Response.json(changed))),/identity or range/);
  await assert.rejects(readVerifiedArtifact(options(async()=>Response.json({...page(0,data.length),dataBase64:Buffer.from('x'.repeat(data.length)).toString('base64')}))),/SHA-256/);
  let calls=0;
  await assert.rejects(readVerifiedArtifact(options(async()=>{calls++;return Response.json(page(0,data.length));},
    {artifact:{...artifact,bytes:MAX_ARTIFACT_DOWNLOAD_BYTES+1}})),/16 MiB/);
  assert.equal(calls,0);
});

test('a scope change or abort during an artifact read cannot release stale bytes or update progress',async()=>{
  let current=true,updates=0;
  await assert.rejects(readVerifiedArtifact(options(async()=>{current=false;return Response.json(page(0,data.length));},
    {current:()=>current,progress:()=>{updates++;}})),{name:'AbortError'});
  assert.equal(updates,0);
  const abort=new AbortController();abort.abort();let calls=0;
  await assert.rejects(readVerifiedArtifact(options(async()=>{calls++;return Response.json(page(0,data.length));},{signal:abort.signal})),{name:'AbortError'});
  assert.equal(calls,0);
});

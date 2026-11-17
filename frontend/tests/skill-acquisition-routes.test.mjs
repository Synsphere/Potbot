import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { sessionCookie } from '../src/lib/server/security.ts';
import { skillAcquisitionRoute } from '../src/lib/server/skill-acquisition.ts';
import { skillInstallBody } from '../src/lib/skill-acquisition.ts';
import { GET as list } from '../src/app/api/ui/skills/route.ts';
import { GET as read, POST as write } from '../src/app/api/ui/skills/[...path]/route.ts';

const fixture=JSON.parse(readFileSync(new URL('./fixtures/skill-acquisition-http.json',import.meta.url),'utf8'));
const {workspaceId,principalId}=fixture.package,packageId=fixture.package.package.id,skillId=fixture.installed.skill.id;
const auth={password:'operator-password-very-long',signingKey:'a'.repeat(64),publicOrigin:'http://localhost:3100'};
const cookie=sessionCookie(auth).split(';')[0],clone=value=>structuredClone(value);
const env={POTBOT_UI_PASSWORD:auth.password,POTBOT_UI_COOKIE_SECRET:auth.signingKey,POTBOT_UI_PUBLIC_ORIGIN:auth.publicOrigin,
  POTBOT_UI_BACKEND_TOKEN:'PRIVATE-backend-token',POTBOT_UI_BACKEND_URL:'http://127.0.0.1:8765/',POTBOT_UI_WORKSPACE_ID:workspaceId,
  POTBOT_UI_SETUP_TOKEN:'PRIVATE-setup-token-'.repeat(4)};
const params=path=>({params:Promise.resolve({path:path.split('/')})});
function request(method='GET',body,options={}) {
  return new Request(`${auth.publicOrigin}/api/ui/skills${options.query??''}`,{method,
    headers:{Cookie:cookie,'X-Potbot-Workspace':workspaceId,...(method==='POST'?{Origin:auth.publicOrigin,'Sec-Fetch-Site':'same-origin','Content-Type':'application/json','Idempotency-Key':'skill:test-key'}:{}),...options.headers},
    ...(body===undefined?{}:{body:JSON.stringify(body)}),...(options.signal?{signal:options.signal}:{})});
}

test('skill BFF keeps source import and lifecycle authority within fixed scoped routes',async t=>{
  const prior={...process.env},priorFetch=globalThis.fetch;Object.assign(process.env,env);delete process.env.POTBOT_UI_WORKSPACES_JSON;
  t.after(()=>{globalThis.fetch=priorFetch;for(const key of [...Object.keys(env),'POTBOT_UI_WORKSPACES_JSON']) {
    if(prior[key]===undefined)delete process.env[key];else process.env[key]=prior[key];
  }});
  await t.test('requires authentication, exact origins, allowlisted workspaces and fixed methods before network',async()=>{
    let calls=0;globalThis.fetch=async()=>{calls++;throw Error('unexpected');};
    assert.equal((await list(new Request(auth.publicOrigin+'/api/ui/skills'))).status,401);
    assert.equal((await list(request('GET',undefined,{headers:{Origin:'https://evil.example'}}))).status,403);
    assert.equal((await list(request('GET',undefined,{headers:{'X-Potbot-Workspace':principalId}}))).status,503);
    assert.equal((await skillAcquisitionRoute(request('PUT'),[])).status,400);
    for(const path of ['execute','imports/directory','imports/github/execute','packages/../install',`${skillId}/run`,`${skillId}/activate/extra`]) {
      assert.equal((await write(request('POST',{}),params(path))).status,400,path);
    }
    assert.equal((await write(request('POST',{}, {headers:{Origin:'https://evil.example'}}),params('imports/zip'))).status,403);
    assert.equal((await write(request('POST',{}, {headers:{'Sec-Fetch-Site':'cross-site'}}),params('imports/zip'))).status,403);
    assert.equal((await read(request(),params(`${skillId}/activate`))).status,400);assert.equal(calls,0);
  });
  await t.test('rejects unexpected queries, unsafe paths and cursor/range ambiguity',async()=>{
    let calls=0;globalThis.fetch=async()=>{calls++;throw Error('unexpected');};
    for(const query of ['?after=not-uuid','?limit=51','?limit=1&limit=2','?url=https://evil.example','?after=../escape'])assert.equal((await list(request('GET',undefined,{query}))).status,400,query);
    for(const query of ['?after=0','?after=-1','?after=9007199254740992','?limit=0'])assert.equal((await read(request('GET',undefined,{query}),params(`${skillId}/history`))).status,400,query);
    for(const query of ['?path=../secret','?path=SKILL.md&length=65537','?path=SKILL.md&offset=-1','?path=SKILL.md&length=0','?path=SKILL.md&offset=9007199254740992'])assert.equal((await read(request('GET',undefined,{query}),params(`packages/${packageId}/resources`))).status,400,query);
    assert.equal((await read(request('GET',undefined,{query:'?path=SKILL.md'}),params(`${skillId}/resources`))).status,400);
    assert.equal((await read(request('GET',undefined,{query:'?skillId=bad'}),params(`packages/${packageId}/plan`))).status,400);
    assert.equal(calls,0);
  });
  await t.test('passive list and detail reads strip backend-only data and never send setup permission',async()=>{
    globalThis.fetch=async(url,options)=>{
      assert.equal(url,'http://127.0.0.1:8765/v1/ui/skills?limit=25');assert.equal(options.method,'GET');
      assert.equal(options.headers.Authorization,'Bearer PRIVATE-backend-token');assert.equal(options.headers['X-Potbot-Setup-Token'],undefined);
      assert.equal(options.redirect,'error');assert.equal(options.cache,'no-store');return Response.json({...fixture.list,setup_token:'PRIVATE'});
    };
    const response=await list(request());assert.equal(response.status,200);assert.equal(response.headers.get('Cache-Control'),'private, no-store');assert.deepEqual(await response.json(),fixture.list);
    globalThis.fetch=async()=>Response.json({...fixture.package,package:{...fixture.package.package,control_mac:'PRIVATE'}});
    assert.deepEqual(await (await read(request(),params(`packages/${packageId}`))).json(),fixture.package);
    globalThis.fetch=async()=>Response.json({...fixture.package,workspaceId:principalId});
    assert.equal((await read(request(),params(`packages/${packageId}`))).status,502);
    globalThis.fetch=async()=>Response.json({schema:fixture.package.schema,workspaceId,principalId,package:fixture.package.package});
    assert.equal((await read(request(),params(`packages/${packageId}`))).status,502);
  });
  await t.test('ZIP import forwards exact bytes and server-only setup capability and checks the source receipt',async()=>{
    const bytes=Buffer.from('exact archive bytes'),body={archiveBase64:bytes.toString('base64'),subdirectory:''};
    const result=clone(fixture.package);delete result.content;delete result.contentTruncated;
    result.package.source.archive_sha256=createHash('sha256').update(bytes).digest('hex');
    globalThis.fetch=async(url,options)=>{
      assert.equal(url,'http://127.0.0.1:8765/v1/ui/skills/imports/zip');assert.deepEqual(JSON.parse(options.body),body);
      assert.equal(options.headers['Idempotency-Key'],'skill:test-key');assert.equal(options.headers['X-Potbot-Setup-Token'],env.POTBOT_UI_SETUP_TOKEN);
      assert.equal(options.headers.Origin,auth.publicOrigin);return Response.json({...result,setup_token:'PRIVATE'},{status:201});
    };
    const response=await write(request('POST',body),params('imports/zip'));assert.equal(response.status,201);assert.deepEqual(await response.json(),result);
    globalThis.fetch=async()=>Response.json(fixture.package,{status:201});assert.equal((await write(request('POST',body),params('imports/zip'))).status,502);
  });
  await t.test('GitHub import is pinned to the exact repository, commit and requested package path',async()=>{
    const body={repository:'https://github.com/owner/repo',commit:'b'.repeat(40),path:'skills/example'};
    const result=clone(fixture.package);result.package.source={...result.package.source,kind:'github',url:body.repository,commit:body.commit,requested_path:body.path};
    globalThis.fetch=async(url,options)=>{assert.equal(url,'http://127.0.0.1:8765/v1/ui/skills/imports/github');assert.deepEqual(JSON.parse(options.body),body);return Response.json(result);};
    assert.equal((await write(request('POST',body),params('imports/github'))).status,200);
    result.package.source.commit='c'.repeat(40);assert.equal((await write(request('POST',body),params('imports/github'))).status,502);
  });
  await t.test('every mutation requires a stable bounded key, setup capability and an exact confirmed body',async()=>{
    let calls=0;globalThis.fetch=async()=>{calls++;throw Error('unexpected');};
    const body={expectedRevision:1,releaseSha256:null,confirm:true};
    for(const extra of [{confirm:false},{extra:true},{expectedRevision:'1'},{expectedRevision:0},{releaseSha256:'d'.repeat(64)}])assert.equal((await write(request('POST',{...body,...extra}),params(`${skillId}/activate`))).status,400);
    for(const key of ['', 'with spaces','x'.repeat(129)])assert.equal((await write(request('POST',body,{headers:{'Idempotency-Key':key}}),params(`${skillId}/activate`))).status,400);
    assert.equal((await write(request('POST',body,{headers:{'Content-Type':'text/plain'}}),params(`${skillId}/activate`))).status,400);
    delete process.env.POTBOT_UI_SETUP_TOKEN;assert.equal((await write(request('POST',body),params(`${skillId}/activate`))).status,503);process.env.POTBOT_UI_SETUP_TOKEN=env.POTBOT_UI_SETUP_TOKEN;
    assert.equal(calls,0);
  });
  await t.test('install and each availability action bind returned owner, revision, target and state',async()=>{
    const body=skillInstallBody(fixture.plan.plan);
    globalThis.fetch=async(_url,options)=>{assert.equal(JSON.parse(options.body).skillId,null);return Response.json(fixture.installed);};
    assert.equal((await write(request('POST',body),params(`packages/${packageId}/install`))).status,200);
    globalThis.fetch=async()=>Response.json(fixture.active);
    assert.equal((await write(request('POST',body),params(`packages/${packageId}/install`))).status,502);
    assert.equal((await write(request('POST',{expectedRevision:1,releaseSha256:null,confirm:true}),params(`${skillId}/activate`))).status,200);
    assert.equal((await write(request('POST',{expectedRevision:2,releaseSha256:null,confirm:true}),params(`${skillId}/activate`))).status,502);
    globalThis.fetch=async()=>Response.json(fixture.rolledBack);
    assert.equal((await write(request('POST',{expectedRevision:3,releaseSha256:fixture.installed.skill.releaseSha256,confirm:true}),params(`${skillId}/rollback`))).status,200);
    assert.equal((await write(request('POST',{expectedRevision:3,releaseSha256:'e'.repeat(64),confirm:true}),params(`${skillId}/rollback`))).status,502);
    const revoked={...fixture.revoked,skill:{...fixture.revoked.skill,revision:3}};globalThis.fetch=async()=>Response.json(revoked);
    assert.equal((await write(request('POST',{expectedRevision:2,releaseSha256:null,confirm:true}),params(`${skillId}/revoke`))).status,200);
  });
  await t.test('history and ranges retain exact cursors and filenames without turning query data into upstream paths',async()=>{
    globalThis.fetch=async(url)=>{assert.equal(url,`http://127.0.0.1:8765/v1/ui/skills/${skillId}/history?limit=2&after=${fixture.history.nextCursor}`);return Response.json(fixture.previousHistory);};
    assert.deepEqual(await (await read(request('GET',undefined,{query:`?limit=2&after=${fixture.history.nextCursor}`}),params(`${skillId}/history`))).json(),fixture.previousHistory);
    const path='references/a..b.md',resource={...fixture.resource,resource:{...fixture.resource.resource,path}};
    globalThis.fetch=async(url)=>{assert.ok(url.includes('path=references%2Fa%2E%2Eb%2Emd'));assert.ok(url.includes(`releaseSha256=${fixture.active.skill.releaseSha256}`));return Response.json(resource);};
    const response=await read(request('GET',undefined,{query:`?path=${path}&releaseSha256=${fixture.active.skill.releaseSha256}`}),params(`${skillId}/resources`));
    assert.equal(response.status,200);assert.deepEqual(await response.json(),resource);
  });
  await t.test('streaming request and response limits, cancellation, errors and unknown outcomes remain bounded',async()=>{
    let calls=0;globalThis.fetch=async()=>{calls++;throw Error('unexpected');};
    const oversized={archiveBase64:'A'.repeat(6*1024*1024),subdirectory:''};
    assert.equal((await write(request('POST',oversized),params('imports/zip'))).status,400);assert.equal(calls,0);
    const controller=new AbortController();let signal;
    globalThis.fetch=async(_url,options)=>{signal=options.signal;return Response.json(fixture.package);};
    await read(request('GET',undefined,{signal:controller.signal}),params(`packages/${packageId}`));controller.abort();assert.equal(signal.aborted,true);
    globalThis.fetch=async()=>new Response('SECRET'.repeat(400000));assert.equal((await list(request())).status,503);
    for(const [upstream,status] of [[400,400],[409,409],[404,404],[401,502],[500,502],[503,503]]) {
      globalThis.fetch=async()=>new Response('PRIVATE error with token',{status:upstream});const response=await list(request());assert.equal(response.status,status);assert.doesNotMatch(await response.text(),/PRIVATE|token/);
    }
    globalThis.fetch=async()=>{throw Error('PRIVATE backend failure');};const response=await list(request());assert.equal(response.status,503);assert.doesNotMatch(await response.text(),/PRIVATE/);
  });
});

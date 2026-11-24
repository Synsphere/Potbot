import test from 'node:test';
import assert from 'node:assert/strict';
import { sessionCookie } from '../src/lib/server/security.ts';
import { GET as listing } from '../src/app/api/ui/operations/route.ts';
import { GET as detail } from '../src/app/api/ui/operations/[id]/route.ts';
import { catalogueJSON } from '../src/lib/server/operation-catalogue.ts';

const workspaceId='c7687924-49a1-4a65-b48e-f1d9d29ed409';
const principalId='1050b1bb-21c2-4188-a053-01498ce7087c';
const operationId='a'.repeat(64);
const auth={password:'operator-password-very-long',signingKey:'a'.repeat(64),publicOrigin:'http://localhost:3100'};
const cookie=sessionCookie(auth).split(';')[0];
const env={POTBOT_UI_PASSWORD:auth.password,POTBOT_UI_COOKIE_SECRET:auth.signingKey,POTBOT_UI_PUBLIC_ORIGIN:auth.publicOrigin,
  POTBOT_UI_BACKEND_TOKEN:'PRIVATE-backend-token',POTBOT_UI_BACKEND_URL:'http://127.0.0.1:8765/',POTBOT_UI_WORKSPACE_ID:workspaceId};
const item={id:operationId,kind:'model',reference:'syn://thing/model/example',name:'Example model',state:'source_only',
  reasons:['source_metadata_only'],contextObjectId:null,version:null,fingerprintSha256:null,updateAvailable:null,
  sources:[{family:'model_catalog',recordId:principalId,revision:null,sha256:null}],release:null,recentInvocations:[],
  accounting:{scope:'linked_recent_invocations',complete:false,reservationIds:[],settledCount:0,indeterminateCount:0,totals:[]}};
const scope={schema:'potbot-operation-catalogue/v1',workspaceId,principalId,snapshotSha256:'b'.repeat(64)};
const request=(query='',workspace=workspaceId)=>new Request(`http://localhost:3100/api/ui/operations${query}`,{
  headers:{Cookie:cookie,'X-Potbot-Workspace':workspace},
});

test('operation BFF authenticates, confines reads and never reflects private backend data',async t=>{
  const prior={...process.env},fetchPrior=globalThis.fetch;
  Object.assign(process.env,env); delete process.env.POTBOT_UI_WORKSPACES_JSON;
  t.after(()=>{globalThis.fetch=fetchPrior;for(const key of [...Object.keys(env),'POTBOT_UI_WORKSPACES_JSON']) {
    if(prior[key]===undefined) delete process.env[key];else process.env[key]=prior[key];
  }});
  let calls=0;
  globalThis.fetch=async()=>{calls++;throw new Error('unexpected network');};
  assert.equal((await listing(new Request('http://remote.invalid:3100/api/ui/operations'))).status,403);
  assert.equal((await listing(request('?workspace_id=other'))).status,400);
  assert.equal((await listing(request('?q=a&q=b'))).status,400);
  assert.equal((await listing(request('',principalId))).status,503);
  assert.equal((await detail(request(),{params:Promise.resolve({id:'../execute'})})).status,400);
  assert.equal(calls,0);

  await t.test('list forwards only bounded filters to one allowed read endpoint',async()=>{
    globalThis.fetch=async(url,options)=>{
      assert.equal(url,'http://127.0.0.1:8765/v1/ui/operations?limit=25&q=Example&kind=model');
      assert.equal(options.headers.Authorization,'Bearer PRIVATE-backend-token');
      assert.equal(options.headers['X-Potbot-Setup-Token'],undefined);
      assert.equal(options.method,undefined);assert.equal(options.cache,'no-store');
      return Response.json({...scope,items:[{...item,privatePath:'/private',credential:'PRIVATE'}],nextCursor:null,raw:'PRIVATE'});
    };
    const response=await listing(request('?q=Example&kind=model'));
    assert.equal(response.status,200);assert.equal(response.headers.get('cache-control'),'private, no-store');
    assert.deepEqual(await response.json(),{...scope,items:[item],nextCursor:null});
  });
  await t.test('details preserve source identity and reject a relabelled item or workspace',async()=>{
    globalThis.fetch=async(url)=>{assert.equal(url,`http://127.0.0.1:8765/v1/ui/operations/${operationId}`);return Response.json({...scope,item});};
    const response=await detail(request(),{params:Promise.resolve({id:operationId})});
    assert.equal(response.status,200);assert.deepEqual(await response.json(),{...scope,item});
    for(const changed of [{...scope,workspaceId:principalId,item},{...scope,item:{...item,id:'f'.repeat(64)}}]) {
      globalThis.fetch=async()=>Response.json(changed);
      assert.equal((await detail(request(),{params:Promise.resolve({id:operationId})})).status,502);
    }
  });
  await t.test('stale cursor stays409 and private upstream errors never become browser output',async()=>{
    globalThis.fetch=async()=>Response.json({error:'PRIVATE diagnostic and token'},{status:409});
    const stale=await listing(request('?after=old-snapshot'));
    assert.equal(stale.status,409);assert.match(await stale.text(),/Refresh/);
    globalThis.fetch=async()=>Response.json({error:'PRIVATE diagnostic and token'},{status:500});
    const failed=await listing(request());
    assert.equal(failed.status,502);assert.ok(!(await failed.text()).includes('PRIVATE'));
    globalThis.fetch=async()=>Response.json({...scope,items:[{...item,state:'ready'}],nextCursor:null});
    assert.equal((await listing(request())).status,502);
  });
});

test('streaming catalogue reader cancels oversized bodies without reading the remaining stream',async()=>{
  let cancelled=false,reads=0;
  const response=new Response(new ReadableStream({pull(controller){reads++;controller.enqueue(new Uint8Array(600_000));},cancel(){cancelled=true;}}));
  await assert.rejects(catalogueJSON(response),/exceeds bound/);
  assert.equal(cancelled,true);assert.ok(reads<=3);
  await assert.rejects(catalogueJSON(new Response(new Uint8Array([0xff,0xfe]))));
});

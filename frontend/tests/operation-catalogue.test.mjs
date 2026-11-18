import test from 'node:test';
import assert from 'node:assert/strict';
import { catalogueQuery, catalogueSearch, exactAmount, operationReason, orderedOperationReasons, projectOperation,
  projectOperationDetail, projectOperationListing } from '../src/lib/operation-catalogue.ts';

const workspaceId = 'a7757f12-7028-4cff-a533-33703549ca3c';
const principalId = '31f4508b-f572-4d68-a951-53a5e5ba3186';
const reservationId = '4f8053ca-708d-4aa8-8ed3-542d51a4b158';
function operation() {
  return {
    id:'a'.repeat(64), kind:'tool', reference:'syn://thing/tool/inspector', name:'Inspector',
    state:'installed_unqualified', reasons:['runtime_observation_not_live','execution_policy_not_evaluated'],
    contextObjectId:null, version:'1.2.0', fingerprintSha256:'b'.repeat(64), updateAvailable:null,
    sources:[{family:'runtime_context',recordId:workspaceId,revision:2,sha256:'c'.repeat(64)}],
    release:null,
    recentInvocations:[{id:principalId,source:'cog',state:'completed',recordedAt:'2026-11-17T15:00:00+00:00',reservationId}],
    accounting:{scope:'linked_recent_invocations',complete:false,reservationIds:[reservationId],settledCount:1,indeterminateCount:0,
      totals:[{unit:'money_microunits',value:'9007199254740993',provenance:'conservative_estimate'}]},
  };
}
const scope = {schema:'potbot-operation-catalogue/v1',workspaceId,principalId,snapshotSha256:'d'.repeat(64)};

test('projects only public operation metadata and preserves exact partial accounting', () => {
  const row=operation();
  const injected={...row,argv:['/private/tool'],token:'PRIVATE',credential:'PRIVATE',
    sources:row.sources.map(source=>({...source,path:'/private/source'})),
    recentInvocations:row.recentInvocations.map(run=>({...run,prompt:'PRIVATE',output:'PRIVATE'})),
    accounting:{...row.accounting,chargeAgain:true,totals:row.accounting.totals.map(amount=>({...amount,secret:'PRIVATE'}))}};
  assert.deepEqual(projectOperation(injected),row);
  const result=projectOperationListing({...scope,private:'PRIVATE',items:[injected],nextCursor:null},workspaceId);
  assert.deepEqual(result,{...scope,items:[row],nextCursor:null});
  assert.ok(!JSON.stringify(result).includes('PRIVATE'));
  assert.equal(exactAmount(row.accounting.totals[0].value),'9,007,199,254,740,993');
  assert.equal(result.items[0].accounting.complete,false);
  assert.equal(result.items[0].accounting.totals[0].unit,'money_microunits');
});

test('unknown readiness and usage are retained without fabricated installation or zero charges', () => {
  const row={...operation(),state:'source_only',version:null,fingerprintSha256:null,updateAvailable:null,
    recentInvocations:[],accounting:{scope:'linked_recent_invocations',complete:false,reservationIds:[],settledCount:0,indeterminateCount:0,totals:[]}};
  assert.deepEqual(projectOperation(row),row);
  assert.equal(projectOperation({...row,state:'ready_to_execute'}),null);
  assert.match(operationReason('release_record_not_current_admission'),/current host admission has not been established/);
});

test('actionable holds remain visible ahead of alphabetically sorted accounting explanations', () => {
  const reasons=['accounting_scope_limited','postflight_failed','qualification_not_established','release_record_not_current_admission'];
  const presented=orderedOperationReasons(reasons);
  assert.equal(presented[0],'postflight_failed');
  assert.deepEqual(new Set(presented),new Set(reasons));
  assert.equal(reasons[0],'accounting_scope_limited');
  assert.equal(orderedOperationReasons(['accounting_scope_limited','source_metadata_only'])[0],'source_metadata_only');
  assert.ok(orderedOperationReasons(['accounting_scope_limited','future_reason']).includes('future_reason'));
});

test('malformed release evidence cannot be presented as a released installation', () => {
  const release={operationId:workspaceId,state:'active',releaseState:'released',generation:1,bindingSha256:'e'.repeat(64),evidenceSha256:'f'.repeat(64)};
  assert.deepEqual(projectOperation({...operation(),release}).release,release);
  for(const changed of [
    {...release,generation:null},{...release,generation:0},{...release,bindingSha256:null},
    {...release,evidenceSha256:'f'.repeat(63)},{...release,state:['active']},{...release,releaseState:'override'},
  ]) assert.equal(projectOperation({...operation(),release:changed}),null);
  assert.ok(projectOperation({...operation(),release:{...release,releaseState:'none',generation:null,bindingSha256:null,evidenceSha256:null}}));
});
test('withdrawn first-install evidence remains visible without implying a usable installation',()=>{
  const release={operationId:workspaceId,state:'rolled_back',releaseState:'withdrawn',generation:2,bindingSha256:'e'.repeat(64),evidenceSha256:'f'.repeat(64)};
  assert.deepEqual(projectOperation({...operation(),state:'source_only',release}).release,release);
  assert.equal(projectOperation({...operation(),state:'qualified',release}),null);
  assert.equal(projectOperation({...operation(),release:{...release,evidenceSha256:null}}),null);
});

test('rejects unbounded, private-shaped or misleading invocation and accounting fields', () => {
  const row=operation();
  for(const changed of [
    {...row,reasons:['/private/secret']}, {...row,reasons:['qualification_not_established','qualification_not_established']},
    {...row,sources:[{...row.sources[0],recordId:'../../private'}]},
    {...row,sources:[{...row.sources[0],family:'raw_credentials'}]},
    {...row,recentInvocations:[{...row.recentInvocations[0],recordedAt:'not-a-date'}]},
    {...row,recentInvocations:[{...row.recentInvocations[0],source:'raw_tool_output'}]},
    {...row,recentInvocations:Array(6).fill(row.recentInvocations[0])},
    {...row,accounting:{...row.accounting,complete:true}},
    {...row,accounting:{...row.accounting,scope:'lifetime'}},
    {...row,accounting:{...row.accounting,reservationIds:[reservationId,reservationId]}},
    {...row,accounting:{...row.accounting,settledCount:6}},
  ]) assert.equal(projectOperation(changed),null);
  for(const changed of [
    {value:9007199254740993},{value:'01'},{value:'-1'},{value:'1e3'},
    {value:'9'.repeat(33)},{provenance:'guessed'},{unit:'concurrency'},
  ]) assert.equal(projectOperation({...row,accounting:{...row.accounting,totals:[{...row.accounting.totals[0],...changed}]}}),null);
});

test('list and detail enforce workspace identity, unique entries and bounded cursors', () => {
  const row=operation();
  assert.equal(projectOperationListing({...scope,workspaceId:principalId,items:[row],nextCursor:null},workspaceId),null);
  assert.equal(projectOperationListing({...scope,items:[row,row],nextCursor:null},workspaceId),null);
  assert.equal(projectOperationListing({...scope,items:[],nextCursor:'cursor'},workspaceId),null);
  assert.equal(projectOperationListing({...scope,items:[row],nextCursor:'x'.repeat(4097)},workspaceId),null);
  assert.equal(projectOperationDetail({...scope,item:row},workspaceId,'f'.repeat(64)),null);
  assert.deepEqual(projectOperationDetail({...scope,item:row},workspaceId,row.id),{...scope,item:row});
});

test('query parsing refuses scope overrides, repeated keys and unsafe limits', () => {
  for(const query of ['workspaceId=other','q=a&q=b','kind=shell','state=ready','limit=0','limit=101','limit=1.5','q=%0a','after=','after=../other',`q=${'x'.repeat(161)}`]) {
    assert.equal(catalogueQuery(new URLSearchParams(query)),null,query);
  }
  const query=catalogueQuery(new URLSearchParams('q=tool..name&kind=tool&state=source_only&limit=25&after=abc_-'));
  assert.ok(query);
  assert.equal(catalogueSearch(query),'limit=25&q=tool%2E%2Ename&kind=tool&state=source_only&after=abc_-');
  assert.equal(new URLSearchParams(catalogueSearch(query)).get('q'),'tool..name');
});

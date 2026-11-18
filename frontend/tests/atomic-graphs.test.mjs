import test from 'node:test';
import assert from 'node:assert/strict';
import { graphList, graphStatus } from '../src/lib/atomic-graphs.ts';

const id = '123e4567-e89b-12d3-a456-426614174000';
const graphId = '123e4567-e89b-12d3-a456-426614174001';
const hash = 'a'.repeat(64);
const record = {schema:'potbot-atomic-graph-list/v1',workspace_id:id,principal_id:id,
  items:[{graph_id:graphId,objective:'Review checked work',state:'active',max_parallel:2,
    manifest_hash:hash,created_at:'2026-11-18T09:00:00Z'}],next_cursor:null,has_more:false};
const status = {schema:'potbot-atomic-graph-status/v1',graph_id:graphId,state:'active',
  outcome:'ready',max_parallel:2,manifest_hash:hash,nodes:[{
    node_id:'a',operation_name:'run_registered_tool',dependencies:[],job_id:null,
    state:'ready',error_code:null,possible_effect:false,resource_holds:0,
  },{
    node_id:'b',operation_name:'run_registered_tool',dependencies:['a'],job_id:null,
    state:'waiting',error_code:null,possible_effect:false,resource_holds:0,
  }]};

test('valid scoped graph history and dependencies',()=>{
  assert.deepEqual(graphList(record,id),record);
  assert.deepEqual(graphStatus(status,graphId),status);
});
test('wrong workspace, graph, unknown state or dangling dependency is rejected',()=>{
  assert.equal(graphList(record,graphId),null);
  assert.equal(graphStatus(status,id),null);
  assert.equal(graphStatus({...status,outcome:'qualified'},graphId),null);
  assert.equal(graphStatus({...status,nodes:[status.nodes[1]]},graphId),null);
});
test('private payloads and duplicate nodes are not accepted',()=>{
  assert.equal(graphStatus({...status,nodes:[status.nodes[0],status.nodes[0]]},graphId),null);
  assert.equal(graphStatus({...status,nodes:[{...status.nodes[0],resource_holds:1000}]},graphId),null);
});

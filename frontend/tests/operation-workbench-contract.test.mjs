import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { projectContract, projectJobEnvelope, projectEvents, projectResult, checkUsable } from '../src/lib/operation-workbench.ts';

test('real HTTP → PostgreSQL → worker hosted lifecycle remains compatible with the browser contract',async()=>{
  // Captured from test_http_checked_launch_worker_result_and_same_key_recovery, using the actual API,
  // PostgreSQL receipt/event store and configured worker with a mock provider.
  // CI may supply a freshly generated file from that same integration gate.
  const path=process.env.POTBOT_WORKBENCH_CONTRACT_FIXTURE??new URL('./fixtures/operation-workbench-hosted.json',import.meta.url);
  const wire=JSON.parse(await readFile(path,'utf8'));
  const {workspaceId,principalId,operationId}=wire.contract;
  const contract=projectContract(wire.contract,workspaceId,operationId,principalId);
  assert.ok(contract);assert.equal(contract.jobType,'model.generate');assert.equal(contract.executionReference,null);
  const check=projectJobEnvelope(wire.check,workspaceId,operationId,wire.check.job.id,principalId);
  const checked=projectJobEnvelope(wire.checked,workspaceId,operationId,wire.check.job.id,principalId);
  const run=projectJobEnvelope(wire.run,workspaceId,operationId,wire.run.job.id,principalId);
  assert.ok(check);assert.ok(checked);assert.ok(run);
  assert.equal(run.job.operation_check_id,checked.job.id);assert.equal(check.job.operation_check_id,null);
  const checkedAt=Date.parse(checked.job.result.operation_check.checkedAt);
  assert.equal(checkUsable(checked.job,operationId,checkedAt+1000),true);
  const result=projectResult(wire.result,workspaceId,operationId,run.job.id,principalId);
  assert.ok(result);assert.equal(result.job.state,'succeeded');assert.equal(result.output,'model output');
  assert.equal(result.job.result.completed_models,1);assert.equal(result.artifact,null);
  const events=projectEvents(wire.events,workspaceId,operationId,run.job.id,0,principalId);
  assert.ok(events);assert.ok(events.items.some(event=>event.event_type==='job.output'&&event.data.text==='model output'));
  assert.equal(events.items.at(-1).event_type,'job.succeeded');
  assert.doesNotMatch(JSON.stringify({contract,check,checked,run,result,events}),/operation_authorization|setup_token|"input":/);
});

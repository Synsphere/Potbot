import test from 'node:test';
import assert from 'node:assert/strict';
import { acquisitionSelection, acquisitionReviewLink } from '../src/lib/acquisition-handoff.ts';

const workspace='11111111-1111-4111-8111-111111111111',target='22222222-2222-4222-8222-22222222222a',other='33333333-3333-4333-8333-333333333333';
test('discovery links select only exact records under a matching explicit workspace',()=>{
  const skill=acquisitionReviewLink(workspace,'skill_package',target),tool=acquisitionReviewLink(workspace,'tool_acquisition',target);
  assert.equal(skill,`/skills?workspace=${workspace}&package=${target}`);assert.equal(tool,`/setup/tools?workspace=${workspace}&operation=${target}`);
  assert.equal(acquisitionSelection(new URL(skill,'http://localhost').search,workspace,'package'),target);
  assert.equal(acquisitionSelection(new URL(tool,'http://localhost').search,workspace,'operation'),target);
  assert.equal(acquisitionSelection(`?workspace=${workspace}&check=${target}`,workspace,'check'),target);
  assert.equal(acquisitionSelection(`?check=${target}`,workspace,'check'),null);
  assert.equal(acquisitionSelection(`?workspace=${workspace}&check=${target}&check=${other}`,workspace,'check'),null);
  for(const query of [`?package=${target}`,`?workspace=${other}&package=${target}`,`?workspace=${workspace}&workspace=${workspace}&package=${target}`,
    `?workspace=${workspace}&package=${target}&package=${other}`,`?workspace=${workspace}&package=../../execute`,`?workspace=${workspace}&package=${target.toUpperCase()}`])assert.equal(acquisitionSelection(query,workspace,'package'),null);
  assert.equal(acquisitionSelection(new URL(tool,'http://localhost').search,workspace,'package'),null);
  assert.equal(acquisitionReviewLink(workspace,'execute',target),null);assert.equal(acquisitionReviewLink('invalid','skill_package',target),null);assert.equal(acquisitionReviewLink(workspace,'tool_acquisition','/private/path'),null);
});

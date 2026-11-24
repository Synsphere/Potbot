import assert from "node:assert/strict";
import test from "node:test";
import { checkedReasoningRoute } from "../src/lib/server/checked-reasoning.ts";

const run = "123e4567-e89b-12d3-a456-426614174001";
const workspace = "123e4567-e89b-12d3-a456-426614174002";
const principal = "123e4567-e89b-12d3-a456-426614174003";
const digest = "a".repeat(64);
const origin = "http://127.0.0.1:3100";
const status = { run_id: run, state: "active", stage_index: 0, phase: "hypothesis_generator",
  decision_id: null, check_job_id: null, job_id: null, error_code: null, completed: [] };
const plan = { task: "Compare two options", constraints: [], evidence: [],
  stages: ["hypothesis_generator", "critic", "synthesizer"].map(role => ({ role, policy_id: principal, request: {} })) };
const headers = { Origin: origin, "X-Potbot-Expected-Principal": principal,
  "X-Potbot-Workspace": workspace, "Content-Type": "application/json" };

const select = {
  POTBOT_UI_PUBLIC_ORIGIN: origin, POTBOT_UI_COOKIE_SECRET: "s".repeat(64),
  POTBOT_UI_WORKSPACE_ID: workspace, POTBOT_UI_BACKEND_URL: "http://127.0.0.1:8100",
  POTBOT_UI_BACKEND_TOKEN: "fixture-backend", POTBOT_UI_SETUP_TOKEN: "t".repeat(64),
};
const upstream = [];
let fixture;
function request(path, body, override = {}) {
  return checkedReasoningRoute(new Request(origin + "/api/ui/checked-reasoning/" + path.join("/"), {
    method: body === undefined ? "GET" : "POST", headers: { ...headers, ...override },
    ...(body === undefined ? {} : { body: typeof body === "string" ? body : JSON.stringify(body) }),
  }), path);
}

test("checked reasoning forwards exactly the admitted action and leaves retries explicit", async t => {
  for (const [key, value] of Object.entries(select)) {
    const before = process.env[key];
    process.env[key] = value;
    t.after(() => { if (before === undefined) delete process.env[key]; else process.env[key] = before; });
  }
  const connections = process.env.POTBOT_UI_WORKSPACES_JSON;
  delete process.env.POTBOT_UI_WORKSPACES_JSON;
  t.after(() => { if (connections !== undefined) process.env.POTBOT_UI_WORKSPACES_JSON = connections; });
  t.mock.method(globalThis, "fetch", async (url, init) => {
    upstream.push({ url, init });
    if (fixture instanceof Error) throw fixture;
    return Response.json(fixture);
  });

  fixture = { ...status, private_prompt: "never project" };
  assert.equal((await request([run, "start"], { confirm: true, plan }, { Origin: "https://other.invalid" })).status, 403);
  assert.equal((await request([run, "advance"], { confirm: true, expected_stage: 3 })).status, 400);
  assert.equal((await request([run, "advance"], { confirm: true, expected_stage: 0 },
    { "X-Potbot-Expected-Principal": "" })).status, 400);
  assert.equal((await request([run, "advance"], '{"confirm":true,"confirm":true,"expected_stage":0}')).status, 400);
  assert.equal((await request([run, "arbitrary"], { confirm: true })).status, 400);
  assert.equal(upstream.length, 0);

  const started = await request([run, "start"], { confirm: true, plan }, { "X-Potbot-Setup-Token": "caller-token" });
  assert.equal(started.status, 200);
  assert.deepEqual(await started.json(), status);
  assert.equal(started.headers.get("cache-control"), "private, no-store");
  assert.equal(upstream[0].url, "http://127.0.0.1:8100/v1/ui/checked-reasoning/" + run + "/start");
  assert.equal(upstream[0].init.headers["X-Potbot-Setup-Token"], undefined);
  assert.deepEqual(JSON.parse(upstream[0].init.body), { confirm: true, plan });

  fixture = new Error("reply lost");
  const failed = await request([run, "advance"], { confirm: true, expected_stage: 0 });
  assert.equal(failed.status, 503);
  assert.equal(upstream.length, 2);
  assert.equal(upstream[1].init.headers["X-Potbot-Setup-Token"], select.POTBOT_UI_SETUP_TOKEN);
  assert.equal(upstream[1].init.headers["X-Potbot-Expected-Principal"], principal);
  assert.equal(upstream[1].init.headers.Origin, origin);

  fixture = { ...status, run_id: principal };
  assert.equal((await request([run])).status, 502);
  fixture = {
    schema: "potbot.checked-reasoning-result/1", role: "synthesizer", decision_id: principal,
    job_id: principal, output_sha256: digest, text: "Compared both options.",
    semantic_correctness: "not_evaluated", parsed: { private: "discarded" },
  };
  const result = await request([run, "result"]);
  assert.equal(result.status, 200);
  assert.deepEqual(await result.json(), {
    decision_id: principal, job_id: principal, output_sha256: digest,
    text: "Compared both options.", semantic_correctness: "not_evaluated",
  });
});

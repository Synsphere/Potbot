"""Native benchmark profile, pinned declarations and no-replay fault boundaries."""

from __future__ import annotations

import copy
import importlib.util
import json
from argparse import Namespace
from pathlib import Path

import httpx
import pytest

from potbot.domain.errors import InvalidInputError
from potbot.evaluation.agentic_experiments import admit_atomic_experiment, freeze_atomic_experiment
from potbot.evaluation.agentic_model_bench import (
    Generation,
    ModelTarget,
    RoleSuite,
    prompt_for_case,
)
from potbot.evaluation.agentic_model_transport import BenchmarkTransport
from potbot.evaluation.agentic_ollama_transport import transport_profile

ROOT = Path(__file__).resolve().parents[2]
DIGEST = "a" * 64


def target(name="tiny"):
    return ModelTarget(
        name,
        "smollm2:360m",
        "loopback_ollama_v1",
        ("evidence", "status"),
        "sha256:" + DIGEST,
        base_url="http://127.0.0.1:11435/api",
        max_output_tokens=128,
    )


def suite():
    return RoleSuite.from_dict(
        json.loads((ROOT / "examples/agentic-benchmark/atomic-reference-suite.json").read_text())
    )


def packets():
    return {
        "/api/tags": {"models": [{"name": "smollm2:360m", "digest": DIGEST}]},
        "/api/chat": {
            "model": "smollm2:360m",
            "done": True,
            "done_reason": "stop",
            "message": {"role": "assistant", "content": '{"valid":true,"kind":"object"}'},
            "prompt_eval_count": 81,
            "eval_count": 13,
        },
        "/api/ps": {"models": [{"name": "smollm2:360m", "digest": DIGEST}]},
    }


def invoke(values, calls):
    def handler(request):
        calls.append(
            (
                request.method,
                request.url.path,
                json.loads(request.content) if request.content else None,
            )
        )
        value = values[request.url.path]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, bytes):
            return httpx.Response(200, content=value)
        if isinstance(value, int):
            return httpx.Response(value, headers={"location": "http://example.invalid/escape"})
        return httpx.Response(200, json=value)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return BenchmarkTransport(http_client=client).request(
            target(), prompt_for_case(target().model, suite().cases[0], "generic")
        )


def test_native_call_has_frozen_controls_no_forced_answers_and_exact_usage():
    calls = []
    result = invoke(packets(), calls)
    assert [(m, p) for m, p, _ in calls] == [
        ("GET", "/api/tags"),
        ("POST", "/api/chat"),
        ("GET", "/api/ps"),
    ]
    body = calls[1][2]
    assert body["think"] is False and body["stream"] is False
    assert body["options"] == {"temperature": 0, "seed": 42, "num_ctx": 4096, "num_predict": 128}
    assert "format" not in body and "expected" not in body
    assert result.text == '{"valid":true,"kind":"object"}'
    assert (result.input_tokens, result.output_tokens, result.billed_microunits) == (81, 13, None)
    assert result.origin == "ollama-native-v1-stop"


@pytest.mark.parametrize("path", ["/api/tags", "/api/ps"])
def test_changed_installed_or_loaded_manifest_is_refused_without_replacement_call(path):
    values = packets()
    values[path]["models"][0]["digest"] = "b" * 64
    calls = []
    with pytest.raises(InvalidInputError, match="manifest differs"):
        invoke(values, calls)
    assert len([c for c in calls if c[0] == "POST"]) == int(path == "/api/ps")


@pytest.mark.parametrize("path", ["/api/tags", "/api/chat", "/api/ps"])
@pytest.mark.parametrize(
    "bad",
    [302, 503, b'{"a":1,"a":2}', b'{"bad":NaN}', b"x" * 131073],
    ids=["redirect", "unavailable", "duplicate", "nonfinite", "oversize"],
)
def test_invalid_protocol_and_redirects_do_not_retry_or_start_another_generation(path, bad):
    values = packets()
    values[path] = bad
    calls = []
    with pytest.raises((InvalidInputError, ValueError)):
        invoke(values, calls)
    assert len([c for c in calls if c[0] == "POST"]) <= 1
    assert calls[-1][1] == path


def test_lost_generation_reply_has_one_post_and_no_ps_or_replay():
    values = packets()
    values["/api/chat"] = httpx.ReadTimeout("synthetic lost reply")
    calls = []
    with pytest.raises(httpx.ReadTimeout):
        invoke(values, calls)
    assert [(m, p) for m, p, _ in calls] == [("GET", "/api/tags"), ("POST", "/api/chat")]


@pytest.mark.parametrize(
    "field,value",
    [
        ("done", False),
        ("done_reason", "unrecognized"),
        ("prompt_eval_count", True),
        ("eval_count", -1),
    ],
)
def test_incomplete_or_forged_usage_is_not_a_completed_observation(field, value):
    values = packets()
    values["/api/chat"][field] = value
    calls = []
    with pytest.raises(InvalidInputError):
        invoke(values, calls)
    assert len(calls) == 2


def test_hidden_thinking_is_refused_and_known_token_limit_remains_visible():
    values = packets()
    values["/api/chat"]["message"]["thinking"] = "unexpected reasoning"
    calls = []
    with pytest.raises(InvalidInputError):
        invoke(values, calls)
    values = packets()
    values["/api/chat"]["done_reason"] = "length"
    assert invoke(values, []).origin == "ollama-native-v1-length"


def test_returned_model_change_reaches_the_existing_runner_identity_fence():
    values = packets()
    values["/api/chat"]["model"] = "wrong-model"
    result = invoke(values, [])
    assert result.returned_model == "wrong-model"


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.1:11435/api",
        "http://127.0.0.1:11435/v1",
        "http://user:secret@127.0.0.1:11435/api",
        "http://127.0.0.1:11435/api?x=1",
    ],
)
def test_native_endpoint_cannot_widen_to_lan_credentials_or_another_protocol(url):
    from dataclasses import replace

    with pytest.raises(InvalidInputError):
        replace(target(), base_url=url)


def test_freeze_binds_native_controls_and_order_without_rewriting_legacy_plan():
    evidence = {
        k: None
        for k in (
            "candidate_package",
            "gold_review",
            "dataset_card",
            "deployment_bindings",
            "review_bundle",
        )
    }
    kwargs = dict(variants=("generic",), repeat=1, shuffle_seed=42, evidence=evidence)
    plan = freeze_atomic_experiment(
        suite(), [target()], execution_order="model_blocks_v1", **kwargs
    )
    assert plan["transport_profiles"] == {"loopback_ollama_v1": transport_profile()}
    with pytest.raises(InvalidInputError):
        admit_atomic_experiment(plan, suite(), [target()], **kwargs)
    assert (
        admit_atomic_experiment(
            plan, suite(), [target()], execution_order="model_blocks_v1", **kwargs
        )
        == plan
    )
    mutated = copy.deepcopy(plan)
    mutated["transport_profiles"]["loopback_ollama_v1"]["think"] = True
    with pytest.raises(InvalidInputError):
        admit_atomic_experiment(
            mutated, suite(), [target()], execution_order="model_blocks_v1", **kwargs
        )
    from dataclasses import replace

    legacy = replace(
        target(), backend="loopback_openai", revision="old", base_url="http://127.0.0.1:11435/v1"
    )
    old = freeze_atomic_experiment(suite(), [legacy], **kwargs)
    assert "execution_order" not in old and "transport_profiles" not in old


def test_grouped_schedule_runs_each_cell_once_in_stable_model_blocks(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "native_bench_cli", ROOT / "scripts/benchmark_agentic_models.py"
    )
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    seen = []

    class Transport:
        def __init__(self, **_kwargs):
            pass

        def request(self, model, _prompt):
            seen.append(model.id)
            return Generation("{}", 1, returned_model=model.model)

    monkeypatch.setattr(cli, "BenchmarkTransport", Transport)
    args = Namespace(
        repeat=1,
        shuffle_seed=42,
        execution_order="model_blocks_v1",
        confirm_prompt_disclosure=True,
        judge_derived=False,
        min_samples=12,
        store_text=True,
        max_cost_microunits=0,
    )
    small = RoleSuite(suite().id, suite().cases[:3])
    models = [target("a"), target("b"), target("c")]
    report = cli._execute(args, small, models, ["generic", "family"], None)
    assert len(seen) == 18 and all(seen.count(name) == 6 for name in ("a", "b", "c"))
    assert sum(a != b for a, b in zip(seen, seen[1:], strict=False)) == 2
    assert (
        len({(r["model_id"], r["case_id"], r["variant"], r["trial"]) for r in report["results"]})
        == 18
    )
    assert report["execution"]["execution_order"] == "model_blocks_v1"
    assert report["qualified"] is False

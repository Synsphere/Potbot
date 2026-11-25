from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from potbot.cogs.execution import execution_cog_snapshot
from potbot.domain.errors import ScopeDeniedError
from potbot.tools.execution_contracts import ExecutionPolicy
from potbot.tools.pixelcue_cog import install_pixelcue, schemas
from potbot.tools.pixelcue_worker import (
    PROFILES,
    REQUEST,
    RESULT,
    _findings,
    _source,
    compact_result,
    encode_result,
    validate_request,
)


def request(**updates):
    return {
        "path": "media/fixture.png",
        "models": ["ocr"],
        "max_frames": 3,
        "max_edge": 768,
        **updates,
    }


@pytest.mark.parametrize(
    "options",
    [
        {"path": "../private.png"},
        {"path": "/etc/passwd"},
        {"path": "C:/private.png"},
        {"path": "media\\private.png"},
        {"path": "a/./b.png"},
        {"path": "a//b.png"},
        {"models": ["joycaption"]},
        {"models": ["ocr", "ocr"]},
        {"max_frames": 6},
        {"max_edge": True},
        {"max_frames": False},
        {"allow_download": True},
    ],
)
def test_worker_rejects_extra_authority_and_bad_paths(options):
    with pytest.raises(ValueError):
        validate_request(request(**options))


def test_no_models_is_an_explicit_fast_request():
    assert validate_request(request(models=[]))["models"] == []


def test_media_is_confined_to_admitted_home(tmp_path):
    path = tmp_path / "media" / "fixture.png"
    path.parent.mkdir()
    path.write_bytes(b"fixture")
    assert _source(tmp_path, "media/fixture.png") == path.resolve()
    try:
        (tmp_path / "alias").symlink_to(path.parent, target_is_directory=True)
    except OSError:
        pytest.skip("Host does not permit unprivileged symlink creation")
    with pytest.raises(ValueError):
        _source(tmp_path, "alias/fixture.png")


def test_installed_schema_is_opt_in_and_preserves_existing_snapshot():
    before = execution_cog_snapshot()
    assert REQUEST not in {str(t.ref) for t in before.types}
    selected = execution_cog_snapshot(pixelcue=True)
    assert {REQUEST, RESULT} <= {str(t.ref) for t in selected.types}
    assert set(before.objects) == set(selected.objects)
    assert set(schemas()) == {REQUEST, RESULT}


def test_installation_is_pinned_and_uses_existing_tool_cog():
    policy = ExecutionPolicy("a" * 64, "b" * 64, "c" * 64, "fixture-v1")
    op = install_pixelcue("d" * 64, policy)
    assert op.binding.kind == "tool"
    assert op.binding.effect_class == "read_only"
    assert op.binding.input_type_ref == REQUEST
    assert op.binding.output_type_ref == RESULT
    assert op.argv == ("/usr/local/libexec/potbot-pixelcue", "/models/pixelcue")
    assert op.executable_sha256 == "d" * 64
    for path in ["/etc", "/models/../etc", "/models//pixelcue"]:
        with pytest.raises(ScopeDeniedError):
            install_pixelcue("d" * 64, policy, models_dir=path)


def report_fixture():
    receipt = {
        "name": "ocr",
        "status": "completed",
        "seconds": 0.01,
        "resolved_revision": "fixture",
        "result": {
            "words": [{"text": "Untrusted OCR", "confidence": 95, "box_xywh": [2, 3, 10, 8]}]
        },
    }
    return {
        "pixelcue_version": "fixture",
        "status": "completed",
        "source": {"name": "fixture.png", "sha256": "a" * 64, "bytes": 123, "kind": "image"},
        "coverage": {
            "exhaustive": True,
            "sampled_frames": 1,
            "requested_max_frames": 3,
            "failures": [],
            "missing_timestamps": 0,
        },
        "timing": {"total_seconds": 0.1},
        "items": [
            {
                "status": "completed",
                "frame": None,
                "analysis": {
                    "source": {"analysis_dimensions": [32, 32]},
                    "measurements": {
                        "colour": {
                            "relative_luminance_percentiles": [0, 0, 0.5, 1, 1],
                            "palette": [{"hex": "#ffffff"}],
                        }
                    },
                    "models": [receipt],
                },
            }
        ],
    }


def test_exact_compact_output_matches_sealed_schema():
    jsonschema = pytest.importorskip("jsonschema")
    compact = compact_result(report_fixture())
    wire = json.loads(encode_result(compact))
    jsonschema.validate(wire["value"], schemas()[RESULT])
    assert wire["value"]["items"][0]["models"][0]["findings"][0] == {
        "label": "Untrusted OCR",
        "score": 0.95,
        "box_xyxy": [2, 3, 12, 11],
    }


def test_unavailable_is_preserved_and_not_converted_to_an_empty_success():
    report = report_fixture()
    report["status"] = "partial"
    report["items"][0]["analysis"]["models"] = [
        {"name": "layout", "status": "unavailable", "seconds": 0.02, "reason": "uncached"}
    ]
    result = compact_result(report)
    assert result["status"] == "partial"
    assert result["items"][0]["models"][0]["status"] == "unavailable"


def test_envelope_truncation_preserves_full_counts_and_coverage():
    result = compact_result(report_fixture())
    template = result["items"][0]
    result["items"] = [json.loads(json.dumps(template)) for _ in range(5)]
    for item in result["items"]:
        item["models"] = [json.loads(json.dumps(item["models"][0])) for _ in PROFILES]
        for profile, model in zip(PROFILES, item["models"], strict=True):
            model["profile"] = profile
            model["findings"] = [
                {"label": "\u4e16" * 160, "score": 0.9, "box_xyxy": [1, 2, 3, 4]} for _ in range(12)
            ]
            model["total_findings"] = 100
            model["truncated"] = True
    raw = encode_result(result)
    assert len(raw) <= 16_000
    assert all(
        model["total_findings"] == 100 for item in result["items"] for model in item["models"]
    )
    assert result["coverage"]["sampled_frames"] == 1


def test_invalid_model_numbers_fail_closed():
    with pytest.raises(ValueError):
        _findings(
            {
                "name": "mobilenet",
                "result": {"categories": [{"label": "bad", "score": float("nan")}]},
            }
        )


def test_typed_worker_main_reads_and_returns_one_bounded_envelope(monkeypatch):
    import potbot.tools.pixelcue_worker as worker

    wire = {
        "schema_version": 1,
        "operation_ref": worker.OPERATION,
        "input": {"type_ref": REQUEST, "value": request()},
        "invocation_id": "00000000-0000-0000-0000-000000000001",
    }
    output = io.BytesIO()
    monkeypatch.setattr(worker.sys, "argv", ["worker", "/models/pixelcue"])
    monkeypatch.setattr(
        worker.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(wire).encode()))
    )
    monkeypatch.setattr(worker.sys, "stdout", SimpleNamespace(buffer=output))
    monkeypatch.setattr(
        worker, "execute_request", lambda value, **kwargs: compact_result(report_fixture())
    )
    assert worker.main() == 0
    result = json.loads(output.getvalue())
    assert result["type_ref"] == RESULT
    assert result["value"]["source"]["sha256"] == "a" * 64


@pytest.mark.parametrize(
    "payload",
    [b'{"schema_version":1,"schema_version":1}', b'{"schema_version":NaN}', b"x" * 16_001],
)
def test_typed_worker_rejects_ambiguous_or_oversized_json(monkeypatch, payload):
    import potbot.tools.pixelcue_worker as worker

    monkeypatch.setattr(worker.sys, "argv", ["worker", "/models/pixelcue"])
    monkeypatch.setattr(worker.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(payload)))
    assert worker.main() == 2

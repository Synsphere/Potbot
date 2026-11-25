"""Sealed optional PixelCue ports and installation for the native tool Cog."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from potbot.domain.errors import ScopeDeniedError
from potbot.domain.serialization import content_hash
from potbot.tools.execution_contracts import ExecutionPolicy, WorkBinding, digest
from potbot.tools.pixelcue_worker import EXECUTABLE, OPERATION, PROFILES, REQUEST, RESULT
from potbot.tools.sandbox_cog_worker import SandboxedCogOperation


def schemas() -> dict[str, dict[str, Any]]:
    from potbot.tools.semantic_contracts import HASH, array, obj

    count = {"type": "integer", "minimum": 0}
    number = {"type": "number", "minimum": 0}
    dimensions = {
        "type": "array",
        "items": {"type": "integer", "minimum": 1, "maximum": 1024},
        "minItems": 2,
        "maxItems": 2,
    }
    finding = obj(
        {
            "label": {"type": "string", "maxLength": 160},
            "score": {"type": "number", "minimum": 0, "maximum": 1},
            "box_xyxy": {
                "anyOf": [
                    {"type": "null"},
                    {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                ]
            },
        }
    )
    model = obj(
        {
            "profile": {"enum": list(PROFILES)},
            "status": {"enum": ["completed", "unavailable"]},
            "revision": {"type": ["string", "null"], "maxLength": 256},
            "reason_code": {"type": ["string", "null"], "maxLength": 80},
            "seconds": number,
            "findings": array(finding, 12),
            "total_findings": count,
            "truncated": {"type": "boolean"},
            "score_kind": {
                "enum": [
                    "classifier_softmax",
                    "detector_score",
                    "tesseract_confidence_divided_by_100",
                ]
            },
        }
    )
    item = obj(
        {
            "timestamp_seconds": {"type": ["number", "null"]},
            "dimensions": dimensions,
            "median_luminance": {"type": "number", "minimum": 0, "maximum": 1},
            "palette": array({"type": "string", "pattern": "^#[0-9a-fA-F]{6}$"}, 6),
            "models": array(model, 4),
        }
    )
    return {
        REQUEST: obj(
            {
                "path": {"type": "string", "minLength": 1, "maxLength": 512},
                "models": {**array({"enum": list(PROFILES)}, 4), "uniqueItems": True},
                "max_frames": {"type": "integer", "minimum": 1, "maximum": 5},
                "max_edge": {"type": "integer", "minimum": 32, "maximum": 1024},
            }
        ),
        RESULT: obj(
            {
                "schema": {"const": "potbot.pixelcue-analysis/1"},
                "pixelcue_version": {"type": "string", "minLength": 1, "maxLength": 64},
                "status": {"enum": ["completed", "partial"]},
                "source": obj(
                    {
                        "name": {"type": "string", "maxLength": 512},
                        "sha256": HASH,
                        "bytes": count,
                        "kind": {"enum": ["image", "video"]},
                    }
                ),
                "items": {**array(item, 5), "minItems": 1},
                "coverage": obj(
                    {
                        "exhaustive": {"type": "boolean"},
                        "sampled_frames": count,
                        "requested_max_frames": count,
                        "failed_candidates": count,
                        "missing_timestamps": count,
                    }
                ),
                "total_seconds": number,
                "limits": {"type": "string", "maxLength": 1024},
            }
        ),
    }


def install_pixelcue(
    executable_sha256: str, policy: ExecutionPolicy, *, models_dir: str = "/models/pixelcue"
) -> SandboxedCogOperation:
    mount = PurePosixPath(models_dir)
    if (
        not mount.is_relative_to("/models")
        or ".." in mount.parts
        or str(mount) != models_dir
        or any(ord(c) < 32 for c in models_dir)
    ):
        raise ScopeDeniedError("PixelCue needs an exact operator-installed model mount")
    if policy.max_output_bytes < 16_384:
        raise ScopeDeniedError("PixelCue needs the standard 16 KiB typed output allowance")
    argv = (EXECUTABLE, models_dir)
    checksum = digest(executable_sha256)
    return SandboxedCogOperation(
        WorkBinding(
            "tool",
            OPERATION,
            REQUEST,
            RESULT,
            content_hash({"argv": argv, "executable_sha256": checksum}),
            "read_only",
            policy,
        ),
        argv,
        checksum,
    )

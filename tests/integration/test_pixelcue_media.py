"""Actual isolated-process adapter checks; not native Syn/host qualification.

Enable only with POTBOT_TEST_PIXELCUE=1 and the exact separately installed
PixelCue release. Missing requested runtime dependencies fail collection.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

if os.environ.get("POTBOT_TEST_PIXELCUE") != "1":
    pytest.skip("Explicit installed PixelCue integration required", allow_module_level=True)

import av  # noqa: E402
import jsonschema  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from potbot.tools.pixelcue_cog import schemas  # noqa: E402
from potbot.tools.pixelcue_worker import OPERATION, REQUEST, RESULT  # noqa: E402

WORKER = Path(__file__).resolve().parents[2] / "src/potbot/tools/pixelcue_worker.py"


def invoke(home, filename, models):
    value = {"path": filename, "models": models, "max_frames": 3, "max_edge": 640}
    request = {
        "schema_version": 1,
        "operation_ref": OPERATION,
        "input": {"type_ref": REQUEST, "value": value},
        "invocation_id": "00000000-0000-0000-0000-000000000001",
    }
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(WORKER), "/models/pixelcue"],
        cwd=home,
        input=json.dumps(request).encode(),
        capture_output=True,
        timeout=45,
        check=False,
        env={**os.environ, "HF_HUB_OFFLINE": "1"},
    )
    if result.returncode:
        return result, None
    assert len(result.stdout) <= 16_000
    wire = json.loads(result.stdout)
    assert wire["type_ref"] == RESULT
    jsonschema.validate(wire["value"], schemas()[RESULT])
    return result, wire["value"]


def page(home):
    image = Image.new("RGB", (640, 360), "white")
    ImageDraw.Draw(image).text(
        (40, 100), "PIXELCUE TOOL DEMO", fill="black", font=ImageFont.load_default(size=36)
    )
    path = home / "page.png"
    image.save(path)
    return path, image


def test_actual_worker_recognises_synthetic_page_and_hash(tmp_path):
    path, _ = page(tmp_path)
    process, result = invoke(tmp_path, path.name, ["ocr"])
    assert process.returncode == 0, process.stderr
    assert result["status"] == "completed"
    assert result["source"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    words = result["items"][0]["models"][0]["findings"]
    assert "PIXELCUE" in [row["label"] for row in words]


def test_actual_video_worker_retains_decoded_times(tmp_path):
    _, image = page(tmp_path)
    path = tmp_path / "clip.mp4"
    with av.open(str(path), "w") as container:
        stream = container.add_stream("mpeg4", rate=5)
        stream.width, stream.height, stream.pix_fmt = 640, 360, "yuv420p"
        for index in range(10):
            frame = image.copy()
            ImageDraw.Draw(frame).rectangle((index * 40, 300, index * 40 + 20, 320), fill="red")
            for packet in stream.encode(av.VideoFrame.from_image(frame)):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    process, result = invoke(tmp_path, path.name, ["ocr"])
    assert process.returncode == 0, process.stderr
    assert result["status"] == "completed"
    assert result["coverage"]["exhaustive"] is False
    assert 1 <= result["coverage"]["sampled_frames"] <= 3
    assert all(row["timestamp_seconds"] is not None for row in result["items"])
    assert all(
        abs(row["timestamp_seconds"] * 5 - round(row["timestamp_seconds"] * 5)) < 1e-5
        for row in result["items"]
    )


def test_actual_worker_preserves_missing_neural_dependency(tmp_path):
    path, _ = page(tmp_path)
    process, result = invoke(tmp_path, path.name, ["mobilenet"])
    assert process.returncode == 0, process.stderr
    assert result["status"] == "partial"
    assert result["items"][0]["models"][0]["status"] == "unavailable"


def test_actual_worker_rejects_outside_home(tmp_path):
    process, result = invoke(tmp_path, "../private.png", [])
    assert process.returncode == 2
    assert result is None

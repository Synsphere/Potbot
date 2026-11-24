#!/opt/potbot/native-inference/bin/python3 -I
"""One offline native-model inference using the installed Context work ports."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

EXECUTABLE = "/usr/local/libexec/potbot-local-model"
REQUEST = "syn://type/potbot-local-model-request-v1"
RESULT = "syn://type/potbot-local-model-result-v1"
SCHEMA = "potbot.native-model-result/1"
BACKEND = "llama-cpp-python"
BACKEND_VERSION = "0.3.19"
ENGINE_LIBRARY = Path("lib/python3.11/site-packages/llama_cpp/lib/libllama.so")
ENGINE_ROOT = Path("/opt/potbot/native-inference")
MODEL_ROOT = Path("/opt/potbot/models")
MAX_CONTEXT = 32768
MAX_ENGINE = 128 * 1024**2
MAX_WEIGHTS = 3 * 1024**3
MAX_TEMPLATE = 64 * 1024
MAX_PROMPT_BYTES = 64 * 1024
_SHA = re.compile(r"[0-9a-f]{64}")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def blob_path(digest: str) -> Path:
    if not isinstance(digest, str) or not _SHA.fullmatch(digest):
        raise ValueError("Expected an exact model asset digest")
    return Path("blobs", digest[:2], digest)


@contextmanager
def verified_file(
    root: Path, relative: Path, expected: str | None, max_bytes: int
) -> Iterator[tuple[int, int]]:
    """Open an immutable asset. Hold a verified inode through any backend load."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory = os.open(root, directory_flags)
    try:
        for part in relative.parts[:-1]:
            nested = os.open(part, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = nested
        descriptor = os.open(
            relative.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=directory,
        )
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_mode & 0o222
                or not 0 < before.st_size <= max_bytes
            ):
                raise ValueError("Expected a bounded, immutable installed file")
            os.set_blocking(descriptor, True)
            if expected is not None:
                with os.fdopen(os.dup(descriptor), "rb") as reader:
                    observed = hashlib.file_digest(reader, "sha256").hexdigest()
                after = os.fstat(descriptor)
                if observed != expected or (
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise ValueError("Installed file digest changed")
            yield descriptor, before.st_size
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def installed_backend(backend: str, version: str, context_tokens: int, output_tokens: int) -> None:
    if (
        (backend, version) != (BACKEND, BACKEND_VERSION)
        or type(context_tokens) is not int
        or type(output_tokens) is not int
        or not 1 <= output_tokens < context_tokens <= MAX_CONTEXT
    ):
        raise ValueError("Unsupported sealed native model deployment")


def selected_request(work: dict[str, Any], deployment: dict[str, Any]) -> dict[str, Any]:
    if (
        set(work) != {"schema_version", "operation_ref", "input", "invocation_id"}
        or type(work["schema_version"]) is not int
        or work["schema_version"] != 1
        or str(UUID(work["invocation_id"])) != work["invocation_id"]
        or set(work["input"]) != {"type_ref", "value"}
        or work["input"]["type_ref"] != REQUEST
    ):
        raise ValueError("Work does not match the installed model request port")
    request = work["input"]["value"]
    if (
        not isinstance(request, dict)
        or set(request) != {"model", "deployment_sha256", "system", "prompt", "max_output_tokens"}
        or request["model"] != deployment["canonical_model"]
        or request["deployment_sha256"] != hashlib.sha256(canonical_bytes(deployment)).hexdigest()
        or not isinstance(request["system"], str)
        or not isinstance(request["prompt"], str)
        or not request["prompt"].strip()
        or type(request["max_output_tokens"]) is not int
        or not 1 <= request["max_output_tokens"] <= deployment["max_output_tokens"]
    ):
        raise ValueError("Work does not select the installed model")
    return request


def _reject_chat_fallback(**_kwargs: Any) -> Any:
    raise ValueError("Only the verified installed chat template is allowed")


def run_inference(
    request: dict[str, Any],
    deployment: dict[str, Any],
    engine_sha256: str,
) -> tuple[str, str, dict[str, int]]:
    installed_backend(
        deployment["backend"],
        deployment["backend_version"],
        deployment["usable_context_tokens"],
        deployment["max_output_tokens"],
    )
    if Path(sys.prefix) != ENGINE_ROOT:
        raise ValueError("The sealed engine interpreter is missing")
    with verified_file(ENGINE_ROOT, ENGINE_LIBRARY, engine_sha256, MAX_ENGINE):
        llama = importlib.import_module("llama_cpp")
        chat_format = importlib.import_module("llama_cpp.llama_chat_format")
        if llama.__version__ != BACKEND_VERSION:
            raise ValueError("Installed engine version differs")
    weights_digest = deployment["weights_digest"]
    template_digest = deployment["chat_template_digest"]
    with verified_file(MODEL_ROOT, blob_path(template_digest), template_digest, MAX_TEMPLATE) as (
        template_fd,
        template_size,
    ):
        template = os.pread(template_fd, template_size, 0).decode("utf-8")
    with verified_file(MODEL_ROOT, blob_path(weights_digest), weights_digest, MAX_WEIGHTS) as (
        model_fd,
        _,
    ):
        header = os.pread(model_fd, 24, 0)
        if (
            len(header) != 24
            or header[:4] != b"GGUF"
            or header[4:8]
            not in (
                b"\x02\x00\x00\x00",
                b"\x03\x00\x00\x00",
            )
        ):
            raise ValueError("Expected an installed single-file GGUF model")
        model = llama.Llama(
            model_path=f"/proc/self/fd/{model_fd}",
            n_ctx=deployment["usable_context_tokens"],
            n_threads=2,
            n_threads_batch=2,
            n_gpu_layers=0,
            verbose=False,
            chat_handler=_reject_chat_fallback,
        )
        try:
            eos = model.token_eos()
            bos = model.token_bos()
            formatter = chat_format.Jinja2ChatFormatter(
                template=template,
                eos_token=model.detokenize([eos], special=True).decode("utf-8"),
                bos_token=model.detokenize([bos], special=True).decode("utf-8") if bos >= 0 else "",
                stop_token_ids=[eos],
            )
            messages = []
            if request["system"]:
                messages.append({"role": "system", "content": request["system"]})
            messages.append({"role": "user", "content": request["prompt"]})
            formatted = formatter(messages=messages)
            prompt = formatted.prompt.encode("utf-8")
            if len(prompt) > MAX_PROMPT_BYTES:
                raise ValueError("Rendered model input exceeds its bound")
            tokens = model.tokenize(prompt, add_bos=not formatted.added_special, special=True)
            output_limit = request["max_output_tokens"]
            if not tokens or len(tokens) + output_limit > deployment["usable_context_tokens"]:
                raise ValueError("Rendered model input exceeds the installed context window")
            response = model.create_completion(
                prompt=tokens,
                max_tokens=output_limit,
                temperature=0.0,
                seed=0,
                stream=False,
                stop=[token for token in (formatted.stop or []) if token],
                stopping_criteria=formatted.stopping_criteria,
            )
        finally:
            model.close()
    (choice,) = response["choices"]
    text, reason = choice["text"], choice["finish_reason"]
    raw_usage = response["usage"]
    usage = {
        "input_tokens": raw_usage["prompt_tokens"],
        "output_tokens": raw_usage["completion_tokens"],
        "total_tokens": raw_usage["total_tokens"],
    }
    if (
        not isinstance(text, str)
        or not text.strip()
        or len(text.encode("utf-8")) > 8192
        or reason not in {"stop", "length"}
        or any(type(value) is not int for value in usage.values())
        or usage["input_tokens"] != len(tokens)
        or not 1 <= usage["output_tokens"] <= output_limit
        or usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]
    ):
        raise ValueError("Model completion did not produce bounded, accounted text")
    return text, reason, usage


def run_work(client: Any, deployment: dict[str, Any], engine_sha256: str) -> None:
    work = client.work_input()
    request = selected_request(work, deployment)
    text, finish, usage = run_inference(request, deployment, engine_sha256)
    value = {
        "schema": SCHEMA,
        "invocation_id": work["invocation_id"],
        "request_sha256": hashlib.sha256(canonical_bytes(request)).hexdigest(),
        "deployment_sha256": request["deployment_sha256"],
        "model": deployment["canonical_model"],
        "provenance": {
            "weights_sha256": deployment["weights_digest"],
            "backend": deployment["backend"],
            "backend_version": deployment["backend_version"],
            "tokenizer_revision": deployment["tokenizer_revision"],
            "chat_template_sha256": deployment["chat_template_digest"],
        },
        "text": text,
        "finish_reason": finish,
        "usage": usage,
    }
    client.finish_work(RESULT, value, request_id=str(uuid4()))


def main() -> None:
    try:
        if len(sys.argv) != 3 or not _SHA.fullmatch(sys.argv[2]):
            raise ValueError("Missing installed model pin")
        # Only the signed image's copy of the directed Context protocol is importable.
        sys.path.insert(0, "/opt/potbot")
        context_client = importlib.import_module("context_client")
        run_work(context_client.ContextClient(), json.loads(sys.argv[1]), sys.argv[2])
    except Exception:
        raise SystemExit("Native model inference refused or failed") from None


if __name__ == "__main__":
    main()

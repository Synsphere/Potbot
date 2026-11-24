"""Experiment JSON permits finite statistics and rejects ambiguous metadata."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from potbot.domain.errors import InvalidInputError
from potbot.evaluation.agentic_experiments import read_experiment_document


@pytest.mark.parametrize(
    "text",
    [
        '{"a":1,"a":2}',
        '{"score":NaN}',
        '{"score":Infinity}',
        '{"score":1e999}',
        '{"nested":[-Infinity]}',
        "[" * 33 + "1" + "]" * 33,
        "1 2",
    ],
)
def test_invalid_json_is_refused(tmp_path, text):
    path = tmp_path / "report.json"
    path.write_text(text)
    with pytest.raises(InvalidInputError):
        read_experiment_document(path)


def test_finite_fractions_and_a_full_sized_cell_array_are_readable(tmp_path):
    import json

    value = {"score": 0.5, "cells": [{str(j): j for j in range(12)} for _ in range(1600)]}
    path = tmp_path / "report.json"
    path.write_text(json.dumps(value))
    assert read_experiment_document(path) == value


def test_reader_never_uses_an_unbounded_read(monkeypatch):
    amounts = []

    class Reader(io.BytesIO):
        def read(self, amount=-1):
            amounts.append(amount)
            return super().read(amount)

    monkeypatch.setattr(Path, "open", lambda *_a, **_kw: Reader(b"{}"))
    assert read_experiment_document(Path("report.json")) == {}
    assert amounts == [16 * 1024 * 1024 + 1]

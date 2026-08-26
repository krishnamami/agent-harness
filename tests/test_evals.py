"""Evaluation harness tests.

The template ships no evaluators, so these exercise the contract with fakes --
which is also how a service should test its own.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals import (
    EvalCase,
    Evaluator,
    Gate,
    Score,
    Threshold,
    load_jsonl,
    run_eval,
)
from evals.report import to_dict, to_text

FIXTURES = Path(__file__).parent.parent / "src" / "evals" / "cases" / "example.jsonl"


# --------------------------------------------------------------------- fakes
class _Fixed:
    def __init__(self, value: float, threshold: float = 0.5, name: str = "fixed") -> None:
        self.name = name
        self._value = value
        self._threshold = threshold

    async def score(self, case: EvalCase, output: object) -> Score:
        return Score(name=self.name, value=self._value, passed=self._value >= self._threshold)


class _Exploding:
    name = "exploding"

    async def score(self, case: EvalCase, output: object) -> Score:
        raise RuntimeError("evaluator is broken")


class _ByCaseId:
    """1.0 for even-numbered cases, 0.0 for odd. Produces a split result."""

    name = "by_case"

    async def score(self, case: EvalCase, output: object) -> Score:
        good = int(case.id.split("-")[-1]) % 2 == 0
        return Score(name=self.name, value=1.0 if good else 0.0, passed=good)


async def _echo(case: EvalCase) -> dict[str, object]:
    return {"echo": case.inputs}


async def _explode(case: EvalCase) -> dict[str, object]:
    raise ValueError("target is down")


# ------------------------------------------------------------------ protocol
def test_score_must_be_a_probability():
    with pytest.raises(ValueError, match="outside"):
        Score(name="x", value=1.4, passed=True)


def test_evaluator_protocol_is_structural():
    assert isinstance(_Fixed(1.0), Evaluator)


# ------------------------------------------------------------------- dataset
def test_dataset_identity_is_a_content_hash():
    ds = load_jsonl(FIXTURES)
    assert ds.info.case_count == 5
    assert len(ds.info.sha256) == 64
    assert load_jsonl(FIXTURES).info.sha256 == ds.info.sha256


def test_editing_a_dataset_changes_its_identity(tmp_path: Path):
    original = tmp_path / "a.jsonl"
    original.write_text('{"id": "1", "inputs": {}}\n')
    before = load_jsonl(original).info.sha256

    original.write_text('{"id": "1", "inputs": {}}\n{"id": "2", "inputs": {}}\n')
    assert load_jsonl(original).info.sha256 != before


def test_duplicate_ids_are_rejected(tmp_path: Path):
    """A repeated id double-weights that case in every aggregate."""
    path = tmp_path / "dupe.jsonl"
    path.write_text('{"id": "1", "inputs": {}}\n{"id": "1", "inputs": {}}\n')
    with pytest.raises(ValueError, match="repeats id"):
        load_jsonl(path)


def test_missing_id_is_rejected(tmp_path: Path):
    path = tmp_path / "noid.jsonl"
    path.write_text('{"inputs": {}}\n')
    with pytest.raises(ValueError, match="no 'id'"):
        load_jsonl(path)


def test_empty_dataset_is_rejected(tmp_path: Path):
    path = tmp_path / "empty.jsonl"
    path.write_text("\n\n")
    with pytest.raises(ValueError, match="no cases"):
        load_jsonl(path)


def test_tag_filtering_keeps_the_dataset_hash():
    ds = load_jsonl(FIXTURES)
    adversarial = ds.filter_by_tag("adversarial")
    assert adversarial.info.case_count == 1
    assert adversarial.info.sha256 == ds.info.sha256
    assert "abstention" in ds.tags


# ------------------------------------------------------------------- harness
async def test_run_scores_every_case():
    run = await run_eval(load_jsonl(FIXTURES), _echo, [_Fixed(0.9)])
    assert len(run.results) == 5
    agg = run.aggregate()["fixed"]
    assert agg.mean == pytest.approx(0.9)
    assert agg.pass_rate == 1.0


async def test_a_failing_target_does_not_abort_the_run():
    run = await run_eval(load_jsonl(FIXTURES), _explode, [_Fixed(1.0)])
    assert len(run.results) == 5
    assert len(run.errors) == 5
    assert "ValueError" in run.errors[0].error


async def test_a_failing_evaluator_scores_zero_rather_than_vanishing():
    run = await run_eval(load_jsonl(FIXTURES), _echo, [_Exploding()])
    agg = run.aggregate()["exploding"]
    assert agg.mean == 0.0
    assert agg.pass_rate == 0.0
    assert "evaluator raised" in run.results[0].scores[0].detail


async def test_concurrency_must_be_positive():
    with pytest.raises(ValueError, match="at least 1"):
        await run_eval(load_jsonl(FIXTURES), _echo, [], concurrency=0)


# --------------------------------------------------------------------- gate
async def test_gate_passes_when_thresholds_are_met():
    run = await run_eval(load_jsonl(FIXTURES), _echo, [_Fixed(0.9)])
    gate = Gate(thresholds=(Threshold("fixed", min_mean=0.85, min_pass_rate=1.0),))
    assert gate.evaluate(run).passed


async def test_a_healthy_mean_does_not_hide_a_bad_pass_rate():
    """The point of carrying both numbers."""
    run = await run_eval(load_jsonl(FIXTURES), _echo, [_ByCaseId()])
    agg = run.aggregate()["by_case"]
    assert agg.mean == pytest.approx(0.4)

    lenient = Gate(thresholds=(Threshold("by_case", min_mean=0.3),))
    assert lenient.evaluate(run).passed

    strict = Gate(thresholds=(Threshold("by_case", min_mean=0.3, min_pass_rate=0.9),))
    result = strict.evaluate(run)
    assert not result.passed
    assert "pass_rate" in result.summary()


async def test_missing_metric_is_a_violation_not_a_pass():
    run = await run_eval(load_jsonl(FIXTURES), _echo, [_Fixed(1.0)])
    gate = Gate(thresholds=(Threshold("groundedness", min_mean=0.8),))
    assert not gate.evaluate(run).passed


async def test_errors_fail_the_gate_by_default():
    run = await run_eval(load_jsonl(FIXTURES), _explode, [])
    assert not Gate(thresholds=()).evaluate(run).passed


async def test_regression_check_catches_slow_decay():
    ds = load_jsonl(FIXTURES)
    baseline = await run_eval(ds, _echo, [_Fixed(0.95, threshold=0.0)])
    current = await run_eval(ds, _echo, [_Fixed(0.86, threshold=0.0)])

    gate = Gate(thresholds=(Threshold("fixed", min_mean=0.80, max_regression=0.02),))
    # Absolute floor alone would pass this: 0.86 is above 0.80.
    assert gate.evaluate(current).passed
    result = gate.evaluate(current, baseline=baseline)
    assert not result.passed
    assert "regression" in result.summary()


async def test_baseline_from_a_different_dataset_is_refused(tmp_path: Path):
    """Comparing across datasets produces a confident wrong answer."""
    other = tmp_path / "other.jsonl"
    other.write_text('{"id": "x", "inputs": {}}\n')

    baseline = await run_eval(load_jsonl(other), _echo, [_Fixed(0.99, threshold=0.0)])
    current = await run_eval(load_jsonl(FIXTURES), _echo, [_Fixed(0.50, threshold=0.0)])

    gate = Gate(thresholds=(Threshold("fixed", max_regression=0.01),))
    result = gate.evaluate(current, baseline=baseline)
    assert result.passed
    assert any("dataset changed" in n for n in result.notes)


async def test_latency_ceiling_is_enforced():
    run = await run_eval(load_jsonl(FIXTURES), _echo, [_Fixed(1.0)])
    assert Gate(thresholds=(), max_p95_latency_ms=10_000).evaluate(run).passed
    assert not Gate(thresholds=(), max_p95_latency_ms=0.0).evaluate(run).passed


# ------------------------------------------------------------------- report
async def test_reports_agree_and_carry_the_dataset_hash():
    run = await run_eval(load_jsonl(FIXTURES), _echo, [_ByCaseId()])
    gate = Gate(thresholds=(Threshold("by_case", min_pass_rate=0.99),))
    result = gate.evaluate(run)

    payload = to_dict(run, result)
    text = to_text(run, result)

    assert payload["dataset"]["sha256"] == run.dataset.sha256
    assert payload["gate"]["passed"] is False
    assert run.dataset.short_sha in text
    assert "FAIL" in text
    assert "failing cases" in text


def test_empty_dataset_object_reports_zero_latency():
    from evals.dataset import DatasetInfo
    from evals.harness import EvalRun

    run = EvalRun(DatasetInfo("empty", "0" * 64, 0), (), 0.0)
    assert run.latency_p95_ms() == 0.0
    assert run.aggregate() == {}

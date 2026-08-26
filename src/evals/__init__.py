"""Evaluation harness.

The contract, not the metrics. See docs/adr/0011 for why this ships no
evaluators: groundedness belongs to a retrieval service, task success to an
agent runtime, and a template that guessed would be wrong for both.
"""

from evals.dataset import Dataset, DatasetInfo, load_jsonl
from evals.harness import CaseResult, EvalRun, run_eval
from evals.protocol import EvalCase, Evaluator, Score
from evals.thresholds import Gate, GateResult, Threshold

__all__ = [
    "CaseResult",
    "Dataset",
    "DatasetInfo",
    "EvalCase",
    "EvalRun",
    "Evaluator",
    "Gate",
    "GateResult",
    "Score",
    "Threshold",
    "load_jsonl",
    "run_eval",
]
